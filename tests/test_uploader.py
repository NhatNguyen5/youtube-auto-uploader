import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import portalocker
import pytest
import requests

from youtube_auto_uploader.drive import (
    UPLOAD_LOG_NAME,
    ApiError,
    Drive,
    format_upload_log_record,
    merge_upload_log,
)
from youtube_auto_uploader.models import Settings, Story
from youtube_auto_uploader.runner import Runner, poll_retry, scheduled_delay
from youtube_auto_uploader.status import ConfigError
from youtube_auto_uploader.storage import (
    Store,
    atomic_json,
    parse_json,
    upload_log_outcome,
)
from youtube_auto_uploader.youtube import NeedsReview, YouTube

NOW = datetime(2026, 10, 17, tzinfo=UTC)
SESSION = "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=test"


def story_data():
    data = json.loads(Path("examples/story-config.example.json").read_text())
    data["enabled"] = True
    for video in data["videos"].values():
        video["publish_at"] = (NOW + timedelta(hours=1)).isoformat()
    return data


def source(role):
    return {
        "id": role,
        "name": f"{role}.mp4",
        "size": "3",
        "version": "1",
        "mimeType": "video/mp4",
        "md5Checksum": hashlib.md5(b"abc").hexdigest(),
        "capabilities": {"canDownload": True},
    }


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path / "state.json")
    store.initialize()
    drive, youtube = Mock(), Mock()
    drive.children.return_value = [
        {
            "id": "story",
            "name": "Story",
            "mimeType": "application/vnd.google-apps.folder",
        }
    ]
    drive.story.return_value = (
        Story.model_validate(story_data()),
        {role: source(role) for role in ("long", "short")},
    )

    def download(src, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"abc")
        return hashlib.md5(b"abc").hexdigest()

    drive.download.side_effect = download
    youtube.start.return_value = SESSION
    runner = Runner(
        Settings(youtube_channel_id="channel"),
        tmp_path,
        store,
        drive,
        youtube,
        report=lambda text: None,
        clock=lambda: NOW,
    )
    return runner, store, drive, youtube


def response(status, data=None, headers=None, content=b""):
    result = Mock(status_code=status, headers=headers or {})
    result.json.return_value = data or {}
    if isinstance(content, str):
        content = content.encode()
    result.content = content
    return result


def attach_log(runner, drive):
    runner.settings.upload_log_folder_id = "log-folder"
    log = {"text": ""}

    def append(folder, record):
        assert folder == "log-folder"
        merged = merge_upload_log(log["text"], record)
        if merged is None:
            return False
        log["text"] = merged
        return True

    drive.append_upload_log.side_effect = append
    return log


def log_lines(log):
    return [json.loads(line) for line in log["text"].splitlines() if line.strip()]


def log_record(**overrides):
    record = {
        "schema_version": 1,
        "date": NOW.isoformat(),
        "channel": "channel",
        "source_file": "long.mp4",
        "source_file_id": "long",
        "video_id": "yt-long",
        "status": "uploaded",
        "error": "",
        "job_id": "channel/story/long",
    }
    record.update(overrides)
    return record


def test_duplicate_json_keys():
    with pytest.raises(ValueError, match="duplicate"):
        parse_json('{"enabled":true,"enabled":false}')


@pytest.mark.parametrize(
    "change",
    [
        "naive",
        "same_file",
        "unknown",
        "missing_short",
        "string_bool",
        "missing_audience",
        "string_synthetic",
    ],
)
def test_invalid_story(change):
    data = story_data()
    if change == "naive":
        data["videos"]["long"]["publish_at"] = "2026-11-01T18:00:00"
    elif change == "same_file":
        data["videos"]["short"]["filename"] = "long.mp4"
    elif change == "unknown":
        data["command"] = "anything"
    elif change == "missing_short":
        del data["videos"]["short"]
    elif change == "missing_audience":
        del data["videos"]["long"]["made_for_kids"]
    elif change == "string_synthetic":
        data["videos"]["short"]["contains_synthetic_media"] = "false"
    else:
        data["enabled"] = "true"
    with pytest.raises(ValueError):
        Story.model_validate(data)


def test_story_template_audience_flags_default_false():
    story = Story.model_validate(
        json.loads(Path("examples/story-config.example.json").read_text())
    )
    for video in story.videos.values():
        assert video.made_for_kids is False
        assert video.contains_synthetic_media is False


def test_upload_and_schedule_send_audience_and_synthetic_flags():
    data = story_data()
    data["videos"]["long"]["made_for_kids"] = False
    data["videos"]["long"]["contains_synthetic_media"] = True
    data["videos"]["short"]["made_for_kids"] = True
    data["videos"]["short"]["contains_synthetic_media"] = False
    story = Story.model_validate(data)
    http = Mock()
    http.post.return_value = response(200, headers={"Location": SESSION})
    http.put.return_value = response(200)
    youtube = YouTube(http)

    youtube.start(story.videos["long"], source("long"))
    insert = http.post.call_args
    assert insert.args[0] == "https://www.googleapis.com/upload/youtube/v3/videos"
    assert insert.kwargs["params"]["part"] == "snippet,status"
    assert insert.kwargs["json"]["status"] == {
        "privacyStatus": "private",
        "selfDeclaredMadeForKids": False,
        "containsSyntheticMedia": True,
        "license": "youtube",
        "embeddable": True,
    }
    assert "madeForKids" not in insert.kwargs["json"]["status"]

    youtube.schedule("video-long", story.videos["long"])
    update = http.put.call_args
    assert update.args[0] == "https://www.googleapis.com/youtube/v3/videos"
    assert update.kwargs["params"] == {"part": "status"}
    assert update.kwargs["json"]["id"] == "video-long"
    status = update.kwargs["json"]["status"]
    assert status["privacyStatus"] == "private"
    assert status["selfDeclaredMadeForKids"] is False
    assert status["containsSyntheticMedia"] is True
    assert status["publishAt"] == story.videos["long"].publish_at.isoformat()
    assert "madeForKids" not in status

    youtube.start(story.videos["short"], source("short"))
    short_status = http.post.call_args.kwargs["json"]["status"]
    assert short_status["selfDeclaredMadeForKids"] is True
    assert short_status["containsSyntheticMedia"] is False


def test_missing_corrupt_and_existing_state(tmp_path):
    store = Store(tmp_path / "state.json")
    with pytest.raises(ValueError, match="missing"):
        store.load()
    store.initialize()
    with pytest.raises(ValueError, match="already exists"):
        store.initialize()
    store.path.write_text("{bad")
    with pytest.raises(ValueError):
        store.load()


def test_exclusive_lock(tmp_path):
    store = Store(tmp_path / "state.json")
    with store.lock(), pytest.raises(portalocker.exceptions.LockException):
        with store.lock():
            pass


def test_atomic_failure_preserves_previous_state(tmp_path, monkeypatch):
    path = tmp_path / "state.json"
    atomic_json(path, {"old": True})
    monkeypatch.setattr(
        "youtube_auto_uploader.storage.os.replace",
        Mock(side_effect=OSError("disk failure")),
    )
    with pytest.raises(OSError):
        atomic_json(path, {"old": False})
    assert parse_json(path.read_text()) == {"old": True}


