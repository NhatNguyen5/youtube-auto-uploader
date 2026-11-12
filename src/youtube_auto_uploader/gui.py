"""Tkinter window. Decisions live in desktop.py; this module only draws them."""

import queue
import threading

from .desktop import (
    Operator,
    StatusSnapshot,
    button_state,
    check_result_text,
    check_running_text,
    collect_status,
    log_header,
    public_error,
    status_text,
)

POLL_MS = 100
CHECKS_PLACEHOLDER = "No validate result yet."


def launch(config_path):
    try:
        import tkinter as tk
    except ImportError:
        print(
            "Tkinter is not available in this Python. "
            "The Windows installer includes it; this command needs that library."
        )
        return 1
    try:
        root = tk.Tk()
    except tk.TclError as error:
        print(f"Could not open a window ({error}).")
        return 1
    window = DesktopWindow(root, config_path)
    root.protocol("WM_DELETE_WINDOW", window.close)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        window.close()
        print("Stopped. Saved upload sessions will be reconciled on restart.")
        return 130
    return 0


class DesktopWindow:
    def __init__(self, root, config_path, operator=None):
        import tkinter as tk
        from tkinter import scrolledtext, ttk

        self.root = root
        self.tk = tk
        self.ttk = ttk
        self.scrolledtext = scrolledtext
        self.config_path = config_path
        self.operator = Operator(config_path) if operator is None else operator
        self.buttons = {}
        self._closed = False
        self._refreshing = False
        self._status_queue = queue.Queue()
        self._build()
        self._sync_buttons()
        root.after(0, self.refresh)

    def _build(self):
        root = self.root
        ttk = self.ttk
        root.title("YouTube Auto Uploader")
        root.geometry("880x720")
        root.minsize(640, 480)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        root.rowconfigure(4, weight=1)

        status_frame = ttk.LabelFrame(root, text="Status")
        status_frame.grid(row=0, column=0, sticky="nsew", padx=8, pady=(8, 4))
        status_frame.columnconfigure(0, weight=1)
        status_frame.rowconfigure(0, weight=1)
        self.status = self._text(status_frame, 8)
        self.status.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        self._button(status_frame, "refresh", "Refresh", self.refresh).grid(
            row=1, column=0, sticky="w", padx=6, pady=(0, 6)
        )

        checks_frame = ttk.LabelFrame(root, text="Config and credentials")
        checks_frame.grid(row=1, column=0, sticky="ew", padx=8, pady=4)
        checks_frame.columnconfigure(0, weight=1)
        self.checks = self._text(checks_frame, 5)
        self.checks.grid(row=0, column=0, sticky="ew", padx=6, pady=6)
        self._set_text(self.checks, CHECKS_PLACEHOLDER)

        commands = ttk.Frame(root)
        commands.grid(row=2, column=0, sticky="ew", padx=8, pady=4)
        for name, label, command in (
            ("login", "Login", lambda: self._start("login")),
            ("validate", "Validate", lambda: self._start("validate")),
            (
                "validate_remote",
                "Validate remote",
                lambda: self._start("validate_remote"),
            ),
            ("export_stats", "Export stats", lambda: self._start("export_stats")),
        ):
            self._button(commands, name, label, command).pack(
                side="left", padx=(0, 6), pady=2
            )

        runner = ttk.Frame(root)
        runner.grid(row=3, column=0, sticky="ew", padx=8, pady=4)
        for name, label, command in (
            ("start", "Start runner", lambda: self._start("run")),
            ("stop", "Stop runner", self._stop),
            ("run_once", "Run once", lambda: self._start("run_once")),
        ):
            self._button(runner, name, label, command).pack(
                side="left", padx=(0, 6), pady=2
            )

        log_frame = ttk.LabelFrame(root, text="Log")
        log_frame.grid(row=4, column=0, sticky="nsew", padx=8, pady=(4, 8))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log = self._text(log_frame, 10)
        self.log.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)

    def _text(self, parent, height):
        widget = self.scrolledtext.ScrolledText(
            parent,
            height=height,
            wrap="word",
            font="TkFixedFont",
            state="disabled",
        )
        return widget

    def _button(self, parent, name, label, command):
        button = self.ttk.Button(parent, text=label, command=command)
        self.buttons[name] = button
        return button

    def _set_text(self, widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", text)
        widget.configure(state="disabled")

    def _append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.see("end")
        self.log.configure(state="disabled")

    def _sync_buttons(self):
        enabled = button_state(self.operator.running_action(), self._refreshing)
        for name, button in self.buttons.items():
            button.configure(state="normal" if enabled[name] else "disabled")

    def refresh(self):
        if self._closed or self._refreshing:
            return
        self._refreshing = True
        self._sync_buttons()
        threading.Thread(
            target=self._load_status, name="uploader-status", daemon=True
        ).start()
        self._poll_status()

    def _load_status(self):
        # Queue only. The worker must not touch Tk widgets.
        try:
            snapshot = collect_status(self.config_path)
        except Exception as error:
            snapshot = StatusSnapshot(False, [], public_error(error))
        self._status_queue.put(snapshot)

    def _poll_status(self):
        if self._closed:
            return
        try:
            snapshot = self._status_queue.get_nowait()
        except queue.Empty:
            self.root.after(POLL_MS, self._poll_status)
            return
        self._show_status(snapshot)

    def _show_status(self, snapshot):
        self._refreshing = False
        if self._closed:
            return
        self._set_text(self.status, status_text(snapshot))
        self._sync_buttons()

    def _start(self, action):
        try:
            run = self.operator.start(action)
        except RuntimeError as error:
            self._append_log(f"{error}\n")
            return
        except Exception as error:
            self._append_log(public_error(error) + "\n")
            return
        self._append_log(log_header(action) + "\n")
        pending = check_running_text(action)
        if pending:
            self._set_text(self.checks, pending)
        self._sync_buttons()
        self._poll(run)

    def _stop(self):
        if self.operator.stop_runner():
            self._append_log(
                ">>> stop (Ctrl+C). Waiting for the current step to finish handling it.\n"
            )
            return
        current = self.operator.current
        if current is not None and current.stop_error:
            self._append_log(f"Could not send stop signal: {current.stop_error}\n")

    def _poll(self, run):
        if self._closed:
            return
        lines = []
        code = None
        for kind, payload in run.drain():
            if kind == "line":
                lines.append(payload)
            elif kind == "exit":
                code = payload
        if lines:
            self._append_log("".join(lines))
        if code is None:
            self.root.after(POLL_MS, lambda run=run: self._poll(run))
            return
        result = check_result_text(run.action, run.output_lines, code)
        if result is not None:
            self._set_text(self.checks, result)
        self._append_log(f">>> exit {code}\n")
        self._sync_buttons()
        self.refresh()

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.operator.interrupt_current()
        except Exception:
            pass
        try:
            self.root.destroy()
        except self.tk.TclError:
            pass
