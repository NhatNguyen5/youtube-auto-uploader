import ast
import json
import os
import queue
import signal
import subprocess
import sys
import time
from pathlib import Path

import portalocker
import pytest

from youtube_auto_uploader.desktop import (
    ACTIONS,
    BUTTONS,
    RUNNER_ACTIVE,
    Operator,
    StatusSnapshot,
    button_state,
    check_result_text,
    check_running_text,
    cli_argv,
    collect_status,
    console_interrupt,
    log_header,
    read_status,
    status_text,
    subprocess_kwargs,
)
from youtube_auto_uploader.storage import Store


def story_snapshot():
    return json.loads(
        Path("examples/story-config.example.json").read_text(encoding="utf-8")
    )


def job(stage, source_id, video_id=None, error=None):
    item = {
        "stage": stage,
        "source": {
            "id": source_id,
            "name": f"{source_id}.mp4",
            "size": "3",
            "version": "1",
            "mimeType": "video/mp4",
        },
        "attempts": 0,
        "consecutive_failures": 0,
    }
    if video_id:
        item["video_id"] = video_id
    if error is not None:
        item["error"] = error
    return item


def write_config(path):
    path.write_text(json.dumps({"youtube_channel_id": "channel"}), encoding="utf-8")


def saved_state(tmp_path):
    store = Store(tmp_path / "data" / "state.json")
    store.initialize()
    state = store.load()
    state["stories"] = {
        "alpha": {
            "name": "Alpha",
            "snapshot": story_snapshot(),
            "jobs": {
                "long": job("uploaded", "alpha-long", video_id="abc123"),
                "short": job("needs_review", "alpha-short", error="quota exceeded"),
            },
        },
        "beta": {
            "name": "Beta",
            "snapshot": story_snapshot(),
            "jobs": {
                "long": job("pending", "beta-long"),
                "short": job("pending", "beta-short"),
            },
        },
    }
    store.save(state)
    return store


def test_status_lines_match_the_status_command(tmp_path):
    store = saved_state(tmp_path)
    snapshot = read_status(store)
    assert snapshot.locked is False
    assert snapshot.rows == [
        "alpha/long: uploaded https://www.youtube.com/watch?v=abc123 ",
        "alpha/short: needs_review  quota exceeded",
        "beta/long: pending  ",
        "beta/short: pending  ",
    ]
    assert status_text(snapshot) == "\n".join(snapshot.rows)
    with store.lock():
        pass


def test_empty_status_and_locked_message():
    assert status_text(StatusSnapshot(False, [])) == "No stories started yet."
    locked = StatusSnapshot(True, ["hidden"], "other")
    assert status_text(locked) == RUNNER_ACTIVE
    assert RUNNER_ACTIVE == "Runner is active, status locked"


def test_status_when_runner_holds_the_lock_does_not_release_it(tmp_path):
    store = Store(tmp_path / "state.json")
    store.initialize()
    held = store.lock()
    held.acquire()
    try:
        snapshot = read_status(store)
        assert snapshot.locked is True
        assert snapshot.rows == []
        assert status_text(snapshot) == "Runner is active, status locked"
        with pytest.raises(portalocker.exceptions.LockException):
            with store.lock():
                pass
    finally:
        held.release()
    snapshot = read_status(store)
    assert snapshot.locked is False
    assert status_text(snapshot) == "No stories started yet."


def test_missing_state_is_reported_and_not_created(tmp_path):
    store = Store(tmp_path / "state.json")
    snapshot = read_status(store)
    assert snapshot.locked is False
    assert "State missing" in snapshot.message
    assert not store.path.exists()
    with store.lock():
        pass


def test_corrupt_state_releases_the_lock(tmp_path):
    store = Store(tmp_path / "state.json")
    store.path.write_text("{bad", encoding="utf-8")
    snapshot = read_status(store)
    assert snapshot.locked is False
    assert snapshot.message
    assert "Runner is active" not in snapshot.message
    with store.lock():
        pass
    assert store.path.read_text(encoding="utf-8") == "{bad"


def test_collect_status_reads_the_configured_state_file(tmp_path):
    config = tmp_path / "config.json"
    write_config(config)
    saved_state(tmp_path)
    snapshot = collect_status(config)
    assert snapshot.rows[0].startswith("alpha/long: uploaded ")
    assert not snapshot.locked


def test_collect_status_missing_config(tmp_path):
    snapshot = collect_status(tmp_path / "config.json")
    assert snapshot.locked is False
    assert "FileNotFoundError" in snapshot.message
    assert "config.json" in snapshot.message
    assert not (tmp_path / "data" / "state.json").exists()


