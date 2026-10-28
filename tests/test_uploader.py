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
from youtube_auto_uploader.runner import Runner
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
    assert f"Story folder-a: {reported}" in text
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
