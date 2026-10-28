import json
import os
import tempfile
import uuid
from pathlib import Path

import portalocker


def parse_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(text, object_pairs_hook=pairs)


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".write-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
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
            raise ValueError("State already exists; refusing to overwrite history")
        atomic_json(
            self.path,
            {"schema_version": 1, "installation_id": str(uuid.uuid4()), "stories": {}},
        )

    def load(self):
        if not self.path.exists():
            raise ValueError(
                "State missing. Use init-state only for a new installation; otherwise restore history"
            )
        data = parse_json(self.path.read_text(encoding="utf-8"))
        if (
            data.get("schema_version") != 1
            or not data.get("installation_id")
            or not isinstance(data.get("stories"), dict)
        ):
            raise ValueError("Invalid state; restore and reconcile history")
        for story in data["stories"].values():
            validate_story_state(story)
        return data

    def save(self, data):
        atomic_json(self.path, data)


def validate_story_state(story):
    from .models import Story

    Story.model_validate(story["snapshot"])
    if set(story["jobs"]) != {"long", "short"}:
        raise ValueError("Incomplete story state")
    for job in story["jobs"].values():
        if not isinstance(job, dict) or "stage" not in job or "source" not in job:
            raise ValueError("Invalid job state")
        if job["stage"] not in {
            "pending",
            "downloading",
            "uploading",
            "uploaded",
            "scheduling",
            "scheduled",
            "published",
            "missed",
            "failed",
            "needs_review",
        }:
            raise ValueError("Unknown job stage")
        if job["stage"] == "uploading" and not job.get("session"):
            raise ValueError("Uploading job missing session")
        if job["stage"] in {
            "uploaded",
            "scheduling",
            "scheduled",
            "published",
        } and not job.get("video_id"):
            raise ValueError("Uploaded job missing video ID")
        if not job["source"].get("id") or int(job["source"].get("size", 0)) <= 0:
            raise ValueError("Invalid source identity")
        if not isinstance(job.get("attempts"), int) or job["attempts"] < 0:
            raise ValueError("Invalid attempt count")
        failures = job.get("consecutive_failures", 0)
        if not isinstance(failures, int) or failures < 0:
            raise ValueError("Invalid consecutive failure count")
