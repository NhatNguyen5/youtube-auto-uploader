"""Story list, story config, retry, and settings edits for the desktop window.

The window draws these results. State changes go through ``Store`` and the
same run lock the runner holds, as one atomic replace.
"""

import copy
import os
import tempfile
from datetime import datetime
from pathlib import Path

from portalocker.exceptions import AlreadyLocked, LockException
from pydantic import ValidationError
from pydantic_core import PydanticUndefined

from . import google_auth
from .drive import Drive, fingerprint, is_video
from .models import Settings
from .runner import apply_live_config, upload_started
from .status import ConfigError, public_error, story_label
from .storage import Store, atomic_json, parse_json, replace_file, validate_story_state
from .tz import local_zone

ROLES = ("long", "short")
RETRY_STAGES = frozenset({"needs_review", "failed", "missed"})
FOLDER_MIME = "application/vnd.google-apps.folder"
SETTINGS_FIELDS = (
    "drive_folder_id",
    "stats_drive_folder_id",
    "upload_log_folder_id",
    "youtube_channel_id",
    "upload_lead_minutes",
    "poll_seconds",
)

# The runner holds this lock for the whole process, including a transfer.
RUNNER_BUSY = "Runner is active and may be uploading. Saved history was not changed."


class EditResult:
    def __init__(self, ok=False, message="", errors=None, confirmation=None):
        self.ok = bool(ok)
        self.message = message or ""
        self.errors = dict(errors or {})
        self.confirmation = confirmation


class RoleRow:
    def __init__(
        self, role, stage, publish_local, title, error, retry_allowed, retry_reason
    ):
        self.role = role
        self.stage = stage
        self.publish_local = publish_local
        self.title = title
        self.error = error
        self.retry_allowed = retry_allowed
        self.retry_reason = retry_reason


class StoryRow:
    def __init__(
        self, folder_id, name, config_file_id, config_error, video_names, raw, roles
    ):
        self.folder_id = folder_id
        self.name = name
        self.config_file_id = config_file_id
        self.config_error = config_error
        self.video_names = list(video_names)
        self.raw = raw
        self.roles = roles


class FolderFiles:
    def __init__(self, config_file_id, raw, error, video_names):
        self.config_file_id = config_file_id
        self.raw = raw
        self.error = error
        self.video_names = list(video_names)


class StoriesSnapshot:
    def __init__(self, stories, message="", state_locked=False):
        self.stories = list(stories)
        self.message = message or ""
        self.state_locked = state_locked


def format_offset(offset):
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    hours, minutes = divmod(total, 3600)
    return f"{sign}{hours:02d}:{minutes // 60:02d}"


def format_publish_at(value):
    """publish_at as a local wall time plus the explicit offset."""
    if value is None or value == "":
        return ""
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value)
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return text
    if moment.tzinfo is None or moment.utcoffset() is None:
        return str(value)
    local = moment.astimezone(local_zone()).replace(microsecond=0)
    return f"{local.strftime('%Y-%m-%d %H:%M')} {format_offset(local.utcoffset())}"


def field_errors(error):
    result = {}
    for item in error.errors():
        loc = ".".join(str(part) for part in item.get("loc") or ()) or "config"
        msg = item.get("msg") or "invalid"
        prefix = "Value error, "
        if msg.startswith(prefix):
            msg = msg[len(prefix) :]
        result.setdefault(loc, msg)
    return result


def retry_decision(job):
    """Whether Retry may reset this job, and the sentence the button shows.

    A saved video id or upload session stays put. Resetting either one could
    start a second upload. The story record is never removed.
    """
    if not isinstance(job, dict):
        return (
            False,
            "This video is not in saved history yet, so there is nothing to retry.",
        )
    if job.get("video_id"):
        return (
            False,
            "Retry is off because this video already has a YouTube id. "
            "Retrying it could create a duplicate upload.",
        )
    if job.get("session"):
        return (
            False,
            "Retry is off because this video already has an upload session. "
            "Retrying it could create a duplicate upload.",
        )
    stage = job.get("stage")
    if stage not in RETRY_STAGES:
        return (
            False,
            "Retry is only for needs review, failed, or missed. "
            f"This video is {stage}.",
        )
    return (
        True,
        "Resets this video to waiting and clears its error, next retry, "
        "and cached checksum. The next run reads the current config.json. "
        "The story stays, including when the other video is already uploaded.",
    )


