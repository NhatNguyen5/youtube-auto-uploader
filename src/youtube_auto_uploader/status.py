"""Messages safe to show in the CLI and the desktop window."""

import sys

from pydantic import ValidationError


class ConfigError(ValueError):
    """A message this project wrote. Safe to show; no tokens, secrets, or request URLs."""


def configure_stdio():
    """Print UTF-8 when stdout or stderr can be reconfigured.

    A redirected console, such as Task Scheduler writing a log, uses the
    Windows locale code page. A folder name like Truyện then raises
    UnicodeEncodeError. ``errors="replace"`` keeps a later save from being
    skipped. Streams without ``reconfigure`` (including ``None``) are left
    alone.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            continue


def story_label(name, story_id):
    """Folder name first, with the Drive id in brackets for debugging.

    Control and format characters are replaced, so a name cannot insert
    another status row. A missing or blank name falls back to the id alone.
    """
    raw = name.strip() if isinstance(name, str) else ""
    title = "".join(character if character.isprintable() else " " for character in raw)
    title = title.strip()
    if not title or title == story_id:
        return story_id
    return f"{title} [{story_id}]"


def format_status_line(story_id, role, job, name=None):
    """One status line for the CLI and the desktop window."""
    url = (
        f"https://www.youtube.com/watch?v={job['video_id']}"
        if job.get("video_id")
        else ""
    )
    detail = job.get("error", "")
    if job.get("log_error"):
        detail = f"{detail} upload-log: {job['log_error']}".strip()
    label = story_label(name, story_id)
    return f"{label} / {role}: {job['stage']} {url} {detail}"


def story_reason(error):
    """Short story-config failure. Field location and message only, no input values."""
    if isinstance(error, ValidationError):
        parts = []
        for item in error.errors():
            loc = ".".join(str(part) for part in item.get("loc") or ())
            msg = item.get("msg") or "invalid"
            parts.append(f"{loc}: {msg}" if loc else msg)
        return "; ".join(parts) or "invalid"
    return str(error)


def public_error(error):
    """Text for an exception.

    ConfigError is project-written and shown as-is. Pydantic errors show the
    field and message only, never the rejected input. ApiError is only an HTTP
    status and reason. Every other exception, including library ValueErrors,
    stays generic because the text can contain a token or request URL.
    """
    from .drive import ApiError

    if isinstance(error, ValidationError):
        return story_reason(error)
    if isinstance(error, (ConfigError, ApiError)):
        return str(error)
    message = (
        f"Error ({type(error).__name__}). "
        "Check configuration, credentials, and saved status; no history was reset."
    )
    if isinstance(error, FileNotFoundError):
        message += (
            " Copy examples/config.example.json to config.json and set your channel ID."
        )
    return message
