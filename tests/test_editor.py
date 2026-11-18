import json
from datetime import timedelta, timezone
from pathlib import Path

import pytest

from youtube_auto_uploader import google_auth
from youtube_auto_uploader.editor import (
    FOLDER_MIME,
    RUNNER_BUSY,
    apply_retry,
    assemble_story_views,
    collect_stories,
    confirmation_for,
    format_publish_at,
    reload_from_drive,
    reload_stories,
    retry_decision,
    retry_saved,
    save_settings,
    settings_form,
)
from youtube_auto_uploader.google_auth import DRIVE_SCOPES, YOUTUBE_SCOPES
from youtube_auto_uploader.gui import STORY_EDIT_NOTE
from youtube_auto_uploader.models import Settings, Story
from youtube_auto_uploader.storage import Store

ROOT = "folder"
CHANNEL = "channel"
OFFSET = timezone(timedelta(hours=-6))


def story_config():
    return json.loads(
        Path("examples/story-config.example.json").read_text(encoding="utf-8")
    )


def job(stage, source_id, **extra):
    item = {
        "stage": stage,
        "source": {
            "id": source_id,
            "name": f"{source_id}.mp4",
            "size": "3",
            "version": "1",
            "mimeType": "video/mp4",
        },
        "attempts": 0,
        "consecutive_failures": 0,
    }
    item.update(extra)
    return item


def config_text(**overrides):
    data = {
        "schema_version": 1,
        "drive_folder_id": ROOT,
        "upload_log_folder_id": "",
        "youtube_channel_id": CHANNEL,
        "upload_lead_minutes": 180,
        "poll_seconds": 30,
        "state_path": "data/state.json",
        "cache_dir": "data/cache",
        "stats_state_path": "data/stats-reach.json",
    }
    data.update(overrides)
    return data


def write_config(path, **overrides):
    path.write_text(json.dumps(config_text(**overrides)), encoding="utf-8")
    return path


def make_store(tmp_path, stories, binding=None):
    store = Store(tmp_path / "data" / "state.json")
    store.initialize()
    state = store.load()
    state["stories"] = stories
    state["binding"] = binding or {
        "drive_folder_id": ROOT,
        "youtube_channel_id": CHANNEL,
    }
    store.save(state)
    return store


def alpha_stories():
    return {
        "alpha": {
            "name": "Alpha",
            "snapshot": story_config(),
            "jobs": {
                "long": job(
                    "uploaded",
                    "alpha-long",
                    video_id="abc123",
                ),
                "short": job(
                    "needs_review",
                    "alpha-short",
                    error="quota exceeded",
                    next_retry="2026-11-01T00:00:00+00:00",
                    cache_md5="abc",
                ),
            },
        }
    }


def settings_form_values(**overrides):
    form = {
        "drive_folder_id": ROOT,
        "stats_drive_folder_id": "",
        "upload_log_folder_id": "",
        "youtube_channel_id": CHANNEL,
        "upload_lead_minutes": "180",
        "poll_seconds": "30",
    }
    form.update(overrides)
    return form


def write_tokens(base, drive_scopes):
    secret = base / "secrets"
    secret.mkdir(parents=True, exist_ok=True)
    body = {
        "token": "access",
        "refresh_token": "refresh",
        "client_id": "id",
        "client_secret": "secret",
    }
    (secret / "token.json").write_text(
        json.dumps({**body, "scopes": list(drive_scopes)}), encoding="utf-8"
    )
    (secret / "token.youtube.json").write_text(
        json.dumps({**body, "scopes": list(YOUTUBE_SCOPES)}), encoding="utf-8"
    )


def use_local_offset(monkeypatch):
    monkeypatch.setattr("youtube_auto_uploader.editor.local_zone", lambda: OFFSET)


def test_libc_display_uses_the_later_fall_back_offset(monkeypatch):
    import os
    import time
    from zoneinfo import ZoneInfoNotFoundError

    import youtube_auto_uploader.tz as tz

    if not hasattr(time, "tzset"):
        pytest.skip("tzset is Unix-only")
    monkeypatch.setenv("TZ", "America/Los_Angeles")

    def missing(key):
        raise ZoneInfoNotFoundError(key)

    monkeypatch.setattr(tz, "ZoneInfo", missing)
    try:
        assert (
            format_publish_at("2026-11-01T08:30:00+00:00") == "2026-11-01 01:30 -07:00"
        )
        assert (
            format_publish_at("2026-11-01T09:30:00+00:00") == "2026-11-01 01:30 -08:00"
        )
    finally:
        os.environ.pop("TZ", None)
        time.tzset()


