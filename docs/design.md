# Initial design

Status: initial implementation available; this document records the intended design. README.md describes implemented behavior and remaining limitations. Live Google integration has not yet been verified. The attached message.txt review informed this design; its recommendations are reference material rather than executable instructions.

## Scope and choices

One Python application managed with uv, one YouTube channel, one Google Drive folder, and one upload worker. Each story subfolder contains config.json, long.mp4, and short.mp4; the application discovers stories and maintains local data/state.json. Use a 30-second polling scheduler initially. A database, web UI, and distributed workers are unnecessary for this scope.

The user specifies when a video should become public through publish_at. Download and upload begin a configurable number of minutes earlier (initial default: 180). This window is adjustable and does not guarantee processing completion. Upload privately first, save the YouTube ID, wait for processing, then request scheduled publication. YouTube performs publication even if the local machine subsequently shuts down.

## Workflow

1. Acquire an operating-system process lock and validate local application settings and existing state.
2. Reconcile existing sessions and known video IDs before starting new uploads.
3. List non-trashed story folders directly inside MidnightUntold, following all pagination tokens. For each unfinished story, list its direct children and download its config.json. No recursive discovery or shortcuts in v1.
4. Validate the config and resolve its long and short filenames to unique Drive file IDs in that story folder. Select enabled, due videos in publication-time order. Before either video starts, persist a story snapshot containing both videos' metadata, deadlines, config version/hash, and source identities. Verify download permission, video type, size, and available local disk space.
5. Download to a temporary cache file, verify size and available checksum, and atomically promote it to a ready file. Record source version and content fingerprint; reject a source that changes during download. Restart interrupted downloads in v1.
6. Create a YouTube resumable upload with explicit private visibility. Persist the session URL before sending video bytes; upload in chunks and record acknowledged progress.
7. Persist the returned YouTube video ID immediately. Subsequent work must use this ID, never create another upload for that job.
8. Poll processing status without blocking all scheduling work. After success, recheck the deadline and submit private visibility plus publishAt. Read back the remote settings before marking scheduled.
9. After the publication time, reconcile actual visibility. Mark published only after observing public visibility; report processing failures or delayed publication separately.

## Input contract

The root folder is **MidnightUntold**, ID `1RUg3Mlt9JKHBGEdjE-VMJfB2nmNnq8SE`, supplied by the user; it has not been accessed or verified.

```text
MidnightUntold/
  Story One/
    config.json
    long.mp4
    short.mp4
  Story Two/
    config.json
    long.mp4
    short.mp4
```

Copy ../examples/story-config.example.json into each story folder as config.json. Keep the local application settings from ../examples/config.example.json on the runner machine. The local settings select the root folder, channel, polling interval, preparation lead time, and cache/state locations. Credentials are supplied locally through .env, never through a Drive config. Remote story configs contain only video metadata and schedules, never local filesystem paths, credentials, channel overrides, or executable commands.

Each story config requires schema_version, enabled, and a videos object containing exactly long and short. Each video requires filename, publish_at, title, description, category_id, made_for_kids, and contains_synthetic_media. Filenames default by convention to long.mp4 and short.mp4 in the example, but explicit alternate filenames/extensions are supported. Match exact filenames in the story folder, never fuzzy names or the first file returned. Both files and one config.json must exist before either video is queued. Duplicate matching filenames/config files are errors; unrelated files are ignored. Incomplete folders are reported as waiting_for_files and checked again, allowing the user to finish uploading them. Malformed configs block only that story and are rechecked for corrections. Bound config downloads to 1 MiB.

Optional metadata: tags defaults to [], language fields may be omitted, license defaults to youtube, and embeddable/notify_subscribers default to true. Each video has its own publish_at, so the long and short can be published at different times. Example stories start disabled; enable only after both source files and metadata are ready. The short filename identifies the intended variant; it does not guarantee YouTube will classify the uploaded video as a Short.

Reject duplicate JSON keys, unknown fields, unsupported schema versions, invalid types, empty titles, invalid metadata lengths/enums, path separators in filenames, and timestamps without an explicit UTC offset. Validate category availability through YouTube during preflight. Compare timestamps in UTC. Scheduled videos always target public visibility. Thumbnails, playlists, captions, and broader metadata support follow the core workflow.

