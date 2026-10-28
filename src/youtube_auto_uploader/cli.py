import argparse
import time
from pathlib import Path

import requests

from . import google_auth
from .drive import ApiError, Drive
from .models import Settings, Story
from .runner import Runner
from .stats import export_stats, stats_targets
from .storage import Store, parse_json
from .youtube import YouTube


def export_command(settings, base, week):
    http = None
    persist = None
    try:
        if not stats_targets(settings):
            raise ValueError(
                "Set stats_drive_folder_id or stats_channels before export-stats"
            )
        missing = google_auth.missing_scopes(base)
        if missing:
            raise ValueError(
                "Saved OAuth token is missing scopes. Run login again to grant: "
                + ", ".join(missing)
            )
        http, persist = google_auth.session(base)
        exported = export_stats(settings, base, http, week=week)
    except (ValueError, ApiError) as error:
        print(f"Error: {error}")
        return 1
    finally:
        if persist:
            persist()
        if http:
            http.close()
    for item in exported:
        print(
            f"Exported {item['videos']} videos for {item['channel_id']} "
            f"week {item['week']} to Drive file {', '.join(item['file_ids'])}."
        )
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Upload Drive story pairs and schedule publication"
    )
    parser.add_argument("--config", type=Path, default=Path("config.json"))
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("login", "init-state", "status"):
        commands.add_parser(name)
    validate = commands.add_parser("validate")
    validate.add_argument(
        "--story", type=Path, help="Validate a local story config without Google access"
    )
    validate.add_argument(
        "--remote",
        action="store_true",
        help="Read and validate Drive stories without uploading",
    )
    run = commands.add_parser("run")
    run.add_argument("--once", action="store_true")
    export = commands.add_parser("export-stats")
    export.add_argument(
        "--week",
        help="ISO week file to overwrite, such as 2026-W44. Defaults to the current Pacific week.",
    )
    args = parser.parse_args()
    base = args.config.resolve().parent
    try:
        if args.command == "login":
            google_auth.login(base)
            print("OAuth credentials saved. Set youtube_channel_id before running.")
            return 0
        settings = Settings.model_validate(
            parse_json(args.config.read_text(encoding="utf-8-sig"))
        )
        if args.command == "export-stats":
            return export_command(settings, base, args.week)
        store = Store(base / settings.state_path)
        if args.command == "validate":
            if args.story:
                Story.model_validate(
                    parse_json(args.story.read_text(encoding="utf-8-sig"))
                )
            if args.remote:
                http, persist = google_auth.session(base)
                try:
                    YouTube(http).verify_channel(settings.youtube_channel_id)
                    drive = Drive(http)
                    for folder in drive.children(settings.drive_folder_id):
                        if folder["mimeType"] == "application/vnd.google-apps.folder":
                            drive.story(folder["id"])
                            print(f"Valid story: {folder['id']}")
                finally:
                    persist()
                    http.close()
            print("Validation passed (no uploads).")
            return 0
        with store.lock():
            if args.command == "init-state":
                store.initialize()
                print("Initialized new upload history.")
                return 0
            if args.command == "status":
                state = store.load()
                for sid, story in state["stories"].items():
                    for role, job in story["jobs"].items():
                        url = (
                            f"https://www.youtube.com/watch?v={job['video_id']}"
                            if job.get("video_id")
                            else ""
                        )
                        print(
                            f"{sid}/{role}: {job['stage']} {url} {job.get('error', '')}"
                        )
                if not state["stories"]:
                    print("No stories started yet.")
                return 0
            store.load()  # Fail before connecting if history is missing/corrupt.
            http, persist = google_auth.session(base)
            try:
                youtube = YouTube(http)
                youtube.verify_channel(settings.youtube_channel_id)
                runner = Runner(settings, base, store, Drive(http), youtube)
                failures = 0
                while True:
                    try:
                        runner.cycle()
                        failures = 0
                    except (ApiError, requests.RequestException) as error:
                        failures += 1
                        transient = isinstance(
                            error, requests.RequestException
                        ) or error.status in {429, 500, 502, 503, 504}
                        if args.once or not transient or failures >= 10:
                            raise
                        print(
                            "Discovery temporarily unavailable; retrying with saved history intact."
                        )
                    persist()
                    if args.once:
                        break
                    time.sleep(
                        max(settings.poll_seconds, min(3600, 30 * 2**failures))
                        if failures
                        else settings.poll_seconds
                    )
            finally:
                persist()
                http.close()
        return 0
    except KeyboardInterrupt:
        print("Stopped. Saved upload sessions will be reconciled on restart.")
        return 130
    except Exception as error:
        # Avoid dumping credential-bearing request URLs or remote validation inputs.
        print(
            f"Error ({type(error).__name__}). Check configuration, credentials, and saved status; no history was reset."
        )
        if isinstance(error, FileNotFoundError):
            print(
                "Copy examples/config.example.json to config.json and set your channel ID."
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