def test_story_list_shows_names_stage_local_time_title_and_error(monkeypatch):
    use_local_offset(monkeypatch)
    raw = story_config()
    raw["videos"]["long"]["title"] = "Full tale"
    raw["videos"]["short"]["title"] = "Quick tale"
    from youtube_auto_uploader.editor import FolderFiles

    folders = [
        {"id": "beta", "name": "Zebra", "mimeType": FOLDER_MIME},
        {"id": "alpha", "name": "Midnight Tale", "mimeType": FOLDER_MIME},
        {"id": "note", "name": "notes.txt", "mimeType": "text/plain"},
    ]
    details = {
        "alpha": FolderFiles("cfg-alpha", raw, "", ["long.mp4", "short.mp4"]),
        "beta": FolderFiles(None, None, "Expected one config.json; found 0", []),
    }
    state = {"stories": alpha_stories()}
    rows = assemble_story_views(folders, details, state)
    assert [row.name for row in rows] == ["Midnight Tale", "Zebra"]
    alpha = rows[0]
    assert alpha.folder_id == "alpha"
    assert alpha.roles["long"].stage == "uploaded"
    assert alpha.roles["long"].title == "Full tale"
    assert alpha.roles["long"].publish_local == "2026-11-01 18:00 -06:00"
    assert alpha.roles["long"].error == ""
    assert alpha.roles["long"].retry_allowed is False
    assert "duplicate upload" in alpha.roles["long"].retry_reason
    short = alpha.roles["short"]
    assert short.stage == "needs_review"
    assert short.title == "Quick tale"
    assert short.error == "quota exceeded"
    assert short.retry_allowed is True
    assert rows[1].config_error == "Expected one config.json; found 0"
    assert rows[1].roles["long"].stage == "not started"
    assert rows[1].roles["long"].retry_allowed is False


@pytest.mark.parametrize("stage", ["needs_review", "failed", "missed"])
def test_retry_resets_an_unstarted_job_and_keeps_the_story(tmp_path, stage):
    stories = alpha_stories()
    stories["alpha"]["jobs"]["short"]["stage"] = stage
    stories["alpha"]["jobs"]["short"]["consecutive_failures"] = 10
    stories["alpha"]["jobs"]["short"]["noticed_publish_at"] = (
        "2026-01-01T00:00:00+00:00"
    )
    store = make_store(tmp_path, stories)
    config = write_config(tmp_path / "config.json")
    before_long = json.loads(json.dumps(stories["alpha"]["jobs"]["long"]))
    result = retry_saved(config, "alpha", "short")
    assert result.ok is True
    assert "waiting" in result.message
    saved = store.load()
    assert list(saved["stories"]) == ["alpha"]
    short = saved["stories"]["alpha"]["jobs"]["short"]
    assert short["stage"] == "waiting"
    assert "error" not in short
    assert "next_retry" not in short
    assert "cache_md5" not in short
    assert short["source"]["id"] == "alpha-short"
    assert short["attempts"] == 0
    assert short["consecutive_failures"] == 0
    assert "noticed_publish_at" not in short
    assert saved["stories"]["alpha"]["jobs"]["long"] == before_long
    assert saved["binding"]["drive_folder_id"] == ROOT


def test_retry_is_off_when_a_video_id_or_session_could_duplicate(tmp_path):
    stories = alpha_stories()
    stories["alpha"]["jobs"]["short"]["session"] = "https://upload.example/session"
    stories["alpha"]["jobs"]["short"].pop("video_id", None)
    store = make_store(tmp_path, stories)
    config = write_config(tmp_path / "config.json")
    allowed, reason = retry_decision(stories["alpha"]["jobs"]["long"])
    assert allowed is False
    assert "duplicate upload" in reason
    allowed, reason = retry_decision(stories["alpha"]["jobs"]["short"])
    assert allowed is False
    assert "duplicate upload" in reason
    before = store.path.read_bytes()
    result = retry_saved(config, "alpha", "short")
    assert result.ok is False
    assert "duplicate upload" in result.message
    assert store.path.read_bytes() == before
    pending = job("pending", "alpha-short")
    allowed, reason = retry_decision(pending)
    assert allowed is False
    assert "pending" in reason