def test_replace_retries_a_windows_permission_error(tmp_path, monkeypatch):
    import youtube_auto_uploader.storage as storage
    from youtube_auto_uploader.status import ConfigError

    path = tmp_path / "state.json"
    atomic_json(path, {"old": True})
    real = storage.os.replace
    calls = {"n": 0}
    slept = []

    def wrapped(source, destination):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("busy")
        return real(source, destination)

    monkeypatch.setattr(storage.os, "replace", wrapped)
    monkeypatch.setattr(storage.time, "sleep", slept.append)
    atomic_json(path, {"new": True})
    assert calls["n"] == 3
    assert slept == [0.05, 0.1]
    assert parse_json(path.read_text()) == {"new": True}

    calls["n"] = 0

    def always_busy(_source, _destination):
        calls["n"] += 1
        raise PermissionError("busy")

    monkeypatch.setattr(storage.os, "replace", always_busy)
    with pytest.raises(ConfigError, match="still open"):
        atomic_json(path, {"newer": True})
    assert calls["n"] == 4
    assert parse_json(path.read_text()) == {"new": True}


def _set_story_publish_at(drive, moment):
    story, sources = drive.story.return_value
    data = story.model_dump(mode="json")
    stamp = moment.isoformat()
    for video in data["videos"].values():
        video["publish_at"] = stamp
    drive.story.return_value = (Story.model_validate(data), sources)


def _replace_story_config(drive, mutate):
    story, sources = drive.story.return_value
    data = story.model_dump(mode="json")
    mutate(data)
    drive.story.return_value = (Story.model_validate(data), sources)
    return data


def test_rename_keeps_one_story_and_rereads_config(setup):
    runner, store, drive, youtube = setup
    runner.discover()

    def mutate(data):
        data["videos"]["short"]["title"] = "Changed"

    _replace_story_config(drive, mutate)
    drive.children.return_value[0]["name"] = "Renamed"
    runner.discover()
    record = runner.state["stories"]["story"]
    assert list(runner.state["stories"]) == ["story"]
    assert record["name"] == "Renamed"
    assert record["snapshot"]["videos"]["short"]["title"] == "Changed"
    assert drive.story.call_count == 2


def test_config_edit_before_upload_is_honored(setup):
    runner, store, drive, youtube = setup
    runner.discover()
    later = (NOW + timedelta(hours=2)).isoformat()

    def mutate(data):
        data["videos"]["long"]["title"] = "Updated title"
        data["videos"]["long"]["publish_at"] = later
        data["videos"]["short"]["description"] = "Updated description"
        data["videos"]["short"]["publish_at"] = later

    _replace_story_config(drive, mutate)
    runner.cycle()
    by_role = {
        call.args[1]["id"]: call.args[0] for call in youtube.start.call_args_list
    }
    assert set(by_role) == {"long", "short"}
    assert by_role["long"].title == "Updated title"
    assert by_role["long"].publish_at.isoformat() == later
    assert by_role["short"].description == "Updated description"
    assert (
        runner.state["stories"]["story"]["snapshot"]["videos"]["long"]["title"]
        == "Updated title"
    )


def test_past_publish_at_waits_and_uploads_after_a_future_edit(setup):
    runner, store, drive, youtube = setup
    reports = []
    runner.report = reports.append
    passed = (
        "Story Story [story] long: publish time passed, waiting for a new publish_at"
    )
    _set_story_publish_at(drive, NOW - timedelta(minutes=5))
    runner.cycle()
    youtube.start.assert_not_called()
    youtube.transfer.assert_not_called()
    jobs = runner.state["stories"]["story"]["jobs"]
    assert jobs["long"]["stage"] == "waiting"
    assert jobs["short"]["stage"] == "waiting"
    assert reports.count(passed) == 1
    runner.cycle()
    assert reports.count(passed) == 1
    youtube.start.assert_not_called()
    _set_story_publish_at(drive, NOW - timedelta(minutes=1))
    runner.cycle()
    assert reports.count(passed) == 2
    youtube.start.assert_not_called()
    _set_story_publish_at(drive, NOW + timedelta(hours=1))
    runner.cycle()
    assert {call.args[1]["id"] for call in youtube.start.call_args_list} == {
        "long",
        "short",
    }
    assert jobs["long"]["stage"] != "waiting"


def test_legacy_missed_without_upload_becomes_eligible(setup):
    runner, store, drive, youtube = setup
    runner.discover()
    record = runner.state["stories"]["story"]
    for job in record["jobs"].values():
        job["stage"] = "missed"
    store.save(runner.state)
    restarted = Runner(
        runner.settings,
        runner.base,
        store,
        drive,
        youtube,
        report=lambda text: None,
        clock=lambda: NOW,
    )
    restarted.cycle()
    assert {call.args[1]["id"] for call in youtube.start.call_args_list} == {
        "long",
        "short",
    }
    saved = store.load()["stories"]["story"]["jobs"]
    assert saved["long"]["stage"] != "missed"
    assert saved["short"]["stage"] != "missed"


def test_video_id_is_never_reuploaded_when_config_changes(setup):
    runner, store, drive, youtube = setup
    runner.discover()
    record = runner.state["stories"]["story"]
    record["jobs"]["long"].update(stage="uploaded", video_id="known")
    old_long = copy.deepcopy(record["snapshot"]["videos"]["long"])

    def mutate(data):
        data["videos"]["long"]["title"] = "Should not apply"
        data["videos"]["long"]["publish_at"] = (NOW + timedelta(hours=5)).isoformat()
        data["videos"]["short"]["title"] = "Short updated"

    _replace_story_config(drive, mutate)
    youtube.inspect.return_value = {
        "status": {"privacyStatus": "private", "uploadStatus": "uploaded"},
        "processingDetails": {"processingStatus": "processing"},
    }
    runner.cycle()
    assert record["jobs"]["long"]["video_id"] == "known"
    assert record["snapshot"]["videos"]["long"] == old_long
    assert record["snapshot"]["videos"]["short"]["title"] == "Short updated"
    assert [call.args[1]["id"] for call in youtube.start.call_args_list] == ["short"]
    assert youtube.start.call_args.args[0].title == "Short updated"


def test_session_keeps_its_snapshot_when_config_publish_at_passes(setup):
    runner, store, drive, youtube = setup
    runner.discover()
    record = runner.state["stories"]["story"]
    record["jobs"]["long"].update(stage="uploading", session=SESSION)
    old_long = copy.deepcopy(record["snapshot"]["videos"]["long"])
    _set_story_publish_at(drive, NOW - timedelta(hours=1))
    runner.cycle()
    youtube.transfer.assert_called()
    assert record["jobs"]["long"]["session"] == SESSION
    assert record["snapshot"]["videos"]["long"] == old_long
    assert record["jobs"]["short"]["stage"] == "waiting"
    youtube.start.assert_not_called()


def test_missed_with_video_id_is_not_reuploaded(setup):
    runner, store, drive, youtube = setup
    runner.discover()
    record = runner.state["stories"]["story"]
    record["jobs"]["long"].update(stage="missed", video_id="known")

    def mutate(data):
        data["videos"]["long"]["title"] = "Should not apply"
        data["videos"]["long"]["publish_at"] = (NOW + timedelta(hours=2)).isoformat()

    _replace_story_config(drive, mutate)
    runner.cycle()
    assert record["jobs"]["long"]["video_id"] == "known"
    assert record["jobs"]["long"]["stage"] == "missed"
    assert "long" not in {call.args[1]["id"] for call in youtube.start.call_args_list}


