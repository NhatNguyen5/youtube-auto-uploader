import json
import os

from dotenv import load_dotenv
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .storage import atomic_json

# login requests every scope the commands need. drive.readonly reads the story
# inbox and finds an existing stats file. drive.file creates and overwrites that
# stats file only. youtube.force-ssl uploads and reads the channel.
# yt-analytics.readonly covers YouTube Analytics reports.query.
SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/drive.file",
    "https://www.googleapis.com/auth/youtube.force-ssl",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]


def token_path(base):
    load_dotenv(base / ".env", override=False)
    return base / os.getenv("GOOGLE_TOKEN_PATH", "secrets/token.json")


def read_token(base):
    path = token_path(base)
    if not path.exists():
        raise ValueError("OAuth token missing; run login first")
    return path, json.loads(path.read_text(encoding="utf-8"))


def missing_scopes(base):
    _, info = read_token(base)
    granted = set(info.get("scopes") or [])
    return [scope for scope in SCOPES if scope not in granted]


def login(base):
    path = token_path(base)
    values = {
        key: os.getenv(key) for key in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET")
    }
    missing = [key for key, value in values.items() if not value]
    if missing:
        raise ValueError("Fill in .env: " + ", ".join(missing))
    flow = InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": values["GOOGLE_CLIENT_ID"],
                "client_secret": values["GOOGLE_CLIENT_SECRET"],
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": ["http://localhost"],
            }
        },
        SCOPES,
    )
    credentials = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    atomic_json(path, json.loads(credentials.to_json()))


def session(base):
    path, info = read_token(base)
    # Refresh with the scopes already granted. Passing the expanded SCOPES list
    # would break refresh for a token saved before export-stats existed.
    credentials = Credentials.from_authorized_user_info(
        info, info.get("scopes") or list(SCOPES)
    )
    return AuthorizedSession(credentials), lambda: atomic_json(
        path, json.loads(credentials.to_json())
    )
