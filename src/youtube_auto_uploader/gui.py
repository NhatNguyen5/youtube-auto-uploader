"""Tkinter window. Status commands live in desktop.py; edits live in editor.py."""

import queue
import threading
from pathlib import Path

from . import google_auth
from .desktop import (
    Operator,
    StatusSnapshot,
    button_state,
    check_result_text,
    check_running_text,
    collect_status,
    log_header,
    status_text,
)
from .editor import (
    ROLES,
    EditResult,
    StoriesSnapshot,
    collect_stories,
    commit_story_config,
    enabled_default,
    publish_at_from_parts,
    reload_from_drive,
    retry_saved,
    role_form_defaults,
    save_settings,
    settings_form,
    validate_story_form,
)
from .status import ConfigError, public_error

POLL_MS = 100
CHECKS_PLACEHOLDER = "No validate result yet."
SETTING_FIELDS = (
    ("drive_folder_id", "Drive folder ID"),
    ("stats_drive_folder_id", "Stats Drive folder ID"),
    ("upload_log_folder_id", "Upload log folder ID"),
    ("youtube_channel_id", "YouTube channel ID"),
    ("upload_lead_minutes", "Upload lead (minutes)"),
    ("poll_seconds", "Poll (seconds)"),
)


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
        self._stories_refreshing = False
        self._stories_dirty = False
        self._stories_loaded = False
        self._settings_loaded = False
        self._story_rows = {}
        self._story_state_locked = False
        self._reload_busy = False
        self._reload_note = ""
        self._story_queue = queue.Queue()
        self._background_queue = queue.Queue()
        self._background_waiters = 0
        self._build()
        self._sync_buttons()
        root.after(0, self.refresh)

    def _build(self):
        root = self.root
        ttk = self.ttk
        root.title("YouTube Auto Uploader")
        root.geometry("980x800")
        root.minsize(720, 560)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)

        self.notebook = ttk.Notebook(root)
        self.notebook.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        status = ttk.Frame(self.notebook)
        stories = ttk.Frame(self.notebook)
        settings = ttk.Frame(self.notebook)
        for frame in (status, stories, settings):
            frame.columnconfigure(0, weight=1)
        status.rowconfigure(0, weight=1)
        status.rowconfigure(4, weight=1)
        stories.rowconfigure(2, weight=1)
        settings.rowconfigure(1, weight=1)
        self.notebook.add(status, text="Status")
        self.notebook.add(stories, text="Stories")
        self.notebook.add(settings, text="Settings")
        self.notebook.bind("<<NotebookTabChanged>>", self._tab_changed)
        self._build_status(status)
        self._build_stories(stories)
        self._build_settings(settings)

    def _build_status(self, parent):
        ttk = self.ttk
        status_frame = ttk.LabelFrame(parent, text="Status")
        status_frame.grid(row=0, column=0, sticky="nsew", padx=8, pady=(8, 4))
        status_frame.columnconfigure(0, weight=1)
        status_frame.rowconfigure(0, weight=1)
        self.status = self._text(status_frame, 8)
        self.status.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        self._button(status_frame, "refresh", "Refresh", self.refresh).grid(
            row=1, column=0, sticky="w", padx=6, pady=(0, 6)
        )

        checks_frame = ttk.LabelFrame(parent, text="Config and credentials")
        checks_frame.grid(row=1, column=0, sticky="ew", padx=8, pady=4)
        checks_frame.columnconfigure(0, weight=1)
        self.checks = self._text(checks_frame, 5)
        self.checks.grid(row=0, column=0, sticky="ew", padx=6, pady=6)
        self._set_text(self.checks, CHECKS_PLACEHOLDER)

        commands = ttk.Frame(parent)
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

        runner = ttk.Frame(parent)
        runner.grid(row=3, column=0, sticky="ew", padx=8, pady=4)
        for name, label, command in (
            ("start", "Start runner", lambda: self._start("run")),
            ("stop", "Stop runner", self._stop),
            ("run_once", "Run once", lambda: self._start("run_once")),
        ):
            self._button(runner, name, label, command).pack(
                side="left", padx=(0, 6), pady=2
            )

        log_frame = ttk.LabelFrame(parent, text="Log")
        log_frame.grid(row=4, column=0, sticky="nsew", padx=8, pady=(4, 8))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        self.log = self._text(log_frame, 10)
        self.log.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)

    def _build_stories(self, parent):
        ttk = self.ttk
        bar = ttk.Frame(parent)
        bar.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 0))
        self._button(
            bar, "refresh_stories", "Refresh stories", self.refresh_stories
        ).pack(side="left")
        self._button(
            bar, "reload_drive", "Reload from Drive", lambda: self._reload_from_drive()
        ).pack(side="left", padx=(6, 0))

        self.stories_message = self.tk.Label(
            parent, text="", justify="left", wraplength=900, anchor="w"
        )
        self.stories_message.grid(row=1, column=0, sticky="ew", padx=8, pady=4)

        columns = ("role", "stage", "publish", "title", "error")
        tree_frame = ttk.Frame(parent)
        tree_frame.grid(row=2, column=0, sticky="nsew", padx=6, pady=4)
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)
        self.story_tree = ttk.Treeview(
            tree_frame, columns=columns, show="tree headings", selectmode="browse"
        )
        self.story_tree.heading("#0", text="Story")
        self.story_tree.heading("role", text="Role")
        self.story_tree.heading("stage", text="Stage")
        self.story_tree.heading("publish", text="Publish (local)")
        self.story_tree.heading("title", text="Title")
        self.story_tree.heading("error", text="Last error")
        self.story_tree.column("#0", width=180, stretch=True)
        self.story_tree.column("role", width=70, stretch=False)
        self.story_tree.column("stage", width=110, stretch=False)
        self.story_tree.column("publish", width=180, stretch=False)
        self.story_tree.column("title", width=220, stretch=True)
        self.story_tree.column("error", width=240, stretch=True)
        scroll = ttk.Scrollbar(
            tree_frame, orient="vertical", command=self.story_tree.yview
        )
        self.story_tree.configure(yscrollcommand=scroll.set)
        self.story_tree.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")
        self.story_tree.bind(
            "<<TreeviewSelect>>", lambda _event: self._show_retry_reason()
        )
        self.story_tree.bind("<Double-1>", lambda _event: self._edit_selected_story())

        actions = ttk.Frame(parent)
        actions.grid(row=3, column=0, sticky="ew", padx=6, pady=(4, 8))
        self._button(
            actions, "edit_story", "Edit story", self._edit_selected_story
        ).pack(side="left")
        reload_one = self._button(
            actions,
            "reload_story",
            "Reload this story",
            lambda: self._reload_from_drive(self._selected_role()[0]),
        )
        reload_one.pack(side="left", padx=(6, 0))
        reload_one.configure(state="disabled")
        retry = self._button(actions, "retry_role", "Retry", self._retry_selected)
        retry.pack(side="left", padx=(6, 0))
        retry.configure(state="disabled")
        self.retry_reason = self.tk.Label(
            actions,
            text="Select long or short.",
            justify="left",
            wraplength=640,
            anchor="w",
        )
        self.retry_reason.pack(side="left", padx=12)

    def _build_settings(self, parent):
        ttk = self.ttk
        form = ttk.Frame(parent, padding=8)
        form.grid(row=0, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)
        self.settings_vars = {}
        self.settings_error_labels = {}
        for row, (field, label) in enumerate(SETTING_FIELDS):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=3)
            var = self.tk.StringVar()
            ttk.Entry(form, textvariable=var).grid(
                row=row, column=1, sticky="ew", padx=8, pady=3
            )
            error = self._error_label(form)
            error.grid(row=row, column=2, sticky="w")
            self.settings_vars[field] = var
            self.settings_error_labels[field] = error
        hint = ttk.Label(
            form,
            text=(
                "Leave the upload log empty to turn it off. "
                "Leave the stats folder empty when it is unset."
            ),
        )
        hint.grid(
            row=len(SETTING_FIELDS), column=0, columnspan=3, sticky="w", pady=(8, 0)
        )

        actions = ttk.Frame(parent)
        actions.grid(row=1, column=0, sticky="nw", padx=8, pady=8)
        self._button(
            actions, "reload_settings", "Reload", self._load_settings_form
        ).pack(side="left")
        self._button(
            actions, "save_settings", "Save settings", self._save_settings
        ).pack(side="left", padx=(6, 0))
        self.settings_message = self.tk.Label(
            parent, text="", justify="left", wraplength=900, anchor="w"
        )
        self.settings_message.grid(row=2, column=0, sticky="ew", padx=8, pady=4)

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

    def _error_label(self, parent):
        return self.tk.Label(
            parent, text="", fg="#8b1e1e", justify="left", wraplength=280, anchor="w"
        )

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
            if name not in enabled:
                continue
            button.configure(state="normal" if enabled[name] else "disabled")

    def _tab_changed(self, _event):
        if self._closed:
            return
        current = self.notebook.select()
        if not current:
            return
        name = self.notebook.tab(current, "text")
        if name == "Stories" and not self._stories_loaded:
            self.refresh_stories()
        elif name == "Settings" and not self._settings_loaded:
            self._load_settings_form()

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

    def refresh_stories(self):
        if self._closed:
            return
        if self._stories_refreshing:
            self._stories_dirty = True
            return
        self._stories_dirty = False
        self._stories_refreshing = True
        self._stories_loaded = True
        self.stories_message.configure(text="Loading stories...", fg="#333333")
        threading.Thread(
            target=self._load_stories, name="uploader-stories", daemon=True
        ).start()
        self._poll_stories()

    def _load_stories(self):
        try:
            snapshot = collect_stories(self.config_path)
        except Exception as error:
            snapshot = StoriesSnapshot([], public_error(error))
        self._story_queue.put(snapshot)

    def _poll_stories(self):
        if self._closed:
            return
        try:
            snapshot = self._story_queue.get_nowait()
        except queue.Empty:
            self.root.after(POLL_MS, self._poll_stories)
            return
        self._show_stories(snapshot)

    def _show_stories(self, snapshot):
        self._stories_refreshing = False
        if self._closed:
            return
        if self._stories_dirty:
            self.refresh_stories()
            return
        self._story_rows = {story.folder_id: story for story in snapshot.stories}
        self._story_state_locked = snapshot.state_locked
        tree = self.story_tree
        tree.delete(*tree.get_children())
        for story in snapshot.stories:
            tree.insert(
                "",
                "end",
                iid=story.folder_id,
                text=story.name,
                values=("", "", "", "", story.config_error),
            )
            for role in ROLES:
                row = story.roles[role]
                tree.insert(
                    story.folder_id,
                    "end",
                    iid=f"{story.folder_id}/{role}",
                    text="",
                    values=(
                        row.role,
                        row.stage,
                        row.publish_local,
                        row.title,
                        row.error,
                    ),
                )
            tree.item(story.folder_id, open=True)
        message = snapshot.message
        if self._reload_note:
            message = f"{self._reload_note}\n{message}".strip()
            self._reload_note = ""
        self.stories_message.configure(text=message, fg="#333333")
        self._show_retry_reason()

    def _selected_role(self):
        selected = self.story_tree.selection()
        if not selected:
            return None, None
        iid = selected[0]
        if "/" in iid:
            folder_id, role = iid.split("/", 1)
            return folder_id, role
        return iid, None

    def _show_retry_reason(self):
        try:
            self._show_retry_reason_text()
        finally:
            self._sync_reload_buttons()

    def _show_retry_reason_text(self):
        button = self.buttons["retry_role"]
        self.retry_reason.configure(fg="#333333")
        folder_id, role = self._selected_role()
        if folder_id is None:
            button.configure(state="disabled")
            self.retry_reason.configure(text="Select long or short.")
            return
        if role is None:
            button.configure(state="disabled")
            self.retry_reason.configure(
                text=(
                    "Select long or short. A story is never deleted from here, "
                    "including when the other video is already uploaded."
                )
            )
            return
        story = self._story_rows.get(folder_id)
        row = story.roles.get(role) if story else None
        if row is None or not row.retry_allowed or self._story_state_locked:
            button.configure(state="disabled")
            if self._story_state_locked and row and row.retry_allowed:
                self.retry_reason.configure(
                    text=(
                        "Runner is active and may be uploading. "
                        "Retry waits until it stops."
                    )
                )
            else:
                self.retry_reason.configure(
                    text=row.retry_reason if row else "Select long or short."
                )
            return
        button.configure(state="normal")
        self.retry_reason.configure(text=row.retry_reason)

    def _sync_reload_buttons(self):
        folder_id, _role = self._selected_role()
        busy = self._reload_busy
        self.buttons["reload_drive"].configure(state="disabled" if busy else "normal")
        self.buttons["reload_story"].configure(
            state="normal" if folder_id and not busy else "disabled"
        )

    def _reload_from_drive(self, folder_id=None):
        if self._closed or self._reload_busy:
            return
        if folder_id is not None and not folder_id:
            self._show_retry_reason()
            return
        self._reload_busy = True
        self._sync_reload_buttons()
        self.stories_message.configure(text="Reloading from Drive...", fg="#333333")
        threading.Thread(
            target=self._reload_worker,
            args=(folder_id,),
            name="uploader-reload",
            daemon=True,
        ).start()
        self._begin_background()

    def _reload_worker(self, folder_id):
        try:
            result = reload_from_drive(self.config_path, folder_id)
        except Exception as error:
            result = EditResult(message=public_error(error))
        self._background_queue.put(("reload", result))

    def _edit_selected_story(self):
        folder_id, _role = self._selected_role()
        story = self._story_rows.get(folder_id) if folder_id else None
        if story is None:
            return
        self._open_story_editor(story)

    def _open_story_editor(self, story):
        tk = self.tk
        ttk = self.ttk
        top = tk.Toplevel(self.root)
        top.title(f"Edit {story.name}")
        top.geometry("780x860")
        top.minsize(560, 480)
        top.transient(self.root)
        top.columnconfigure(0, weight=1)
        top.rowconfigure(2, weight=1)

        base = Path(self.config_path).resolve().parent
        notice = google_auth.story_config_write_block(base) or ""
        scope = tk.Label(top, text=notice, fg="#8b1e1e", justify="left", wraplength=720)
        scope.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 2))
        summary = tk.Label(
            top,
            text=story.config_error,
            fg="#8b1e1e",
            justify="left",
            wraplength=720,
        )
        summary.grid(row=1, column=0, sticky="ew", padx=8, pady=(2, 4))

        enabled = tk.BooleanVar(value=enabled_default(story.raw))
        ttk.Checkbutton(top, text="Enabled", variable=enabled).grid(
            row=2, column=0, sticky="w", padx=8
        )
        # The role notebook needs the spare row. Shift it down by using row 3.
        # Enabled sits with the summary block, so give the notebook the weight.
        top.rowconfigure(2, weight=0)
        top.rowconfigure(3, weight=1)

        book = ttk.Notebook(top)
        book.grid(row=3, column=0, sticky="nsew", padx=8, pady=4)
        widgets = {}
        for role in ROLES:
            frame = ttk.Frame(book, padding=8)
            frame.columnconfigure(1, weight=1)
            book.add(frame, text=role)
            defaults = role_form_defaults(story.raw, role, story.video_names)
            widgets[role] = self._role_form(frame, story, defaults)

        buttons = ttk.Frame(top)
        buttons.grid(row=4, column=0, sticky="ew", padx=8, pady=8)
        save = ttk.Button(buttons, text="Save")
        save.pack(side="left")
        ttk.Button(buttons, text="Cancel", command=top.destroy).pack(
            side="left", padx=(6, 0)
        )

        def submit():
            form = {"enabled": bool(enabled.get()), "videos": {}}
            for role, fields in widgets.items():
                form["videos"][role] = fields["value"]()
            _story, errors = validate_story_form(story.raw, form, story.video_names)
            if errors:
                self._show_story_errors(summary, widgets, errors)
                return
            save.configure(state="disabled")
            summary.configure(
                text="Checked. Saving config.json in the story folder...",
                fg="#333333",
            )
            threading.Thread(
                target=self._save_story_worker,
                args=(top, summary, widgets, save, story, form),
                name="uploader-story-save",
                daemon=True,
            ).start()
            self._begin_background()

        save.configure(command=submit)

    def _role_form(self, frame, story, defaults):
        tk = self.tk
        ttk = self.ttk
        names = list(story.video_names)
        current = defaults["filename"]
        if current and current not in names:
            names = [current, *names]
        filename = tk.StringVar(value=current)
        ttk.Label(frame, text="Filename").grid(row=0, column=0, sticky="w", pady=2)
        ttk.Combobox(
            frame,
            textvariable=filename,
            values=names,
            state="readonly",
            width=40,
        ).grid(row=0, column=1, sticky="ew", pady=2)
        filename_error = self._error_label(frame)
        filename_error.grid(row=1, column=1, sticky="w")

        year = tk.StringVar(value=str(defaults["year"]))
        month = tk.StringVar(value=str(defaults["month"]))
        day = tk.StringVar(value=str(defaults["day"]))
        hour = tk.StringVar(value=str(defaults["hour"]))
        minute = tk.StringVar(value=str(defaults["minute"]))
        ttk.Label(frame, text="Publish (local)").grid(
            row=2, column=0, sticky="w", pady=2
        )
        when = ttk.Frame(frame)
        when.grid(row=2, column=1, sticky="w")
        offset = tk.Label(frame, text="", justify="left", anchor="w")

        def refresh_offset(*_args):
            try:
                text = publish_at_from_parts(
                    year.get(), month.get(), day.get(), hour.get(), minute.get()
                )
            except ConfigError as error:
                offset.configure(text=str(error), fg="#8b1e1e")
                return
            offset.configure(text=f"Saves as {text}", fg="#333333")

        for label, var, low, high, width in (
            ("Y", year, 2000, 2100, 5),
            ("M", month, 1, 12, 3),
            ("D", day, 1, 31, 3),
            ("h", hour, 0, 23, 3),
            ("m", minute, 0, 59, 3),
        ):
            ttk.Label(when, text=label).pack(side="left", padx=(0, 2))
            spin = ttk.Spinbox(
                when,
                from_=low,
                to=high,
                textvariable=var,
                width=width,
                command=refresh_offset,
            )
            spin.pack(side="left", padx=(0, 6))
            spin.bind("<FocusOut>", refresh_offset)
            spin.bind("<KeyRelease>", refresh_offset)
        offset.grid(row=3, column=1, sticky="w")
        publish_error = self._error_label(frame)
        publish_error.grid(row=4, column=1, sticky="w")
        refresh_offset()

        title = tk.StringVar(value=defaults["title"])
        ttk.Label(frame, text="Title").grid(row=5, column=0, sticky="w", pady=2)
        ttk.Entry(frame, textvariable=title).grid(row=5, column=1, sticky="ew", pady=2)
        title_error = self._error_label(frame)
        title_error.grid(row=6, column=1, sticky="w")

        ttk.Label(frame, text="Description").grid(row=7, column=0, sticky="nw", pady=2)
        description = tk.Text(frame, height=3, width=40, wrap="word")
        description.insert("1.0", defaults["description"])
        description.grid(row=7, column=1, sticky="ew", pady=2)
        description_error = self._error_label(frame)
        description_error.grid(row=8, column=1, sticky="w")

        tags = tk.StringVar(value=defaults["tags"])
        ttk.Label(frame, text="Tags").grid(row=9, column=0, sticky="w", pady=2)
        ttk.Entry(frame, textvariable=tags).grid(row=9, column=1, sticky="ew", pady=2)
        tags_error = self._error_label(frame)
        tags_error.grid(row=10, column=1, sticky="w")
        ttk.Label(frame, text="Comma-separated.").grid(row=11, column=1, sticky="w")

        category = tk.StringVar(value=defaults["category_id"])
        ttk.Label(frame, text="Category ID").grid(row=12, column=0, sticky="w", pady=2)
        ttk.Entry(frame, textvariable=category, width=12).grid(
            row=12, column=1, sticky="w", pady=2
        )
        category_error = self._error_label(frame)
        category_error.grid(row=13, column=1, sticky="w")

        kids = tk.BooleanVar(value=defaults["made_for_kids"])
        synthetic = tk.BooleanVar(value=defaults["contains_synthetic_media"])
        notify = tk.BooleanVar(value=defaults["notify_subscribers"])
        flags = ttk.Frame(frame)
        flags.grid(row=14, column=1, sticky="w", pady=4)
        ttk.Checkbutton(flags, text="Made for kids", variable=kids).pack(side="left")
        ttk.Checkbutton(flags, text="Synthetic media", variable=synthetic).pack(
            side="left", padx=8
        )
        ttk.Checkbutton(flags, text="Notify subscribers", variable=notify).pack(
            side="left"
        )
        flags_error = self._error_label(frame)
        flags_error.grid(row=15, column=1, sticky="w")
        role_error = self._error_label(frame)
        role_error.grid(row=16, column=1, sticky="w")

        def value():
            return {
                "filename": filename.get(),
                "year": year.get(),
                "month": month.get(),
                "day": day.get(),
                "hour": hour.get(),
                "minute": minute.get(),
                "title": title.get(),
                "description": description.get("1.0", "end-1c"),
                "tags": tags.get(),
                "category_id": category.get(),
                "made_for_kids": bool(kids.get()),
                "contains_synthetic_media": bool(synthetic.get()),
                "notify_subscribers": bool(notify.get()),
            }

        return {
            "value": value,
            "errors": {
                "filename": filename_error,
                "publish_at": publish_error,
                "title": title_error,
                "description": description_error,
                "tags": tags_error,
                "category_id": category_error,
                "made_for_kids": flags_error,
                "contains_synthetic_media": flags_error,
                "notify_subscribers": flags_error,
                "": role_error,
            },
        }

    def _show_story_errors(self, summary, widgets, errors):
        summary.configure(
            text="\n".join(f"{key}: {message}" for key, message in errors.items()),
            fg="#8b1e1e",
        )
        for role, fields in widgets.items():
            for name, label in fields["errors"].items():
                label.configure(text="")
            for name, label in fields["errors"].items():
                key = f"videos.{role}" if name == "" else f"videos.{role}.{name}"
                text = errors.get(key, "")
                if text:
                    current = label.cget("text")
                    label.configure(text=f"{current} {text}".strip())

    def _save_story_worker(self, top, summary, widgets, save, story, form):
        try:
            result = commit_story_config(
                self.config_path,
                story.config_file_id,
                story.raw,
                form,
                story.video_names,
            )
        except Exception as error:
            result = EditResult(message=public_error(error))
        self._background_queue.put(("story-save", top, summary, widgets, save, result))

    def _retry_selected(self):
        folder_id, role = self._selected_role()
        story = self._story_rows.get(folder_id) if folder_id else None
        row = story.roles.get(role) if story and role else None
        if row is None or not row.retry_allowed or self._story_state_locked:
            self._show_retry_reason()
            return
        self.buttons["retry_role"].configure(state="disabled")
        self.retry_reason.configure(text="Retrying...")
        threading.Thread(
            target=self._retry_worker,
            args=(folder_id, role),
            name="uploader-retry",
            daemon=True,
        ).start()
        self._begin_background()

    def _retry_worker(self, folder_id, role):
        try:
            result = retry_saved(self.config_path, folder_id, role)
        except Exception as error:
            result = EditResult(message=public_error(error))
        self._background_queue.put(("retry", result))

    def _begin_background(self):
        self._background_waiters += 1
        if self._background_waiters == 1:
            self._await_background()

    def _await_background(self):
        if self._closed:
            return
        while True:
            try:
                item = self._background_queue.get_nowait()
            except queue.Empty:
                break
            self._background_waiters = max(0, self._background_waiters - 1)
            self._handle_background(item)
        if self._background_waiters:
            self.root.after(POLL_MS, self._await_background)

    def _handle_background(self, item):
        kind = item[0]
        if kind == "retry":
            result = item[1]
            self.retry_reason.configure(
                text=result.message, fg="#1b5e20" if result.ok else "#8b1e1e"
            )
            self.refresh_stories()
            return
        if kind == "reload":
            result = item[1]
            self._reload_busy = False
            self._reload_note = result.message
            self.stories_message.configure(
                text=result.message, fg="#1b5e20" if result.ok else "#8b1e1e"
            )
            self._sync_reload_buttons()
            self.refresh_stories()
            return
        _kind, top, summary, widgets, save, result = item
        self._apply_story_save(top, summary, widgets, save, result)

    def _apply_story_save(self, top, summary, widgets, save, result):
        try:
            alive = bool(top.winfo_exists())
        except self.tk.TclError:
            alive = False
        if self._closed or not alive:
            if result.ok:
                self.refresh_stories()
            return
        if result.errors:
            self._show_story_errors(summary, widgets, result.errors)
        else:
            summary.configure(
                text=result.message, fg="#1b5e20" if result.ok else "#8b1e1e"
            )
        try:
            save.configure(state="normal")
        except self.tk.TclError:
            return
        if result.ok:
            self.refresh_stories()

    def _load_settings_form(self):
        self._settings_loaded = True
        try:
            form = settings_form(self.config_path)
        except Exception as error:
            self.settings_message.configure(text=public_error(error), fg="#8b1e1e")
            return
        for field, var in self.settings_vars.items():
            var.set(form.get(field, ""))
        self._show_settings_errors({})
        self.settings_message.configure(text="", fg="#333333")

    def _show_settings_errors(self, errors):
        for field, label in self.settings_error_labels.items():
            label.configure(text=errors.get(field, ""))

    def _save_settings(self):
        import tkinter.messagebox as messagebox

        form = {field: var.get() for field, var in self.settings_vars.items()}
        result = save_settings(self.config_path, form, confirm_binding=False)
        self._show_settings_errors(result.errors)
        if result.confirmation:
            agreed = messagebox.askokcancel(
                "Update saved history", result.confirmation, parent=self.root
            )
            if not agreed:
                self.settings_message.configure(
                    text="Not saved. The binding was left as it is.", fg="#333333"
                )
                return
            result = save_settings(self.config_path, form, confirm_binding=True)
            self._show_settings_errors(result.errors)
        self.settings_message.configure(
            text=result.message, fg="#1b5e20" if result.ok else "#8b1e1e"
        )

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