def test_retry_is_refused_while_the_runner_holds_the_lock(tmp_path):
    store = make_store(tmp_path, alpha_stories())
    config = write_config(tmp_path / "config.json")
    before = store.path.read_bytes()
    held = store.lock()
    held.acquire()
    try:
        result = retry_saved(config, "alpha", "short")
        assert result.ok is False
        assert result.message == RUNNER_BUSY
        assert "uploading" in result.message
        assert store.path.read_bytes() == before
    finally:
        held.release()
    with store.lock():
        saved = store.load()
    assert saved["stories"]["alpha"]["jobs"]["short"]["stage"] == "needs_review"
    assert saved["stories"]["alpha"]["jobs"]["short"]["error"] == "quota exceeded"


def test_retry_failure_does_not_write_a_partial_story(tmp_path):
    store = make_store(tmp_path, alpha_stories())
    config = write_config(tmp_path / "config.json")
    before = store.path.read_bytes()
    result = retry_saved(config, "missing", "long")
    assert result.ok is False
    assert "not in saved history" in result.message
    assert store.path.read_bytes() == before
    apply_retry(store.load()["stories"]["alpha"]["jobs"]["short"])
    # The in-memory reset above is not a save. The file still has the error.
    assert b"quota exceeded" in store.path.read_bytes()


def test_settings_validation_is_inline_and_keeps_unedited_fields(tmp_path):
    config = write_config(
        tmp_path / "config.json",
        stats_channels=[],
        schedule_margin_seconds=90,
    )
    shown = settings_form(config)
    assert shown["youtube_channel_id"] == CHANNEL
    assert shown["upload_lead_minutes"] == "180"
    assert shown["stats_drive_folder_id"] == ""

    result = save_settings(
        config,
        settings_form_values(
            youtube_channel_id="REPLACE_WITH_YOUTUBE_CHANNEL_ID",
            poll_seconds="4",
            drive_folder_id="",
            upload_log_folder_id="REPLACE_WITH_UPLOAD_LOG_FOLDER_ID",
        ),
    )
    assert result.ok is False
    assert "youtube_channel_id" in result.errors
    assert "poll_seconds" in result.errors
    assert result.errors["drive_folder_id"] == "Drive folder ID is required."
    assert "upload_log_folder_id" in result.errors
    assert (
        json.loads(config.read_text(encoding="utf-8"))["schedule_margin_seconds"] == 90
    )

    store = make_store(tmp_path, alpha_stories())
    before_jobs = store.load()["stories"]
    result = save_settings(
        config,
        settings_form_values(poll_seconds="45", stats_drive_folder_id=""),
    )
    assert result.ok is True
    assert result.confirmation is None
    saved = json.loads(config.read_text(encoding="utf-8"))
    assert saved["poll_seconds"] == 45
    assert saved["stats_drive_folder_id"] is None
    assert saved["state_path"] == "data/state.json"
    assert saved["schedule_margin_seconds"] == 90
    assert saved["cache_dir"] == "data/cache"
    Settings.model_validate(saved)
    assert store.load()["stories"] == before_jobs
    assert store.load()["binding"]["youtube_channel_id"] == CHANNEL


def test_binding_change_waits_for_confirmation_and_keeps_jobs(tmp_path):
    config = write_config(tmp_path / "config.json")
    store = make_store(tmp_path, alpha_stories())
    before = store.path.read_bytes()
    config_before = config.read_bytes()
    proposed = settings_form_values(
        drive_folder_id="other-folder", youtube_channel_id="other-channel"
    )
    preview = save_settings(config, proposed, confirm_binding=False)
    assert preview.ok is False
    assert preview.confirmation
    assert ROOT in preview.confirmation
    assert CHANNEL in preview.confirmation
    assert "other-folder" in preview.confirmation
    assert "other-channel" in preview.confirmation
    assert "Jobs already in state.json stay" in preview.confirmation
    assert config.read_bytes() == config_before
    assert store.path.read_bytes() == before

    saved = save_settings(config, proposed, confirm_binding=True)
    assert saved.ok is True
    assert "Existing jobs were kept" in saved.message
    state = store.load()
    assert state["binding"] == {
        "drive_folder_id": "other-folder",
        "youtube_channel_id": "other-channel",
    }
    assert set(state["stories"]) == {"alpha"}
    assert state["stories"]["alpha"]["jobs"]["long"]["video_id"] == "abc123"
    assert state["stories"]["alpha"]["jobs"]["short"]["stage"] == "needs_review"
    written = json.loads(config.read_text(encoding="utf-8"))
    assert written["drive_folder_id"] == "other-folder"
    assert written["youtube_channel_id"] == "other-channel"
    assert confirmation_for(state, Settings.model_validate(written)) is None