@pytest.mark.parametrize(
    ("action", "tail"),
    [
        ("login", ["login"]),
        ("validate", ["validate"]),
        ("validate_remote", ["validate", "--remote"]),
        ("run", ["run"]),
        ("run_once", ["run", "--once"]),
        ("export_stats", ["export-stats"]),
    ],
)
def test_actions_map_to_cli_arguments(action, tail, tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "executable", r"C:\Python\python.exe")
    config = tmp_path / "config.json"
    argv = cli_argv(config, action)
    assert argv == [
        r"C:\Python\python.exe",
        "-u",
        "-m",
        "youtube_auto_uploader.cli",
        "--config",
        str(config.resolve()),
        *tail,
    ]
    assert "init-state" not in argv


def test_unknown_action_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        cli_argv(tmp_path / "config.json", "init-state")
    assert "init-state" not in ACTIONS


def test_button_state_disables_conflicts():
    idle = button_state(None)
    assert set(idle) == set(BUTTONS)
    assert idle["stop"] is False
    assert all(idle[name] for name in BUTTONS if name != "stop")

    validating = button_state("validate_remote")
    assert validating["refresh"] is True
    assert validating["stop"] is False
    assert validating["login"] is False
    assert validating["start"] is False
    assert validating["run_once"] is False
    assert validating["export_stats"] is False

    running = button_state("run")
    assert running["stop"] is True
    assert running["refresh"] is True
    assert running["start"] is False
    assert running["run_once"] is False
    assert running["validate"] is False
    assert running["export_stats"] is False

    refreshing = button_state(None, refreshing=True)
    assert refreshing["refresh"] is True
    assert refreshing["login"] is True
    assert refreshing["start"] is False
    assert refreshing["run_once"] is False
    assert refreshing["stop"] is False


def test_check_text_and_log_header():
    assert check_running_text("validate") == "Running validate..."
    assert check_running_text("validate_remote") == "Running validate --remote..."
    assert check_running_text("run") is None
    assert log_header("run_once") == ">>> youtube-auto-uploader run --once"
    assert log_header("export_stats") == ">>> youtube-auto-uploader export-stats"
    passed = check_result_text("validate", ["Validation passed (no uploads).\n"], 0)
    assert passed == "Validate: passed\nValidation passed (no uploads)."
    failed = check_result_text("validate_remote", ["Error: token\n"], 1)
    assert failed.startswith("Validate remote: failed (exit 1)\nError: token")
    assert check_result_text("run", ["Stopped.\n"], 130) is None


def test_console_interrupt_is_ctrl_c_and_not_kill():
    if sys.platform == "win32":
        assert console_interrupt() == signal.CTRL_BREAK_EVENT
    else:
        assert console_interrupt() == signal.SIGINT
    assert console_interrupt() != signal.SIGTERM


def test_child_is_started_in_its_own_group(tmp_path):
    config = tmp_path / "config.json"
    kwargs = subprocess_kwargs(config)
    assert kwargs["stdout"] is subprocess.PIPE
    assert kwargs["stderr"] is subprocess.STDOUT
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["cwd"] == str(tmp_path)
    assert kwargs["env"]["PYTHONUNBUFFERED"] == "1"
    assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8"
    assert "shell" not in kwargs
    if sys.platform == "win32":
        assert kwargs["creationflags"] == subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        assert kwargs["start_new_session"] is True


class LineStream:
    def __init__(self, lines):
        self.lines = list(lines)

    def readline(self):
        if not self.lines:
            return ""
        return self.lines.pop(0)

    def close(self):
        self.lines.clear()


class HeldStream:
    def __init__(self):
        self.queue = queue.Queue()

    def readline(self):
        return self.queue.get()

    def close(self):
        self.queue.put("")


