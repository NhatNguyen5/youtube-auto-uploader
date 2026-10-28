import hashlib
import random
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from pydantic import ValidationError

from .drive import ApiError
from .models import Story
from .youtube import NeedsReview

PACIFIC = ZoneInfo("America/Los_Angeles")
TRANSIENT_STATUSES = {429, 500, 502, 503, 504}
TRANSIENT_REASONS = {
    "rateLimitExceeded",
    "userRateLimitExceeded",
    "backendError",
}
QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded"}


def now():
    return datetime.now(UTC)


def next_quota_reset(moment):
    # YouTube quota resets at midnight Pacific time.
    local = moment.astimezone(PACIFIC)
    reset = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if reset <= local:
        reset += timedelta(days=1)
    return reset.astimezone(UTC)


def error_kind(error):
    if isinstance(error, ApiError):
        if error.reason in QUOTA_REASONS:
            return "quota"
        if error.status in TRANSIENT_STATUSES or error.reason in TRANSIENT_REASONS:
            return "transient"
        return "fatal"
    if isinstance(error, requests.RequestException):
        return "transient"
    return "fatal"


def scheduled_delay(computed, retry_after=0):
    # Retry-After is a floor the cap must not undercut. Never wait under a second.
    return max(float(computed), float(retry_after or 0), 1.0)


def poll_retry(error, failures, once, moment, poll_seconds=30):
    kind = error_kind(error)
    retry_after = getattr(error, "retry_after", 0)
    if kind == "quota" and not once:
        wait = (next_quota_reset(moment) - moment).total_seconds()
        return "quota", failures, scheduled_delay(wait, retry_after)
    failures += 1
    if once or kind != "transient" or failures >= 10:
        return "stop", failures, 0.0
    computed = max(poll_seconds, min(3600, 30 * 2**failures))
    return "transient", failures, scheduled_delay(computed, retry_after)


