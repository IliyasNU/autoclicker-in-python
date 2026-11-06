"""Single-worker click engine. No tkinter or OS hooks at import time."""
from dataclasses import dataclass
import threading
import time


@dataclass(frozen=True)
class Snapshot:
    state: str
    repetitions: int
    clicks: int
    error: str
    active: bool
    remaining: float


class ClickEngine:
    def __init__(self, mouse_factory, *, event_factory=threading.Event, clock=time.monotonic):
        self._factory = mouse_factory
        self._event_factory = event_factory
        self._clock = clock
        self._lock = threading.RLock()
        self._thread = None
        self._stop = None
        self._closed = False
        self._state = "ready"
        self._repetitions = self._clicks = 0
        self._error = ""
        self._deadline = 0

    def snapshot(self):
        with self._lock:
            return Snapshot(self._state, self._repetitions, self._clicks, self._error,
                            self._thread is not None and self._thread.is_alive(),
                            max(0, self._deadline - self._clock()) if self._state == "waiting" else 0)

    def start(self, settings):
        settings.validated()  # No lifecycle mutation until validation succeeds.
        with self._lock:
            if self._closed or (self._thread and self._thread.is_alive()):
                return False
            self._stop = self._event_factory()
            self._repetitions = self._clicks = 0
            self._error = ""
            self._state = "waiting" if float(settings.delay) else "running"
            self._deadline = self._clock() + float(settings.delay)
            self._thread = threading.Thread(target=self._run, args=(settings, self._stop),
                                            name="autoclicker-worker", daemon=False)
            try:
                self._thread.start()
            except Exception as exc:
                self._state, self._error = "error", str(exc)
                self._thread = None
                return False
            return True

    def stop(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                self._stop.set()
                if self._state in ("waiting", "running"):
                    self._state = "stopping"

    def close(self, timeout=0):
        """Irreversibly stop; return False while an in-flight OS call is finishing."""
        with self._lock:
            self._closed = True
            self.stop()
            thread = self._thread
        if thread is not None:
            thread.join(timeout)
        with self._lock:
            done = thread is None or not thread.is_alive()
            if done:
                self._state = "closed"
            return done

    def wait(self, timeout=2):
        """Join for tests/CLI callers; never call with a blocking timeout from Tk."""
        with self._lock:
            thread = self._thread
        if thread:
            thread.join(timeout)
        return not (thread and thread.is_alive())

    def _run(self, settings, stop):
        try:
            delay = float(settings.delay)
            if delay and stop.wait(delay):
                return
            if stop.is_set():
                return
            mouse = self._factory()
            with self._lock:
                if stop.is_set():
                    return
                self._state = "running"
            while not stop.is_set():
                if settings.coordinates is not None:
                    mouse.position = settings.coordinates
                if stop.is_set():
                    break
                # One repeat is an indivisible single click or double-click pair.
                mouse.click(settings.button, settings.clicks_per_repeat)
                with self._lock:
                    self._repetitions += 1
                    self._clicks += settings.clicks_per_repeat
                    finished = settings.limit is not None and self._repetitions >= settings.limit
                if finished or stop.wait(settings.seconds):
                    break
        except Exception as exc:
            with self._lock:
                self._state, self._error = "error", f"Ошибка управления мышью: {exc}"
        finally:
            with self._lock:
                if self._state != "error":
                    self._state = "stopped"


class PynputMouse:
    def __init__(self):
        from pynput.mouse import Button, Controller
        self._buttons = {name: getattr(Button, name) for name in ("left", "right", "middle")}
        self._controller = Controller()

    @property
    def position(self):
        return self._controller.position

    @position.setter
    def position(self, value):
        self._controller.position = value

    def click(self, button, count):
        self._controller.click(self._buttons[button], count)