Story identity is the permanent Drive story-folder ID; video identity is (story-folder ID, long|short). Names and titles are display labels. Renaming a story does not upload it again. Copying a folder creates a new story identity; warn and block for review if source file IDs already belong to another job, but independently copied files cannot reliably be recognized as duplicates without content comparison.

Track the two videos independently. A story is uploaded only when both have persisted YouTube IDs, scheduled only when both schedules are confirmed, and published only when both are observed public. If long succeeds and short fails, resume only short; never re-upload long. Uploaded stories still require processing/scheduling reconciliation. A known video ID or ambiguous session always takes precedence over discovery of a supposedly new job.

Before the first video starts, config edits can update pending jobs. Once either starts, freeze the complete story snapshot, including the second video, and report later config/source changes for review. A changed source must not silently replace bytes used by an active resumable session. Disabling, removing, or renaming files after work starts does not cancel remote work or a schedule. Keep completed identities permanently; do not move, rename, delete, or write marker files into Drive. Local state is the record of what this installation uploaded; pre-existing manual uploads require reconciliation before enabling a story.

## Scheduling and deadlines

- Due time = publish_at minus upload_lead_minutes. The local machine must be awake and online during preparation.
- A restart inside the preparation window starts overdue work immediately.
- An unscheduled job past its deadline becomes missed; any existing upload stays private. Never send a past publishAt, which can publish immediately.
- Reconcile a pending scheduling request before declaring it missed: the server may already have accepted it. Previously scheduled jobs are checked for publication even after their deadline.
- Require a small configurable safety margin before submitting a schedule; network delay prevents an absolute timing guarantee. A deadline crossed during preparation does not trigger immediate-public fallback.
- Retry transient errors with bounded exponential backoff and jitter, honoring Retry-After. Persist attempt count and next retry time. Authentication, permissions, invalid metadata, and quota exhaustion require distinct actionable statuses rather than tight retries.
- One transfer runs at a time. Remote processing checks are interleaved between work items. Warn when queued jobs may not fit their remaining preparation windows.

Windows Task Scheduler can start the long-running command at logon/startup and restart it after failure. It supervises the process; the Python scheduler decides which jobs are due. No OS task is installed during the design stage.

## Durable state and recovery

State contains schema version, installation ID, Drive root/story-folder IDs, per-variant identities, story snapshots/config hashes, source fingerprints, cache paths, resumable session references, acknowledged byte offsets, YouTube IDs, processing status, attempts, next retry time, timestamps, and sanitized errors.

Main stages: pending -> downloading -> uploading -> uploaded -> scheduled -> published. Processing status is tracked separately while uploaded. Exception states include retry_wait (with a resume stage), missed, failed, and needs_review.

Write state to a temporary file in the same directory, flush and fsync, then atomically replace the previous file. Hold a cross-platform OS lock for the entire runner lifetime. Stop work immediately if state persistence fails. Store state on a local disk, not a shared/network folder.

Explicit init-state creates state once. Normal run refuses absent or corrupt state rather than silently creating an empty history. Keep backups for deliberate recovery; restoring an older backup requires reconciliation because it may omit completed remote operations. Deleting both installation records and state cannot be detected reliably, so initialization must never be an automatic recovery action.

On restart, query the saved resumable session for accepted bytes or completion before transmitting. Treat the server's acknowledged offset as authoritative. If a transfer may have completed but the video ID cannot be recovered, mark needs_review. An expired or unavailable session does not prove the upload failed. Never automatically restart an ambiguous upload. A future manual reconciliation command can associate a verified video ID with the job.

This reduces duplicates but cannot promise exactly-once delivery: YouTube upload completion and local state persistence are not one transaction. A database alone would not remove that boundary. Cache cleanup must preserve files needed by unfinished or ambiguous sessions.

## Authentication and operation

