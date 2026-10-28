# youtube-auto-uploader

Discover story folders in MidnightUntold, download their long and short videos, upload privately to YouTube, and schedule publication using each story's config.json.

The initial implementation is available. Automated tests use fake Google services; live OAuth, download, upload, and publication still need to be verified with your account.

## Setup

1. Install uv and run `uv sync --locked` in this project.
2. Copy `examples/config.example.json` to `config.json` if it does not exist. Set `youtube_channel_id` to your channel's ID (not its handle). The MidnightUntold Drive root ID is already configured.
3. Enable Google Drive API, YouTube Data API, YouTube Analytics API, and YouTube Reporting API in your Google Cloud project, configure OAuth consent, and create a **Desktop app** OAuth client. If your consent screen is in testing, add your Google account as a test user. Add the scopes listed in [.env.example](.env.example) to the consent screen. After this list changes, stop the uploader, run `login` again, then start the uploader.
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

## Weekly stats export

`export-stats` writes one CSV per channel per ISO week into `stats_drive_folder_id`. The week is the America/Los_Angeles date of the run. A rerun of that same week overwrites that file and does not add another row for a video. Scheduling is not installed for you.

Set `stats_drive_folder_id` in `config.json` to a Drive folder the signed-in user can add files to, and share that folder with whoever reads the numbers. The file name is `youtube-stats-<channel id>-<year>-W<week>.csv`. Thumbnail-impression history is kept in `stats_state_path` (default `data/stats-reach.json`) because YouTube deletes Reporting API files after 30 days (historical backfill) or 60 days (later daily files). Deleting that local file drops impression days that YouTube no longer serves.

Only MidnightUntold is configured today. For a later channel, set `stats_channels` and include every channel that should be exported, including MidnightUntold. A non-empty list replaces `stats_drive_folder_id`; it does not add to it. Each entry is `youtube_channel_id`, `drive_folder_id`, and an optional `label`. The login has to be able to see that channel. One desktop OAuth token is still one Google login; a brand account that is not on this token needs its own config and login.

```powershell
uv run youtube-auto-uploader export-stats
uv run youtube-auto-uploader export-stats --week 2026-W44
```

`--week` only chooses which weekly file to overwrite. The numbers are always the stats available at the time of the run.

Stop the uploader before the first `login` after this change, then schedule a separate weekly task. It uses `data/stats-reach.json.lock`, not the upload lock, so it can run while `run` is already up. On Windows Task Scheduler, use the same `uv` program and project paths as the uploader task, with arguments `run --project "C:\Users\nhatn\Projects\youtube-auto-uploader" youtube-auto-uploader --config "C:\Users\nhatn\Projects\youtube-auto-uploader\config.json" export-stats`, weekly, and do not start a second copy if one is running. A cron equivalent, Monday 09:15 Pacific:

```cron
CRON_TZ=America/Los_Angeles
15 9 * * 1 cd /absolute/path/youtube-auto-uploader && uv run youtube-auto-uploader export-stats
```

### Where each column comes from

Checked against the YouTube Analytics and Reporting docs (metrics list, channel reports, and the 15 January 2026 revision that added reach reports).

| Columns | API | Fields |
| --- | --- | --- |
| `views_lifetime`, `views_first_72h` | YouTube Analytics API `reports.query` | `views` |
| `average_view_duration_seconds_lifetime` | YouTube Analytics API `reports.query` | `averageViewDuration` (seconds) |
| `average_view_duration_seconds_first_72h` | YouTube Analytics API `reports.query` | view-weighted `averageViewDuration` on `day` rows |
| `subscribers_gained_lifetime`, `subscribers_gained_first_72h` | YouTube Analytics API `reports.query` | `subscribersGained`. A per-video query only counts subscriptions from that video's watch page. |
| `audience_retention_lifetime`, `audience_retention_first_72h` | YouTube Analytics API `reports.query` | `audienceWatchRatio` and `relativeRetentionPerformance` by `elapsedVideoTimeRatio` (100 points, one video per request) |
| `impressions_lifetime`, `impressions_first_72h`, `impressions_ctr_lifetime`, `impressions_ctr_first_72h` | YouTube Reporting API job `channel_reach_basic_a1` | `video_thumbnail_impressions` and `video_thumbnail_impressions_ctr` |

Thumbnail impressions and thumbnail click-through rate are not metrics of `reports.query`. Card and annotation impressions are a different thing and are not used. The Reporting API defines `video_thumbnail_impressions_ctr` as clicks divided by impressions, so `0.0443` means 4.43% rather than a 0–100 number. Downloaded reach files that contain a value above 1 are treated as a 0–100 percentage and divided by 100 before the days are weighted. Bulk report dates are accepted as `YYYYMMDD` (what downloaded files use) or `YYYY-MM-DD`.