def test_binding_change_is_refused_while_the_runner_is_active(tmp_path):
    config = write_config(tmp_path / "config.json")
    store = make_store(tmp_path, alpha_stories())
    before = store.path.read_bytes()
    config_before = config.read_bytes()
    held = store.lock()
    held.acquire()
    try:
        result = save_settings(
            config,
            settings_form_values(youtube_channel_id="other-channel"),
            confirm_binding=True,
        )
        assert result.ok is False
        assert result.message.startswith(RUNNER_BUSY)
        assert "other-channel" in result.message
        assert config.read_bytes() == config_before
        assert store.path.read_bytes() == before
    finally:
        held.release()


def test_settings_save_restores_config_when_state_cannot_be_saved(
    tmp_path, monkeypatch
):
    config = write_config(tmp_path / "config.json")
    store = make_store(tmp_path, alpha_stories())
    before = config.read_bytes()
    state_before = store.path.read_bytes()

    def fail_save(self, data):
        raise OSError("disk full")

    monkeypatch.setattr(Store, "save", fail_save)
    result = save_settings(
        config,
        settings_form_values(drive_folder_id="other-folder"),
        confirm_binding=True,
    )
    assert result.ok is False
    assert config.read_bytes() == before
    assert store.path.read_bytes() == state_before


def test_settings_write_failure_leaves_the_binding_alone(tmp_path, monkeypatch):
    config = write_config(tmp_path / "config.json")
    store = make_store(tmp_path, alpha_stories())
    before = config.read_bytes()

    def fail_config(path, data):
        raise OSError("disk full")

    monkeypatch.setattr("youtube_auto_uploader.editor.atomic_json", fail_config)
    result = save_settings(
        config,
        settings_form_values(youtube_channel_id="other-channel"),
        confirm_binding=True,
    )
    assert result.ok is False
    assert config.read_bytes() == before
    assert store.load()["binding"]["youtube_channel_id"] == CHANNEL
    assert store.load()["stories"]["alpha"]["jobs"]["long"]["video_id"] == "abc123"


def test_poll_change_does_not_need_the_state_lock(tmp_path):
    config = write_config(tmp_path / "config.json")
    store = make_store(tmp_path, alpha_stories())
    before = store.path.read_bytes()
    held = store.lock()
    held.acquire()
    try:
        result = save_settings(config, settings_form_values(poll_seconds="60"))
        assert result.ok is True
        assert json.loads(config.read_text(encoding="utf-8"))["poll_seconds"] == 60
        assert store.path.read_bytes() == before
    finally:
        held.release()


def test_collect_stories_lists_drive_folders_by_name(tmp_path, monkeypatch):
    use_local_offset(monkeypatch)
    monkeypatch.setenv("GOOGLE_TOKEN_PATH", "secrets/token.json")
    monkeypatch.delenv("GOOGLE_YOUTUBE_TOKEN_PATH", raising=False)
    write_tokens(tmp_path, DRIVE_SCOPES)
    config = write_config(tmp_path / "config.json", drive_folder_id="root")
    make_store(tmp_path, alpha_stories())
    raw = story_config()
    raw["videos"]["long"]["title"] = "Named story"

    class FakeDrive:
        def __init__(self, _http):
            pass

        def children(self, folder):
            if folder == "root":
                return [
                    {
                        "id": "alpha",
                        "name": "Midnight Tale",
                        "mimeType": FOLDER_MIME,
                    }
                ]
            return [
                {"id": "cfg", "name": "config.json", "mimeType": "application/json"},
                {"id": "v1", "name": "long.mp4", "mimeType": "video/mp4"},
                {"id": "v2", "name": "short.mp4", "mimeType": "video/mp4"},
            ]

        def read_text(self, file_id, limit):
            assert file_id == "cfg"
            return json.dumps(raw)

        def close(self):
            pass

    monkeypatch.setattr(
        "youtube_auto_uploader.editor.Drive", lambda http: FakeDrive(http)
    )

    class Http:
        def close(self):
            pass

    monkeypatch.setattr(google_auth, "session", lambda _base: (Http(), lambda: None))
    snapshot = collect_stories(config)
    assert snapshot.message == ""
    assert [story.name for story in snapshot.stories] == ["Midnight Tale"]
    long = snapshot.stories[0].roles["long"]
    assert long.stage == "uploaded"
    assert long.title == "Named story"
    assert long.publish_local == "2026-11-01 18:00 -06:00"
    assert snapshot.stories[0].video_names == ["long.mp4", "short.mp4"]