def _point_story_at(drive, filenames, sources):
    story, _old = drive.story.return_value
    data = story.model_dump(mode="json")
    for role, filename in filenames.items():
        data["videos"][role]["filename"] = filename
    drive.story.return_value = (Story.model_validate(data), sources)


def test_started_file_rename_does_not_wedge_state(setup):
    runner, store, drive, youtube = setup
    reports = []
    runner.report = reports.append
    runner.discover()
    record = runner.state["stories"]["story"]
    record["jobs"]["long"].update(stage="uploading", session=SESSION)
    before = copy.deepcopy(record["snapshot"])
    short_source = copy.deepcopy(record["jobs"]["short"]["source"])
    other = source("other")
    other["name"] = "other.mp4"
    reused = source("fresh-long")
    reused["name"] = "long.mp4"
    _point_story_at(
        drive,
        {"long": "other.mp4", "short": "long.mp4"},
        {"long": other, "short": reused},
    )
    runner.cycle()
    assert record["snapshot"] == before
    assert record["jobs"]["long"]["session"] == SESSION
    assert record["jobs"]["long"]["source"]["id"] == "long"
    assert record["jobs"]["short"]["source"] == short_source
    assert record["jobs"]["short"]["stage"] == "pending"
    youtube.start.assert_not_called()
    text = "\n".join(reports)
    assert "ConfigError" in text
    assert "different files" in text
    loaded = store.load()
    Story.model_validate(loaded["stories"]["story"]["snapshot"])
    restarted = Runner(
        runner.settings,
        runner.base,
        store,
        drive,
        youtube,
        report=lambda text: None,
        clock=lambda: NOW,
    )
    assert restarted.state["stories"]["story"]["snapshot"] == before
    assert restarted.state["stories"]["story"]["jobs"]["short"]["source"]["id"] == (
        "short"
    )


def test_unstarted_role_cannot_take_a_started_source(setup):
    runner, store, drive, youtube = setup
    reports = []
    runner.report = reports.append
    runner.discover()
    record = runner.state["stories"]["story"]
    record["jobs"]["long"].update(stage="uploading", session=SESSION)
    other = source("other")
    other["name"] = "other.mp4"
    reused = source("long")
    reused["name"] = "clip.mp4"
    _point_story_at(
        drive,
        {"long": "other.mp4", "short": "clip.mp4"},
        {"long": other, "short": reused},
    )
    runner.cycle()
    assert record["jobs"]["short"]["source"]["id"] == "short"
    assert record["snapshot"]["videos"]["short"]["filename"] == "short.mp4"
    assert record["jobs"]["short"]["stage"] == "pending"
    youtube.start.assert_not_called()
    assert "Source already belongs to a video that has started" in "\n".join(reports)
    Story.model_validate(store.load()["stories"]["story"]["snapshot"])


def test_finished_story_is_not_reread(setup):
    runner, store, drive, youtube = setup
    reports = []
    runner.report = reports.append
    runner.discover()
    assert drive.story.call_count == 1
    record = runner.state["stories"]["story"]
    record["jobs"]["long"].update(stage="published", video_id="known-long")
    record["jobs"]["short"].update(stage="published", video_id="known-short")
    drive.story.side_effect = ValueError("files deleted")
    youtube.inspect.return_value = {
        "status": {"privacyStatus": "public", "uploadStatus": "processed"},
    }
    runner.cycle()
    runner.cycle()
    assert drive.story.call_count == 1
    assert not any("waiting for valid config" in line for line in reports)
    assert record["jobs"]["long"]["video_id"] == "known-long"
    assert record["jobs"]["short"]["video_id"] == "known-short"


def test_config_fetch_failure_does_not_upload_stale_metadata(setup):
    runner, store, drive, youtube = setup
    reports = []
    runner.report = reports.append
    runner.discover()
    record = runner.state["stories"]["story"]
    record["jobs"]["long"].update(stage="uploaded", video_id="known")
    youtube.inspect.return_value = {
        "status": {"privacyStatus": "private", "uploadStatus": "uploaded"},
        "processingDetails": {"processingStatus": "processing"},
    }
    drive.story.side_effect = requests.ConnectionError("secret URL must not be logged")
    runner.cycle()
    youtube.inspect.assert_called_with("known")
    youtube.start.assert_not_called()
    assert record["jobs"]["short"]["stage"] == "pending"
    text = "\n".join(reports)
    assert "Story Story [story]: ConnectionError" in text
    assert "secret URL" not in text

    drive.story.side_effect = ApiError(503, "backendError")
    runner.cycle()
    youtube.start.assert_not_called()
    assert "Story Story [story]: HTTP 503: backendError" in "\n".join(reports)


def test_disabled_story_is_not_uploaded(setup):
    runner, store, drive, youtube = setup

    def mutate(data):
        data["enabled"] = False

    _replace_story_config(drive, mutate)
    runner.cycle()
    assert runner.state["stories"] == {}
    youtube.start.assert_not_called()


def test_disabling_before_upload_skips_the_story(setup):
    runner, store, drive, youtube = setup
    runner.discover()

    def mutate(data):
        data["enabled"] = False

    _replace_story_config(drive, mutate)
    runner.cycle()
    youtube.start.assert_not_called()
    assert runner.state["stories"]["story"]["enabled"] is False


def test_partial_success_only_uploads_remaining_video(setup):
    runner, store, drive, youtube = setup
    runner.discover()
    record = runner.state["stories"]["story"]
    record["jobs"]["long"].update(stage="uploaded", video_id="known")
    youtube.inspect.return_value = {
        "status": {"privacyStatus": "private", "uploadStatus": "uploaded"},
        "processingDetails": {"processingStatus": "processing"},
    }
    runner.cycle()
    assert youtube.start.call_count == 1
    assert youtube.start.call_args.args[1]["id"] == "short"
    assert record["jobs"]["long"]["video_id"] == "known"


def test_session_is_persisted_before_any_bytes(setup):
    runner, store, drive, youtube = setup
    runner.discover()

    def transfer(job, path, save):
        assert store.load()["stories"]["story"]["jobs"]["long"]["session"] == SESSION

    youtube.transfer.side_effect = transfer
    runner.step("story", "long", runner.state["stories"]["story"])


def test_storage_failure_prevents_transfer(setup, monkeypatch):
    runner, store, drive, youtube = setup
    runner.discover()
    real_save = store.save

    def save(data):
        if data["stories"]["story"]["jobs"]["long"].get("session"):
            raise OSError("disk failed")
        real_save(data)

    monkeypatch.setattr(store, "save", save)
    with pytest.raises(OSError):
        runner.process("story", "long", runner.state["stories"]["story"])
    youtube.transfer.assert_not_called()


def test_expired_deadline_never_starts_upload(setup):
    runner, store, drive, youtube = setup
    runner.discover()
    runner.clock = lambda: NOW + timedelta(hours=2)
    runner.cycle()
    youtube.start.assert_not_called()
    assert runner.state["stories"]["story"]["jobs"]["long"]["stage"] == "waiting"


def test_lost_schedule_response_is_reconciled_after_deadline(setup):
    runner, store, drive, youtube = setup
    runner.discover()
    record = runner.state["stories"]["story"]
    job = record["jobs"]["long"]
    job.update(stage="scheduling", video_id="known")
    youtube.inspect.return_value = {
        "status": {
            "privacyStatus": "private",
            "publishAt": record["snapshot"]["videos"]["long"]["publish_at"],
        }
    }
    runner.clock = lambda: NOW + timedelta(hours=2)
    runner.step("story", "long", record)
    assert job["stage"] == "scheduled"
    youtube.schedule.assert_not_called()


