"""Deterministic tests: fake mouse only, no desktop input or global hooks."""
from dataclasses import asdict, replace
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from ac_engine import ClickEngine, PynputMouse
from ac_hotkeys import GlobalShortcuts
from ac_settings import Settings, ValidationError, load_settings, save_settings


class ControlledEvent:
    """Each wait is advanced explicitly, or interrupted by set()."""
    def __init__(self):
        self.requests = queue.Queue()
        self.lock = threading.Lock()
        self.stopped = False
        self.current = None

    def is_set(self):
        with self.lock:
            return self.stopped

    def set(self):
        with self.lock:
            self.stopped = True
            if self.current:
                self.current.set()

    def wait(self, timeout):
        gate = threading.Event()
        with self.lock:
            if self.stopped:
                return True
            self.current = gate
            self.requests.put((timeout, gate))
        if not gate.wait(5):
            raise AssertionError("Test failed to release controlled wait")
        return self.is_set()

    def next_wait(self):
        return self.requests.get(timeout=2)


class FakeMouse:
    def __init__(self):
        self.position = (50, 60)
        self.calls = []

    def click(self, button, count):
        self.calls.append((button, count, self.position))


class SettingsTests(unittest.TestCase):
    def test_defaults_and_unit_conversion(self):
        self.assertEqual(Settings().validated().seconds, .1)
        self.assertEqual(replace(Settings(), interval="0.2", unit="s").validated().seconds, .2)

    def test_invalid_fields(self):
        bad = {
            "interval": ["", " ", "abc", "-1", "0", "nan", "NaN", "inf", "-inf", "1e999", "0.1", "1,5"],
            "delay": ["", "text", "-1", "nan", "inf", "1e999"],
            "count": ["", "0", "-2", "1.5", "nan", "inf", "999999999999"],
            "x": ["", "1.1", "nan", "inf", "2147483648"],
            "y": ["", "1e2", "-2147483649"],
            "unit": ["minutes"], "button": ["extra"], "mode": ["triple"],
            "repeat": ["once"], "position": ["last"],
        }
        for field, values in bad.items():
            for value in values:
                with self.subTest(field=field, value=value), self.assertRaises(ValidationError) as caught:
                    replace(Settings(), **{field: value}).validated()
                self.assertIn(field, caught.exception.errors)
        with self.assertRaises(ValidationError):
            replace(Settings(), interval=None).validated()

    def test_coordinates_delay_zero_and_max_count(self):
        value = replace(Settings(), x="-1920", y="+20", position="fixed", delay="0",
                        repeat="count", count="2147483647").validated()
        self.assertEqual(value.coordinates, (-1920, 20))
        self.assertEqual(value.limit, 2147483647)

    def test_roundtrip_and_no_autostart_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "settings.json"
            value = replace(Settings(), button="right", mode="double", interval="2", unit="s")
            save_settings(path, value)
            self.assertEqual(load_settings(path), (value, ""))
            doc = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(set(doc), {"version", "settings"})
            self.assertEqual(set(doc["settings"]), set(asdict(Settings())))
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_missing_corrupt_wrong_schema_and_nonfinite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            self.assertEqual(load_settings(path), (Settings(), ""))
            for contents in ("{broken", "[]", '{"version":2}', '{"version":1,"settings":[]}',
                             json.dumps({"version": 1, "settings": asdict(replace(Settings(), interval="nan"))}),
                             json.dumps({"version": 1, "settings": {**asdict(Settings()), "running": True}})):
                with self.subTest(contents=contents):
                    path.write_text(contents, encoding="utf-8")
                    value, warning = load_settings(path)
                    self.assertEqual(value, Settings())
                    self.assertTrue(warning)
            path.write_bytes(b"\xff\xfe")
            self.assertTrue(load_settings(path)[1])

    def test_failed_atomic_replace_preserves_original(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            save_settings(path, Settings())
            with patch("ac_settings.os.replace", side_effect=PermissionError("disk")):
                with self.assertRaises(OSError):
                    save_settings(path, replace(Settings(), button="right"))
            self.assertEqual(load_settings(path), (Settings(), ""))
            self.assertEqual(list(path.parent.glob("*.tmp")), [])


class EngineTests(unittest.TestCase):
    def make_engine(self, mouse=None, factory=None):
        event = ControlledEvent()
        events = iter([event])
        mouse = mouse or FakeMouse()
        engine = ClickEngine(factory or (lambda: mouse), event_factory=lambda: next(events, ControlledEvent()))
        self.addCleanup(engine.close, 2)
        return engine, mouse, event

    def test_bad_input_does_not_latch_start(self):
        engine, mouse, _event = self.make_engine()
        with self.assertRaises(ValidationError):
            engine.start(replace(Settings(), interval="bad"))
        self.assertEqual(engine.snapshot().state, "ready")
        self.assertTrue(engine.start(replace(Settings(), repeat="count", count="1")))
        self.assertTrue(engine.wait())
        self.assertEqual(len(mouse.calls), 1)

    def test_all_buttons_modes_positions_and_exact_repetitions(self):
        for button in ("left", "right", "middle"):
            for mode in ("single", "double"):
                for position in ("cursor", "fixed"):
                    with self.subTest(button=button, mode=mode, position=position):
                        engine, mouse, event = self.make_engine()
                        settings = replace(Settings(), button=button, mode=mode, position=position,
                                           x="-10", y="30", repeat="count", count="3")
                        self.assertTrue(engine.start(settings))
                        for _ in range(2):
                            timeout, gate = event.next_wait()
                            self.assertEqual(timeout, .1)
                            gate.set()
                        self.assertTrue(engine.wait())
                        size = 2 if mode == "double" else 1
                        coords = (-10, 30) if position == "fixed" else (50, 60)
                        self.assertEqual(mouse.calls, [(button, size, coords)] * 3)
                        self.assertEqual(engine.snapshot().clicks, 3 * size)
                        self.assertEqual(engine.snapshot().repetitions, 3)
                        self.assertEqual(engine.snapshot().state, "stopped")

    def test_cancel_delay_never_constructs_mouse(self):
        factory = Mock()
        engine, _, event = self.make_engine(factory=factory)
        engine.start(replace(Settings(), delay="3600"))
        self.assertEqual(event.next_wait()[0], 3600)
        self.assertEqual(engine.snapshot().state, "waiting")
        engine.stop()
        self.assertTrue(engine.wait())
        factory.assert_not_called()
        self.assertEqual(engine.snapshot().clicks, 0)

    def test_delay_release_and_stop_during_large_interval(self):
        engine, mouse, event = self.make_engine()
        engine.start(replace(Settings(), delay="10", interval="3600", unit="s"))
        event.next_wait()[1].set()
        self.assertEqual(event.next_wait()[0], 3600)
        self.assertEqual(len(mouse.calls), 1)
        engine.stop()
        self.assertTrue(engine.wait())
        self.assertEqual(len(mouse.calls), 1)

    def test_repeated_start_and_fast_stop_start_while_in_flight(self):
        entered, release = threading.Event(), threading.Event()
        mouse = FakeMouse()
        original = mouse.click

        def blocked_click(*args):
            entered.set()
            if not release.wait(5):
                raise AssertionError("test release missing")
            original(*args)

        mouse.click = blocked_click
        engine, _, _event = self.make_engine(mouse)
        self.addCleanup(release.set)
        self.assertTrue(engine.start(Settings()))
        self.assertTrue(entered.wait(2))
        self.assertFalse(engine.start(Settings()))
        engine.stop()
        self.assertFalse(engine.start(Settings()))
        release.set()
        self.assertTrue(engine.wait())
        self.assertEqual(len(mouse.calls), 1)
        # Only after the prior thread has exited is another run accepted.
        self.assertTrue(engine.start(replace(Settings(), repeat="count", count="1")))
        self.assertTrue(engine.wait())
        self.assertEqual(engine.snapshot().clicks, 1)
        self.assertEqual(len(mouse.calls), 2)

    def test_factory_click_and_position_exceptions_and_recovery(self):
        for operation in ("factory", "click", "position"):
            with self.subTest(operation=operation):
                mouse = Mock()
                if operation == "position":
                    class BrokenPosition(FakeMouse):
                        @property
                        def position(self):
                            return (0, 0)

                        @position.setter
                        def position(self, _value):
                            raise OSError("position unavailable")
                    factory = lambda: BrokenPosition()
                elif operation == "factory":
                    factory = Mock(side_effect=OSError("mouse unavailable"))
                else:
                    mouse.click.side_effect = OSError("click unavailable")
                    factory = lambda: mouse
                engine, _, _ = self.make_engine(factory=factory)
                engine.start(replace(Settings(), repeat="count", count="1", position="fixed"))
                self.assertTrue(engine.wait())
                self.assertEqual(engine.snapshot().state, "error")
                self.assertTrue(engine.snapshot().error)
                self.assertEqual(engine.snapshot().clicks, 0)
                engine._factory = FakeMouse
                self.assertTrue(engine.start(replace(Settings(), repeat="count", count="1")))
                self.assertTrue(engine.wait())
                self.assertEqual(engine.snapshot().state, "stopped")

    def test_thread_start_failure_and_recovery(self):
        engine, _, _ = self.make_engine()
        with patch("ac_engine.threading.Thread.start", side_effect=RuntimeError("no resources")):
            self.assertFalse(engine.start(Settings()))
        self.assertEqual(engine.snapshot().state, "error")
        self.assertFalse(engine.snapshot().active)
        self.assertTrue(engine.start(replace(Settings(), repeat="count", count="1")))
        self.assertTrue(engine.wait())

    def test_close_cancels_delay_and_prohibits_future_runs(self):
        engine, mouse, event = self.make_engine()
        engine.start(replace(Settings(), delay="86400"))
        event.next_wait()
        self.assertTrue(engine.close(2))
        self.assertEqual(engine.snapshot().state, "closed")
        self.assertFalse(engine.start(Settings()))
        self.assertFalse(engine.snapshot().active)
        self.assertEqual(mouse.calls, [])

    def test_stop_does_not_erase_terminal_state_before_thread_exit(self):
        engine, _, _ = self.make_engine()
        engine._thread = Mock()
        engine._thread.is_alive.return_value = True
        engine._stop = Mock()
        for state in ("stopped", "error"):
            engine._state = state
            engine.stop()
            self.assertEqual(engine.snapshot().state, state)
        engine._thread = None

    def test_concurrent_starts_accept_only_one(self):
        engine, mouse, event = self.make_engine()
        barrier = threading.Barrier(9)
        results = queue.Queue()

        def start():
            barrier.wait(2)
            results.put(engine.start(replace(Settings(), delay="10")))

        callers = [threading.Thread(target=start) for _ in range(8)]
        for caller in callers:
            caller.start()
        barrier.wait(2)
        for caller in callers:
            caller.join(2)
            self.assertFalse(caller.is_alive())
        event.next_wait()
        self.assertEqual(sum(results.get_nowait() for _ in range(8)), 1)
        engine.stop()
        self.assertTrue(engine.wait())
        self.assertEqual(mouse.calls, [])

    def test_pynput_adapter_with_fake_controller(self):
        controller = Mock()
        module = Mock()
        module.Controller.return_value = controller
        module.Button.left = "LEFT"
        with patch.dict(sys.modules, {"pynput.mouse": module}):
            adapter = PynputMouse()
            adapter.position = (-4, 9)
            adapter.click("left", 2)
            self.assertEqual(controller.position, (-4, 9))
            controller.click.assert_called_once_with("LEFT", 2)


class ShortcutTests(unittest.TestCase):
    def test_listener_accepts_accessibility_keys_and_debounces(self):
        captured = {}

        class FakeListener:
            def __init__(self, **callbacks):
                captured.update(callbacks)

        module = Mock()
        module.Listener = FakeListener
        module.Key.f6 = "F6"
        module.Key.f7 = "F7"
        start, stop = Mock(), Mock()
        keys = GlobalShortcuts(Mock())
        with patch.dict(sys.modules, {"pynput.keyboard": module}):
            keys._make_listener({"<f6>": start, "<f7>": stop})
        captured["on_press"]("F6", True)
        captured["on_press"]("F6", True)
        start.assert_called_once()
        captured["on_release"]("F6", True)
        captured["on_press"]("F6", False)
        self.assertEqual(start.call_count, 2)
        captured["on_press"]("F7", False)
        stop.assert_called_once()

    def test_callbacks_enqueue_and_shutdown(self):
        output = Mock()
        listener = Mock()
        listener.is_alive.return_value = True
        factory = Mock(return_value=listener)
        keys = GlobalShortcuts(output, factory)
        keys.start()
        keys.start()
        factory.assert_called_once()
        callbacks = factory.call_args.args[0]
        callbacks["<f6>"]()
        callbacks["<f7>"]()
        self.assertEqual(output.call_args_list, [(('start',), {}), (('stop',), {})])
        self.assertFalse(keys.close())
        listener.is_alive.return_value = False
        self.assertTrue(keys.close())
        listener.stop.assert_called_once()
        listener.join.assert_called_with(0)

    def test_initialization_and_async_failures_visible(self):
        for startup in (True, False):
            with self.subTest(startup=startup):
                output = Mock()
                listener = Mock()
                listener.is_alive.return_value = False
                listener.join.side_effect = OSError("hook failed")
                factory = Mock(side_effect=OSError("init failed")) if startup else Mock(return_value=listener)
                keys = GlobalShortcuts(output, factory)
                keys.start()
                keys.check()
                keys.check()
                output.assert_called_once()
                self.assertEqual(output.call_args.args[0], "hotkey_error")
                self.assertIn("failed", output.call_args.args[1])
                self.assertTrue(keys.close())


class ImportTests(unittest.TestCase):
    def test_imports_do_not_open_windows_start_threads_or_import_pynput(self):
        code = """
import threading, tkinter, sys
before = set(threading.enumerate())
import autoclick, ac_settings, ac_engine, ac_hotkeys, ac_gui
assert set(threading.enumerate()) == before
assert tkinter._default_root is None
assert not any(n == 'pynput' or n.startswith('pynput.') for n in sys.modules)
"""
        result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).parent,
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


