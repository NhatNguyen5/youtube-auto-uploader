import json
import os

from dotenv import load_dotenv
from google.auth.transport.requests import AuthorizedSession
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from .storage import atomic_json

SCOPES = [
    "https://www.googleapis.com/auth/drive.readonly",
    "https://www.googleapis.com/auth/youtube.force-ssl",
]


def token_path(base):
    load_dotenv(base / ".env", override=False)
    return base / os.getenv("GOOGLE_TOKEN_PATH", "secrets/token.json")


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
    path = token_path(base)
    if not path.exists():
        raise ValueError("OAuth token missing; run login first")
    credentials = Credentials.from_authorized_user_file(str(path), SCOPES)
    return AuthorizedSession(credentials), lambda: atomic_json(
        path, json.loads(credentials.to_json())
    )