def test_resume_uses_server_offset(tmp_path):
    path = tmp_path / "video"
    path.write_bytes(b"abc")
    http = Mock()
    http.put.side_effect = [
        response(308, headers={"Range": "bytes=0-0"}),
        response(201, {"id": "uploaded"}),
    ]
    job = {"source": source("long"), "session": SESSION, "offset": 0}
    saves = []
    YouTube(http).transfer(job, path, lambda: saves.append(copy.deepcopy(job)))
    assert http.put.call_args_list[1].kwargs["data"] == b"bc"
    assert job["video_id"] == "uploaded"
    assert saves[-1]["video_id"] == "uploaded"


def test_completed_session_recovers_without_cache(tmp_path):
    http = Mock()
    http.put.return_value = response(201, {"id": "recovered"})
    job = {"source": source("long"), "session": SESSION}
    YouTube(http).transfer(job, tmp_path / "missing", lambda: None)
    assert job["video_id"] == "recovered"


def test_expired_session_requires_review(tmp_path):
    http = Mock()
    http.put.return_value = response(404)
    with pytest.raises(NeedsReview):
        YouTube(http).transfer(
            {"source": source("long"), "session": SESSION},
            tmp_path / "missing",
            lambda: None,
        )
    assert http.put.call_count == 1


def test_alternating_blips_keep_first_backoff(setup):
    runner, store, drive, youtube = setup
    runner.discover()
    record = runner.state["stories"]["story"]
    job = record["jobs"]["long"]
    job.update(stage="uploading", session=SESSION)
    clock = {"now": NOW}
    runner.clock = lambda: clock["now"]
    mode = {"fail": True}

    def transfer(job, path, save):
        if mode["fail"]:
            raise requests.ConnectionError("blip")
        job["offset"] = job.get("offset", 0) + 1
        save()

    youtube.transfer.side_effect = transfer
    for _ in range(12):
        mode["fail"] = True
        runner.process("story", "long", record)
        assert job["stage"] != "needs_review"
        assert job["consecutive_failures"] == 1
        retry_at = datetime.fromisoformat(job["next_retry"])
        delay = (retry_at - clock["now"]).total_seconds()
        assert 60 <= delay <= 70
        clock["now"] = retry_at
        mode["fail"] = False
        runner.process("story", "long", record)
        assert job["stage"] != "needs_review"
        assert job["consecutive_failures"] == 0
        assert "next_retry" not in job
    saved = store.load()["stories"]["story"]["jobs"]["long"]
    assert saved["stage"] == "uploading"
    assert saved["attempts"] == 12
    assert saved["consecutive_failures"] == 0
    assert "next_retry" not in saved


def test_schedule_failures_back_off_until_review(setup):
    runner, store, drive, youtube = setup
    runner.discover()
    record = runner.state["stories"]["story"]
    job = record["jobs"]["long"]
    record["snapshot"]["videos"]["long"]["publish_at"] = (
        NOW + timedelta(days=2)
    ).isoformat()
    job.update(stage="uploaded", video_id="known")
    clock = {"now": NOW}
    runner.clock = lambda: clock["now"]
    youtube.inspect.return_value = {
        "status": {"privacyStatus": "private", "uploadStatus": "processed"},
        "processingDetails": {"processingStatus": "succeeded"},
    }
    youtube.schedule.side_effect = ApiError(503, "backendError")
    delays = []
    for _ in range(9):
        runner.process("story", "long", record)
        assert job["stage"] != "needs_review"
        retry_at = datetime.fromisoformat(job["next_retry"])
        delays.append((retry_at - clock["now"]).total_seconds())
        clock["now"] = retry_at
    runner.process("story", "long", record)
    assert job["stage"] == "needs_review"
    assert job["consecutive_failures"] == 10
    assert youtube.inspect.call_count == 10
    assert youtube.schedule.call_count == 10
    for delay, base in zip(
        delays, [60, 120, 240, 480, 960, 1920, 3600, 3600, 3600], strict=True
    ):
        assert base <= delay <= base + 10


def test_schedule_margin_cap_respects_retry_after(setup):
    runner, store, drive, youtube = setup
    runner.discover()
    record = runner.state["stories"]["story"]
    job = record["jobs"]["long"]
    job.update(stage="uploading", session=SESSION)
    publish_at = Story.model_validate(record["snapshot"]).videos["long"].publish_at
    margin = timedelta(seconds=runner.settings.schedule_margin_seconds)
    clock = {"now": publish_at - margin - timedelta(seconds=30)}
    runner.clock = lambda: clock["now"]

    def delay_after(error):
        job.pop("next_retry", None)
        youtube.transfer.side_effect = error
        runner.process("story", "long", record)
        return (
            datetime.fromisoformat(job["next_retry"]) - clock["now"]
        ).total_seconds()

    capped = delay_after(requests.ConnectionError("blip"))
    assert capped == pytest.approx(30)

    clock["now"] = publish_at - margin - timedelta(seconds=30)
    honored = delay_after(ApiError(429, "rateLimitExceeded", 180))
    assert honored == pytest.approx(180)

    clock["now"] = publish_at - margin - timedelta(milliseconds=250)
    tiny = delay_after(requests.ConnectionError("blip"))
    assert tiny >= 60


def test_network_failure_preserves_session_and_backs_off(setup):
    runner, store, drive, youtube = setup
    runner.discover()
    youtube.transfer.side_effect = requests.ConnectionError(
        "secret URL must not be logged"
    )
    record = runner.state["stories"]["story"]
    runner.process("story", "long", record)
    job = store.load()["stories"]["story"]["jobs"]["long"]
    assert job["session"] == SESSION
    assert job["next_retry"]
    assert "secret URL" not in job["error"]


def test_paginated_drive_listing():
    http = Mock()
    http.get.side_effect = [
        response(200, {"files": [{"id": "one"}], "nextPageToken": "next"}),
        response(200, {"files": [{"id": "two"}]}),
    ]
    assert [f["id"] for f in Drive(http).children("root")] == ["one", "two"]
    assert http.get.call_count == 2


def test_duplicate_drive_configs_rejected():
    drive = Drive(Mock())
    drive.children = Mock(
        return_value=[
            {"name": "config.json", "id": "a"},
            {"name": "config.json", "id": "b"},
        ]
    )
    with pytest.raises(ValueError, match="found 2"):
        drive.story("story")


class _ConfigMedia:
    def __init__(self, payload):
        self.status_code = 200
        self.headers = {}
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_content(self, chunk):
        yield self.payload


def read_story(files, config):
    drive = Drive(Mock())
    drive.children = Mock(return_value=files)
    drive.http.get.return_value = _ConfigMedia(json.dumps(config).encode())
    return drive.story("story")


@pytest.mark.parametrize(
    ("mime", "filename"),
    [
        ("application/mp4", "long.mp4"),
        ("application/mp4", "clip"),
        ("application/octet-stream", "long.MP4"),
    ],
)
def test_story_accepts_mp4_without_video_mime(mime, filename):
    data = story_data()
    data["videos"]["long"]["filename"] = filename
    long = source("long")
    long.update(name=filename, mimeType=mime)
    story, sources = read_story(
        [{"name": "config.json", "id": "config"}, long, source("short")],
        data,
    )
    assert sources["long"]["mimeType"] == mime
    assert sources["long"]["name"] == filename
    assert story.videos["long"].filename == filename