class FakeShortcuts:
    def __init__(self, enqueue):
        self.enqueue = enqueue
        self.closed = False

    def start(self):
        pass

    def check(self):
        pass

    def close(self):
        self.closed = True
        return True


class GuiTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from ac_gui import App
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"Tk display unavailable: {exc}")
        self.root.withdraw()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.mouse = FakeMouse()
        self.app = App(self.root, path=Path(self.directory.name) / "settings.json",
                       mouse_factory=lambda: self.mouse, shortcuts_factory=FakeShortcuts)
        self.addCleanup(self.cleanup_app)

    def cleanup_app(self):
        self.app.engine.close(2)
        self.app.shortcuts.close()
        try:
            self.root.destroy()
        except Exception:
            pass

    def test_invalid_input_recovery_and_counts(self):
        app = self.app
        app.vars["interval"].set("nan")
        app.start()
        self.assertTrue(app.errors["interval"].get())
        self.assertFalse(app.engine.snapshot().active)
        app.vars["interval"].set("10")
        app.vars["repeat"].set("count")
        app.vars["count"].set("1")
        app.vars["mode"].set("double")
        app.start()
        self.assertTrue(app.engine.wait())
        app._render()
        self.assertEqual(app.counter.get(), "Клики: 2   ·   Повторения: 1")
        self.assertEqual(app.status.get(), "Остановлен")
        self.assertEqual(str(app.controls["interval"][0].cget("state")), "normal")

    def test_lock_stop_and_shortcut_error(self):
        app = self.app
        app.vars["delay"].set("86400")
        app.start()
        self.assertEqual(str(app.controls["interval"][0].cget("state")), "disabled")
        self.assertIn("Ожидание", app.status.get())
        app.enqueue("hotkey_error", "test hook error")
        app._tick()
        self.assertTrue(app.engine.wait())
        app._render()
        self.assertIn("test hook error", app.notice.get())
        self.assertEqual(str(app.controls["interval"][0].cget("state")), "normal")
        self.assertEqual(self.mouse.calls, [])

    def test_stop_wins_over_key_repeat_and_close_during_delay(self):
        app = self.app
        app.enqueue("start")
        app.enqueue("stop")
        app._tick()
        self.assertFalse(app.engine.snapshot().active)
        app.vars["delay"].set("86400")
        app.start()
        app.close()
        self.assertTrue(app.engine.wait())
        self.assertTrue(app.shortcuts.closed)
        self.assertFalse(app.engine.start(Settings()))
        self.assertEqual(self.mouse.calls, [])
        self.assertEqual(load_settings(app.path)[0].delay, "86400")

    def test_settings_error_does_not_prevent_manual_run(self):
        app = self.app
        app.vars["repeat"].set("count")
        app.vars["count"].set("1")
        with patch("ac_gui.save_settings", side_effect=PermissionError("disk read only")):
            app.start()
        self.assertTrue(app.engine.wait())
        self.assertIn("disk read only", app.notice.get())
        self.assertEqual(len(self.mouse.calls), 1)


if __name__ == "__main__":
    unittest.main()