class FakeProcess:
    def __init__(self, args, stream, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.stdout = stream
        self.returncode = None
        self.signals = []
        self.killed = False
        self.terminated = False

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    def send_signal(self, sig):
        self.signals.append(sig)
        self.returncode = 130

    def kill(self):
        self.killed = True

    def terminate(self):
        self.terminated = True


def test_output_streams_until_the_process_exits(tmp_path):
    def popen(argv, **kwargs):
        return FakeProcess(argv, LineStream(["one\n", "two\n"]), **kwargs)

    operator = Operator(tmp_path / "config.json", popen=popen)
    run = operator.start("validate")
    code = wait_for(run)
    assert code == 0
    assert run.output_lines == ["one\n", "two\n"]
    assert "init-state" not in run.process.args
    if sys.platform == "win32":
        assert (
            run.process.kwargs["creationflags"] == subprocess.CREATE_NEW_PROCESS_GROUP
        )
    else:
        assert run.process.kwargs["start_new_session"] is True


def test_stop_sends_ctrl_c_and_does_not_kill(tmp_path):
    stream = HeldStream()
    created = []

    def popen(argv, **kwargs):
        process = FakeProcess(argv, stream, **kwargs)
        created.append(process)
        return process

    operator = Operator(tmp_path / "config.json", popen=popen)
    try:
        operator.start("run")
        assert operator.stop_runner() is True
        process = created[0]
        assert process.signals == [console_interrupt()]
        assert process.killed is False
        assert process.terminated is False
        assert operator.stop_runner() is False
        assert process.signals == [console_interrupt()]
    finally:
        stream.queue.put("")


def test_stop_does_nothing_for_a_one_shot_command(tmp_path):
    stream = HeldStream()

    def popen(argv, **kwargs):
        return FakeProcess(argv, stream, **kwargs)

    operator = Operator(tmp_path / "config.json", popen=popen)
    try:
        operator.start("run_once")
        assert operator.stop_runner() is False
        assert operator.current.process.signals == []
        assert operator.interrupt_current() is True
        assert operator.current.process.signals == [console_interrupt()]
        assert operator.current.process.killed is False
    finally:
        stream.queue.put("")


def test_second_command_is_rejected_while_one_is_running(tmp_path):
    stream = HeldStream()
    calls = []

    def popen(argv, **kwargs):
        calls.append(argv)
        return FakeProcess(argv, stream, **kwargs)

    operator = Operator(tmp_path / "config.json", popen=popen)
    try:
        operator.start("run")
        with pytest.raises(RuntimeError, match="already running"):
            operator.start("export_stats")
        assert len(calls) == 1
    finally:
        stream.queue.put("")


def wait_for(run, timeout=10):
    deadline = time.monotonic() + timeout
    code = None
    while time.monotonic() < deadline:
        for kind, payload in run.drain():
            if kind == "exit":
                code = payload
        if code is not None:
            return code
        time.sleep(0.02)
    raise AssertionError("command did not exit")


def test_operator_validate_uses_the_real_cli(tmp_path):
    config = tmp_path / "config.json"
    write_config(config)
    operator = Operator(config)
    run = operator.start("validate")
    code = wait_for(run)
    assert code == 0
    assert "Validation passed (no uploads)." in "".join(run.output_lines)
    assert not (tmp_path / "data" / "state.json").exists()


def test_operator_run_does_not_create_state(tmp_path):
    config = tmp_path / "config.json"
    write_config(config)
    operator = Operator(config)
    run = operator.start("run_once")
    code = wait_for(run)
    assert code == 1
    text = "".join(run.output_lines)
    assert "Error (ValueError)" in text
    assert not (tmp_path / "data" / "state.json").exists()


def test_gui_help_lists_the_window_and_keeps_cli_commands():
    env = os_env_without_display()
    gui_help = subprocess.run(
        [sys.executable, "-m", "youtube_auto_uploader.cli", "gui", "--help"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )
    assert gui_help.returncode == 0
    assert "Open the desktop window" in gui_help.stdout
    assert "init-state" not in gui_help.stdout

    main_help = subprocess.run(
        [sys.executable, "-m", "youtube_auto_uploader.cli", "--help"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )
    assert main_help.returncode == 0
    assert "gui" in main_help.stdout
    assert "init-state" in main_help.stdout
    assert "login" in main_help.stdout


def test_gui_module_does_not_import_tkinter_at_import_time():
    import youtube_auto_uploader.gui as gui

    tree = ast.parse(Path(gui.__file__).read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Import):
            names = [alias.name.split(".")[0] for alias in node.names]
            assert "tkinter" not in names
        if isinstance(node, ast.ImportFrom) and node.module:
            assert node.module.split(".")[0] != "tkinter"
    assert callable(gui.launch)


def test_desktop_and_window_never_initialize_state():
    root = Path("src/youtube_auto_uploader")
    for name in ("desktop.py", "gui.py"):
        text = (root / name).read_text(encoding="utf-8")
        assert "init-state" not in text
        assert ".initialize(" not in text
        assert ".kill(" not in text
        assert ".terminate(" not in text


def os_env_without_display():
    env = os.environ.copy()
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    return env
