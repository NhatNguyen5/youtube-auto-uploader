import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

from .drive import ApiError, checked

ROOT = "https://www.googleapis.com/youtube/v3"
UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos"
CHUNK = 8 * 1024 * 1024


class NeedsReview(Exception):
    pass


def session_url(url):
    parsed = urlparse(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "www.googleapis.com"
        or parsed.port not in (None, 443)
        or parsed.username
        or not parsed.path.startswith("/upload/youtube/")
    ):
        raise NeedsReview("Invalid resumable session URL")
    return url


class YouTube:
    def __init__(self, http):
        self.http = http

    def verify_channel(self, expected):
        data = checked(
            self.http.get(
                f"{ROOT}/channels", params={"part": "id", "mine": "true"}, timeout=60
            )
        ).json()
        if expected not in {item["id"] for item in data.get("items", [])}:
            raise ValueError(
                "Authenticated YouTube channel does not match youtube_channel_id"
            )

    def validate_category(self, category):
        data = checked(
            self.http.get(
                f"{ROOT}/videoCategories",
                params={"part": "snippet", "id": category},
                timeout=60,
            )
        ).json()
        if not any(
            item.get("snippet", {}).get("assignable") for item in data.get("items", [])
        ):
            raise ValueError("Category is not assignable")

    def start(self, video, source):
        snippet = {
            "title": video.title,
            "description": video.description,
            "categoryId": video.category_id,
            "tags": video.tags,
        }
        for field, remote in [
            ("default_language", "defaultLanguage"),
            ("default_audio_language", "defaultAudioLanguage"),
        ]:
            if getattr(video, field):
                snippet[remote] = getattr(video, field)
        body = {"snippet": snippet, "status": status_fields(video)}
        response = checked(
            self.http.post(
                UPLOAD,
                params={
                    "uploadType": "resumable",
                    "part": "snippet,status",
                    "notifySubscribers": str(video.notify_subscribers).lower(),
                },
                json=body,
                headers={
                    "X-Upload-Content-Length": str(source["size"]),
                    "X-Upload-Content-Type": source["mimeType"],
                },
                timeout=60,
            )
        )
        return session_url(response.headers["Location"])

    def transfer(self, job, path, save):
        url = session_url(job["session"])
        size = int(job["source"]["size"])
        response = self.http.put(
            url,
            data=b"",
            headers={"Content-Length": "0", "Content-Range": f"bytes */{size}"},
            timeout=120,
            allow_redirects=False,
        )
        previous_offset = None
        while True:
            if response.status_code in (200, 201):
                video_id = response.json().get("id")
                if not video_id:
                    raise NeedsReview("Upload completion response missing video ID")
                job.update(video_id=video_id, stage="uploaded")
                save()
                return
            if response.status_code in (404, 410):
                raise NeedsReview(
                    "Upload session unavailable; reconcile before retrying"
                )
            checked(response)
            if response.status_code != 308:
                raise NeedsReview("Unexpected upload session response")
            match = re.fullmatch(r"bytes=0-(\d+)", response.headers.get("Range", ""))
            offset = int(match.group(1)) + 1 if match else 0
            if "Range" in response.headers and not match or offset >= size:
                raise NeedsReview("Unexpected upload offset")
            if previous_offset is not None and offset <= previous_offset:
                raise ApiError(503, "upload_made_no_progress")
            previous_offset = offset
            retry = response.headers.get("Retry-After")
            if retry:
                try:
                    seconds = (
                        int(retry)
                        if retry.isdigit()
                        else max(
                            0,
                            int(
                                (
                                    parsedate_to_datetime(retry) - datetime.now(UTC)
                                ).total_seconds()
                            ),
                        )
                    )
                except (ValueError, TypeError):
                    seconds = 30
                if seconds > 0:
                    raise ApiError(429, "upload_retry_after", seconds)
            job["offset"] = offset
            save()
            if not path.exists():
                raise NeedsReview(
                    "Resumable upload cache missing; session is still incomplete"
                )
            with path.open("rb") as stream:
                stream.seek(offset)
                chunk = stream.read(CHUNK)
            if not chunk:
                raise NeedsReview("Cached video is incomplete")
            response = self.http.put(
                url,
                data=chunk,
                headers={
                    "Content-Type": job["source"]["mimeType"],
                    "Content-Range": f"bytes {offset}-{offset + len(chunk) - 1}/{size}",
                },
                timeout=120,
                allow_redirects=False,
            )

    def inspect(self, video_id):
        data = checked(
            self.http.get(
                f"{ROOT}/videos",
                params={"part": "status,processingDetails", "id": video_id},
                timeout=60,
            )
        ).json()
        if not data.get("items"):
            raise NeedsReview("Known YouTube video no longer accessible")
        return data["items"][0]

    def schedule(self, video_id, video):
        status = status_fields(video)
        status["publishAt"] = video.publish_at.isoformat()
        checked(
            self.http.put(
                f"{ROOT}/videos",
                params={"part": "status"},
                json={"id": video_id, "status": status},
                timeout=60,
            )
        )


def status_fields(video):
    return {
        "privacyStatus": "private",
        "selfDeclaredMadeForKids": video.made_for_kids,
        "containsSyntheticMedia": video.contains_synthetic_media,
        "license": video.license,
        "embeddable": video.embeddable,
    }
