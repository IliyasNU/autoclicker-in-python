"""Lazy global shortcuts; callbacks only enqueue commands, never touch tkinter."""


class GlobalShortcuts:
    def __init__(self, enqueue, listener_factory=None):
        self._enqueue = enqueue
        self._factory = listener_factory
        self._listener = None
        self._closing = False
        self._failed = False

    def start(self):
        if self._listener is not None:
            return
        try:
            if self._factory is None:
                self._factory = self._make_listener
            self._listener = self._factory({
                "<f6>": lambda: self._enqueue("start"),
                "<f7>": lambda: self._enqueue("stop"),
            })
            self._listener.start()
        except Exception as exc:
            self._failed = True
            self._enqueue("hotkey_error", f"Горячие клавиши недоступны: {exc}")

    def _make_listener(self, callbacks):
        from pynput.keyboard import Key, Listener
        actions = {Key.f6: callbacks["<f6>"], Key.f7: callbacks["<f7>"]}
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
