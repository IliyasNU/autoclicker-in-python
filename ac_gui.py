"""Compact, native ttk form. Only the Tk thread reads or changes widgets."""
from dataclasses import asdict, replace
from pathlib import Path
import queue
import sys
import tkinter as tk
from tkinter import font, ttk

from ac_engine import ClickEngine, PynputMouse
from ac_hotkeys import GlobalShortcuts
from ac_settings import (
    FUNCTION_KEYS, Settings, ValidationError, combine_interval, config_path,
    load_settings, save_settings, split_interval, validate_hotkeys,
)


class App:
    def __init__(self, root, *, path=None, mouse_factory=PynputMouse,
                 shortcuts_factory=GlobalShortcuts):
        self.root = root
        self.path = Path(path) if path is not None else config_path()
        settings, warning = load_settings(self.path)
        self.vars = {name: tk.StringVar(root, value=value) for name, value in asdict(settings).items()}
        self.interval_vars = {name: tk.StringVar(root, value=value)
                              for name, value in split_interval(settings).items()}
        self.errors = {name: tk.StringVar(root) for name in self.vars}
        self.controls = {}
        self.mode_controls = []
        self.commands = queue.Queue(maxsize=32)
        self.key_errors = queue.SimpleQueue()
        self.engine = ClickEngine(mouse_factory)
        self._mouse_factory = mouse_factory
        self.closing = False
        self.hotkey_error = ""
        self.notice = tk.StringVar(root, value=warning)
        self.status = tk.StringVar(root, value="Ready")
        self.counter = tk.StringVar(root, value="Clicks: 0   |   Repeats: 0")
        self.picker = self.hotkey_dialog = self.help_dialog = None
        self._pick_after = self._resize_after = None
        self.shortcuts = shortcuts_factory(self.enqueue)
        self.shortcuts.configure(settings.toggle_key, settings.stop_key)
        self._build()
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<Escape>", lambda _event: self.stop())
        self.shortcuts.start()
        self._tick()

    def _build(self):
        root = self.root
        root.title("Auto Clicker")
        style = ttk.Style(root)
        if sys.platform == "win32":
            font.nametofont("TkDefaultFont").configure(family="Segoe UI", size=9)
        style.theme_use("vista" if "vista" in style.theme_names() else "clam")
        style.configure("Error.TLabel", foreground="#ad1f2d")
        style.configure("Status.TLabel", foreground="#205d37")
        self._scale = float(root.tk.call("tk", "scaling")) / (96 / 72)
        u = self._u
        body = ttk.Frame(root, padding=u(8))
        body.grid(sticky="nsew")
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)
        body.columnconfigure((0, 1), weight=1, uniform="halves")

        interval = ttk.LabelFrame(body, text="Click interval", padding=(u(6), u(5)))
        interval.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, u(7)))
        interval.columnconfigure(0, weight=1)
        units = ttk.Frame(interval)
        units.grid(row=0, column=0, sticky="ew")
        for col, (name, label) in enumerate((("hours", "hours"), ("minutes", "mins"),
                                             ("seconds", "secs"), ("milliseconds", "milliseconds"))):
            box = ttk.Frame(units)
            box.grid(row=0, column=col, sticky="ew", padx=(0, u(7) if col < 3 else 0))
            units.columnconfigure(col, weight=1)
            box.columnconfigure(0, weight=1)
            entry = ttk.Entry(box, textvariable=self.interval_vars[name], width=5, justify="right")
            entry.grid(row=0, column=0, sticky="ew")
            ttk.Label(box, text=label).grid(row=0, column=1, padx=(u(4), 0))
            self.controls[name] = (entry, False)
        delay_row = ttk.Frame(interval)
        delay_row.grid(row=1, column=0, sticky="w", pady=(u(6), 0))
        ttk.Label(delay_row, text="Start delay:").grid(row=0, column=0)
        delay = ttk.Entry(delay_row, textvariable=self.vars["delay"], width=6, justify="right")
        delay.grid(row=0, column=1, padx=u(5))
        ttk.Label(delay_row, text="seconds").grid(row=0, column=2)
        self.controls["delay"] = (delay, False)
        self._error_area(interval, ("interval", "unit", "delay"), row=2)

        options = ttk.LabelFrame(body, text="Click options", padding=u(6))
        options.grid(row=1, column=0, sticky="nsew", padx=(0, u(5)), pady=(0, u(7)))
        options.columnconfigure(1, weight=1)
        self._choice(options, "button", "Mouse button:", ("Left", "Right", "Middle"), 0)
        self._choice(options, "mode", "Click type:", ("Single", "Double"), 1)
        self._error_area(options, ("button", "mode"), row=2, span=2)

        repeat = ttk.LabelFrame(body, text="Click repeat", padding=u(6))
        repeat.grid(row=1, column=1, sticky="nsew", padx=(u(5), 0), pady=(0, u(7)))
        repeat.columnconfigure(1, weight=1)
        counted = ttk.Radiobutton(repeat, text="Repeat", variable=self.vars["repeat"], value="count")
        counted.grid(row=0, column=0, sticky="w")
        count = ttk.Spinbox(repeat, from_=1, to=2_147_483_647, textvariable=self.vars["count"],
                            width=5, justify="right")
        count.grid(row=0, column=1, sticky="ew", padx=u(5))
        ttk.Label(repeat, text="times").grid(row=0, column=2)
        forever = ttk.Radiobutton(repeat, text="Repeat until stopped", variable=self.vars["repeat"], value="infinite")
        forever.grid(row=1, column=0, columnspan=3, sticky="w", pady=(u(7), 0))
        self.mode_controls.extend((counted, forever))
        self.controls["count"] = (count, False)
        self._error_area(repeat, ("repeat", "count"), row=2, span=3)

        position = ttk.LabelFrame(body, text="Cursor position", padding=(u(6), u(7)))
        position.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(0, u(8)))
        position.columnconfigure((4, 6), weight=1)
        current = ttk.Radiobutton(position, text="Current location", variable=self.vars["position"], value="cursor")
        current.grid(row=0, column=0, sticky="w", padx=(0, u(10)))
        fixed = ttk.Radiobutton(position, variable=self.vars["position"], value="fixed")
        fixed.grid(row=0, column=1, sticky="e")
        self.pick_button = ttk.Button(position, text="Pick location", command=self.pick_location, padding=(u(6), u(3)))
        self.pick_button.grid(row=0, column=2, padx=(0, u(7)))
        self.mode_controls.extend((current, fixed))
        for col, name in ((3, "x"), (5, "y")):
            ttk.Label(position, text=name.upper()).grid(row=0, column=col, padx=(0, u(3)))
            widget = ttk.Entry(position, textvariable=self.vars[name], width=5, justify="right")
            widget.grid(row=0, column=col + 1, sticky="ew", padx=(0, u(5) if name == "x" else 0))
            self.controls[name] = (widget, False)
        self._error_area(position, ("position", "x", "y"), row=1, span=7)

        buttons = ttk.Frame(body)
        buttons.grid(row=3, column=0, columnspan=2, sticky="ew")
        buttons.columnconfigure((0, 1), weight=1, uniform="actions")
        self.start_button = ttk.Button(buttons, command=self.start, padding=(u(6), u(8)))
        self.stop_button = ttk.Button(buttons, command=self.stop, padding=(u(6), u(8)))
        self.hotkey_button = ttk.Button(buttons, text="Hotkey setting", command=self.show_hotkeys, padding=(u(6), u(8)))
        self.help_button = ttk.Button(buttons, text="Help? >>", command=self.show_help, padding=(u(6), u(8)))
        for row, pair in enumerate(((self.start_button, self.stop_button), (self.hotkey_button, self.help_button))):
            for col, widget in enumerate(pair):
                widget.grid(row=row, column=col, sticky="ew", padx=(0, u(9)) if col == 0 else (u(9), 0),
                            pady=(u(3), u(3)))
        self.status_label = ttk.Label(body, textvariable=self.status, style="Status.TLabel")
        self.status_label.grid(row=4, column=0, sticky="w", pady=(u(6), 0))
        ttk.Label(body, textvariable=self.counter).grid(row=4, column=1, sticky="e", pady=(u(6), 0))
        self.notice_label = ttk.Label(body, textvariable=self.notice, style="Error.TLabel", wraplength=u(380))
        self.notice_label.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(u(4), 0))
        self.notice.trace_add("write", lambda *_: self._notice_changed())
        self._notice_changed()
        for name, var in self.vars.items():
            var.trace_add("write", lambda *_args, key=name: self._edited(key))
        for var in self.interval_vars.values():
            var.trace_add("write", lambda *_: self._edited("interval"))
        self._update_shortcut_labels()
        self._lock_controls(False)
        root.update_idletasks()
        root.minsize(root.winfo_reqwidth(), root.winfo_reqheight())
        root.resizable(True, True)

    def _u(self, value):
        return max(1, round(value * self._scale)) if value else 0

    def _choice(self, parent, name, label, choices, row):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, self._u(7)), pady=self._u(4))
        display = tk.StringVar(parent, value=self.vars[name].get().title())
        combo = ttk.Combobox(parent, values=choices, textvariable=display, state="readonly", width=8)
        combo.grid(row=row, column=1, sticky="ew", pady=self._u(4))
        display.trace_add("write", lambda *_: self.vars[name].set(display.get().lower()))

        def model_changed(*_):
            value = self.vars[name].get().title()
            if display.get() != value:
                display.set(value)

        self.vars[name].trace_add("write", model_changed)
        self.controls[name] = (combo, True)

    def _error_area(self, parent, names, *, row, span=1):
        message = tk.StringVar(parent)
        label = ttk.Label(parent, textvariable=message, style="Error.TLabel", wraplength=self._u(175 if span in (2, 3) else 380))
        label.grid(row=row, column=0, columnspan=span, sticky="ew", pady=(self._u(4), 0))
        label.grid_remove()

        def refresh(*_):
            text = "\n".join(dict.fromkeys(self.errors[name].get() for name in names if self.errors[name].get()))
            message.set(text)
            label.grid() if text else label.grid_remove()
            self._schedule_resize()

        for name in names:
            self.errors[name].trace_add("write", refresh)

    def _notice_changed(self):
        self.notice_label.grid() if self.notice.get() else self.notice_label.grid_remove()
        self._schedule_resize()

    def _schedule_resize(self):
        if not self.closing and self._resize_after is None:
            self._resize_after = self.root.after_idle(self._fit_height)

    def _fit_height(self):
        self._resize_after = None
        if self.closing:
            return
        self.root.update_idletasks()
        width, height = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        self.root.minsize(width, height)
        if height > self.root.winfo_height() and self.root.state() != "withdrawn":
            self.root.geometry(f"{max(width, self.root.winfo_width())}x{height}")

    def _update_shortcut_labels(self):
        key = self.vars["toggle_key"].get()
        self.start_button.configure(text=f"Start ({key})")
        self.stop_button.configure(text=f"Stop ({key})")

    def enqueue(self, command, detail=""):
        if command == "hotkey_error":
            self.key_errors.put(detail)
            self.engine.stop()
            return
        try:
            self.commands.put_nowait((command, detail))
        except queue.Full:
            if command in ("stop", "toggle"):
                self.engine.stop()

    def _edited(self, name):
        self.errors[name].set("")
        if name in ("repeat", "position"):
            self._lock_controls(self.engine.snapshot().active)

    def _settings(self):
        data = {key: var.get().strip() for key, var in self.vars.items()}
        data["interval"] = combine_interval({key: var.get() for key, var in self.interval_vars.items()})
        data["unit"] = "ms"
        return Settings(**data).validated()

    def _lock_controls(self, active):
        busy = active or self.closing or self.picker is not None
        for name, (widget, combo) in self.controls.items():
            disabled = busy or (name == "count" and self.vars["repeat"].get() != "count")
            disabled = disabled or (name in ("x", "y") and self.vars["position"].get() != "fixed")
            widget.configure(state="disabled" if disabled else "readonly" if combo else "normal")
        for widget in self.mode_controls:
            widget.configure(state="disabled" if busy else "normal")
        self.pick_button.configure(state="disabled" if busy else "normal")
        self.hotkey_button.configure(state="disabled" if busy else "normal")
        self.start_button.configure(state="disabled" if busy else "normal")
        self.stop_button.configure(state="normal" if (active or self.picker is not None) and not self.closing else "disabled")

    def start(self):
        if self.closing or self.picker is not None or self.hotkey_dialog is not None or self.engine.snapshot().active:
            return
        for error in self.errors.values():
            error.set("")
        try:
            settings = self._settings()
        except ValidationError as exc:
            for key, message in exc.errors.items():
                self.errors[key].set(message)
            self.status.set("Error: check settings")
            self.status_label.configure(foreground="#ad1f2d")
            return
        self.notice.set(self.hotkey_error)
        self._save(settings)
        self.engine.start(settings)
        self._render()

    def _save(self, settings):
        try:
            save_settings(self.path, settings)
        except OSError as exc:
            self.notice.set(f"Could not save settings: {exc}")

    def stop(self):
        if self.picker is not None:
            self._end_pick()
        self.engine.stop()
        self._render()

    def _render(self):
        snap = self.engine.snapshot()
        labels = {"ready": "Ready", "waiting": f"Waiting: {snap.remaining:.1f} s", "running": "Running",
                  "stopping": "Stopping...", "stopped": "Stopped", "error": "Error", "closed": "Stopped"}
        self.status.set("Closing..." if self.closing else labels[snap.state])
        key_failure = bool(self.hotkey_error) and not snap.active and not self.closing
        if key_failure and snap.state != "error":
            self.status.set("Hotkey error")
        self.status_label.configure(foreground="#ad1f2d" if snap.state == "error" or key_failure else "#205d37")
        self.counter.set(f"Clicks: {snap.clicks}   |   Repeats: {snap.repetitions}")
        if snap.error:
            self.notice.set(snap.error)
        self._lock_controls(snap.active)

    def _tick(self):
        if self.closing:
            engine_done = self.engine.close()
            keys_done = self.shortcuts.close()
            if engine_done and keys_done:
                self.root.destroy()
                return
        else:
            self.shortcuts.check()
            pending = []
            for _ in range(32):
                try:
                    pending.append(self.commands.get_nowait()[0])
                except queue.Empty:
                    break
            errors = []
            while not self.key_errors.empty():
                errors.append(self.key_errors.get_nowait())
            if errors:
                self.hotkey_error = errors[-1]
                self.notice.set(self.hotkey_error)
                self.stop()
            if "stop" in pending:
                self.stop()
            elif not errors:
                if "toggle" in pending:
                    self.stop() if self.engine.snapshot().active else self.start()
                elif "start" in pending:
                    self.start()
        if not any(error.get() for error in self.errors.values()):
            self._render()
        self.root.after(50, self._tick)

    def _dialog(self, title):
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        dialog.transient(self.root)
        dialog.resizable(False, False)
        frame = ttk.Frame(dialog, padding=self._u(12))
        frame.grid(sticky="nsew")
        dialog.update_idletasks()
        dialog.geometry(f"+{self.root.winfo_rootx() + self._u(20)}+{self.root.winfo_rooty() + self._u(40)}")
        return dialog, frame

    def show_hotkeys(self):
        if self.engine.snapshot().active or self.closing or self.picker is not None:
            return
        if self.hotkey_dialog is not None:
            self.hotkey_dialog.lift()
            return
        dialog, frame = self._dialog("Hotkey setting")
        self.hotkey_dialog = dialog
        self.hotkey_vars = {name: tk.StringVar(dialog, value=self.vars[name].get())
                            for name in ("toggle_key", "stop_key")}
        for row, (name, label) in enumerate((("toggle_key", "Start / Stop:"), ("stop_key", "Emergency stop:"))):
            ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, self._u(12)), pady=self._u(4))
            ttk.Combobox(frame, textvariable=self.hotkey_vars[name], values=FUNCTION_KEYS,
                         state="readonly", width=8).grid(row=row, column=1, sticky="ew")
        self.hotkey_message = tk.StringVar(dialog)
        ttk.Label(frame, textvariable=self.hotkey_message, style="Error.TLabel", wraplength=self._u(260)).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=self._u(5))
        ttk.Button(frame, text="Save", command=self._apply_hotkeys).grid(row=3, column=0, sticky="ew", pady=self._u(5))
        ttk.Button(frame, text="Cancel", command=self._close_hotkeys).grid(row=3, column=1, sticky="ew", padx=(self._u(8), 0))
        dialog.protocol("WM_DELETE_WINDOW", self._close_hotkeys)
        dialog.bind("<Escape>", lambda _event: self._close_hotkeys())
        dialog.grab_set()

    def _apply_hotkeys(self):
        toggle, stop = (self.hotkey_vars[key].get() for key in ("toggle_key", "stop_key"))
        try:
            validate_hotkeys(toggle, stop)
            self.shortcuts.configure(toggle, stop)
        except (ValidationError, OSError, RuntimeError) as exc:
            self.hotkey_message.set(str(exc))
            return
        self.vars["toggle_key"].set(toggle)
        self.vars["stop_key"].set(stop)
        self._update_shortcut_labels()
        previous, _warning = load_settings(self.path)
        try:
            settings = self._settings()
        except ValidationError:
            settings = replace(previous, toggle_key=toggle, stop_key=stop)
        self._save(settings)
        self._close_hotkeys()

    def _close_hotkeys(self):
        if self.hotkey_dialog is not None:
            self.hotkey_dialog.destroy()
            self.hotkey_dialog = None

    def pick_location(self):
        if self.closing or self.engine.snapshot().active or self.picker is not None:
            return
        dialog, frame = self._dialog("Pick location")
        self.picker = dialog
        self.pick_message = tk.StringVar(dialog)
        ttk.Label(frame, textvariable=self.pick_message, justify="center").grid(padx=self._u(15), pady=self._u(8))
        ttk.Button(frame, text="Cancel (Esc)", command=self._end_pick).grid(sticky="ew", pady=self._u(5))
        dialog.protocol("WM_DELETE_WINDOW", self._end_pick)
        dialog.bind("<Escape>", lambda _event: self._end_pick())
        self._pick_countdown(3)
        self._lock_controls(False)

    def _pick_countdown(self, seconds):
        self._pick_after = None
        if self.picker is None or self.closing:
            return
        if seconds == 0:
            self._capture_position()
            return
        self.pick_message.set(f"Move the pointer to your target.\nCapturing in {seconds} seconds...\nNo click will be sent.")
        self._pick_after = self.root.after(1000, self._pick_countdown, seconds - 1)

    def _capture_position(self):
        if self.picker is None:
            return
        try:
            x, y = self._mouse_factory().position
            replace(Settings(), position="fixed", x=str(x), y=str(y)).validated()
        except Exception as exc:
            self._end_pick()
            self.notice.set(f"Could not read cursor position: {exc}")
            return
        self.vars["x"].set(str(x))
        self.vars["y"].set(str(y))
        self.vars["position"].set("fixed")
        self._end_pick()
        self.root.lift()

    def _end_pick(self):
        if self._pick_after is not None:
            self.root.after_cancel(self._pick_after)
            self._pick_after = None
        if self.picker is not None:
            self.picker.destroy()
            self.picker = None
        self._lock_controls(self.engine.snapshot().active)

    def show_help(self):
        if self.help_dialog is not None:
            self.help_dialog.lift()
            return
        dialog, frame = self._dialog("Help")
        self.help_dialog = dialog
        toggle, stop = self.vars["toggle_key"].get(), self.vars["stop_key"].get()
        text = (f"{toggle}: start / stop from any window.\n{stop}: emergency stop. Esc: stop in this window.\n\n"
                "Interval = hours + minutes + seconds + milliseconds.\nMinimum total: 1 ms. Start delay is in seconds.\n\n"
                "One repeat = one click or one double-click pair.\n3 Double repeats = 6 clicks.\n\n"
                "Current location follows the pointer. Fixed X/Y uses\nscreen pixels; negative coordinates are allowed.\n"
                "Pick location reads the pointer after 3 seconds.\nEsc or Cancel leaves the previous position unchanged.\n\n"
                "Settings lock during a run. Stop cancels all waits;\na click already in progress finishes first.\n"
                "Settings are saved. Reopening never starts clicking.")
        ttk.Label(frame, text=text, justify="left").grid(sticky="w")
        ttk.Button(frame, text="Close", command=self._close_help).grid(sticky="e", pady=(self._u(12), 0))
        dialog.protocol("WM_DELETE_WINDOW", self._close_help)
        dialog.bind("<Escape>", lambda _event: self._close_help())

    def _close_help(self):
        if self.help_dialog is not None:
            self.help_dialog.destroy()
            self.help_dialog = None

    def close(self):
        if self.closing:
            return
        self.closing = True
        self._end_pick()
        self._close_hotkeys()
        self._close_help()
        if self._resize_after is not None:
            self.root.after_cancel(self._resize_after)
            self._resize_after = None
        self.engine.close()
        self.shortcuts.close()
        try:
            save_settings(self.path, self._settings())
        except ValidationError:
            pass
        except OSError as exc:
            print(f"Could not save settings: {exc}", file=sys.stderr)
        self._render()


def run():
    if sys.platform == "win32":
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            pass
    root = tk.Tk()
    App(root)
    root.mainloop()
