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
from ac_settings import (
    Settings, ValidationError, combine_interval, load_settings, save_settings, split_interval,
)


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
            self.assertEqual(doc["version"], 2)
            self.assertEqual(set(doc["settings"]), set(asdict(Settings())))
            self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_interval_parts_preserve_units_and_fractional_milliseconds(self):
        parts = dict(hours="1", minutes="2", seconds="3", milliseconds="4.5")
        self.assertEqual(combine_interval(parts), "3723004.5")
        for unit, value in (("ms", "3723004.5"), ("s", "3723.0045")):
            self.assertEqual(split_interval(replace(Settings(), interval=value, unit=unit)), parts)
        self.assertEqual(combine_interval(split_interval(Settings())), "100")

    def test_interval_parts_reject_empty_bad_ranges_nan_and_zero(self):
        base = dict(hours="0", minutes="0", seconds="0", milliseconds="100")
        for name, bad_values in {
            "hours": ("", "-1", "1.5", "nan", "999999999999"),
            "minutes": ("", "60", "-1", "inf"),
            "seconds": ("", "60", "0.1", "NaN"),
            "milliseconds": ("", "-1", "1000", "nan", "inf", "-inf", "text", "1,5", "0", "0.1"),
        }.items():
            for value in bad_values:
                with self.subTest(name=name, value=value), self.assertRaises(ValidationError):
                    combine_interval({**base, name: value})

    def test_legacy_settings_migrate_without_losing_run_options(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            legacy = asdict(replace(Settings(), interval="1.25", unit="s", x="-50", mode="double"))
            del legacy["toggle_key"], legacy["stop_key"]
            path.write_text(json.dumps({"version": 1, "settings": legacy}), encoding="utf-8")
            value, warning = load_settings(path)
            self.assertFalse(warning)
            self.assertEqual(value.seconds, 1.25)
            self.assertEqual((value.mode, value.x, value.toggle_key, value.stop_key), ("double", "-50", "F6", "F7"))
            save_settings(path, value)
            self.assertEqual(json.loads(path.read_text())["version"], 2)

    def test_invalid_hotkeys(self):
        for options in (dict(toggle_key="F13"), dict(stop_key="Escape"), dict(stop_key="F6")):
            with self.subTest(options=options), self.assertRaises(ValidationError):
                replace(Settings(), **options).validated()

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
        self.assertEqual(output.call_args_list, [(('toggle',), {}), (('stop',), {})])
        self.assertFalse(keys.close())
        listener.is_alive.return_value = False
        self.assertTrue(keys.close())
        listener.stop.assert_called_once()
        listener.join.assert_called_with(0)

    def test_rebinding_changes_callbacks_without_restarting_listener(self):
        output = Mock()
        factory = Mock()
        keys = GlobalShortcuts(output, factory)
        keys.start()
        callbacks = factory.call_args.args[0]
        keys.configure("F8", "F9")
        callbacks["<f6>"]()
        callbacks["<f7>"]()
        output.assert_not_called()
        callbacks["<f8>"]()
        callbacks["<f9>"]()
        self.assertEqual(output.call_args_list, [(('toggle',), {}), (('stop',), {})])
        factory.assert_called_once()
        with self.assertRaises(ValidationError):
            keys.configure("F10", "F10")
        callbacks["<f8>"]()
        self.assertEqual(output.call_count, 3)

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

    def configure(self, toggle_key, stop_key):
        self.keys = (toggle_key, stop_key)

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
        self.app.close()
        self.app.engine.close(2)
        self.app.shortcuts.close()
        try:
            self.root.destroy()
        except Exception:
            pass

    def test_invalid_input_recovery_and_counts(self):
        app = self.app
        app.interval_vars["milliseconds"].set("nan")
        app.start()
        self.assertTrue(app.errors["interval"].get())
        self.assertFalse(app.engine.snapshot().active)
        app.interval_vars["milliseconds"].set("10")
        app.vars["repeat"].set("count")
        app.vars["count"].set("1")
        app.vars["mode"].set("double")
        app.start()
        self.assertTrue(app.engine.wait())
        app._render()
        self.assertEqual(app.counter.get(), "Clicks: 2   |   Repeats: 1")
        self.assertEqual(app.status.get(), "Stopped")
        self.assertEqual(str(app.controls["milliseconds"][0].cget("state")), "normal")

    def test_lock_stop_and_shortcut_error(self):
        app = self.app
        app.vars["delay"].set("86400")
        app.start()
        self.assertEqual(str(app.controls["milliseconds"][0].cget("state")), "disabled")
        self.assertIn("Waiting", app.status.get())
        app.enqueue("hotkey_error", "test hook error")
        app._tick()
        self.assertTrue(app.engine.wait())
        app._render()
        self.assertIn("test hook error", app.notice.get())
        self.assertEqual(str(app.controls["milliseconds"][0].cget("state")), "normal")
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

    def test_toggle_starts_and_cancels_wait_and_stop_has_priority(self):
        app = self.app
        app.vars["delay"].set("3600")
        app.enqueue("toggle")
        app._tick()
        self.assertTrue(app.engine.snapshot().active)
        app.enqueue("toggle")
        app._tick()
        self.assertTrue(app.engine.wait())
        app.enqueue("toggle")
        app.enqueue("stop")
        app._tick()
        self.assertFalse(app.engine.snapshot().active)
        self.assertEqual(self.mouse.calls, [])

    def test_pick_location_captures_without_clicking_and_blocks_start(self):
        app = self.app
        self.mouse.position = (-200, 300)
        app.pick_location()
        self.assertIsNotNone(app.picker)
        self.assertIn("3 seconds", app.pick_message.get())
        app.start()
        self.assertFalse(app.engine.snapshot().active)
        app._capture_position()
        self.assertIsNone(app.picker)
        self.assertIsNone(app._pick_after)
        self.assertEqual((app.vars["position"].get(), app.vars["x"].get(), app.vars["y"].get()), ("fixed", "-200", "300"))
        self.assertEqual(self.mouse.calls, [])

    def test_cancel_pick_and_close_remove_pending_capture(self):
        app = self.app
        app.vars["x"].set("40")
        app.pick_location()
        app.stop()
        self.assertIsNone(app.picker)
        self.assertIsNone(app._pick_after)
        self.assertEqual(app.vars["x"].get(), "40")
        app.pick_location()
        app.close()
        self.assertIsNone(app.picker)
        self.assertIsNone(app._pick_after)
        self.assertEqual(self.mouse.calls, [])

    def test_pick_backend_error_preserves_coordinates_and_is_visible(self):
        app = self.app
        app.vars["x"].set("45")
        app._mouse_factory = Mock(side_effect=OSError("no cursor"))
        app.pick_location()
        app._capture_position()
        self.assertIsNone(app.picker)
        self.assertEqual(app.vars["x"].get(), "45")
        self.assertIn("no cursor", app.notice.get())

    def test_hotkey_dialog_validates_updates_buttons_and_persists(self):
        app = self.app
        app.show_hotkeys()
        app.hotkey_vars["stop_key"].set("F6")
        app._apply_hotkeys()
        self.assertIsNotNone(app.hotkey_dialog)
        self.assertIn("different keys", app.hotkey_message.get())
        app.hotkey_vars["toggle_key"].set("F8")
        app.hotkey_vars["stop_key"].set("F9")
        app._apply_hotkeys()
        self.assertIsNone(app.hotkey_dialog)
        self.assertEqual(app.shortcuts.keys, ("F8", "F9"))
        self.assertEqual(app.start_button.cget("text"), "Start (F8)")
        self.assertEqual(app.stop_button.cget("text"), "Stop (F8)")
        settings, warning = load_settings(app.path)
        self.assertFalse(warning)
        self.assertEqual((settings.toggle_key, settings.stop_key), ("F8", "F9"))

    def test_cancel_hotkeys_and_help_do_not_change_settings(self):
        app = self.app
        app.show_hotkeys()
        app.hotkey_vars["toggle_key"].set("F10")
        app._close_hotkeys()
        self.assertEqual(app.vars["toggle_key"].get(), "F6")
        app.show_help()
        original = app.help_dialog
        app.show_help()
        self.assertIs(app.help_dialog, original)
        app._close_help()
        self.assertIsNone(app.help_dialog)


if __name__ == "__main__":
    unittest.main()