def test_story_rejects_non_video_file():
    data = story_data()
    data["videos"]["long"]["filename"] = "notes.txt"
    notes = source("long")
    notes.update(name="notes.txt", mimeType="application/pdf")
    with pytest.raises(ValueError, match="Source must be a nonempty video file"):
        read_story(
            [{"name": "config.json", "id": "config"}, notes, source("short")],
            data,
        )


def test_waiting_log_includes_story_error(setup):
    runner, store, drive, youtube = setup
    reports = []
    runner.report = reports.append
    drive.story.side_effect = ValueError("Source must be a nonempty video file")
    runner.discover()
    assert runner.state["stories"] == {}
    assert reports == [
        "Story Story [story]: waiting for valid config and two unique videos "
        "(ValueError: Source must be a nonempty video file)"
    ]
    youtube.start.assert_not_called()


def test_runner_reports_use_the_folder_name_and_fall_back_to_the_id(setup):
    runner, store, drive, youtube = setup
    reports = []
    runner.report = reports.append
    folder_id = "1fdyS-2RBEBEd7swBLXKYsaZlHwjw2ttG"
    drive.children.return_value = [
        {
            "id": folder_id,
            "name": "The Gray Hall",
            "mimeType": "application/vnd.google-apps.folder",
        }
    ]
    drive.story.side_effect = ValueError("Source must be a nonempty video file")
    runner.discover()
    assert reports == [
        "Story The Gray Hall "
        f"[{folder_id}]: waiting for valid config and two unique videos "
        "(ValueError: Source must be a nonempty video file)"
    ]
    assert runner.state["stories"] == {}

    reports.clear()
    drive.children.return_value = [
        {
            "id": "unnamed-folder",
            "name": "   ",
            "mimeType": "application/vnd.google-apps.folder",
        }
    ]
    runner.discover()
    assert reports == [
        "Story unnamed-folder: waiting for valid config and two unique videos "
        "(ValueError: Source must be a nonempty video file)"
    ]

    reports.clear()
    drive.story.side_effect = None
    drive.story.return_value = (
        Story.model_validate(story_data()),
        {role: source(role) for role in ("long", "short")},
    )
    drive.children.return_value = [
        {
            "id": folder_id,
            "name": "The Gray Hall",
            "mimeType": "application/vnd.google-apps.folder",
        }
    ]
    runner.discover()
    record = runner.state["stories"][folder_id]
    assert record["name"] == "The Gray Hall"
    drive.download.side_effect = ConfigError("Category is not assignable")
    runner.process(folder_id, "short", record)
    assert reports[-1] == f"The Gray Hall [{folder_id}] / short: needs_review"
    assert record["jobs"]["short"]["error"] == "Category is not assignable"

    record["name"] = "  "
    reports.clear()
    drive.download.side_effect = ConfigError("Insufficient cache disk space")
    runner.process(folder_id, "long", record)
    assert reports[-1] == f"{folder_id} / long: needs_review"
    assert record["jobs"]["long"]["error"] == "Insufficient cache disk space"


@pytest.mark.parametrize(
    "message",
    [
        "Drive source changed since snapshot",
        "Category is not assignable",
        "Insufficient cache disk space",
    ],
)
def test_config_error_review_reason_is_preserved(setup, message):
    runner, store, drive, youtube = setup
    assert issubclass(ConfigError, ValueError)
    runner.discover()
    record = runner.state["stories"]["story"]
    drive.download.side_effect = ConfigError(message)
    runner.process("story", "long", record)
    job = store.load()["stories"]["story"]["jobs"]["long"]
    assert job["stage"] == "needs_review"
    assert job["error"] == message
    assert job["error"] != "Source or metadata validation failed"


def test_other_value_error_stays_generic(setup):
    runner, store, drive, youtube = setup
    runner.discover()
    record = runner.state["stories"]["story"]
    drive.download.side_effect = ValueError("secret-token https://upload.example/token")
    runner.process("story", "long", record)
    job = store.load()["stories"]["story"]["jobs"]["long"]
    assert job["stage"] == "needs_review"
    assert job["error"] == "Source or metadata validation failed"
    assert "secret-token" not in job["error"]


def test_pydantic_validation_error_stays_generic(setup):
    runner, store, drive, youtube = setup
    runner.discover()
    record = runner.state["stories"]["story"]
    record["snapshot"]["videos"]["long"]["title"] = "secret-token " + ("x" * 120)
    runner.process("story", "long", record)
    job = record["jobs"]["long"]
    assert job["stage"] == "needs_review"
    assert job["error"] == "Source or metadata validation failed"
    assert "secret-token" not in job["error"]


def test_corrupt_uploaded_state_cannot_become_new_job(setup):
    runner, store, drive, youtube = setup
    runner.discover()
    runner.state["stories"]["story"]["jobs"]["long"]["stage"] = "uploaded"
    store.save(runner.state)
    with pytest.raises(ValueError, match="missing video ID"):
        store.load()


def test_history_cannot_change_channels(setup):
    runner, store, drive, youtube = setup
    with pytest.raises(ValueError, match="different"):
        Runner(Settings(youtube_channel_id="other"), runner.base, store, drive, youtube)


def test_complete_pair_workflow_survives_restart(setup):
    runner, store, drive, youtube = setup
    remote = {}

    def transfer(job, path, save):
        video_id = "video-" + job["source"]["id"]
        job.update(stage="uploaded", video_id=video_id)
        remote[video_id] = {
            "status": {"privacyStatus": "private", "uploadStatus": "processed"}
        }
        save()

    def schedule(video_id, video):
        remote[video_id]["status"]["publishAt"] = video.publish_at.isoformat()

    youtube.transfer.side_effect = transfer
    youtube.inspect.side_effect = lambda video_id: remote[video_id]
    youtube.schedule.side_effect = schedule
    runner.cycle()
    restarted = Runner(
        runner.settings,
        runner.base,
        store,
        drive,
        youtube,
        report=lambda text: None,
        clock=lambda: NOW,
    )
    restarted.cycle()
    assert all(
        j["stage"] == "scheduled"
        for j in store.load()["stories"]["story"]["jobs"].values()
    )
    for item in remote.values():
        item["status"]["privacyStatus"] = "public"
    restarted.cycle()
    assert all(
        j["stage"] == "published"
        for j in store.load()["stories"]["story"]["jobs"].values()
    )
    assert youtube.start.call_count == 2


def test_changed_source_stops_download(tmp_path):
    drive = Drive(Mock())
    drive.metadata = Mock(return_value={**source("long"), "version": "2"})
    with pytest.raises(ValueError, match="changed"):
        drive.download(source("long"), tmp_path / "video")
    drive.http.get.assert_not_called()