def source_file(file_id, name):
    return {
        "id": file_id,
        "name": name,
        "size": "3",
        "version": "1",
        "mimeType": "video/mp4",
    }


def enabled_story():
    raw = story_config()
    raw["enabled"] = True
    return raw


class ListingDrive:
    def __init__(self, folders, stories):
        self.folders = folders
        self.stories = stories
        self.story_calls = []

    def children(self, folder):
        assert folder == ROOT
        return [
            {"id": item["id"], "name": item["name"], "mimeType": FOLDER_MIME}
            for item in self.folders
        ]

    def story(self, folder_id):
        self.story_calls.append(folder_id)
        found = self.stories[folder_id]
        if isinstance(found, Exception):
            raise found
        return found


def pair_record(short_stage="pending", short_extra=None, long_extra=None):
    raw = enabled_story()
    long_job = job("uploaded", "long-file", video_id="abc123")
    long_job["source"] = source_file("long-file", "long.mp4")
    if long_extra:
        long_job.update(long_extra)
    short_job = job(short_stage, "short-file")
    short_job["source"] = source_file("short-file", "short.mp4")
    if short_extra:
        short_job.update(short_extra)
    return {
        "name": "Midnight Tale",
        "enabled": True,
        "snapshot": raw,
        "jobs": {"long": long_job, "short": short_job},
    }


def listed_story(raw, sources):
    return Story.model_validate(raw), sources


def test_reload_updates_unstarted_jobs_and_reports_started_ones(tmp_path, monkeypatch):
    from youtube_auto_uploader.editor import apply_live_config as real_apply

    calls = []

    def spy(state, folder_id, record, story, sources):
        calls.append(folder_id)
        return real_apply(state, folder_id, record, story, sources)

    monkeypatch.setattr("youtube_auto_uploader.editor.apply_live_config", spy)
    raw = enabled_story()
    fresh = json.loads(json.dumps(raw))
    fresh["videos"]["long"]["title"] = "Should not apply"
    fresh["videos"]["short"]["title"] = "New short"
    fresh["videos"]["short"]["publish_at"] = "2026-12-01T18:00:00-06:00"
    sources = {
        "long": source_file("long-file", "long.mp4"),
        "short": source_file("short-new", "short.mp4"),
    }
    sources["short"]["md5Checksum"] = "changed"
    drive = ListingDrive(
        [{"id": "alpha", "name": "Renamed Tale"}],
        {"alpha": listed_story(fresh, sources)},
    )
    state = {"stories": {"alpha": pair_record(short_extra={"cache_md5": "old"})}}
    notes, changed = reload_stories(drive, state, ROOT)
    text = "\n".join(notes)
    assert calls == ["alpha"]
    assert changed is True
    record = state["stories"]["alpha"]
    assert record["name"] == "Renamed Tale"
    assert record["snapshot"]["videos"]["long"]["title"] == "Example story - full video"
    assert record["jobs"]["long"]["video_id"] == "abc123"
    assert record["jobs"]["long"]["source"]["id"] == "long-file"
    assert record["snapshot"]["videos"]["short"]["title"] == "New short"
    assert record["snapshot"]["videos"]["short"]["publish_at"] == (
        "2026-12-01T18:00:00-06:00"
    )
    assert record["jobs"]["short"]["source"]["id"] == "short-new"
    assert "cache_md5" not in record["jobs"]["short"]
    assert "renamed to Renamed Tale" in text
    assert "short:" in text
    assert "title" in text
    assert "publish_at" in text
    assert "file" in text
    assert "not applied" in text
    assert "YouTube id" in text

    config = write_config(tmp_path / "config.json")
    make_store(tmp_path, {"alpha": pair_record(short_extra={"cache_md5": "old"})})
    monkeypatch.setattr(google_auth, "login_message", lambda _base: None)
    monkeypatch.setattr(google_auth, "session", lambda _base: (object(), lambda: None))

    class Http:
        def close(self):
            pass

    monkeypatch.setattr(google_auth, "session", lambda _base: (Http(), lambda: None))
    monkeypatch.setattr("youtube_auto_uploader.editor.Drive", lambda _http: drive)
    result = reload_from_drive(config)
    assert result.ok is True
    saved = Store(tmp_path / "data" / "state.json").load()
    assert saved["stories"]["alpha"]["snapshot"]["videos"]["short"]["title"] == (
        "New short"
    )
    assert saved["stories"]["alpha"]["jobs"]["long"]["video_id"] == "abc123"
    assert (
        saved["stories"]["alpha"]["snapshot"]["videos"]["long"]["title"]
        == "Example story - full video"
    )


