"""Lazy global shortcuts; callbacks only enqueue commands, never touch tkinter."""
import threading

from ac_settings import FUNCTION_KEYS, validate_hotkeys


class GlobalShortcuts:
    def __init__(self, enqueue, listener_factory=None):
        self._enqueue = enqueue
        self._factory = listener_factory
        self._listener = None
        self._closing = False
        self._failed = False
        self._lock = threading.Lock()
        self._toggle_key = "F6"
        self._stop_key = "F7"

    def configure(self, toggle_key, stop_key):
        """Atomically rebind without stopping/restarting the global listener."""
        validate_hotkeys(toggle_key, stop_key)
        with self._lock:
            self._toggle_key, self._stop_key = toggle_key, stop_key

    def _dispatch(self, key):
        with self._lock:
            command = "toggle" if key == self._toggle_key else "stop" if key == self._stop_key else None
        if command is not None:
            self._enqueue(command)

    def start(self):
        if self._listener is not None:
            return
        try:
            if self._factory is None:
                self._factory = self._make_listener
            self._listener = self._factory({
                f"<{key.lower()}>": lambda name=key: self._dispatch(name) for key in FUNCTION_KEYS
            })
            self._listener.start()
        except Exception as exc:
            self._failed = True
            self._enqueue("hotkey_error", f"Горячие клавиши недоступны: {exc}")

    def _make_listener(self, callbacks):
        from pynput.keyboard import Key, Listener
        actions = {getattr(Key, key[1:-1]): action for key, action in callbacks.items()}
        pressed = set()
        enqueue = self._enqueue

        def on_press(key, *_extra):
            # Accept physical and accessibility-generated keys. GlobalHotKeys
            # in pynput 1.8.2 ignores synthetic keys; this app emits no keys.
            if key in actions and key not in pressed:
                pressed.add(key)
                actions[key]()

        def on_release(key, *_extra):
            pressed.discard(key)

        class GuardedListener(Listener):
            def run(self):
                try:
                    super().run()
                except Exception as exc:
                    enqueue("hotkey_error", f"Ошибка обработчика клавиш: {exc}")

        return GuardedListener(on_press=on_press, on_release=on_release)

    def check(self):
        if self._listener is None or self._failed:
            return
        if not self._listener.is_alive() and not self._closing:
            try:
                self._listener.join(0)  # pynput re-raises callback/backend exceptions here.
                raise RuntimeError("обработчик неожиданно завершился")
            except Exception as exc:
                self._failed = True
                self._enqueue("hotkey_error", f"Ошибка горячих клавиш: {exc}")

    def close(self):
        self._closing = True
        # Retry on each GUI poll: a listener may not have entered run() yet on
        # the first close. pynput.stop() is idempotent once it is running.
        if self._listener is not None and self._listener.is_alive():
            try:
                self._listener.stop()
            except Exception as exc:
                if not self._failed:
                    self._failed = True
                    self._enqueue("hotkey_error", f"Ошибка отключения горячих клавиш: {exc}")
        if self._listener is None:
            return True
        if self._listener.is_alive():
            return False
        try:
            self._listener.join(0)
        except Exception:
            pass  # Already stopped; startup/callback errors were reported by start/check.
        return True
