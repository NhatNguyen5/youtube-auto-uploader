"""Desktop actions that do not need a window."""

import os
import queue
import signal
import subprocess
import sys
import threading
from pathlib import Path

from portalocker.exceptions import AlreadyLocked, LockException

from .models import Settings
from .storage import Store, parse_json

# Same sentence the window shows when `run` holds the state lock.
RUNNER_ACTIVE = "Runner is active, status locked"

# Subprocess argv tails the window is allowed to start.
ACTIONS = {
    "login": ("login",),
    "validate": ("validate",),
    "validate_remote": ("validate", "--remote"),
    "run": ("run",),
    "run_once": ("run", "--once"),
    "export_stats": ("export-stats",),
}

BUTTONS = (
    "refresh",
    "login",
    "validate",
    "validate_remote",
    "start",
    "stop",
    "run_once",
    "export_stats",
)

CHECK_ACTIONS = frozenset({"validate", "validate_remote"})


class StatusSnapshot:
    def __init__(self, locked, rows, message=""):
        self.locked = locked
        self.rows = list(rows)
        self.message = message


def status_line(story_id, role, job):
    """One line, matching the `status` command."""
    url = (
        f"https://www.youtube.com/watch?v={job['video_id']}"
        if job.get("video_id")
        else ""
    )
    return f"{story_id}/{role}: {job['stage']} {url} {job.get('error', '')}"


def status_rows(state):
    rows = []
    for story_id, story in state["stories"].items():
        for role, job in story["jobs"].items():
            rows.append(status_line(story_id, role, job))
    return rows


def status_text(snapshot):
    if snapshot.locked:
        return RUNNER_ACTIVE
    if snapshot.message:
        return snapshot.message
    if not snapshot.rows:
        return "No stories started yet."
    return "\n".join(snapshot.rows)


def public_error(error):
    if isinstance(error, ValueError):
        return str(error)
    message = (
        f"Error ({type(error).__name__}). "
        "Check configuration, credentials, and saved status; no history was reset."
    )
    if isinstance(error, FileNotFoundError):
        message += (
            " Copy examples/config.example.json to config.json and set your channel ID."
        )
    return message


def read_status(store):
    """Load status if the state lock is free.

    ``Store.lock`` uses timeout 0, so this either acquires it or raises
    ``AlreadyLocked`` without waiting. There is no shared lock. A failure to
    acquire leaves the caller's lock held.
    """
    lock = store.lock()
    try:
        lock.acquire()
    except AlreadyLocked:
        return StatusSnapshot(True, [], RUNNER_ACTIVE)
    except LockException as error:
        return StatusSnapshot(False, [], public_error(error))
    try:
        state = store.load()
        rows = status_rows(state)
    except Exception as error:
        return StatusSnapshot(False, [], public_error(error))
    finally:
        lock.release()
    return StatusSnapshot(False, rows, "")


def collect_status(config_path):
    path = Path(config_path).resolve()
    try:
        settings = Settings.model_validate(
            parse_json(path.read_text(encoding="utf-8-sig"))
        )
    except Exception as error:
        return StatusSnapshot(False, [], public_error(error))
    return read_status(Store(path.parent / settings.state_path))


def button_state(running_action, refreshing=False):
    """Which buttons accept a click.

    `running_action` is an ACTIONS key, or None when nothing is running.
    While a refresh holds the state lock, start and run-once stay disabled so
    they cannot lose the race for that lock.
    """
    enabled = {name: running_action is None for name in BUTTONS}
    enabled["refresh"] = True
    enabled["stop"] = running_action == "run"
    if refreshing and running_action is None:
        enabled["start"] = False
        enabled["run_once"] = False
    return enabled


def log_header(action):
    return ">>> youtube-auto-uploader " + " ".join(ACTIONS[action])


def check_running_text(action):
    if action == "validate":
        return "Running validate..."
    if action == "validate_remote":
        return "Running validate --remote..."
    return None


def check_result_text(action, lines, returncode):
    if action not in CHECK_ACTIONS:
        return None
    label = "Validate remote" if action == "validate_remote" else "Validate"
    head = (
        f"{label}: passed"
        if returncode == 0
        else f"{label}: failed (exit {returncode})"
    )
    body = "".join(lines).strip()
    if body:
        return head + "\n" + body
    return head


def cli_argv(config, action):
    if action not in ACTIONS:
        raise ValueError(f"unknown action: {action}")
    return [
        sys.executable,
        "-u",
        "-m",
        "youtube_auto_uploader.cli",
        "--config",
        str(Path(config).resolve()),
        *ACTIONS[action],
    ]


def console_interrupt():
    """Signal the child the way Ctrl+C does.

    On Windows a new process group ignores CTRL_C_EVENT, and CTRL_BREAK_EVENT
    is delivered to that group as KeyboardInterrupt. Elsewhere this is SIGINT.
    """
    if sys.platform == "win32":
        return signal.CTRL_BREAK_EVENT
    return signal.SIGINT


def subprocess_kwargs(config):
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    kwargs = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "stdin": subprocess.DEVNULL,
        "cwd": str(Path(config).resolve().parent),
        "env": env,
        "text": True,
        "bufsize": 1,
        "encoding": "utf-8",
        "errors": "replace",
    }
    # Own process group so the interrupt reaches the child and not this window.
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return kwargs


class CommandRun:
    def __init__(self, process, action):
        self.process = process
        self.action = action
        self.events = queue.Queue()
        self.output_lines = []
        self.returncode = None
        self.done = False
        self._interrupted = False
        self._signal_lock = threading.Lock()
        self._thread = threading.Thread(
            target=self._pump, name="uploader-output", daemon=True
        )
        self._thread.start()

    def _pump(self):
        stream = self.process.stdout
        try:
            if stream is not None:
                while True:
                    line = stream.readline()
                    if line == "":
                        break
                    self.events.put(("line", line))
        except Exception as error:
            self.events.put(
                ("line", f"Error reading output ({type(error).__name__}).\n")
            )
        finally:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass
            code = self.process.wait()
            self.returncode = code
            self.events.put(("exit", code))
            self.done = True

    def drain(self):
        events = []
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            events.append(event)
            if event[0] == "line":
                self.output_lines.append(event[1])
        return events

    def interrupt(self):
        """Send Ctrl+C and return. Does not terminate or kill the process."""
        with self._signal_lock:
            if self._interrupted or self.done or self.process.poll() is not None:
                return False
            self.process.send_signal(console_interrupt())
            self._interrupted = True
            return True


class Operator:
    """Starts one CLI command at a time and stops `run` with Ctrl+C."""

    def __init__(self, config, popen=None):
        self.config = Path(config).resolve()
        self.popen = popen or subprocess.Popen
        self.current = None
        self._guard = threading.Lock()

    def running_action(self):
        current = self.current
        if current is None or current.done:
            return None
        return current.action

    def start(self, action):
        argv = cli_argv(self.config, action)
        kwargs = subprocess_kwargs(self.config)
        with self._guard:
            if self.running_action() is not None:
                raise RuntimeError("A command is already running")
            process = self.popen(argv, **kwargs)
            self.current = CommandRun(process, action)
            return self.current

    def stop_runner(self):
        return self._interrupt(runner_only=True)

    def interrupt_current(self):
        return self._interrupt(runner_only=False)

    def _interrupt(self, runner_only):
        current = self.current
        if current is None:
            return False
        if runner_only and current.action != "run":
            return False
        return current.interrupt()
