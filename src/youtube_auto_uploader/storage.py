import json
import os
import tempfile
import time
import uuid
from pathlib import Path

import portalocker

from .status import ConfigError

# Windows can refuse os.replace while another process still has the file open.
_REPLACE_PAUSES = (0.05, 0.1, 0.2)


def parse_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ConfigError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    try:
        return json.loads(text, object_pairs_hook=pairs)
    except json.JSONDecodeError as error:
        raise ConfigError(
            f"Invalid JSON at line {error.lineno} column {error.colno}: {error.msg}"
        ) from error


def replace_file(source, destination):
    """Replace destination with source.

    On Windows, os.replace raises PermissionError while another process still
    has the destination open. A few short waits usually clear that. The same
    retry runs for that error on every platform. Any other error is raised
    immediately, and the destination is left as it was.
    """
    last = None
    for pause in (*_REPLACE_PAUSES, None):
        try:
            os.replace(source, destination)
            return
        except PermissionError as error:
            last = error
            if pause is None:
                break
            time.sleep(pause)
    raise ConfigError(
        "Could not replace the file because it is still open. "
        "Nothing was left half-written. Close the other program and try again."
    ) from last


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".write-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        replace_file(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class Store:
    def __init__(self, path):
        self.path = Path(path)

    def lock(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return portalocker.Lock(str(self.path) + ".lock", timeout=0)

    def initialize(self):
        if self.path.exists():
            raise ConfigError("State already exists; refusing to overwrite history")
        atomic_json(
            self.path,
            {"schema_version": 1, "installation_id": str(uuid.uuid4()), "stories": {}},
        )

    def load(self):
        if not self.path.exists():
            raise ConfigError(
                "State missing. Use init-state only for a new installation; otherwise restore history"
            )
        data = parse_json(self.path.read_text(encoding="utf-8"))
        if (
            data.get("schema_version") != 1
            or not data.get("installation_id")
            or not isinstance(data.get("stories"), dict)
        ):
            raise ConfigError("Invalid state; restore and reconcile history")
        for story in data["stories"].values():
            validate_story_state(story)
        return data

    def save(self, data):
        atomic_json(self.path, data)


def validate_story_state(story):
    from .models import Story

    Story.model_validate(story["snapshot"])
    if set(story["jobs"]) != {"long", "short"}:
        raise ConfigError("Incomplete story state")
    for job in story["jobs"].values():
        if not isinstance(job, dict) or "stage" not in job or "source" not in job:
            raise ConfigError("Invalid job state")
        if job["stage"] not in {
            "pending",
            "downloading",
            "uploading",
            "uploaded",
            "scheduling",
            "scheduled",
            "published",
            "waiting",
            "missed",
            "failed",
            "needs_review",
        }:
            raise ConfigError("Unknown job stage")
        if job["stage"] == "uploading" and not job.get("session"):
            raise ConfigError("Uploading job missing session")
        if job["stage"] in {
            "uploaded",
            "scheduling",
            "scheduled",
            "published",
        } and not job.get("video_id"):
            raise ConfigError("Uploaded job missing video ID")
        if not job["source"].get("id") or int(job["source"].get("size", 0)) <= 0:
            raise ConfigError("Invalid source identity")
        if not isinstance(job.get("attempts"), int) or job["attempts"] < 0:
            raise ConfigError("Invalid attempt count")
        if "log_recorded" in job and not isinstance(job["log_recorded"], bool):
            raise ConfigError("Invalid upload log flag")
        failures = job.get("consecutive_failures", 0)
        if not isinstance(failures, int) or failures < 0:
            raise ConfigError("Invalid consecutive failure count")


def upload_log_job_id(channel_id, story_id, role):
    return f"{channel_id}/{story_id}/{role}"


def upload_log_outcome(job):
    """Return (status, error) for one finished upload attempt, or None.

    Local state decides whether YouTube already has the video. A saved
    video_id must not be uploaded again. Unstarted jobs left waiting after
    the publish time, and transfers still in progress, are not log lines.
    """
    stage = job.get("stage")
    video_id = job.get("video_id") or ""
    if video_id and stage == "failed":
        return "failed", job.get("error") or ""
    if video_id:
        return "uploaded", ""
    if stage in {"failed", "needs_review"} and job.get("session"):
        return stage, job.get("error") or ""
    return None
