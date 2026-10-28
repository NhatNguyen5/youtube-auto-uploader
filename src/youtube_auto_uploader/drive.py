import hashlib
import json
import os
import shutil
import uuid
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from .models import Story, require_drive_id
from .storage import parse_json

ROOT = "https://www.googleapis.com/drive/v3/files"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
FIELDS = "id,name,mimeType,size,md5Checksum,version,parents,capabilities(canDownload)"
STATS_EXPORTER = "youtube-auto-uploader-stats"


class ApiError(Exception):
    def __init__(self, status, reason="request_failed", retry_after=0):
        self.status, self.reason, self.retry_after = status, reason, retry_after
        super().__init__(f"Google API HTTP {status}: {reason}")


def checked(response):
    if response.status_code >= 400:
        reason = "request_failed"
        try:
            reason = response.json()["error"]["errors"][0]["reason"]
        except (ValueError, KeyError, IndexError, TypeError):
            pass
        retry = response.headers.get("Retry-After", "0")
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
            seconds = 0
        raise ApiError(response.status_code, reason, seconds)
    return response


class Drive:
    def __init__(self, http):
        self.http = http

    def children(self, folder):
        require_drive_id(folder, "Drive folder ID")
        params = {
            "q": f"'{folder}' in parents and trashed = false",
            "pageSize": 1000,
            "fields": f"nextPageToken,incompleteSearch,files({FIELDS})",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        result = []
        while True:
            data = checked(self.http.get(ROOT, params=params, timeout=60)).json()
            if data.get("incompleteSearch"):
                raise ValueError(
                    "Drive returned an incomplete search; refusing partial discovery"
                )
            result.extend(data.get("files", []))
            if not data.get("nextPageToken"):
                return result
            params["pageToken"] = data["nextPageToken"]

    def metadata(self, file_id):
        return checked(
            self.http.get(
                f"{ROOT}/{file_id}",
                params={"fields": FIELDS, "supportsAllDrives": "true"},
                timeout=60,
            )
        ).json()

    def story(self, folder):
        files = self.children(folder)

        def unique(name):
            matches = [f for f in files if f["name"] == name]
            if len(matches) != 1:
                raise ValueError(f"Expected one {name}; found {len(matches)}")
            return matches[0]

        config = unique("config.json")
        content = bytearray()
        with checked(
            self.http.get(
                f"{ROOT}/{config['id']}",
                params={"alt": "media", "supportsAllDrives": "true"},
                stream=True,
                timeout=60,
            )
        ) as response:
            for chunk in response.iter_content(65536):
                content.extend(chunk)
                if len(content) > 1024 * 1024:
                    raise ValueError("Story config exceeds 1 MiB")
        story = Story.model_validate(parse_json(content.decode("utf-8-sig")))
        sources = {role: unique(video.filename) for role, video in story.videos.items()}
        for source in sources.values():
            if (
                not source.get("mimeType", "").startswith("video/")
                or int(source.get("size", 0)) <= 0
            ):
                raise ValueError("Source must be a nonempty video file")
            if not source.get("capabilities", {}).get("canDownload", False):
                raise ValueError("Source does not permit downloads")
        return story, sources

    def download(self, source, target):
        target.parent.mkdir(parents=True, exist_ok=True)
        size = int(source["size"])
        if shutil.disk_usage(target.parent).free < size + 64 * 1024 * 1024:
            raise ValueError("Insufficient cache disk space")
        if fingerprint(self.metadata(source["id"])) != fingerprint(source):
            raise ValueError("Drive source changed since snapshot")
        partial = target.with_suffix(".part")
        digest = hashlib.md5()
        total = 0
        with (
            checked(
                self.http.get(
                    f"{ROOT}/{source['id']}",
                    params={"alt": "media", "supportsAllDrives": "true"},
                    stream=True,
                    timeout=120,
                )
            ) as response,
            partial.open("wb") as stream,
        ):
            for chunk in response.iter_content(1024 * 1024):
                total += len(chunk)
                if total > size:
                    raise ValueError("Source exceeds expected size")
                stream.write(chunk)
                digest.update(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        if total != size or (
            source.get("md5Checksum") and digest.hexdigest() != source["md5Checksum"]
        ):
            raise ValueError("Downloaded video integrity check failed")
        if fingerprint(self.metadata(source["id"])) != fingerprint(source):
            raise ValueError("Drive source changed during download")
        os.replace(partial, target)
        return digest.hexdigest()

    def files_named(self, folder, name):
        require_drive_id(folder, "Drive folder ID")
        quoted_name = name.replace("\\", "\\\\").replace("'", "\\'")
        quoted_folder = folder.replace("\\", "\\\\").replace("'", "\\'")
        params = {
            "q": (
                f"name = '{quoted_name}' and '{quoted_folder}' in parents "
                "and trashed = false"
            ),
            "pageSize": 1000,
            "fields": "nextPageToken,files(id,name,appProperties)",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        found = []
        while True:
            data = checked(self.http.get(ROOT, params=params, timeout=60)).json()
            found.extend(data.get("files", []))
            if not data.get("nextPageToken"):
                return found
            params["pageToken"] = data["nextPageToken"]

    def upsert_stats_file(self, folder, name, content):
        """Create or overwrite the weekly stats CSV. Never adds a second file."""
        matches = self.files_named(folder, name)
        ours = [
            item
            for item in matches
            if (item.get("appProperties") or {}).get("exporter") == STATS_EXPORTER
        ]
        our_ids = {item["id"] for item in ours}
        foreign = [item for item in matches if item["id"] not in our_ids]
        if foreign and not ours:
            raise ValueError(
                f"{name} already exists in the stats folder and was not created by export-stats"
            )
        metadata = {
            "name": name,
            "mimeType": "text/csv",
            "appProperties": {"exporter": STATS_EXPORTER},
        }
        if ours:
            return [
                self._upload_csv(item["id"], metadata, content)
                for item in sorted(ours, key=lambda item: item["id"])
            ]
        metadata["parents"] = [folder]
        return [self._upload_csv(None, metadata, content)]

    def _upload_csv(self, file_id, metadata, content):
        boundary = f"stats{uuid.uuid4().hex}"
        body = b"".join(
            [
                f"--{boundary}\r\n".encode(),
                b"Content-Type: application/json; charset=UTF-8\r\n\r\n",
                json.dumps(metadata).encode("utf-8"),
                f"\r\n--{boundary}\r\n".encode(),
                b"Content-Type: text/csv; charset=UTF-8\r\n\r\n",
                content,
                f"\r\n--{boundary}--\r\n".encode(),
            ]
        )
        headers = {"Content-Type": f"multipart/related; boundary={boundary}"}
        params = {
            "uploadType": "multipart",
            "supportsAllDrives": "true",
            "fields": "id",
        }
        if file_id:
            response = self.http.patch(
                f"{UPLOAD}/{file_id}",
                params=params,
                data=body,
                headers=headers,
                timeout=120,
            )
        else:
            response = self.http.post(
                UPLOAD,
                params=params,
                data=body,
                headers=headers,
                timeout=120,
            )
        return checked(response).json()["id"]


def fingerprint(source):
    return tuple(source.get(k) for k in ("id", "version", "size", "md5Checksum"))
