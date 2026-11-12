from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from youtube_auto_uploader.cli import report_remote_stories
from youtube_auto_uploader.desktop import check_result_text
from youtube_auto_uploader.drive import ApiError
from youtube_auto_uploader.models import Story
from youtube_auto_uploader.status import ConfigError, story_reason

FOLDER = "application/vnd.google-apps.folder"


def test_story_reason_keeps_field_and_drops_input():
    try:
        Story.model_validate(
            {
                "schema_version": 1,
                "enabled": "SECRETTOKEN",
                "videos": {},
            }
        )
    except ValidationError as error:
        raw = str(error)
        reason = story_reason(error)
    else:
        raise AssertionError("expected a validation error")
    assert "SECRETTOKEN" not in reason
    assert "input_value" not in reason
    assert "enabled:" in reason
    assert raw != reason
    assert "SECRETTOKEN" in raw


def test_remote_validate_reports_valid_and_invalid_stories(capsys):
    drive = Mock()
    drive.children.return_value = [
        {"id": "good", "name": "Good", "mimeType": FOLDER},
        {"id": "missing", "name": "Missing Config", "mimeType": FOLDER},
        {"id": "notes", "name": "notes.txt", "mimeType": "text/plain"},
        {"id": "badjson", "name": "Bad Config", "mimeType": FOLDER},
    ]

    def story(folder_id):
        if folder_id == "missing":
            raise ConfigError("Expected one config.json; found 0")
        if folder_id == "badjson":
            Story.model_validate(
                {"schema_version": 1, "enabled": "SECRETTOKEN", "videos": {}}
            )
        return None

    drive.story.side_effect = story
    invalid = report_remote_stories(drive, "root")
    assert invalid == 2
    text = capsys.readouterr().out
    assert "Valid story: good" in text
    assert (
        "Invalid story: missing (Missing Config): Expected one config.json; found 0"
        in text
    )
    assert "Invalid story: badjson (Bad Config):" in text
    assert "enabled:" in text
    assert "SECRETTOKEN" not in text
    assert "notes.txt" not in text
    shown = check_result_text("validate_remote", text.splitlines(keepends=True), 1)
    assert shown.startswith("Validate remote: failed (exit 1)\n")
    assert "Invalid story: missing (Missing Config):" in shown
    assert "Valid story: good" in shown


def test_bad_json_is_reported_beside_halloween_and_radio(capsys):
    from pathlib import Path

    from youtube_auto_uploader.drive import Drive

    story = Path("examples/story-config.example.json").read_bytes()
    secret_doc = b"{not-json SECRETDOC"

    def files_for(folder_id):
        return [
            {
                "id": f"{folder_id}-config",
                "name": "config.json",
                "mimeType": "application/json",
            },
            {
                "id": f"{folder_id}-long",
                "name": "long.mp4",
                "mimeType": "video/mp4",
                "size": "3",
                "capabilities": {"canDownload": True},
            },
            {
                "id": f"{folder_id}-short",
                "name": "short.mp4",
                "mimeType": "video/mp4",
                "size": "3",
                "capabilities": {"canDownload": True},
            },
        ]

    media = {
        "h1-config": story,
        "r1-config": story,
        "bad-config": secret_doc,
    }

    def get(url, params=None, **kwargs):
        params = params or {}
        result = Mock(status_code=200, headers={})
        if params.get("alt") == "media":
            body = media[url.rstrip("/").rsplit("/", 1)[-1]]
            result.iter_content = lambda _size: [body]
            result.content = body
            result.__enter__ = Mock(return_value=result)
            result.__exit__ = Mock(return_value=False)
            return result
        query = params.get("q", "")
        if "'root'" in query:
            files = [
                {"id": "h1", "name": "Halloween", "mimeType": FOLDER},
                {"id": "r1", "name": "Radio", "mimeType": FOLDER},
                {"id": "bad", "name": "Broken", "mimeType": FOLDER},
            ]
        else:
            folder_id = next(
                item for item in ("h1", "r1", "bad") if f"'{item}'" in query
            )
            files = files_for(folder_id)
        result.json.return_value = {"files": files}
        return result

    http = Mock()
    http.get.side_effect = get
    assert report_remote_stories(Drive(http), "root") == 1
    text = capsys.readouterr().out
    assert "Valid story: h1" in text
    assert "Valid story: r1" in text
    assert "Invalid story: bad (Broken): Invalid JSON at line 1 column" in text
    assert "SECRETDOC" not in text


def test_remote_validate_does_not_swallow_other_errors(capsys):
    drive = Mock()
    drive.children.return_value = [
        {"id": "leaky", "name": "Leaky", "mimeType": FOLDER},
    ]

    def story(_folder_id):
        raise ValueError("https://www.googleapis.com/drive?access_token=SECRET")

    drive.story.side_effect = story
    with pytest.raises(ValueError, match="SECRET"):
        report_remote_stories(drive, "root")
    assert "Invalid story" not in capsys.readouterr().out


def test_bad_week_message_is_shown():
    from datetime import UTC, datetime

    from youtube_auto_uploader.stats import canonical_week
    from youtube_auto_uploader.status import public_error

    with pytest.raises(ConfigError) as caught:
        canonical_week("bad", datetime(2026, 10, 17, tzinfo=UTC))
    assert public_error(caught.value) == "Week must look like 2026-W44"


def test_api_error_text_omits_request_url():
    from youtube_auto_uploader.status import public_error

    error = ApiError(403, "forbidden")
    error.url = "https://www.googleapis.com/youtube/v3/videos?access_token=SECRET"
    text = public_error(error)
    assert text == "Google API HTTP 403: forbidden"
    assert "SECRET" not in text
    assert "access_token" not in text


def test_remote_validate_still_raises_api_errors():
    drive = Mock()
    drive.children.return_value = [
        {"id": "good", "name": "Good", "mimeType": FOLDER},
        {"id": "down", "name": "Down", "mimeType": FOLDER},
    ]

    def story(folder_id):
        if folder_id == "down":
            raise ApiError(503, "backendError")
        return None

    drive.story.side_effect = story
    try:
        report_remote_stories(drive, "root")
    except ApiError as error:
        assert error.status == 503
    else:
        raise AssertionError("API errors must stop validation")
