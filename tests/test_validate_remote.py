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
    from youtube_auto_uploader.storage import parse_json

    drive = Mock()
    drive.children.return_value = [
        {"id": "h1", "name": "Halloween", "mimeType": FOLDER},
        {"id": "r1", "name": "Radio", "mimeType": FOLDER},
        {"id": "bad", "name": "Broken", "mimeType": FOLDER},
    ]

    def story(folder_id):
        if folder_id == "bad":
            parse_json("{")
        raise ConfigError("Expected one config.json; found 0")

    drive.story.side_effect = story
    assert report_remote_stories(drive, "root") == 3
    text = capsys.readouterr().out
    assert "Invalid story: h1 (Halloween): Expected one config.json; found 0" in text
    assert "Invalid story: r1 (Radio): Expected one config.json; found 0" in text
    assert "Invalid story: bad (Broken): Invalid JSON at line 1 column" in text
    assert "{" not in text


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


def test_api_error_text_is_shown():
    from youtube_auto_uploader.status import public_error

    text = public_error(ApiError(403, "forbidden"))
    assert text == "Google API HTTP 403: forbidden"


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