def apply_retry(job):
    """Reset one job so live config can pick it up. Does not delete the story."""
    job["stage"] = "waiting"
    job["consecutive_failures"] = 0
    job.pop("error", None)
    job.pop("next_retry", None)
    job.pop("cache_md5", None)
    job.pop("noticed_publish_at", None)


def _require_job(state, story_id, role):
    stories = state.get("stories")
    if not isinstance(stories, dict) or story_id not in stories:
        raise ConfigError("That story is not in saved history.")
    record = stories[story_id]
    jobs = record.get("jobs") if isinstance(record, dict) else None
    if (
        not isinstance(jobs, dict)
        or role not in jobs
        or not isinstance(jobs[role], dict)
    ):
        raise ConfigError("That video is not in saved history.")
    return jobs[role]


def with_state(store, mutator):
    """Load, change, and save state under the run lock.

    A held lock means the runner is in this process, including mid-upload.
    The file is replaced only after the whole change is valid.
    """
    lock = store.lock()
    try:
        lock.acquire()
    except AlreadyLocked:
        return EditResult(message=RUNNER_BUSY)
    except LockException as error:
        return EditResult(message=public_error(error))
    try:
        state = store.load()
        mutator(state)
        for story in state["stories"].values():
            validate_story_state(story)
        store.save(state)
    except Exception as error:
        return EditResult(message=public_error(error))
    finally:
        lock.release()
    return EditResult(ok=True)


def read_state(store):
    """Return ``(state or None, locked, message)`` for display or a pre-check.

    A locked file is still read. Replacing it is atomic, so the bytes are a
    complete earlier or later document. Callers that write take the lock again.
    """
    lock = store.lock()
    try:
        lock.acquire()
    except AlreadyLocked:
        locked = True
    except LockException as error:
        return None, False, public_error(error)
    else:
        locked = False
        lock.release()
    if not store.path.exists():
        return None, locked, ""
    try:
        return store.load(), locked, ""
    except Exception as error:
        return None, locked, public_error(error)


def _settings_from_path(config_path):
    path = Path(config_path).resolve()
    settings = Settings.model_validate(parse_json(path.read_text(encoding="utf-8-sig")))
    return path, settings, Store(path.parent / settings.state_path)


def _changed_fields(before, after):
    keys = set(before) | set(after)
    return sorted(key for key in keys if before.get(key) != after.get(key))


def _started_reason(job):
    if job.get("video_id"):
        return "a YouTube id"
    return "an upload session"


def reload_record(state, folder_id, record, story, sources, name):
    """Apply one Drive story with the runner's live-config rules.

    Returns lines describing what changed, or why this story was rejected.
    A rejection leaves this record as it was. Jobs with a session or video
    id are never changed.
    """
    label = story_label(name, folder_id)
    notes = []
    if name and record.get("name") != name:
        record["name"] = name
        notes.append(f"{label}: renamed to {name}.")
    if not story.enabled:
        if record.get("enabled") is not False:
            record["enabled"] = False
            notes.append(f"{label}: disabled. Saved video metadata was not changed.")
        else:
            notes.append(f"{label}: disabled. Already skipped.")
        return notes
    if all(upload_started(job) for job in record["jobs"].values()):
        notes.append(
            f"{label}: left unchanged because every video has already started."
        )
        return notes
    before = copy.deepcopy(record)
    fresh = story.model_dump(mode="json")
    try:
        apply_live_config(state, folder_id, record, story, sources)
    except (ConfigError, ValidationError) as error:
        record.clear()
        record.update(before)
        notes.append(f"{label}: {public_error(error)}")
        return notes
    for role, job in record["jobs"].items():
        previous = before["jobs"][role]
        if upload_started(previous):
            drive_video = fresh["videos"][role]
            saved_video = before["snapshot"]["videos"][role]
            if (
                drive_video != saved_video
                or sources[role]["id"] != previous["source"]["id"]
            ):
                notes.append(
                    f"{label} {role}: not applied because this video already has "
                    f"{_started_reason(previous)}."
                )
            continue
        fields = _changed_fields(
            before["snapshot"]["videos"][role], record["snapshot"]["videos"][role]
        )
        if fingerprint(previous["source"]) != fingerprint(job["source"]):
            fields = sorted({*fields, "file"})
        if fields:
            notes.append(f"{label} {role}: {', '.join(fields)}.")
        else:
            notes.append(f"{label} {role}: unchanged.")
    if not notes:
        notes.append(f"{label}: unchanged.")
    return notes


