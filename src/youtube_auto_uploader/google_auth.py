import json
import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .storage import atomic_json

# Google rejects Drive scopes and YouTube scopes in one consent request.
# Login runs the two flows separately and sends include_granted_scopes=false.
DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
]
YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
SCOPES = DRIVE_SCOPES + YOUTUBE_SCOPES

# (role, consent title, scopes, which Google account to use)
LOGIN_STEPS = (
    (
        "drive",
        "Google Drive",
        DRIVE_SCOPES,
        "the Google account that owns the Drive folders",
    ),
    (
        "youtube",
        "YouTube",
        YOUTUBE_SCOPES,
        "the Google account that owns the YouTube channel",
    ),
)


def _load_env(base):
    load_dotenv(base / ".env", override=False)


def drive_token_path(base):
    _load_env(base)
    return base / os.getenv("GOOGLE_TOKEN_PATH", "secrets/token.json")


def token_path(base):
    """Drive token. Kept so GOOGLE_TOKEN_PATH still selects that file."""
    return drive_token_path(base)


def youtube_token_path(base):
    _load_env(base)
    override = os.getenv("GOOGLE_YOUTUBE_TOKEN_PATH")
    if override:
        path = Path(override)
        return path if path.is_absolute() else base / path
    drive = drive_token_path(base)
    return drive.with_name(f"{drive.stem}.youtube{drive.suffix}")


def _path_for(base, role):
    if role == "drive":
        return drive_token_path(base)
    if role == "youtube":
        return youtube_token_path(base)
    raise ValueError(f"Unknown OAuth token role: {role}")


def _read_info(path):
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(info, dict):
        return None
    return info


def _scope_list(info):
    scopes = info.get("scopes") or []
    if isinstance(scopes, str):
        return scopes.split()
    return list(scopes)


def _granted(info):
    return set(_scope_list(info))


def _other_scopes(role):
    if role == "drive":
        return set(YOUTUBE_SCOPES)
    return set(DRIVE_SCOPES)


def missing_scopes(base):
    """Scopes that are not on a usable token for their own consent flow.

    A token saved by the old single login lists Drive and YouTube scopes
    together. That file is not usable for either flow.
    """
    missing = []
    for role, _title, scopes, _account in LOGIN_STEPS:
        path = _path_for(base, role)
        info = _read_info(path) if path.exists() else None
        granted = set() if info is None else _granted(info)
        if info is None or granted & _other_scopes(role):
            missing.extend(scopes)
            continue
        missing.extend(scope for scope in scopes if scope not in granted)
    return missing


def login_message(base):
    missing = missing_scopes(base)
    if not missing:
        return None
    return (
        "Saved OAuth tokens are missing or out of date. Run login again to grant: "
        + ", ".join(missing)
    )


def _client_config(values):
    return {
        "installed": {
            "client_id": values["GOOGLE_CLIENT_ID"],
            "client_secret": values["GOOGLE_CLIENT_SECRET"],
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


def _scope_set(value):
    if value is None:
        return None
    if isinstance(value, str):
        value = value.split()
    value = list(value)
    # google-auth uses an empty grant when it matches the request.
    if not value:
        return None
    return set(value)


def _missing_grant(credentials, requested):
    granted = _scope_set(getattr(credentials, "granted_scopes", None))
    if granted is None:
        return []
    return [scope for scope in requested if scope not in granted]


def _permission_name(scope):
    names = {
        "https://www.googleapis.com/auth/drive.readonly": "Drive read-only",
        "https://www.googleapis.com/auth/drive.file": "Drive file",
        "https://www.googleapis.com/auth/youtube.force-ssl": "YouTube",
        "https://www.googleapis.com/auth/yt-analytics.readonly": "YouTube Analytics",
    }
    return names.get(scope, scope)


def login(base):
    _load_env(base)
    values = {
        key: os.getenv(key) for key in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET")
    }
    missing = [key for key, value in values.items() if not value]
    if missing:
        raise ValueError("Fill in .env: " + ", ".join(missing))
    drive_path = drive_token_path(base).resolve()
    youtube_path = youtube_token_path(base).resolve()
    if drive_path == youtube_path:
        raise ValueError(
            "GOOGLE_YOUTUBE_TOKEN_PATH is the same file as GOOGLE_TOKEN_PATH. "
            "Choose a different file for the YouTube token."
        )
    config = _client_config(values)
    for index, (role, title, scopes, account) in enumerate(LOGIN_STEPS, start=1):
        print(f"Step {index} of 2: {title}. Sign in with {account}.")
        flow = InstalledAppFlow.from_client_config(config, list(scopes))
        # false, not omitted and not true: Google must not merge an older grant
        # into this request, which is what makes the mixed-scope consent fail.
        credentials = flow.run_local_server(
            port=0,
            access_type="offline",
            prompt="consent",
            include_granted_scopes="false",
        )
        refused = _missing_grant(credentials, scopes)
        if refused:
            names = ", ".join(
                f"{_permission_name(scope)} ({scope})" for scope in refused
            )
            message = (
                f"Step {index} of 2: {title} did not grant {names}. "
                "Tick every box on the consent screen and run login again."
            )
            print(message)
            raise ValueError(message)
        atomic_json(_path_for(base, role), json.loads(credentials.to_json()))


def _credentials(info, fallback):
    scopes = _scope_list(info) or list(fallback)
    try:
        return Credentials.from_authorized_user_info(info, scopes)
    except ValueError as error:
        raise ValueError(
            "Saved OAuth tokens are missing or out of date. Run login again."
        ) from error


def uses_youtube_token(url):
    parsed = urlparse(url)
    host = parsed.hostname or ""
    path = parsed.path or ""
    if host in {
        "youtubeanalytics.googleapis.com",
        "youtubereporting.googleapis.com",
    }:
        return True
    if host == "www.googleapis.com" and (
        path.startswith("/youtube/") or path.startswith("/upload/youtube/")
    ):
        return True
    if host == "www.googleapis.com" and (
        path.startswith("/drive/") or path.startswith("/upload/drive/")
    ):
        return False
    raise ValueError(
        "No OAuth token matches this Google API URL. "
        "Drive calls use the Drive token and YouTube calls use the YouTube token."
    )


class RoutedSession:
    """Send Drive calls with the Drive token and YouTube calls with the YouTube token."""

    def __init__(self, drive, youtube):
        self.drive = drive
        self.youtube = youtube

    def _client(self, url):
        return self.youtube if uses_youtube_token(url) else self.drive

    def get(self, url, **kwargs):
        return self._client(url).get(url, **kwargs)

    def post(self, url, **kwargs):
        return self._client(url).post(url, **kwargs)

    def put(self, url, **kwargs):
        return self._client(url).put(url, **kwargs)

    def patch(self, url, **kwargs):
        return self._client(url).patch(url, **kwargs)

    def close(self):
        self.drive.close()
        self.youtube.close()


def session(base):
    message = login_message(base)
    if message:
        raise ValueError(message)
    opened = []
    for role, _title, scopes, _account in LOGIN_STEPS:
        path = _path_for(base, role)
        info = _read_info(path)
        credentials = _credentials(info, scopes)
        http = AuthorizedSession(credentials)
        opened.append((path, credentials, http))

    def persist():
        for path, credentials, _http in opened:
            atomic_json(path, json.loads(credentials.to_json()))

    return RoutedSession(opened[0][2], opened[1][2]), persist