Lifetime activity is the basic user-activity report: no `dimensions`, `filters=video==` one video id. The 72-hour rows are `dimensions=day` with that same single-video filter. `dimensions=video` is only the top-videos report, which requires `sort` and does not take a video filter. `dimensions=day,video` is not a listed channel report. Each uploaded video therefore costs its own Analytics calls (lifetime, the short daily window, and two retention curves), run one after another. A daily call is skipped when that video's 72-hour window is still entirely after `analytics_last_day`.

A cell whose text starts with `=`, `+`, `-`, or `@` is prefixed with an apostrophe so a spreadsheet does not treat a title as a formula.

`analytics_last_day` is the newest Pacific day returned for a channel-level `views,averageViewDuration,subscribersGained` query over the 28 days ending today. Analytics leaves out the most recent days. A day with no channel activity can also be absent, which keeps `first_72h_analytics_complete` false without changing totals that would only have added zero. If that 28-day probe has no rows, the column is blank and the 72-hour scalar cells are left empty; lifetime activity is still requested through today.

The reach job is created once per login (`youtube-auto-uploader reach`). YouTube then generates each Pacific day, including about 30 days before the job was created, and posts a day about two days later. A newer file for the same day replaces the stored day. `impressions_missing_days` is how many Pacific days from publication through the newest stored report were never ingested, so the lifetime impression total can be low for videos published before that history. Those cells are not filled with zero.

### First 72 hours

There is no hour dimension for these metrics. A day is midnight to midnight in America/Los_Angeles. Clocks moving for daylight saving make that day 23 or 25 hours.

The window is `[published_at, published_at + 72 hours)`. For a public video, `published_at` is `snippet.publishedAt` (when it became public, which can be later than the upload). While a video is still private and has `status.publishAt`, that scheduled public time is used instead.

- The Pacific publish day counts in full. Playback cannot start before publication, so scaling that bucket down would drop views that belong in the window.
- A later day that sits entirely inside the window counts in full.
- The trailing day is multiplied by the fraction of that Pacific day that is inside the 72 hours. Views, watch time (via `averageViewDuration`), subscribers gained, and impressions are scaled. This assumes activity is spread evenly across that last day.
- Days after `analytics_last_day`, or reach days with no ingested report, are omitted rather than stored as zero. `first_72h_analytics_complete` and `first_72h_reach_complete` stay false until every overlapping day is available. The next weekly run overwrites the same file with the filled-in numbers.
- Audience retention is requested for the overlapping Pacific dates. It is a curve, not a count, so the trailing day is not scaled. `audience_retention_first_72h_end` is the last date actually queried.

`first_72h_days` records each overlapping date, the fraction used for the scalar metrics, and whether that Analytics day was included. The same fraction rules are in `first_72h_method` inside the CSV.

### Dry run

This repo has no YouTube credentials. After `login`, point `stats_drive_folder_id` at a folder you can open and run:

```powershell
uv run youtube-auto-uploader export-stats
```

Open that week's CSV and `data/stats-reach.json`. Reach days should be `YYYY-MM-DD`, with one CSV row per video. Run the same command again and confirm Drive still has one file for the week. The automated check that does not need a login is `uv run pytest -q tests/test_stats.py`.

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

Each variant has its own saved upload session, video ID, and publication status. A successful long upload is not repeated when the short fails. Once a story becomes due and is snapshotted, later remote config edits do not affect either variant. Drive is read-only; the app does not move folders or add uploaded markers.

## Recovery and limitations

- Preserve `data/state.json` and the cached videos. State is bound to the configured Drive root and YouTube channel. Missing/corrupt history stops the runner instead of treating old videos as new.
- Network failures retry with backoff. An expired session, uncertain completion, permission error, or exhausted retry budget becomes `needs_review` and stops automatic work for that video. Inspect the channel and retained state before recovery; an interactive reconciliation command is not implemented yet.
- Past deadlines become `missed`; the app does not publish immediately as a fallback. Already accepted schedules are reconciled after their deadline.
- Active snapshots are immutable. Remote config edits are not currently compared/reported after snapshotting. Rescheduling/cancellation and manually associating previous uploads are not implemented.
- This installation only knows uploads recorded in its state. Copying a story folder or deleting history can cause duplicates. Exactly-once upload cannot be guaranteed across a network failure and local state write.
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