def read_drive_stories(drive, root_id, folder_id=None):
    """Download story configs. Does not touch saved history.

    Returns ``(folders, reads)``. ``reads[folder id]`` is ``(story, sources)``
    or the exception from that folder. A missing single folder returns a
    note instead, as ``(None, note)``.
    """
    folders = [
        item for item in drive.children(root_id) if item.get("mimeType") == FOLDER_MIME
    ]
    if folder_id:
        folders = [item for item in folders if str(item.get("id") or "") == folder_id]
        if not folders:
            return None, (
                "That story folder is not in the Drive folder. "
                "Saved history was not changed."
            )
    reads = {}
    for folder in folders:
        sid = str(folder.get("id") or "")
        if not sid:
            continue
        try:
            reads[sid] = drive.story(sid)
        except Exception as error:
            reads[sid] = error
    return folders, reads


def apply_reloaded_stories(state, folders, reads, folder_id=None):
    """Apply configs already read from Drive. Returns ``(notes, changed)``."""
    seen = set()
    notes = []
    changed = False
    for folder in folders:
        sid = str(folder.get("id") or "")
        if not sid:
            continue
        seen.add(sid)
        name = str(folder.get("name") or sid)
        record = state["stories"].get(sid)
        found = reads.get(sid)
        if record is None:
            if isinstance(found, Exception):
                notes.append(f"{story_label(name, sid)}: {public_error(found)}")
            else:
                notes.append(
                    f"{story_label(name, sid)}: not in saved history yet. "
                    "Nothing was added."
                )
            continue
        if isinstance(found, Exception):
            notes.append(f"{story_label(name, sid)}: {public_error(found)}")
            continue
        story, sources = found
        before = copy.deepcopy(record)
        notes.extend(reload_record(state, sid, record, story, sources, name))
        if record != before:
            changed = True
    if folder_id is None:
        for sid, record in state["stories"].items():
            if sid in seen:
                continue
            label = story_label(record.get("name"), sid)
            notes.append(f"{label}: not in the Drive folder. It was left as it is.")
    if not notes:
        notes.append("No story folders in this Drive folder.")
    return notes, changed


def reload_stories(drive, state, root_id, folder_id=None):
    """Reread story configs and apply them to unstarted jobs.

    ``folder_id`` limits the pass to one story. Stories that are not in
    saved history are reported and not added. A rejected story is left as
    it was. Returns ``(notes, changed)``.
    """
    folders, reads = read_drive_stories(drive, root_id, folder_id)
    if folders is None:
        return [reads], False
    return apply_reloaded_stories(state, folders, reads, folder_id)


def _apply_under_lock(store, folders, reads, folder_id):
    lock = store.lock()
    try:
        lock.acquire()
    except AlreadyLocked:
        return EditResult(message=RUNNER_BUSY)
    except LockException as error:
        return EditResult(message=public_error(error))
    try:
        state = store.load()
        notes, changed = apply_reloaded_stories(state, folders, reads, folder_id)
        if changed:
            for story in state["stories"].values():
                validate_story_state(story)
            store.save(state)
        return EditResult(ok=True, message="\n".join(notes))
    except Exception as error:
        return EditResult(message=public_error(error))
    finally:
        lock.release()


def reload_from_drive(config_path, folder_id=None):
    """Read Drive first, then take the run lock only to apply the result.

    The runner's ``apply_live_config`` decides which jobs move. A runner
    that already holds the lock still blocks the save. It does not block
    the Drive read.
    """
    try:
        path, settings, store = _settings_from_path(config_path)
    except Exception as error:
        return EditResult(message=public_error(error))
    if not store.path.exists():
        return EditResult(message="State missing. Reload did not create a new history.")
    blocked = google_auth.login_message(path.parent)
    if blocked:
        return EditResult(message=blocked)
    http = None
    persist = None
    try:
        http, persist = google_auth.session(path.parent)
        folders, reads = read_drive_stories(
            Drive(http), settings.drive_folder_id, folder_id
        )
    except Exception as error:
        return EditResult(message=public_error(error))
    finally:
        if persist:
            persist()
        if http:
            http.close()
    if folders is None:
        return EditResult(ok=True, message=reads)
    return _apply_under_lock(store, folders, reads, folder_id)


def retry_saved(config_path, story_id, role):
    try:
        _path, _settings, store = _settings_from_path(config_path)
    except Exception as error:
        return EditResult(message=public_error(error))

    def mutate(state):
        job = _require_job(state, story_id, role)
        allowed, reason = retry_decision(job)
        if not allowed:
            raise ConfigError(reason)
        apply_retry(job)

    result = with_state(store, mutate)
    if result.ok:
        result.message = (
            "Reset to waiting. The next run reads this story's current config.json."
        )
    return result


