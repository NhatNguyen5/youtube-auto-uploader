import hashlib
import random
from datetime import UTC, datetime, timedelta

import requests
from pydantic import ValidationError

from .drive import ApiError
from .models import Story
from .youtube import NeedsReview


def now():
    return datetime.now(UTC)


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

    def note_progress(self, job):
        job["consecutive_failures"] = 0
        job.pop("next_retry", None)

    def transfer_with_progress(self, job, path):
        acknowledged = job.get("offset") or 0

        def checkpoint():
            nonlocal acknowledged
            offset = job.get("offset")
            if job.get("video_id") or (
                isinstance(offset, int) and offset > acknowledged
            ):
                self.note_progress(job)
            if isinstance(offset, int):
                acknowledged = offset
            self.save()

        self.youtube.transfer(job, path, checkpoint)

    def seconds_before_schedule_margin(self, record, role):
        publish_at = Story.model_validate(record["snapshot"]).videos[role].publish_at
        return (
            publish_at
            - timedelta(seconds=self.settings.schedule_margin_seconds)
            - self.clock()
        ).total_seconds()

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
                        role: {
                            "stage": "pending",
                            "source": source,
                            "attempts": 0,
                            "consecutive_failures": 0,
                        }
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
            self.note_progress(job)
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
                self.note_progress(job)
                self.save()
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
                self.transfer_with_progress(job, path)
                return
            job["stage"] = "downloading"
            self.save()
            job["cache_md5"] = self.drive.download(source, path)
            self.note_progress(job)
            self.save()
        if not job.get("session"):
            if self.clock() >= video.publish_at:
                job["stage"] = "missed"
                self.save()
                return
            self.youtube.validate_category(video.category_id)
            job["session"] = self.youtube.start(video, source)
            job["stage"] = "uploading"
            self.note_progress(job)
            self.save()  # No video bytes may be sent until this succeeds.
        self.transfer_with_progress(job, path)

    def process(self, sid, role, record):
        job = record["jobs"][role]
        try:
            self.step(sid, role, record)
        except NeedsReview as error:
            job.update(stage="needs_review", error=str(error))
            self.save()
        except (ApiError, requests.RequestException) as error:
            job["attempts"] += 1
            job["consecutive_failures"] = job.get("consecutive_failures", 0) + 1
            transient = isinstance(
                error, requests.RequestException
            ) or error.status in {429, 500, 502, 503, 504}
            job["error"] = (
                str(error)
                if isinstance(error, ApiError)
                else "Network failure; session will be reconciled"
            )
            if transient and job["consecutive_failures"] < 10:
                delay = max(
                    getattr(error, "retry_after", 0),
                    min(3600, 30 * 2 ** min(job["consecutive_failures"], 7))
                    + random.uniform(0, 10),
                )
                remaining = self.seconds_before_schedule_margin(record, role)
                if remaining > 0:
                    delay = min(delay, remaining)
                job["next_retry"] = (
                    self.clock() + timedelta(seconds=delay)
                ).isoformat()
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
