"""Serialized keyboard sessions and owned resources with fake physical ports."""
import queue
import time
import unittest
from contextlib import ExitStack
from unittest.mock import patch

import mido
from midictl import config, win_input
from midictl.actions import ActionRunner
from midictl.engine import Engine, GestureTracker
from midictl.midi_io import MidiEvent


def note(event="press"):
    return MidiEvent("note", 1, 60, 100 if event == "press" else 0, event)


def mapping(actions, **trigger):
    result = config.default_mapping()
    result["trigger"].update(number=60, event="both", **trigger)
    result["actions"] = actions
    return result


class FakePort:
    def __init__(self, name, callback=None):
        self.name, self.callback, self.closed = name, callback, False
        self.sent = []

    def close(self):
        self.closed = True

    def send(self, msg):
        self.sent.append(msg)


def drain(engine):
    while True:
        try:
            item = engine._q.get_nowait()
        except queue.Empty:
            return
        engine._dispatch(item)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.inputs, self.outputs, self.opened, self.effects = ["A 0", "B 0"], ["Out 0"], [], []
        self.stack.enter_context(patch("mido.get_input_names", side_effect=lambda: self.inputs[:]))
        self.stack.enter_context(patch("mido.get_output_names", side_effect=lambda: self.outputs[:]))
        self.stack.enter_context(patch("mido.open_input", side_effect=self.open_input))
        self.stack.enter_context(patch("mido.open_output", side_effect=lambda name: FakePort(name)))
        for method in ("key_down", "key_up", "key_tap", "mouse_button", "mouse_move"):
            self.stack.enter_context(patch.object(win_input, method,
                side_effect=lambda *args, method=method: self.effects.append((method, args))))
        self.cfg = config.default_config()
        self.cfg["settings"]["input_ports"] = ["A 0"]
        self.engine = Engine(self.cfg, log=lambda text: None)
        self.addCleanup(self.engine.stop)

    def require_lifecycle(self):
        for name in ("_dispatch", "bind_ports", "poll_ports", "select_keyboard", "set_enabled"):
            self.assertTrue(hasattr(self.engine, name), name + " must exist")

    def open_input(self, name, callback):
        port = FakePort(name, callback)
        self.opened.append(port)
        return port

    def test_healthy_port_not_reopened_and_none_stops_reconnect(self):
        self.require_lifecycle()
        self.engine.open_ports()
        first = self.opened[0]
        for _ in range(3):
            self.engine.poll_ports()
            self.engine.open_ports()
        self.assertEqual(len(self.opened), 1)
        self.assertFalse(first.closed)
        self.engine.bind_ports("", "")
        self.engine.poll_ports()
        self.assertTrue(first.closed)
        self.assertEqual(len(self.opened), 1)

    def test_disconnect_releases_hold_then_exact_name_reconnects(self):
        self.require_lifecycle()
        self.cfg["profiles"][0]["mappings"] = [mapping([{"type": "key", "keys": "w", "mode": "hold"}])]
        self.engine.open_ports()
        old = self.opened[-1]
        old.callback(mido.Message("note_on", note=60, velocity=100))
        drain(self.engine)
        self.assertEqual(self.engine.runner.held != {}, True)
        self.inputs[:] = ["A 1", "B 0"]
        self.engine.poll_ports()
        self.assertTrue(old.closed)
        self.assertEqual(self.engine.runner.held, {})
        self.assertIn(("key_up", ([ord("W")],)), self.effects)
        self.assertEqual(len(self.opened), 1)  # Do not guess that A 1 is A 0.
        self.inputs.append("A 0")
        self.engine.poll_ports()
        self.assertEqual([p.name for p in self.opened], ["A 0", "A 0"])

    def test_switch_rejects_queued_and_late_old_messages_and_fire(self):
        self.require_lifecycle()
        action = {"type": "key", "keys": "x", "mode": "tap"}
        m = mapping([action])
        self.cfg["profiles"][0]["mappings"] = [m]
        other = config.add_keyboard(self.cfg, "B", copy_current=True)
        other["input_ports"] = ["B 0"]
        self.engine.open_ports()
        old = self.opened[-1]
        old.callback(mido.Message("note_on", note=60, velocity=100))
        old_tracker = self.engine.gestures
        self.engine._q.put(("fire", old_tracker, [m], note()))
        self.engine.variables["shift"] = 1
        self.engine._last_values[note().key] = 99
        self.engine.select_keyboard(other["id"])
        old.callback(mido.Message("note_on", note=60, velocity=100))
        drain(self.engine)
        self.assertEqual(self.effects, [])
        self.assertEqual(self.engine.variables, {})
        self.assertEqual(self.engine._last_values, {})
        self.opened[-1].callback(mido.Message("note_on", note=60, velocity=100))
        drain(self.engine)
        self.assertEqual(self.effects, [("key_tap", ([ord("X")],))])

    def test_pause_cancels_macro_and_pending_gesture_and_keeps_monitor(self):
        self.require_lifecycle()
        m = mapping([{"type": "key", "keys": "x", "delay_ms": 80}])
        self.cfg["profiles"][0]["mappings"] = [m]
        self.engine.open_ports()
        self.engine.inject(note())
        drain(self.engine)
        self.engine.set_enabled(False)
        self.engine._q.put(("fire", self.engine.gestures, [m], note()))
        observed = []
        self.engine.on_midi = observed.append
        self.opened[-1].callback(mido.Message("note_on", note=60, velocity=100))
        drain(self.engine)
        time.sleep(0.13)
        self.assertEqual(self.effects, [])
        self.assertEqual(len(observed), 1)

    def test_release_of_conditioned_hold_does_not_require_matching_conditions(self):
        self.require_lifecycle()
        m = mapping([{"type": "key", "keys": "w", "mode": "hold"}])
        m["trigger"]["event"] = "press"
        m["conditions"] = "shift==1"
        self.cfg["profiles"][0]["mappings"] = [m]
        self.engine.variables["shift"] = 1
        self.engine.inject(note())
        drain(self.engine)
        self.engine.variables["shift"] = 0
        self.engine.inject(note("release"))
        drain(self.engine)
        self.assertEqual(self.engine.runner.held, {})
        self.assertEqual(self.effects[-1], ("key_up", ([ord("W")],)))

    def test_old_queued_macro_profile_request_cannot_override_new_foreground(self):
        self.cfg["profiles"] += [
            {"name": "A", "apps": ["a.exe"], "mappings": []},
            {"name": "B", "apps": ["b.exe"], "mappings": []}]
        self.engine._dispatch(("fg", "a.exe"))
        self.assertEqual(self.engine.active_profile, "A")
        self.engine._force_profile("A")
        old_request = self.engine._q.get_nowait()
        self.engine._dispatch(("fg", "b.exe"))
        self.engine._dispatch(old_request)
        self.assertEqual(self.engine.active_profile, "B")
        self.assertIsNone(self.engine.forced_profile)


