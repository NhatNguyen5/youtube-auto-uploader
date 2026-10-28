# youtube-auto-uploader

Discover story folders in MidnightUntold, download their long and short videos, upload privately to YouTube, and schedule publication using each story's config.json.

The initial implementation is available. Automated tests use fake Google services; live OAuth, download, upload, and publication still need to be verified with your account.

## Setup

1. Install uv and run `uv sync --locked` in this project.
2. Copy `examples/config.example.json` to `config.json` if it does not exist. Set `youtube_channel_id` to your channel's ID (not its handle). The MidnightUntold Drive root ID is already configured.
3. Enable Google Drive API and YouTube Data API in your Google Cloud project, configure OAuth consent, and create a **Desktop app** OAuth client. If your consent screen is in testing, add your Google account as a test user.
4. Fill `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` in `.env`. See [.env.example](.env.example). Login saves refreshable credentials to `GOOGLE_TOKEN_PATH`, defaulting to `secrets/token.json`. Environment variables take precedence over .env. Keep this project in a directory accessible only to your user account.
5. Run the commands below. `init-state` is for a new installation only; never delete history to resolve a failure.

```powershell
uv run youtube-auto-uploader validate
uv run youtube-auto-uploader login
uv run youtube-auto-uploader init-state
uv run youtube-auto-uploader validate --remote
uv run youtube-auto-uploader run
```

`validate --remote` reads Drive configs and checks your channel without uploading. `run` performs real uploads for enabled, due stories. Stop with Ctrl+C; saved sessions are reconciled on restart. `run --once` performs one pass and exits, so it may leave processing or schedule confirmation for a later pass. Use continuous `run` for unattended publication monitoring.

```powershell
uv run youtube-auto-uploader status
uv run youtube-auto-uploader validate --story examples/story-config.example.json
```

The runner holds an exclusive state lock. Stop it before using `status`; concurrent processes cannot change its history. Paths in config and .env are relative to the config directory. An alternate config can be selected before the subcommand: `uv run youtube-auto-uploader --config C:\path\config.json status`.

## Drive layout

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

Copy [the story template](examples/story-config.example.json) into each story folder as config.json. Fill both videos' metadata and future publication times with explicit timezone offsets, then set `enabled` to true. Each filename can be changed explicitly in the story config. Both files must be present and uniquely named before either video starts. Extra files are ignored. Folders can be renamed without creating new jobs.

The default preparation window is three hours. The machine must be awake and online to download, upload, and submit the schedule. YouTube handles publication after accepting it. Processing time varies, so increase `upload_lead_minutes` for large videos. The short filename identifies your intended variant; YouTube determines Shorts classification.

Each variant has its own saved upload session, video ID, and publication status. A successful long upload is not repeated when the short fails. Once a story becomes due and is snapshotted, later remote config edits do not affect either variant. Story folders are left unchanged; the app does not move folders or add per-story marker files.

## Upload log

Set `upload_log_folder_id` in `config.json` to a Drive folder the other reader can access. See [examples/config.example.json](examples/config.example.json). Leave it empty to skip the log. The folder id is not a secret and is not read from `.env`. After a finished upload attempt, the runner appends one line to `uploads.jsonl` in that folder. It creates the file on first use. Do not replace it with a Google Doc of the same name.

The file is JSONL (one JSON object per line). Another program that can only read Drive can download it and parse each line. A Google Sheet would need the Sheets API, and CSV would break on error text that contains commas or quotes. Each line contains:

- `date` — ISO-8601 UTC time captured on the first attempt to record that job
- `channel` — YouTube channel ID from config, so a later kids channel can share this file
- `source_file` — Drive file name, such as `long.mp4`
- `source_file_id` — Drive file ID, because every story reuses the same file names
- `video_id` — YouTube video ID, or empty when the attempt never received one
- `status` — `uploaded`, `failed`, or `needs_review`
- `error` — empty when `status` is `uploaded`
- `job_id` — `{channel}/{story folder id}/{long|short}`, the idempotency key

```json
{"schema_version":1,"date":"2026-10-17T00:00:00+00:00","channel":"UCchannel","source_file":"long.mp4","source_file_id":"1abc","video_id":"yt-video-id","status":"uploaded","error":"","job_id":"UCchannel/storyFolderId/long"}
```

`data/state.json` decides whether YouTube already has the video. A saved video ID is reused and is never uploaded again. `job_id` decides whether the log already has that attempt. Running the same job again, or crashing after Drive stores the line and before local confirmation, does not add a second line. The first outcome for that job stays as written.

The log write happens only after that local result is saved. If Drive rejects the append, the YouTube video stays saved, `status` prints `upload-log: ...`, and a later pass retries the same line. Transfers still in progress, and missed deadlines that never uploaded, are not logged. `uploaded` includes videos that are already scheduled or public. A processing failure noticed after the line was written remains in local state instead of adding another line.

Creating the file needs the Drive file scope (`drive.file`) in addition to read-only Drive. Run `uv run youtube-auto-uploader login` again after upgrading. An older token keeps uploading; log writes fail until that login, then the pending line is written. No new value is added to `.env`.

## Recovery and limitations

- Preserve `data/state.json` and the cached videos. State is bound to the configured Drive root and YouTube channel. Missing/corrupt history stops the runner instead of treating old videos as new.
- Network failures retry with backoff. An expired session, uncertain completion, permission error, or exhausted retry budget becomes `needs_review` and stops automatic work for that video. Inspect the channel and retained state before recovery; an interactive reconciliation command is not implemented yet.
- Past deadlines become `missed`; the app does not publish immediately as a fallback. Already accepted schedules are reconciled after their deadline.
- Active snapshots are immutable. Remote config edits are not currently compared/reported after snapshotting. Rescheduling/cancellation and manually associating previous uploads are not implemented.
- This installation only knows uploads recorded in its state. Copying a story folder or deleting history can cause duplicates. Exactly-once upload cannot be guaranteed across a network failure and local state write.
- A failed upload-log write does not remove a saved YouTube ID and does not upload that video again. The next run retries the log line. `status` shows `upload-log:` until the append succeeds.
- Cache cleanup is manual in this version. Keep unfinished/ambiguous uploads' cache files; completed uploads' files can be removed after stopping the runner and verifying their saved IDs.
- Transferring one video blocks the single worker until it completes or errors. Processing checks run between transfers; allow ample preparation time for all due videos.
- Thumbnails, playlists, captions, recursive folders, and Drive shortcuts are not supported yet.

## Windows startup

After a live test succeeds, create a Windows Task Scheduler task under the same user account. Use the absolute path from `Get-Command uv`, arguments `run --project "C:\Users\nhatn\Projects\youtube-auto-uploader" youtube-auto-uploader --config "C:\Users\nhatn\Projects\youtube-auto-uploader\config.json" run`, and set the project directory as **Start in**. Configure restart on failure and prevent overlapping instances. No task is installed automatically. Initial OAuth login must be completed interactively first.

## Development

```powershell
uv run pytest -q
uv run ruff check src tests
uv run ruff format --check src tests
```

See [the design](docs/design.md), [story config](examples/story-config.example.json), and [application settings](examples/config.example.json). Credentials, local settings, state, and downloaded videos are excluded from Git; uv.lock is committed with the source when you commit changes.