def test_upload_log_outcome_matches_saved_upload():
    assert upload_log_outcome({"stage": "uploaded", "video_id": "abc"}) == (
        "uploaded",
        "",
    )
    assert upload_log_outcome({"stage": "scheduled", "video_id": "abc"}) == (
        "uploaded",
        "",
    )
    assert upload_log_outcome(
        {"stage": "failed", "video_id": "abc", "error": "rejected"}
    ) == ("failed", "rejected")
    assert upload_log_outcome(
        {"stage": "needs_review", "video_id": "abc", "error": "schedule removed"}
    ) == ("uploaded", "")
    assert upload_log_outcome(
        {"stage": "needs_review", "session": "https://upload", "error": "expired"}
    ) == ("needs_review", "expired")
    assert (
        upload_log_outcome({"stage": "uploading", "session": "https://upload"}) is None
    )
    assert upload_log_outcome({"stage": "missed"}) is None
    assert upload_log_outcome({"stage": "waiting"}) is None
    assert upload_log_outcome({"stage": "needs_review", "error": "validation"}) is None


def test_corrupt_log_flag_rejected(setup):
    runner, store, drive, youtube = setup
    runner.discover()
    job = runner.state["stories"]["story"]["jobs"]["long"]
    job.update(stage="uploaded", video_id="yt-long", log_recorded="yes")
    store.save(runner.state)
    with pytest.raises(ValueError, match="upload log"):
        store.load()


def test_same_job_twice_writes_one_log_line_and_uploads_once(setup):
    runner, store, drive, youtube = setup
    log = attach_log(runner, drive)
    runner.discover()
    youtube.inspect.return_value = {
        "status": {"privacyStatus": "private", "uploadStatus": "uploaded"},
        "processingDetails": {"processingStatus": "processing"},
    }

    def transfer(job, path, save):
        job.update(stage="uploaded", video_id="yt-long")
        save()

    youtube.transfer.side_effect = transfer
    runner.process("story", "long", runner.state["stories"]["story"])
    restarted = Runner(
        runner.settings,
        runner.base,
        store,
        drive,
        youtube,
        report=lambda text: None,
        clock=lambda: NOW,
    )
    restarted.process("story", "long", restarted.state["stories"]["story"])
    # Crash after Drive stored the line and before local confirmation.
    restarted.state["stories"]["story"]["jobs"]["long"].pop("log_recorded")
    restarted.process("story", "long", restarted.state["stories"]["story"])
    assert youtube.start.call_count == 1
    assert youtube.transfer.call_count == 1
    assert youtube.start.call_args.args[1]["id"] == "long"
    assert log_lines(log) == [log_record()]
    assert store.load()["stories"]["story"]["jobs"]["long"]["log_recorded"] is True


def test_failed_upload_attempt_is_logged_once(setup):
    runner, store, drive, youtube = setup
    log = attach_log(runner, drive)
    runner.discover()

    def transfer(job, path, save):
        raise NeedsReview("Upload session unavailable; reconcile before retrying")

    youtube.transfer.side_effect = transfer
    record = runner.state["stories"]["story"]
    runner.process("story", "long", record)
    runner.process("story", "long", record)
    lines = log_lines(log)
    assert len(lines) == 1
    assert lines[0]["status"] == "needs_review"
    assert lines[0]["video_id"] == ""
    assert lines[0]["error"] == "Upload session unavailable; reconcile before retrying"
    assert lines[0]["channel"] == "channel"
    assert lines[0]["source_file"] == "long.mp4"
    assert youtube.start.call_count == 1
    assert youtube.transfer.call_count == 1


def test_log_failure_does_not_lose_or_repeat_upload(setup):
    runner, store, drive, youtube = setup
    reports = []
    runner.report = reports.append
    log = attach_log(runner, drive)
    real_append = drive.append_upload_log.side_effect
    secret = "https://secret.example/token"

    def flaky(folder, record):
        if flaky.failed:
            flaky.failed = False
            raise requests.ConnectionError(secret)
        return real_append(folder, record)

    flaky.failed = True
    drive.append_upload_log.side_effect = flaky
    runner.discover()
    youtube.inspect.return_value = {
        "status": {"privacyStatus": "private", "uploadStatus": "uploaded"},
        "processingDetails": {"processingStatus": "processing"},
    }

    def transfer(job, path, save):
        job.update(stage="uploaded", video_id="yt-long")
        save()

    youtube.transfer.side_effect = transfer
    record = runner.state["stories"]["story"]
    runner.process("story", "long", record)
    saved = store.load()["stories"]["story"]["jobs"]["long"]
    assert saved["video_id"] == "yt-long"
    assert saved["stage"] == "uploaded"
    assert not saved.get("log_recorded")
    assert saved["log_error"] == "Network failure"
    assert secret not in saved["log_error"]
    assert log["text"] == ""
    assert any("upload log failed" in line for line in reports)
    assert all(secret not in line for line in reports)
    runner.process("story", "long", record)
    assert youtube.start.call_count == 1
    assert youtube.transfer.call_count == 1
    assert log_lines(log) == [log_record()]
    assert store.load()["stories"]["story"]["jobs"]["long"]["log_recorded"] is True
    assert "log_error" not in store.load()["stories"]["story"]["jobs"]["long"]


def test_upload_log_line_escapes_error_text():
    error = 'comma, quote " and\nnewline'
    line = format_upload_log_record(
        log_record(status="needs_review", video_id="", error=error)
    )
    assert "\n" not in line
    assert json.loads(line)["error"] == error


def test_merge_upload_log_keeps_first_outcome_and_unparsed_lines():
    original = merge_upload_log("not-json\n", log_record())
    assert original.startswith("not-json\n")
    assert (
        merge_upload_log(original, log_record(status="failed", error="later")) is None
    )
    added = merge_upload_log(original, log_record(job_id="channel/story/short"))
    assert len([line for line in added.splitlines() if line.strip()]) == 3


def test_drive_upload_log_append_is_idempotent():
    http = Mock()
    store = {"body": None, "id": None}

    def get(url, params=None, **kwargs):
        if params and params.get("alt") == "media":
            return response(200, content=store["body"] or "")
        files = []
        if store["id"]:
            files.append(
                {
                    "id": store["id"],
                    "name": UPLOAD_LOG_NAME,
                    "mimeType": "text/plain",
                }
            )
        return response(200, {"files": files})

    def post(url, params=None, json=None, **kwargs):
        assert json["name"] == UPLOAD_LOG_NAME
        assert json["parents"] == ["folder"]
        store["id"] = "file-1"
        store["body"] = ""
        return response(200, {"id": "file-1"})

    def patch(url, params=None, data=None, **kwargs):
        store["body"] = data.decode("utf-8")
        return response(200, {"id": store["id"]})

    http.get.side_effect = get
    http.post.side_effect = post
    http.patch.side_effect = patch
    drive = Drive(http)
    record = log_record()
    assert drive.append_upload_log("folder", record) is True
    assert drive.append_upload_log("folder", record) is False
    assert (
        drive.append_upload_log("folder", log_record(status="failed", error="later"))
        is False
    )
    lines = [ln for ln in store["body"].splitlines() if ln]
    assert len(lines) == 1
    assert json.loads(lines[0]) == record
    assert http.post.call_count == 1
    assert http.patch.call_count == 1


def test_duplicate_upload_logs_rejected():
    http = Mock()
    http.get.return_value = response(
        200,
        {
            "files": [
                {"id": "a", "name": UPLOAD_LOG_NAME, "mimeType": "text/plain"},
                {"id": "b", "name": UPLOAD_LOG_NAME, "mimeType": "text/plain"},
            ]
        },
    )
    with pytest.raises(ValueError, match="found 2"):
        Drive(http).append_upload_log("folder", log_record())