def video_dict(raw, role):
    if not isinstance(raw, dict):
        return {}
    videos = raw.get("videos")
    if not isinstance(videos, dict):
        return {}
    video = videos.get(role)
    return video if isinstance(video, dict) else {}


def displayed_setting(raw, field):
    if field in raw and raw[field] is not None:
        return str(raw[field])
    default = Settings.model_fields[field].default
    if default is None or default is PydanticUndefined:
        return ""
    return str(default)


def settings_form(config_path):
    path = Path(config_path).resolve()
    raw = parse_json(path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise ConfigError("config.json must be a JSON object.")
    return {field: displayed_setting(raw, field) for field in SETTINGS_FIELDS}


def settings_from_form(original, form):
    data = dict(original)
    errors = {}
    for field in ("drive_folder_id", "upload_log_folder_id", "youtube_channel_id"):
        data[field] = str(form.get(field, "")).strip()
    stats = str(form.get("stats_drive_folder_id", "")).strip()
    data["stats_drive_folder_id"] = stats or None
    if not data["drive_folder_id"]:
        errors["drive_folder_id"] = "Drive folder ID is required."
    for field in ("upload_lead_minutes", "poll_seconds"):
        text = str(form.get(field, "")).strip()
        if not text.isdigit():
            errors[field] = "Enter a whole number."
        else:
            data[field] = int(text)
    if "upload_lead_minutes" not in errors and "poll_seconds" not in errors:
        try:
            Settings.model_validate(data)
        except ValidationError as error:
            for loc, msg in field_errors(error).items():
                errors.setdefault(loc, msg)
    if errors:
        return None, errors
    return Settings.model_validate(data), {}


def binding_of(settings):
    return {
        "drive_folder_id": settings.drive_folder_id,
        "youtube_channel_id": settings.youtube_channel_id,
    }


def confirmation_for(state, settings):
    """Explain a binding change, or None when saved history already matches."""
    if not state or not isinstance(state.get("binding"), dict):
        return None
    current = state["binding"]
    proposed = binding_of(settings)
    if current == proposed:
        return None
    return (
        "Saved history is bound to Drive folder "
        f"{current.get('drive_folder_id', '')} and YouTube channel "
        f"{current.get('youtube_channel_id', '')}. "
        "Saving these settings will point that same history at Drive folder "
        f"{proposed['drive_folder_id']} and YouTube channel "
        f"{proposed['youtube_channel_id']}. "
        "Jobs already in state.json stay. Nothing is deleted. "
        "Confirm to update the binding."
    )


def _replace_bytes(path, payload):
    path = Path(path)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".write-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        replace_file(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def _save_settings_and_binding(path, store, settings):
    payload = settings.model_dump(mode="json")
    proposed = binding_of(settings)
    lock = store.lock()
    try:
        lock.acquire()
    except AlreadyLocked:
        return EditResult(message=RUNNER_BUSY)
    except LockException as error:
        return EditResult(message=public_error(error))
    try:
        data = store.load()
        data["binding"] = proposed
        for story in data["stories"].values():
            validate_story_state(story)
        previous_config = path.read_bytes()
        atomic_json(path, payload)
        try:
            store.save(data)
        except Exception:
            _replace_bytes(path, previous_config)
            raise
        return EditResult(
            ok=True,
            message=(
                "Saved config.json and updated the saved folder and channel "
                "binding. Existing jobs were kept."
            ),
        )
    except Exception as error:
        return EditResult(message=public_error(error))
    finally:
        lock.release()


def save_settings(config_path, form, *, confirm_binding=False):
    path = Path(config_path).resolve()
    try:
        original = parse_json(path.read_text(encoding="utf-8-sig"))
    except Exception as error:
        return EditResult(message=public_error(error))
    if not isinstance(original, dict):
        return EditResult(message="config.json must be a JSON object.")
    settings, errors = settings_from_form(original, form)
    if errors:
        return EditResult(errors=errors, message="Fix the fields below before saving.")
    store = Store(path.parent / settings.state_path)
    if store.path.exists():
        state, locked, message = read_state(store)
        if state is None and message:
            return EditResult(message=message)
        confirmation = confirmation_for(state, settings)
        if confirmation and locked:
            return EditResult(message=f"{RUNNER_BUSY} {confirmation}")
        if confirmation and not confirm_binding:
            return EditResult(confirmation=confirmation)
        if confirmation:
            return _save_settings_and_binding(path, store, settings)
    try:
        atomic_json(path, settings.model_dump(mode="json"))
    except Exception as error:
        return EditResult(message=public_error(error))
    return EditResult(ok=True, message="Saved config.json.")


def video_names_from(files):
    names = []
    for item in files:
        mime = str(item.get("mimeType", ""))
        if mime.startswith("application/vnd.google-apps."):
            continue
        if item.get("name") and is_video(item):
            names.append(item["name"])
    return names


def inspect_folder(drive, folder_id):
    try:
        files = drive.children(folder_id)
    except Exception as error:
        return FolderFiles(None, None, public_error(error), [])
    videos = video_names_from(files)
    matches = [item for item in files if item.get("name") == "config.json"]
    if len(matches) != 1:
        return FolderFiles(
            None,
            None,
            f"Expected one config.json; found {len(matches)}",
            videos,
        )
    file_id = matches[0].get("id")
    try:
        text = drive.read_text(file_id, 1024 * 1024)
        raw = parse_json(text)
    except Exception as error:
        return FolderFiles(file_id, None, public_error(error), videos)
    if not isinstance(raw, dict):
        return FolderFiles(file_id, None, "Story config must be a JSON object.", videos)
    return FolderFiles(file_id, raw, "", videos)


def _job(state, folder_id, role):
    if not isinstance(state, dict):
        return None
    stories = state.get("stories")
    if not isinstance(stories, dict):
        return None
    record = stories.get(folder_id)
    if not isinstance(record, dict):
        return None
    jobs = record.get("jobs")
    if not isinstance(jobs, dict):
        return None
    job = jobs.get(role)
    return job if isinstance(job, dict) else None


def assemble_story_views(folders, details, state):
    """Rows for story folders, in name order. ``details`` is keyed by folder id."""
    rows = []
    ordered = sorted(
        folders,
        key=lambda item: (
            str(item.get("name") or "").casefold(),
            str(item.get("id") or ""),
        ),
    )
    for folder in ordered:
        if folder.get("mimeType") != FOLDER_MIME:
            continue
        folder_id = str(folder.get("id") or "")
        if not folder_id:
            continue
        found = details.get(folder_id) or FolderFiles(
            None, None, "Story folder could not be read.", []
        )
        roles = {}
        for role in ROLES:
            video = video_dict(found.raw, role)
            job = _job(state, folder_id, role)
            allowed, reason = retry_decision(job)
            roles[role] = RoleRow(
                role,
                job.get("stage") if job else "not started",
                format_publish_at(video.get("publish_at")),
                str(video.get("title") or ""),
                (job.get("error") or "") if job else "",
                allowed,
                reason,
            )
        rows.append(
            StoryRow(
                folder_id,
                str(folder.get("name") or folder_id),
                found.config_file_id,
                found.error,
                found.video_names,
                found.raw,
                roles,
            )
        )
    return rows


def collect_stories(config_path):
    path = Path(config_path).resolve()
    try:
        settings = Settings.model_validate(
            parse_json(path.read_text(encoding="utf-8-sig"))
        )
    except Exception as error:
        return StoriesSnapshot([], public_error(error))
    store = Store(path.parent / settings.state_path)
    state, locked, state_message = read_state(store)
    notes = []
    if locked:
        notes.append(
            "Runner is active. Stages are the last saved values. "
            "Retry and binding changes wait until it stops."
        )
    if state_message:
        notes.append(state_message)
    base = path.parent
    login = google_auth.login_message(base)
    if login:
        notes.append(login)
        return StoriesSnapshot([], " ".join(notes), state_locked=locked)
    http = None
    persist = None
    try:
        http, persist = google_auth.session(base)
        drive = Drive(http)
        folders = drive.children(settings.drive_folder_id)
        details = {}
        for folder in folders:
            if folder.get("mimeType") != FOLDER_MIME:
                continue
            folder_id = str(folder.get("id") or "")
            details[folder_id] = inspect_folder(drive, folder_id)
        stories = assemble_story_views(folders, details, state)
    except Exception as error:
        notes.append(public_error(error))
        return StoriesSnapshot([], " ".join(notes), state_locked=locked)
    finally:
        if persist:
            persist()
        if http:
            http.close()
    if not stories and not any(
        note for note in notes if "Runner is active" not in note
    ):
        notes.append("No story folders in this Drive folder.")
    return StoriesSnapshot(stories, " ".join(notes), state_locked=locked)
