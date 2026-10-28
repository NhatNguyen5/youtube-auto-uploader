import copy
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import portalocker
import pytest
import requests

from youtube_auto_uploader.drive import ApiError, Drive
from youtube_auto_uploader.models import Settings, Story
from youtube_auto_uploader.runner import Runner, poll_retry, scheduled_delay
from youtube_auto_uploader.storage import Store, atomic_json, parse_json
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


def response(status, data=None, headers=None):
    result = Mock(status_code=status, headers=headers or {})
    result.json.return_value = data or {}
    return result


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
    runner.discover()
    record = runner.state["stories"]["story"]
    later = (NOW + timedelta(hours=hours)).isoformat()
    for video in record["snapshot"]["videos"].values():
        video["publish_at"] = later
    return record


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
    runner.clock = lambda: datetime.fromisoformat(record["jobs"]["long"]["next_retry"])
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
    runner.clock = lambda: datetime.fromisoformat(job["next_retry"])
    runner.cycle()
    assert job["stage"] == "missed"
    youtube.start.assert_not_called()


def test_quota_403_misses_unstarted_job_already_past_deadline(setup, monkeypatch):
    runner, store, drive, youtube = setup
    runner.discover()
    record = runner.state["stories"]["story"]

    def explode(sid, role, rec):
        runner.clock = lambda: NOW + timedelta(hours=2)
        raise ApiError(403, "quotaExceeded")

    monkeypatch.setattr(runner, "step", explode)
    runner.process("story", "long", record)
    job = record["jobs"]["long"]
    assert job["stage"] == "missed"
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
    later = (NOW + timedelta(hours=3)).isoformat()
    for video in record["snapshot"]["videos"].values():
        video["publish_at"] = later
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