class ResourceTests(unittest.TestCase):
    def test_release_all_tracks_mouse_hold_and_explicit_down(self):
        effects = []
        with ExitStack() as stack:
            for method in ("key_down", "key_up", "mouse_button"):
                stack.enter_context(patch.object(win_input, method,
                    side_effect=lambda *args, method=method: effects.append((method, args))))
            runner = ActionRunner()
            runner.run(mapping([{"type": "key", "keys": "ctrl", "mode": "down"},
                                {"type": "mouse_button", "button": "left", "mode": "hold"},
                                {"type": "mouse_button", "button": "right", "mode": "down"}]), note())
            runner.release_all()
        self.assertIn(("key_up", ([0x11],)), effects)
        self.assertIn(("mouse_button", ("left", "up")), effects)
        self.assertIn(("mouse_button", ("right", "up")), effects)

    def test_pressure_zero_never_enters_note_gesture_state(self):
        tracker = GestureTracker(lambda maps, ev: self.fail("Pressure is not a note gesture"))
        m = mapping([{"type": "none"}], kind="polytouch", gesture="long", long_ms=100)
        tracker.handle(MidiEvent("polytouch", 1, 60, 0, "change"), [m])
        self.assertEqual(tracker.state, {})

    def test_cancelled_gesture_does_not_fire_after_clear(self):
        fired = []
        tracker = GestureTracker(lambda maps, ev: fired.append(ev))
        self.assertTrue(hasattr(tracker, "clear"))
        tracker.handle(note(), [mapping([{"type": "none"}], gesture="long", long_ms=60)])
        tracker.clear()
        time.sleep(0.1)
        self.assertEqual(fired, [])
        self.assertEqual(tracker.state, {})