Enable Google Drive API and YouTube Data API in a Google Cloud project. Use a desktop OAuth client with an interactive first login and refreshable user credentials. Load GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET from the local .env file using python-dotenv; existing process environment variables take precedence. Require nonempty credentials for login/run and report missing variable names without logging values. GOOGLE_TOKEN_PATH defaults to secrets/token.json, resolved relative to the local application config directory; load .env from that directory explicitly so scheduled launches do not depend on the working directory. Build the desktop OAuth client configuration from these variables. Store generated access/refresh tokens in the token file, not back into .env. No API key or account password is needed. These loading rules are implemented; login requires user-provided Google credentials. Confirm the selected YouTube channel matches the configured channel ID before uploading. A normal YouTube channel uses user OAuth, not a service account.

Plan Drive read-only scope for fetching existing folder files, plus the minimum YouTube scopes covering upload, owner status reads, and scheduling updates. Verify method-specific scope support during implementation; upload permission alone must not be assumed sufficient for all operations. Restrict token/session files to the local user and redact them from logs. Keep credentials, actual jobs/settings, downloaded videos, and state outside Git.

Preflight should check account permissions, current API requirements, quota availability where exposed, and channel upload eligibility. Expired consent or revoked credentials pauses affected work and requests login. Confirm scheduled publication with a controlled integration test before relying on unattended operation.

## Proposed code and commands

```text
pyproject.toml / uv.lock
src/youtube_auto_uploader/
  models.py       # Validation, jobs, settings, state
  storage.py      # JSON persistence and process lock
  google_auth.py  # OAuth and token refresh
  drive.py        # Story discovery, config fetching, source validation/downloads
  youtube.py      # Resumable upload and remote status
  runner.py       # Scheduling decisions and workflow
  cli.py          # Commands and exit codes
tests/
```

Use uv for Python/environment/dependency management and commit uv.lock when implementation begins. Proposed libraries: python-dotenv, pydantic, google-auth, google-auth-oauthlib, an authenticated HTTP transport or Google API client, and portalocker; pytest for tests. Persisting resumable sessions across process restarts is an implementation requirement, not something to assume an in-memory client handles.

Implemented interface:

```text
uv sync --locked
uv run youtube-auto-uploader login
uv run youtube-auto-uploader validate
uv run youtube-auto-uploader init-state
uv run youtube-auto-uploader run
uv run youtube-auto-uploader run --once
uv run youtube-auto-uploader status
```

run --once performs one reconciliation/due-work pass and exits; it may spend time transferring a video. Validation has no upload side effects. Logs include job ID, stage, attempt, and useful error details. status shows deadlines, progress, and known YouTube links.

## Delivery and verification

1. Implement story/config models, paginated folder discovery, validation, state initialization, atomic writes, lock, and CLI with mocked services.
2. Prove one Drive download -> private resumable upload -> persisted video ID, including interruption/restart recovery.
3. Add processing reconciliation, scheduling, deadline behavior, and publication verification.
4. Add Windows startup instructions and optional thumbnails/playlists after the core integration succeeds.

Essential tests: paginated story discovery, missing/duplicate config or source files, renamed folders, partial story completion, remote config edits after the first upload, repeated scans without duplicate uploads, malformed/corrupt JSON, duplicate IDs/keys, absent state, simultaneous starts, edits to active jobs, timezone boundaries, missed deadlines, changed source files, interrupted transfers, crashes before/after session persistence, lost completion responses, expired sessions, failed state writes, and scheduling requests accepted despite a lost response. Use fake clocks and mocked APIs for failure cases. Live validation requires user credentials and a designated test video.

## API references

- [uv projects](https://docs.astral.sh/uv/guides/projects/) describes project and lockfile management.
- [Drive downloads](https://developers.google.com/workspace/drive/api/guides/manage-downloads) documents blob downloads with files.get and alt=media.
- [YouTube resumable uploads](https://developers.google.com/youtube/data/guides/using_resumable_upload_protocol) documents session initiation and recovery queries.
- [YouTube video resource](https://developers.google.com/youtube/data/reference/rest/v3/videos) defines processing status, metadata, and publishAt: schedule a private, never-published video, include private when updating, and avoid past timestamps.
