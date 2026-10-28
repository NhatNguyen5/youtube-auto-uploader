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
    Drive,
    format_upload_log_record,
    merge_upload_log,
)
from youtube_auto_uploader.google_auth import SCOPES, scopes_for_refresh
from youtube_auto_uploader.models import Settings, Story
from youtube_auto_uploader.runner import Runner
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
    "change", ["naive", "same_file", "unknown", "missing_short", "string_bool"]
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
    else:
        data["enabled"] = "true"
    with pytest.raises(ValueError):
        Story.model_validate(data)


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


def test_discovery_freezes_both_variants_and_renames_without_duplicates(setup):
    runner, store, drive, youtube = setup
    runner.discover()
    old = copy.deepcopy(runner.state["stories"]["story"]["snapshot"])
    drive.children.return_value[0]["name"] = "Renamed"
    drive.story.return_value[0].videos["short"].title = "Changed"
    runner.discover()
    assert len(runner.state["stories"]) == 1
    assert runner.state["stories"]["story"]["snapshot"] == old
    assert drive.story.call_count == 1


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
    assert runner.state["stories"]["story"]["jobs"]["long"]["stage"] == "missed"


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


def test_old_token_refresh_does_not_request_new_drive_scope():
    old = [
        "https://www.googleapis.com/auth/drive.readonly",
        "https://www.googleapis.com/auth/youtube.force-ssl",
    ]
    refreshed = scopes_for_refresh(old)
    assert "https://www.googleapis.com/auth/drive.file" not in refreshed
    assert set(refreshed) == set(old)
    assert set(scopes_for_refresh(SCOPES)) == set(SCOPES)


def test_incomplete_session_no_progress_does_not_loop(tmp_path):
    from youtube_auto_uploader.drive import ApiError

    path = tmp_path / "video"
    path.write_bytes(b"abc")
    http = Mock()
    http.put.return_value = response(308)
    with pytest.raises(ApiError, match="no_progress"):
        YouTube(http).transfer(
            {"source": source("long"), "session": SESSION}, path, lambda: None
        )
    assert http.put.call_count == 2
