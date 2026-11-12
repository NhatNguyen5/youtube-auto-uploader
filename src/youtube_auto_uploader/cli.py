import argparse
import signal
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import requests
from pydantic import ValidationError

from . import google_auth
from .drive import ApiError, Drive
from .models import Settings, Story
from .runner import Runner, poll_retry
from .stats import export_stats, stats_targets
from .status import ConfigError, format_status_line, public_error
from .storage import Store, parse_json
from .youtube import YouTube


def report_remote_stories(drive, root_id, report=print):
    """Check each story folder. Return how many were invalid.

    One bad folder does not stop the rest. Only ConfigError and pydantic
    ValidationError are folder failures. Anything else propagates.
    """
    invalid = 0
    for folder in drive.children(root_id):
        if folder.get("mimeType") != "application/vnd.google-apps.folder":
            continue
        folder_id = folder.get("id", "")
        name = folder.get("name") or ""
        try:
            drive.story(folder_id)
        except (ValidationError, ConfigError) as error:
            invalid += 1
            report(f"Invalid story: {folder_id} ({name}): {public_error(error)}")
        else:
            report(f"Valid story: {folder_id}")
    return invalid


def export_command(settings, base, week):
    http = None
    persist = None
    try:
        folder = settings.stats_drive_folder_id
        if (
            isinstance(folder, str)
            and folder.startswith("REPLACE_WITH_")
            and not settings.stats_channels
        ):
            raise ConfigError(
                "Set stats_drive_folder_id in config.json. "
                "Replace the REPLACE_WITH_ placeholder with the real id, "
                "or remove the line."
            )
        if not stats_targets(settings):
            raise ConfigError(
                "Set stats_drive_folder_id or stats_channels before export-stats"
            )
        message = google_auth.login_message(base)
        if message:
            raise ConfigError(message)
        http, persist = google_auth.session(base)
        exported = export_stats(settings, base, http, week=week)
    except Exception as error:
        print(public_error(error))
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


def install_console_interrupt_handler():
    """Let Windows Ctrl+Break reach the KeyboardInterrupt handler.

    Python installs that handler for SIGINT only. CTRL_BREAK_EVENT arrives as
    SIGBREAK, and the default action is ExitProcess, which skips finally
    blocks. A new process group ignores Ctrl+C, so the desktop Stop button
    sends Ctrl+Break instead.
    """
    if sys.platform != "win32":
        return
    signal.signal(signal.SIGBREAK, signal.default_int_handler)


def interruptible_sleep(seconds):
    """Sleep for ``seconds``.

    On Windows, SIGBREAK does not wake ``time.sleep``. A poll or quota wait
    can last for hours, so sleep at most one second at a time and let the
    pending KeyboardInterrupt run between slices. Other platforms keep one
    sleep, which SIGINT already interrupts.
    """
    if sys.platform != "win32" or seconds <= 1:
        time.sleep(seconds)
        return
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(1.0, remaining))


def main():
    install_console_interrupt_handler()
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
    commands.add_parser(
        "gui",
        help="Open the desktop window",
        description="Open the desktop window",
    )
    args = parser.parse_args()
    if args.command == "gui":
        from .gui import launch

        return launch(args.config)
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
                message = google_auth.login_message(base)
                if message:
                    print(message)
                    return 1
                http, persist = google_auth.session(base)
                try:
                    YouTube(http).verify_channel(settings.youtube_channel_id)
                    drive = Drive(http)
                    if report_remote_stories(drive, settings.drive_folder_id):
                        return 1
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
                        print(format_status_line(sid, role, job))
                if not state["stories"]:
                    print("No stories started yet.")
                return 0
            store.load()  # Fail before connecting if history is missing/corrupt.
            message = google_auth.login_message(base)
            if message:
                print(message)
                return 1
            http, persist = google_auth.session(base)
            try:
                youtube = YouTube(http)
                runner = None
                failures = 0
                while True:
                    pause = None
                    try:
                        if runner is None:
                            youtube.verify_channel(settings.youtube_channel_id)
                            runner = Runner(settings, base, store, Drive(http), youtube)
                        runner.cycle()
                        failures = 0
                    except (ApiError, requests.RequestException) as error:
                        action, failures, pause = poll_retry(
                            error,
                            failures,
                            args.once,
                            datetime.now(UTC),
                            settings.poll_seconds,
                        )
                        if action == "stop":
                            raise
                        if action == "quota":
                            print(
                                "API quota exhausted; waiting for the next reset. Saved history is intact."
                            )
                        else:
                            print(
                                "Discovery temporarily unavailable; retrying with saved history intact."
                            )
                    persist()
                    if args.once:
                        break
                    interruptible_sleep(
                        settings.poll_seconds if pause is None else pause
                    )
            finally:
                persist()
                http.close()
        return 0
    except KeyboardInterrupt:
        print("Stopped. Saved upload sessions will be reconciled on restart.")
        return 130
    except Exception as error:
        # ValueError text is ours. Other exceptions can carry request URLs.
        print(public_error(error))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