def _http_error(status, reason):
    error = requests.HTTPError("secret URL must not be logged")
    error.response = Mock(status_code=status, reason=reason)
    return error


@pytest.mark.parametrize(
    ("failure", "reported"),
    [
        (ApiError(403, "fileNotDownloadable"), "HTTP 403: fileNotDownloadable"),
        (requests.ConnectionError("secret URL must not be logged"), "ConnectionError"),
        (_http_error(404, "notFound"), "HTTP 404: notFound"),
    ],
)
def test_story_discovery_error_does_not_block_other_stories(setup, failure, reported):
    runner, store, drive, youtube = setup
    reports = []
    runner.report = reports.append
    drive.children.return_value = [
        {
            "id": "folder-a",
            "name": "Broken",
            "mimeType": "application/vnd.google-apps.folder",
        },
        {
            "id": "folder-b",
            "name": "Ready",
            "mimeType": "application/vnd.google-apps.folder",
        },
    ]

    def story(folder_id):
        if folder_id == "folder-a":
            raise failure
        return (
            Story.model_validate(story_data()),
            {role: source(role) for role in ("long", "short")},
        )

    drive.story.side_effect = story
    runner.cycle()
    assert set(runner.state["stories"]) == {"folder-b"}
    assert set(runner.state["stories"]["folder-b"]["jobs"]) == {"long", "short"}
    assert {call.args[1]["id"] for call in youtube.start.call_args_list} == {
        "long",
        "short",
    }
    text = "\n".join(reports)
    assert f"Story Broken [folder-a]: {reported}" in text
    assert "secret URL" not in text


def test_root_listing_api_error_still_stops_the_cycle(setup):
    runner, store, drive, youtube = setup
    drive.children.side_effect = ApiError(404, "notFound")
    with pytest.raises(ApiError, match="404") as caught:
        runner.cycle()
    assert caught.value.reason == "notFound"
    youtube.start.assert_not_called()
    drive.story.assert_not_called()


def test_incomplete_session_no_progress_does_not_loop(tmp_path):
    path = tmp_path / "video"
    path.write_bytes(b"abc")
    http = Mock()
    http.put.return_value = response(308)
    with pytest.raises(ApiError, match="no_progress"):
        YouTube(http).transfer(
            {"source": source("long"), "session": SESSION}, path, lambda: None
        )
    assert http.put.call_count == 2


def _due_snapshot(runner, hours):
    runner.settings.upload_lead_minutes = 24 * 60
    _set_story_publish_at(runner.drive, NOW + timedelta(hours=hours))
    runner.discover()
    return runner.state["stories"]["story"]


@pytest.mark.parametrize("reason", ["quotaExceeded", "dailyLimitExceeded"])
def test_quota_403_retries_upload_after_reset(setup, reason):
    runner, store, drive, youtube = setup
    record = _due_snapshot(runner, 8)

    def start(video, source):
        if runner.clock() == NOW:
            raise ApiError(403, reason)
        return SESSION

    youtube.start.side_effect = start
    runner.cycle()
    for job in record["jobs"].values():
        assert job["stage"] != "needs_review"
        assert reason in job["error"]
        assert job["attempts"] == 0
        assert datetime.fromisoformat(job["next_retry"]) == datetime(
            2026, 10, 17, 7, tzinfo=UTC
        )
    youtube.start.reset_mock()
    retry_at = datetime.fromisoformat(record["jobs"]["long"]["next_retry"])
    runner.clock = lambda: retry_at
    runner.cycle()
    assert youtube.start.called


@pytest.mark.parametrize(
    "reason", ["rateLimitExceeded", "userRateLimitExceeded", "backendError"]
)
def test_transient_403_retries_download_after_backoff(setup, reason):
    runner, store, drive, youtube = setup
    runner.discover()
    record = runner.state["stories"]["story"]
    original = drive.download.side_effect

    def download(src, path):
        if runner.clock() == NOW:
            raise ApiError(403, reason)
        return original(src, path)

    drive.download.side_effect = download
    runner.cycle()
    job = record["jobs"]["long"]
    assert job["stage"] != "needs_review"
    assert reason in job["error"]
    assert job["attempts"] == 1
    assert job["next_retry"]
    publish_at = Story.model_validate(record["snapshot"]).videos["long"].publish_at
    retry_at = datetime.fromisoformat(job["next_retry"])
    assert NOW < retry_at < publish_at
    drive.download.reset_mock()
    runner.clock = lambda: retry_at
    runner.cycle()
    assert drive.download.called


def test_quota_403_gives_up_after_publish_deadline(setup):
    runner, store, drive, youtube = setup
    runner.discover()
    record = runner.state["stories"]["story"]
    youtube.start.side_effect = ApiError(403, "quotaExceeded")
    runner.cycle()
    job = record["jobs"]["long"]
    assert job["stage"] != "needs_review"
    assert job["next_retry"]
    assert job["attempts"] == 0
    youtube.start.reset_mock()
    retry_at = datetime.fromisoformat(job["next_retry"])
    runner.clock = lambda: retry_at
    runner.cycle()
    assert job["stage"] == "waiting"
    assert runner.state["stories"]["story"]["jobs"]["short"]["stage"] == "waiting"
    youtube.start.assert_not_called()


def test_quota_403_waits_on_unstarted_job_already_past_deadline(setup, monkeypatch):
    runner, store, drive, youtube = setup
    runner.discover()
    record = runner.state["stories"]["story"]

    def explode(sid, role, rec):
        runner.clock = lambda: NOW + timedelta(hours=2)
        raise ApiError(403, "quotaExceeded")

    monkeypatch.setattr(runner, "step", explode)
    runner.process("story", "long", record)
    job = record["jobs"]["long"]
    assert job["stage"] == "waiting"
    assert job["attempts"] == 0
    assert not job.get("next_retry")


def test_quota_on_uploaded_video_waits_past_deadline(setup):
    runner, store, drive, youtube = setup
    runner.discover()
    record = runner.state["stories"]["story"]
    job = record["jobs"]["long"]
    job.update(stage="uploaded", video_id="known", attempts=2)
    youtube.inspect.side_effect = ApiError(403, "quotaExceeded")
    moment = NOW + timedelta(hours=2)
    runner.clock = lambda: moment
    runner.process("story", "long", record)
    assert job["stage"] == "uploaded"
    assert job["attempts"] == 2
    assert datetime.fromisoformat(job["next_retry"]) == datetime(
        2026, 10, 17, 7, tzinfo=UTC
    )
    youtube.inspect.reset_mock()
    runner.clock = lambda: datetime.fromisoformat(job["next_retry"])
    youtube.inspect.side_effect = lambda video_id: {
        "status": {"privacyStatus": "public", "uploadStatus": "processed"}
    }
    runner.cycle()
    assert youtube.inspect.called
    assert job["stage"] == "published"


def test_permission_403_needs_review(setup):
    runner, store, drive, youtube = setup
    runner.discover()
    record = runner.state["stories"]["story"]
    drive.download.side_effect = ApiError(403, "forbidden")
    runner.process("story", "long", record)
    job = record["jobs"]["long"]
    assert job["stage"] == "needs_review"
    assert job["attempts"] == 1
    assert not job.get("next_retry")


