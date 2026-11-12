import json
import sys
from unittest.mock import Mock

import pytest

from youtube_auto_uploader import google_auth
from youtube_auto_uploader.cli import main
from youtube_auto_uploader.drive import Drive
from youtube_auto_uploader.google_auth import (
    DRIVE_SCOPES,
    YOUTUBE_SCOPES,
    RoutedSession,
    uses_youtube_token,
)
from youtube_auto_uploader.storage import Store
from youtube_auto_uploader.youtube import YouTube


def _token(scopes):
    return {
        "token": "access",
        "refresh_token": "refresh",
        "client_id": "id",
        "client_secret": "secret",
        "scopes": list(scopes),
    }


def _write_split_tokens(tmp_path):
    (tmp_path / "token.json").write_text(
        json.dumps(_token(DRIVE_SCOPES)), encoding="utf-8"
    )
    (tmp_path / "token.youtube.json").write_text(
        json.dumps(_token(YOUTUBE_SCOPES)), encoding="utf-8"
    )


def test_login_requests_two_separate_scope_sets(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("GOOGLE_TOKEN_PATH", "secrets/token.json")
    requested = []

    class FakeFlow:
        def __init__(self, scopes):
            self.scopes = list(scopes)

        def run_local_server(self, **kwargs):
            requested.append((list(self.scopes), kwargs))
            credentials = Mock()
            credentials.to_json.return_value = json.dumps(_token(self.scopes))
            return credentials

    def from_client_config(_config, scopes):
        return FakeFlow(scopes)

    monkeypatch.setattr(
        google_auth.InstalledAppFlow,
        "from_client_config",
        from_client_config,
    )
    google_auth.login(tmp_path)
    assert len(requested) == 2
    drive_scopes, drive_kwargs = requested[0]
    youtube_scopes, youtube_kwargs = requested[1]
    assert drive_scopes == DRIVE_SCOPES
    assert youtube_scopes == YOUTUBE_SCOPES
    assert not (set(drive_scopes) & set(youtube_scopes))
    for kwargs in (drive_kwargs, youtube_kwargs):
        assert kwargs["include_granted_scopes"] == "false"
        assert kwargs["include_granted_scopes"] is not True
    text = capsys.readouterr().out
    assert "Step 1 of 2: Google Drive" in text
    assert "Step 2 of 2: YouTube" in text
    assert "owns the Drive folders" in text
    assert "owns the YouTube channel" in text
    drive = json.loads((tmp_path / "secrets" / "token.json").read_text())
    youtube = json.loads((tmp_path / "secrets" / "token.youtube.json").read_text())
    assert drive["scopes"] == DRIVE_SCOPES
    assert youtube["scopes"] == YOUTUBE_SCOPES


def test_each_api_uses_its_own_token():
    calls = []

    def client(name):
        http = Mock()

        def send(url, **kwargs):
            calls.append((name, url))
            result = Mock(status_code=200, headers={})
            result.content = b""
            if uses_youtube_token(url) and "channels" in url:
                result.json.return_value = {"items": [{"id": "channel"}]}
            else:
                result.json.return_value = {"files": []}
            return result

        http.get.side_effect = send
        http.post.side_effect = send
        http.put.side_effect = send
        http.patch.side_effect = send
        return http

    routed = RoutedSession(client("drive"), client("youtube"))
    assert Drive(routed).children("folderid") == []
    YouTube(routed).verify_channel("channel")
    urls = [
        "https://www.googleapis.com/upload/drive/v3/files",
        "https://www.googleapis.com/upload/youtube/v3/videos",
        "https://youtubeanalytics.googleapis.com/v2/reports",
        "https://youtubereporting.googleapis.com/v1/jobs",
        "https://www.googleapis.com/download/v1/media",
    ]
    for url in urls:
        routed.get(url)
    for name, url in calls:
        assert name == ("youtube" if uses_youtube_token(url) else "drive")
    assert ("drive", "https://www.googleapis.com/drive/v3/files") in [
        (name, url.split("?", 1)[0]) for name, url in calls
    ]
    assert any(url.endswith("/channels") and name == "youtube" for name, url in calls)


def test_old_or_missing_tokens_ask_for_login(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("GOOGLE_TOKEN_PATH", "secrets/token.json")
    message = google_auth.login_message(tmp_path)
    assert "Run login again" in message
    with pytest.raises(ValueError, match="Run login again"):
        google_auth.session(tmp_path)

    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "token.json").write_text(
        json.dumps(_token(google_auth.SCOPES)), encoding="utf-8"
    )
    message = google_auth.login_message(tmp_path)
    assert "Run login again" in message
    assert "drive.file" in message
    assert "yt-analytics.readonly" in message
    with pytest.raises(ValueError, match="Run login again"):
        google_auth.session(tmp_path)

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"youtube_channel_id": "channel"}), encoding="utf-8")
    Store(tmp_path / "data" / "state.json").initialize()
    monkeypatch.setattr(
        sys, "argv", ["youtube-auto-uploader", "--config", str(config), "run", "--once"]
    )
    assert main() == 1
    assert "Run login again" in capsys.readouterr().out


def test_refresh_uses_each_tokens_granted_scopes(tmp_path, monkeypatch):
    monkeypatch.setenv("GOOGLE_TOKEN_PATH", "token.json")
    _write_split_tokens(tmp_path)
    seen = []

    def from_info(info, scopes=None):
        seen.append(list(scopes))
        credentials = Mock()
        credentials.to_json.return_value = json.dumps(info)
        return credentials

    monkeypatch.setattr(google_auth.Credentials, "from_authorized_user_info", from_info)
    monkeypatch.setattr(google_auth, "AuthorizedSession", lambda credentials: Mock())
    _routed, persist = google_auth.session(tmp_path)
    assert seen == [DRIVE_SCOPES, YOUTUBE_SCOPES]
    persist()
    assert json.loads((tmp_path / "token.json").read_text())["scopes"] == DRIVE_SCOPES
    assert (
        json.loads((tmp_path / "token.youtube.json").read_text())["scopes"]
        == YOUTUBE_SCOPES
    )