def test_reload_rejects_an_unsafe_merge_without_saving(tmp_path):
    raw = enabled_story()
    fresh = json.loads(json.dumps(raw))
    fresh["videos"]["long"]["filename"] = "other.mp4"
    fresh["videos"]["short"]["filename"] = "long.mp4"
    drive = ListingDrive(
        [{"id": "alpha", "name": "Midnight Tale"}],
        {
            "alpha": listed_story(
                fresh,
                {
                    "long": source_file("other-file", "other.mp4"),
                    "short": source_file("short-new", "long.mp4"),
                },
            )
        },
    )
    store = make_store(tmp_path, {"alpha": pair_record()})
    before = store.path.read_bytes()
    state = store.load()
    notes, changed = reload_stories(drive, state, ROOT)
    assert changed is False
    assert state["stories"]["alpha"]["snapshot"]["videos"]["short"]["filename"] == (
        "short.mp4"
    )
    assert state["stories"]["alpha"]["jobs"]["long"]["video_id"] == "abc123"
    assert "different files" in "\n".join(notes)
    assert store.path.read_bytes() == before


def test_reload_rejects_a_file_another_story_already_uses():
    raw = enabled_story()
    fresh = json.loads(json.dumps(raw))
    drive = ListingDrive(
        [
            {"id": "alpha", "name": "Midnight Tale"},
            {"id": "beta", "name": "Other"},
        ],
        {
            "alpha": listed_story(
                fresh,
                {
                    "long": source_file("long-file", "long.mp4"),
                    "short": source_file("taken", "short.mp4"),
                },
            )
        },
    )
    beta = pair_record()
    beta["jobs"]["long"]["source"] = source_file("taken", "other.mp4")
    beta["snapshot"]["videos"]["long"]["filename"] = "other.mp4"
    state = {"stories": {"alpha": pair_record(), "beta": beta}}
    notes, changed = reload_stories(drive, state, ROOT, folder_id="alpha")
    assert changed is False
    assert state["stories"]["alpha"]["jobs"]["short"]["source"]["id"] == "short-file"
    assert state["stories"]["beta"]["jobs"]["long"]["source"]["id"] == "taken"
    assert "another story" in "\n".join(notes)
    assert drive.story_calls == ["alpha"]


def test_reload_one_story_does_not_read_the_other():
    raw = enabled_story()
    fresh = json.loads(json.dumps(raw))
    fresh["videos"]["short"]["title"] = "Only alpha"
    drive = ListingDrive(
        [
            {"id": "alpha", "name": "Midnight Tale"},
            {"id": "beta", "name": "Other"},
        ],
        {
            "alpha": listed_story(
                fresh,
                {
                    "long": source_file("long-file", "long.mp4"),
                    "short": source_file("short-file", "short.mp4"),
                },
            )
        },
    )
    state = {
        "stories": {
            "alpha": pair_record(),
            "beta": pair_record(),
        }
    }
    beta_before = json.loads(json.dumps(state["stories"]["beta"]))
    notes, changed = reload_stories(drive, state, ROOT, folder_id="alpha")
    assert changed is True
    assert state["stories"]["beta"] == beta_before
    assert drive.story_calls == ["alpha"]
    assert (
        "Only alpha"
        in state["stories"]["alpha"]["snapshot"]["videos"]["short"]["title"]
    )
    assert "alpha" in "\n".join(notes) or "Midnight Tale" in "\n".join(notes)


