"""ttk interface. Only the Tk thread reads variables or changes widgets."""
from dataclasses import asdict
from pathlib import Path
import queue
import sys
import tkinter as tk
from tkinter import ttk

from ac_engine import ClickEngine, PynputMouse
from ac_hotkeys import GlobalShortcuts
from ac_settings import Settings, ValidationError, config_path, load_settings, save_settings


class App:
    def __init__(self, root, *, path=None, mouse_factory=PynputMouse, shortcuts_factory=GlobalShortcuts):
        self.root = root
        self.path = Path(path) if path is not None else config_path()
        settings, warning = load_settings(self.path)
        self.vars = {name: tk.StringVar(root, value=value) for name, value in asdict(settings).items()}
        self.errors = {}
        self.controls = {}
        # A bounded command queue prevents key-repeat flooding while Tk is busy.
        self.commands = queue.Queue(maxsize=32)
        self.key_errors = queue.SimpleQueue()
        self.engine = ClickEngine(mouse_factory)
        self.closing = False
        self.hotkey_error = ""
        self.notice = tk.StringVar(root, value=warning)
        self.status = tk.StringVar(root, value="Готов")
        self.counter = tk.StringVar(root, value="Клики: 0   ·   Повторения: 0")
        self.shortcuts = shortcuts_factory(self.enqueue)
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<Escape>", lambda _event: self.stop())
        self.shortcuts.start()
        self._tick()

    def _build(self):
        root = self.root
        root.title("Autoclicker")
        root.configure(background="#f3f5f9")
        style = ttk.Style(root)
        style.theme_use("clam")
        style.configure(".", font=("Segoe UI", 10))
        style.configure("TFrame", background="#f3f5f9")
        style.configure("TLabel", background="#f3f5f9", foreground="#17233b")
        style.configure("TLabelframe", background="#f3f5f9", padding=14)
        style.configure("TLabelframe.Label", background="#f3f5f9", font=("Segoe UI", 11, "bold"))
        style.configure("TButton", padding=(14, 9))
        style.configure("Start.TButton", background="#176b50", foreground="white", font=("Segoe UI", 11, "bold"))
        style.map("Start.TButton", background=[("disabled", "#b6c7c1"), ("active", "#0d513c")])
        style.configure("Stop.TButton", background="#ae3042", foreground="white", font=("Segoe UI", 11, "bold"))
        style.map("Stop.TButton", background=[("disabled", "#d5bdc1"), ("active", "#8a2030")])
        style.configure("Error.TLabel", foreground="#ac2438", font=("Segoe UI", 9))
        style.configure("Hint.TLabel", foreground="#526078", font=("Segoe UI", 9))
        style.configure("Title.TLabel", font=("Segoe UI", 22, "bold"))
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)
        header = ttk.Frame(root, padding=(24, 18, 24, 10))
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text="Autoclicker", style="Title.TLabel").pack(anchor="w")
        ttk.Label(header, text="Настройте действие. Запускайте и останавливайте из любого окна.",
                  style="Hint.TLabel").pack(anchor="w", pady=(4, 0))

        # Scrollable form keeps controls reachable on smaller or scaled displays.
        area = ttk.Frame(root)
        area.grid(row=1, column=0, sticky="nsew", padx=20)
        area.columnconfigure(0, weight=1)
        area.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(area, background="#f3f5f9", highlightthickness=0)
        bar = ttk.Scrollbar(area, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=bar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        bar.grid(row=0, column=1, sticky="ns")
        form = ttk.Frame(self.canvas, padding=4)
        window = self.canvas.create_window(0, 0, window=form, anchor="nw")
        self.canvas.bind("<Configure>", lambda event: self.canvas.itemconfigure(window, width=event.width))
        form.bind("<Configure>", lambda _event: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        root.bind("<MouseWheel>", lambda event: self.canvas.yview_scroll(-int(event.delta / 120), "units"))
        form.columnconfigure((0, 1), weight=1, uniform="groups")

        action = self._group(form, "Действие", 0, 0)
        self._field(action, "interval", "Интервал", 0)
        self._field(action, "unit", "Единицы: ms — миллисекунды, s — секунды", 1, ("ms", "s"))
        self._field(action, "button", "Кнопка: left / right / middle", 2, ("left", "right", "middle"))
        self._field(action, "mode", "Клик: single — один, double — двойной", 3, ("single", "double"))
        timing = self._group(form, "Повторения и запуск", 0, 1)
        self._field(timing, "repeat", "Режим: infinite — без лимита, count — число", 0, ("infinite", "count"))
        self._field(timing, "count", "Количество повторений", 1)
        self._field(timing, "delay", "Задержка перед запуском (секунды)", 2)
        ttk.Label(timing, text="1 повторение = 1 клик или пара кликов.\nStop отменяет и задержку запуска.",
                  style="Hint.TLabel", wraplength=290).grid(row=9, column=0, sticky="w", pady=8)
        position = self._group(form, "Позиция курсора", 1, 0, span=2)
        self._field(position, "position", "cursor — текущая позиция, fixed — координаты экрана", 0, ("cursor", "fixed"))
        coords = ttk.Frame(position)
        coords.grid(row=3, column=0, sticky="ew")
        coords.columnconfigure((0, 1), weight=1)
        for col, key in enumerate(("x", "y")):
            box = ttk.Frame(coords, padding=(0, 0, 12 if col == 0 else 0, 0))
            box.grid(row=0, column=col, sticky="ew")
            box.columnconfigure(0, weight=1)
            self._field(box, key, key.upper() + " (пиксели; допустимы отрицательные)", 0)
        ttk.Label(position, text="В fixed курсор перемещается перед каждым повторением. Учитывайте расположение мониторов.",
                  wraplength=650, style="Hint.TLabel").grid(row=4, column=0, sticky="w", pady=(6, 0))

        footer = ttk.Frame(root, padding=(24, 12, 24, 18))
        footer.grid(row=2, column=0, sticky="ew")
        footer.columnconfigure((0, 1), weight=1)
        self.status_label = ttk.Label(footer, textvariable=self.status, font=("Segoe UI", 12, "bold"))
        self.status_label.grid(row=0, column=0, sticky="w")
        ttk.Label(footer, textvariable=self.counter).grid(row=0, column=1, sticky="e")
        self.start_button = ttk.Button(footer, text="▶  Start  ·  F6", style="Start.TButton", command=self.start)
        self.stop_button = ttk.Button(footer, text="■  Stop  ·  F7", style="Stop.TButton", command=self.stop)
        self.start_button.grid(row=1, column=0, sticky="ew", padx=(0, 6), pady=10)
        self.stop_button.grid(row=1, column=1, sticky="ew", padx=(6, 0), pady=10)
        ttk.Label(footer, text="Глобально: F6 — запуск · F7 — остановка. Esc — остановка в окне.",
                  style="Hint.TLabel").grid(row=2, column=0, columnspan=2, sticky="w")
        ttk.Label(footer, textvariable=self.notice, style="Error.TLabel", wraplength=700).grid(
            row=3, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        for key, var in self.vars.items():
            var.trace_add("write", lambda *_args, name=key: self._edited(name))
        root.update_idletasks()
        # Tk uses points for fonts and Windows DPI for scaling; no forced tk scaling.
        width = min(root.winfo_screenwidth() - 60, max(800, root.winfo_reqwidth()))
        height = min(root.winfo_screenheight() - 100, 830)
        root.geometry(f"{width}x{height}")
        root.minsize(min(width, 680), min(height, 540))
        self._lock_controls(False)

    def _group(self, parent, title, row, col, span=1):
        group = ttk.LabelFrame(parent, text=title)
        group.grid(row=row, column=col, columnspan=span, sticky="nsew", padx=5, pady=6)
        group.columnconfigure(0, weight=1)
        return group

    def _field(self, parent, name, label, row, choices=None):
        row *= 3
        ttk.Label(parent, text=label, wraplength=310 if name != "position" else 650).grid(
            row=row, column=0, sticky="w", pady=(4, 4))
        if choices:
            widget = ttk.Combobox(parent, textvariable=self.vars[name], values=choices, state="readonly", width=12)
        else:
            widget = ttk.Entry(parent, textvariable=self.vars[name], width=14)
        widget.grid(row=row + 1, column=0, sticky="ew")
        self.controls[name] = (widget, bool(choices))
        error = tk.StringVar(parent)
        self.errors[name] = error
        ttk.Label(parent, textvariable=error, style="Error.TLabel", wraplength=300).grid(
            row=row + 2, column=0, sticky="w", pady=(2, 3))

    def enqueue(self, command, detail=""):
        if command == "hotkey_error":
            self.key_errors.put(detail)
            self.engine.stop()
            return
        try:
            self.commands.put_nowait((command, detail))
        except queue.Full:
            # Stop/error must never be lost behind repeated Start requests.
            if command != "start":
                self.engine.stop()

    def _edited(self, name):
        self.errors[name].set("")
        if name in ("repeat", "position"):
            self._lock_controls(self.engine.snapshot().active)

    def _settings(self):
        return Settings(**{key: var.get().strip() for key, var in self.vars.items()}).validated()

    def _lock_controls(self, active):
        for name, (widget, combo) in self.controls.items():
            disabled = active or self.closing or (name == "count" and self.vars["repeat"].get() != "count")
            disabled = disabled or (name in ("x", "y") and self.vars["position"].get() != "fixed")
            widget.configure(state="disabled" if disabled else "readonly" if combo else "normal")
        self.start_button.configure(state="disabled" if active or self.closing else "normal")
        self.stop_button.configure(state="normal" if active and not self.closing else "disabled")

    def start(self):
        if self.closing or self.engine.snapshot().active:
            return
        for error in self.errors.values():
            error.set("")
        try:
            settings = self._settings()
        except ValidationError as exc:
            for key, message in exc.errors.items():
                self.errors[key].set(message)
            self.status.set("Ошибка · проверьте настройки")
            self.status_label.configure(foreground="#ac2438")
            return
        self.notice.set(self.hotkey_error)
        try:
            save_settings(self.path, settings)
        except OSError as exc:
            self.notice.set(f"Не удалось сохранить настройки: {exc}")
        self.engine.start(settings)
        self._render()

    def stop(self):
        self.engine.stop()
        self._render()

    def _render(self):
        snap = self.engine.snapshot()
        labels = {"ready": "Готов", "waiting": f"Ожидание · {snap.remaining:.1f} с",
                  "running": "Работает", "stopping": "Остановка…", "stopped": "Остановлен",
                  "error": "Ошибка", "closed": "Остановлен"}
        self.status.set("Завершение…" if self.closing else labels[snap.state])
        key_failure = bool(self.hotkey_error) and not snap.active and not self.closing
        if key_failure and snap.state != "error":
            self.status.set("Ошибка горячих клавиш · доступен Start")
        self.status_label.configure(foreground="#ac2438" if snap.state == "error" or key_failure else "#176b50")
        self.counter.set(f"Клики: {snap.clicks}   ·   Повторения: {snap.repetitions}")
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
            # Process a bounded snapshot, coalescing keys; Stop wins within this poll.
            pending = []
            for _ in range(32):
                try:
                    pending.append(self.commands.get_nowait())
                except queue.Empty:
                    break
            errors = []
            while not self.key_errors.empty():
                errors.append(self.key_errors.get_nowait())
            if errors:
                self.hotkey_error = errors[-1]
                self.notice.set(self.hotkey_error)
                self.engine.stop()
            if any(command == "stop" for command, _ in pending):
                self.stop()
            elif any(command == "start" for command, _ in pending) and not errors:
                self.start()
        # Preserve field-validation status until the user edits or starts again.
        if not any(error.get() for error in self.errors.values()):
            self._render()
        self.root.after(50, self._tick)

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.engine.close()
        self.shortcuts.close()
        try:
            save_settings(self.path, self._settings())
        except ValidationError:
            pass  # Keep the last valid settings rather than overwrite them with bad input.
        except OSError as exc:
            # Closing must still finish even if the settings disk is unavailable.
            print(f"Не удалось сохранить настройки: {exc}", file=sys.stderr)
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