class Runner:
    def __init__(self, settings, base, store, drive, youtube, report=print, clock=now):
        self.settings, self.base, self.store = settings, base, store
        self.drive, self.youtube, self.report, self.clock = (
            drive,
            youtube,
            report,
            clock,
        )
        self.state = store.load()
        binding = {
            "drive_folder_id": settings.drive_folder_id,
            "youtube_channel_id": settings.youtube_channel_id,
        }
        if self.state.get("binding") and self.state["binding"] != binding:
            raise ValueError(
                "State belongs to a different root folder or YouTube channel"
            )
        self.state["binding"] = binding
        self.save()

    def save(self):
        self.store.save(self.state)

    def discover(self):
        folders = self.drive.children(self.settings.drive_folder_id)
        for folder in folders:
            if folder["mimeType"] != "application/vnd.google-apps.folder":
                continue
            sid = folder["id"]
            existing = self.state["stories"].get(sid)
            if existing:
                existing["name"] = folder["name"]
                # Frozen once work starts. Reconcile existing jobs without changing them.
                continue
            try:
                story, sources = self.drive.story(sid)
                if not story.enabled:
                    continue
                if all(
                    v.publish_at - timedelta(minutes=self.settings.upload_lead_minutes)
                    > self.clock()
                    for v in story.videos.values()
                ):
                    continue
                used = {
                    j["source"]["id"]
                    for s in self.state["stories"].values()
                    for j in s["jobs"].values()
                }
                if any(source["id"] in used for source in sources.values()):
                    raise ValueError("Source already belongs to another story")
                self.state["stories"][sid] = {
                    "name": folder["name"],
                    "snapshot": story.model_dump(mode="json"),
                    "jobs": {
                        role: {"stage": "pending", "source": source, "attempts": 0}
                        for role, source in sources.items()
                    },
                }
                self.save()
            except (ValueError, ValidationError) as error:
                # Do not print validation payloads, which may contain remote content.
                self.report(
                    f"Story {sid}: waiting for valid config and two unique videos ({type(error).__name__})"
                )

    def step(self, sid, role, record):
        video = Story.model_validate(record["snapshot"]).videos[role]
        job = record["jobs"][role]
        stage = job["stage"]
        if stage in {"published", "missed", "failed", "needs_review"}:
            return
        if (
            job.get("next_retry")
            and datetime.fromisoformat(job["next_retry"]) > self.clock()
        ):
            return
        if job.get("video_id"):
            remote = self.youtube.inspect(job["video_id"])
            status = remote["status"]
            processing = remote.get("processingDetails", {}).get("processingStatus")
            job["processing_status"] = processing
            if status.get("privacyStatus") == "public":
                job["stage"] = "published"
            elif status.get("uploadStatus") in {
                "failed",
                "rejected",
                "deleted",
            } or processing in {"failed", "terminated"}:
                job["stage"] = "failed"
                job["error"] = "YouTube processing failed or video rejected"
            elif status.get("publishAt"):
                if (
                    datetime.fromisoformat(status["publishAt"].replace("Z", "+00:00"))
                    != video.publish_at
                ):
                    raise NeedsReview("Remote publication time differs from snapshot")
                job["stage"] = "scheduled"
                if self.clock() >= video.publish_at:
                    job["error"] = "Publication time passed; awaiting public visibility"
            elif job.get("stage") == "scheduled":
                raise NeedsReview("Confirmed schedule was removed remotely")
            elif (
                self.clock() + timedelta(seconds=self.settings.schedule_margin_seconds)
                >= video.publish_at
            ):
                job["stage"] = "missed"
            elif processing == "succeeded" or status.get("uploadStatus") == "processed":
                # Persist intent before network call; next pass always reads remote status.
                job["stage"] = "scheduling"
                self.save()
                self.youtube.schedule(job["video_id"], video)
                return
            self.save()
            return

        # Recover an existing session even after deadline to resolve upload identity.
        if not job.get("session"):
            if self.clock() >= video.publish_at:
                job["stage"] = "missed"
                self.save()
                return
            if self.clock() < video.publish_at - timedelta(
                minutes=self.settings.upload_lead_minutes
            ):
                return
        key = hashlib.sha256(f"{sid}:{role}".encode()).hexdigest()
        path = self.base / self.settings.cache_dir / f"{key}.video"
        source = job["source"]
        if job.get("cache_md5") and path.exists():
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "md5").hexdigest()
            if digest != job["cache_md5"] or path.stat().st_size != int(source["size"]):
                raise NeedsReview("Cached video changed; cannot safely resume")
        else:
            if job.get("session"):
                # The remote session can still recover a completed video's ID.
                self.youtube.transfer(job, path, self.save)
                return
            job["stage"] = "downloading"
            self.save()
            job["cache_md5"] = self.drive.download(source, path)
            self.save()
        if not job.get("session"):
            if self.clock() >= video.publish_at:
                job["stage"] = "missed"
                self.save()
                return
            self.youtube.validate_category(video.category_id)
            job["session"] = self.youtube.start(video, source)
            job["stage"] = "uploading"
            self.save()  # No video bytes may be sent until this succeeds.
        self.youtube.transfer(job, path, self.save)

    def process(self, sid, role, record):
        job = record["jobs"][role]
        try:
            self.step(sid, role, record)
        except NeedsReview as error:
            job.update(stage="needs_review", error=str(error))
            self.save()
        except (ApiError, requests.RequestException) as error:
            kind = error_kind(error)
            # Quota resets on a clock, so it must not burn the attempt budget.
            if kind != "quota":
                job["attempts"] += 1
            job["error"] = (
                str(error)
                if isinstance(error, ApiError)
                else "Network failure; session will be reconciled"
            )
            if kind == "quota":
                publish_at = (
                    Story.model_validate(record["snapshot"]).videos[role].publish_at
                )
                if (
                    self.clock() >= publish_at
                    and not job.get("video_id")
                    and not job.get("session")
                ):
                    job["stage"] = "missed"
                    job.pop("next_retry", None)
                else:
                    moment = self.clock()
                    remaining = (next_quota_reset(moment) - moment).total_seconds()
                    delay = scheduled_delay(remaining, getattr(error, "retry_after", 0))
                    job["next_retry"] = (moment + timedelta(seconds=delay)).isoformat()
            elif kind == "transient" and job["attempts"] < 10:
                moment = self.clock()
                grown = 30 * 2 ** min(job["attempts"], 7)
                computed = min(3600, grown) + random.uniform(0, 10)
                delay = scheduled_delay(computed, getattr(error, "retry_after", 0))
                job["next_retry"] = (moment + timedelta(seconds=delay)).isoformat()
            else:
                job["stage"] = "needs_review"
            self.save()
        except ValueError:
            job.update(
                stage="needs_review", error="Source or metadata validation failed"
            )
            self.save()
        # Storage/OAuth errors deliberately propagate: do not continue remote work.
        self.report(f"{sid}/{role}: {job['stage']}")

    def reconcile_uploaded(self):
        for sid, record in list(self.state["stories"].items()):
            for role, job in record["jobs"].items():
                if job.get("video_id"):
                    self.process(sid, role, record)

    def cycle(self):
        # Reconcile known IDs/sessions before any discovery or new upload.
        self.reconcile_uploaded()
        for sid, record in list(self.state["stories"].items()):
            for role, job in record["jobs"].items():
                if job.get("session") and not job.get("video_id"):
                    self.process(sid, role, record)
                    self.reconcile_uploaded()
        self.discover()
        pending = [
            (sid, role, record)
            for sid, record in self.state["stories"].items()
            for role, job in record["jobs"].items()
            if not job.get("video_id") and not job.get("session")
        ]
        pending.sort(
            key=lambda item: (
                Story.model_validate(item[2]["snapshot"]).videos[item[1]].publish_at
            )
        )
        for sid, role, record in pending:
            self.process(sid, role, record)
            self.reconcile_uploaded()