def test_reload_reports_a_bad_config_and_leaves_history(tmp_path):
    from youtube_auto_uploader.status import ConfigError

    drive = ListingDrive(
        [{"id": "alpha", "name": "Midnight Tale"}],
        {"alpha": ConfigError("Expected one config.json; found 0")},
    )
    store = make_store(tmp_path, {"alpha": pair_record()})
    before = store.path.read_bytes()
    state = store.load()
    notes, changed = reload_stories(drive, state, ROOT)
    assert changed is False
    assert "Expected one config.json; found 0" in "\n".join(notes)
    assert state["stories"]["alpha"]["jobs"]["short"]["source"]["id"] == "short-file"
    assert store.path.read_bytes() == before


def test_reload_reads_drive_before_it_takes_the_lock(tmp_path, monkeypatch):
    config = write_config(tmp_path / "config.json")
    make_store(tmp_path, {"alpha": pair_record()})
    events = []

    class Drive:
        def __init__(self, _http):
            pass

        def children(self, folder):
            events.append("read")
            return [{"id": "alpha", "name": "Midnight Tale", "mimeType": FOLDER_MIME}]

        def story(self, folder_id):
            events.append("story")
            raw = enabled_story()
            return listed_story(
                raw,
                {
                    "long": source_file("long-file", "long.mp4"),
                    "short": source_file("short-file", "short.mp4"),
                },
            )

    class Http:
        def close(self):
            pass

    real_lock = Store.lock

    def tracking_lock(self):
        events.append("lock")
        return real_lock(self)

    monkeypatch.setattr(google_auth, "login_message", lambda _base: None)
    monkeypatch.setattr(google_auth, "session", lambda _base: (Http(), lambda: None))
    monkeypatch.setattr("youtube_auto_uploader.editor.Drive", Drive)
    monkeypatch.setattr("youtube_auto_uploader.editor.Store.lock", tracking_lock)
    result = reload_from_drive(config)
    assert result.ok is True
    assert events.index("read") < events.index("story") < events.index("lock")


def test_reload_is_refused_while_the_runner_holds_the_lock(tmp_path, monkeypatch):
    config = write_config(tmp_path / "config.json")
    store = make_store(tmp_path, {"alpha": pair_record()})
    before = store.path.read_bytes()
    events = []

    class Drive:
        def __init__(self, _http):
            pass

        def children(self, folder):
            events.append("read")
            return []

    class Http:
        def close(self):
            pass

    monkeypatch.setattr(google_auth, "login_message", lambda _base: None)
    monkeypatch.setattr(google_auth, "session", lambda _base: (Http(), lambda: None))
    monkeypatch.setattr("youtube_auto_uploader.editor.Drive", Drive)
    held = store.lock()
    held.acquire()
    try:
        result = reload_from_drive(config)
        assert result.ok is False
        assert result.message == RUNNER_BUSY
        assert events == ["read"]
        assert store.path.read_bytes() == before
    finally:
        held.release()


def _widgets(widget):
    yield widget
    for child in widget.winfo_children():
        yield from _widgets(child)


def test_window_shows_a_read_only_stories_note_and_settings(tmp_path):
    tkinter = pytest.importorskip("tkinter")
    try:
        root = tkinter.Tk()
    except tkinter.TclError:
        pytest.skip("no display")
    root.withdraw()
    config = write_config(tmp_path / "config.json")
    make_store(tmp_path, alpha_stories())
    from youtube_auto_uploader.gui import DesktopWindow

    window = DesktopWindow(root, config)
    try:
        root.update()
        assert [window.notebook.tab(tab, "text") for tab in window.notebook.tabs()] == [
            "Status",
            "Stories",
            "Settings",
        ]
        texts = []
        for widget in _widgets(window.root):
            try:
                text = widget.cget("text")
            except tkinter.TclError:
                continue
            if isinstance(text, str) and text:
                texts.append(text)
        assert STORY_EDIT_NOTE in texts
        assert "Reload from Drive" in texts
        assert "Reload this story" in texts
        assert "Retry" in texts
        assert "Edit story" not in texts
        window.notebook.select(2)
        root.update()
        assert window.settings_vars["youtube_channel_id"].get() == CHANNEL
        window.settings_vars["youtube_channel_id"].set(
            "REPLACE_WITH_YOUTUBE_CHANNEL_ID"
        )
        window._save_settings()
        assert window.settings_error_labels["youtube_channel_id"].cget("text")
        window.settings_vars["youtube_channel_id"].set(CHANNEL)
        window.settings_vars["poll_seconds"].set("40")
        window._save_settings()
        assert json.loads(config.read_text(encoding="utf-8"))["poll_seconds"] == 40
    finally:
        window.close()