def test_429_retry_after_is_a_floor_above_the_cap(setup):
    runner, store, drive, youtube = setup
    runner.discover()
    record = runner.state["stories"]["story"]
    _set_story_publish_at(drive, NOW + timedelta(hours=3))
    original = drive.download.side_effect

    def download(src, path):
        if runner.clock() == NOW:
            raise ApiError(429, "rateLimitExceeded", retry_after=7200)
        return original(src, path)

    drive.download.side_effect = download
    runner.cycle()
    job = record["jobs"]["long"]
    assert job["stage"] != "needs_review"
    assert job["next_retry"]
    retry_at = datetime.fromisoformat(job["next_retry"])
    assert retry_at - NOW >= timedelta(seconds=7200)
    assert retry_at < NOW + timedelta(hours=3)
    drive.download.reset_mock()
    runner.clock = lambda: retry_at
    runner.cycle()
    assert drive.download.called


def test_quota_wait_never_retries_in_under_a_second(setup):
    runner, store, drive, youtube = setup
    record = _due_snapshot(runner, 8)
    reset = datetime(2026, 10, 17, 7, tzinfo=UTC)
    almost = reset - timedelta(milliseconds=200)
    runner.clock = lambda: almost

    def start(video, source):
        if runner.clock() < reset:
            raise ApiError(403, "quotaExceeded", retry_after=0.4)
        return SESSION

    youtube.start.side_effect = start
    runner.cycle()
    job = record["jobs"]["long"]
    assert job["stage"] != "needs_review"
    assert job["attempts"] == 0
    retry_at = datetime.fromisoformat(job["next_retry"])
    assert retry_at - almost >= timedelta(seconds=1)
    assert retry_at - almost >= timedelta(seconds=0.4)
    youtube.start.reset_mock()
    runner.clock = lambda: retry_at
    runner.cycle()
    assert youtube.start.called


def test_scheduled_delay_keeps_retry_after_and_one_second():
    assert scheduled_delay(0.2, 0) >= 1
    assert scheduled_delay(0.2, 0.4) >= 1
    assert scheduled_delay(10, 0.4) >= 10
    assert scheduled_delay(min(3600, 30 * 2**9), 7200) >= 7200
    assert scheduled_delay(100, 2.5) >= 2.5


def test_poll_retry_classifies_quota_and_rate_limits():
    action, failures, wait = poll_retry(ApiError(403, "quotaExceeded"), 9, False, NOW)
    assert action == "quota"
    assert failures == 9
    assert wait == timedelta(hours=7).total_seconds()
    assert poll_retry(ApiError(403, "dailyLimitExceeded"), 0, True, NOW)[0] == "stop"
    action, failures, _wait = poll_retry(
        ApiError(403, "rateLimitExceeded"), 0, False, NOW
    )
    assert action == "transient"
    assert failures == 1
    assert poll_retry(ApiError(403, "userRateLimitExceeded"), 0, False, NOW)[0] == (
        "transient"
    )
    assert poll_retry(ApiError(403, "backendError"), 0, False, NOW)[0] == "transient"
    assert poll_retry(ApiError(429, "rateLimitExceeded"), 0, False, NOW)[0] == (
        "transient"
    )
    assert poll_retry(ApiError(403, "forbidden"), 0, False, NOW)[0] == "stop"
    assert poll_retry(requests.ConnectionError("down"), 0, False, NOW)[0] == (
        "transient"
    )
    assert poll_retry(ApiError(503, "backendError"), 9, False, NOW)[0] == "stop"
    action, failures, wait = poll_retry(
        ApiError(429, "rateLimitExceeded", retry_after=7200), 8, False, NOW
    )
    assert action == "transient"
    assert failures == 9
    assert wait >= 7200
    almost = datetime(2026, 10, 17, 7, tzinfo=UTC) - timedelta(milliseconds=200)
    action, failures, wait = poll_retry(
        ApiError(403, "quotaExceeded", retry_after=0.4), 4, False, almost
    )
    assert action == "quota"
    assert failures == 4
    assert wait >= 1


def test_importing_runner_does_not_load_tz_database(monkeypatch):
    import importlib
    import zoneinfo

    import youtube_auto_uploader.runner as runner

    calls = []
    original = zoneinfo.ZoneInfo

    def explode(key):
        calls.append(key)
        raise zoneinfo.ZoneInfoNotFoundError(key)

    monkeypatch.setattr(zoneinfo, "ZoneInfo", explode)
    try:
        importlib.reload(runner)
        assert calls == []
    finally:
        monkeypatch.setattr(zoneinfo, "ZoneInfo", original)
        importlib.reload(runner)


def test_quota_reset_fallback_around_pacific_dst(monkeypatch):
    import zoneinfo

    import youtube_auto_uploader.runner as runner
    import youtube_auto_uploader.tz as pacific

    def explode(key):
        raise zoneinfo.ZoneInfoNotFoundError(key)

    monkeypatch.setattr(pacific, "ZoneInfo", explode)
    cases = {
        datetime(2026, 1, 15, 12, tzinfo=UTC): datetime(2026, 1, 16, 8, tzinfo=UTC),
        datetime(2026, 7, 15, 12, tzinfo=UTC): datetime(2026, 7, 16, 7, tzinfo=UTC),
        datetime(2026, 3, 8, 9, 30, tzinfo=UTC): datetime(2026, 3, 9, 7, tzinfo=UTC),
        datetime(2026, 3, 8, 10, 30, tzinfo=UTC): datetime(2026, 3, 9, 7, tzinfo=UTC),
        datetime(2026, 11, 1, 7, 30, tzinfo=UTC): datetime(2026, 11, 2, 8, tzinfo=UTC),
        datetime(2026, 11, 1, 10, 30, tzinfo=UTC): datetime(2026, 11, 2, 8, tzinfo=UTC),
    }
    for moment, expected in cases.items():
        assert runner.next_quota_reset(moment) == expected


def test_pacific_fallback_matches_zoneinfo():
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    from youtube_auto_uploader.tz import _next_pacific_midnight

    try:
        zone = ZoneInfo("America/Los_Angeles")
    except ZoneInfoNotFoundError:
        pytest.skip("IANA time zone database unavailable")
    moments = [
        datetime(2026, 1, 15, 12, tzinfo=UTC),
        datetime(2026, 1, 16, 8, tzinfo=UTC),
        datetime(2026, 7, 15, 12, tzinfo=UTC),
        datetime(2026, 3, 8, 9, 30, tzinfo=UTC),
        datetime(2026, 3, 8, 10, tzinfo=UTC),
        datetime(2026, 3, 8, 10, 30, tzinfo=UTC),
        datetime(2026, 11, 1, 7, 30, tzinfo=UTC),
        datetime(2026, 11, 1, 8, 30, tzinfo=UTC),
        datetime(2026, 11, 1, 9, tzinfo=UTC),
        datetime(2026, 11, 1, 9, 30, tzinfo=UTC),
        datetime(2024, 3, 10, 9, 59, tzinfo=UTC),
        datetime(2024, 11, 3, 8, 59, tzinfo=UTC),
        datetime(2020, 3, 8, 9, 30, tzinfo=UTC),
        datetime(2030, 11, 3, 8, 30, tzinfo=UTC),
    ]
    for moment in moments:
        local = moment.astimezone(zone)
        reset = local.replace(hour=0, minute=0, second=0, microsecond=0)
        if reset <= local:
            reset += timedelta(days=1)
        assert _next_pacific_midnight(moment) == reset.astimezone(UTC)
