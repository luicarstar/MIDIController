"""Tests for phase-2 rules: direction, conditions, variables, gestures, stop."""
import time
import unittest
from unittest import mock

from midictl import win_input
from midictl.config import _normalize, default_config, default_mapping
from midictl.engine import GestureTracker, find_matches
from midictl.midi_io import MidiEvent, direction, trigger_matches
from midictl.rules import apply_set_var, check_conditions, parse_conditions


def note(ev, n=60, v=100):
    return MidiEvent("note", 1, n, v if ev == "press" else 0, ev)


def mapping(trigger=None, cond="", stop=False, gesture="any"):
    m = default_mapping()
    m["trigger"] = dict(trigger or {"kind": "note", "channel": None, "number": 60, "event": "press"})
    m["trigger"]["gesture"] = gesture
    m["trigger"]["long_ms"] = 100
    m["conditions"], m["stop"] = cond, stop
    return m


class RuleTests(unittest.TestCase):
    def test_migrate_single_action(self):
        cfg = _normalize({"profiles": [{"name": "全局", "mappings": [{"trigger": {}, "action": {"type": "none"}}]}]})
        self.assertEqual(cfg["profiles"][0]["mappings"][0]["actions"], [{"type": "none"}])

    def test_direction(self):
        t = {"kind": "cc", "number": 20, "event": "increase"}
        self.assertEqual(direction(t, MidiEvent("cc", 1, 20, 9, "change", prev=8)), 1)
        self.assertEqual(direction(t, MidiEvent("cc", 1, 20, 7, "change", prev=8)), -1)
        self.assertEqual(direction(t, MidiEvent("cc", 1, 20, 127, "change", prev=127)), 1)
        t["encoding"] = "rel_2c"
        self.assertEqual(direction(t, MidiEvent("cc", 1, 20, 127, "change")), -1)
        self.assertTrue(trigger_matches(t, MidiEvent("cc", 1, 20, 1, "change")))

    def test_value_range(self):
        t = {"kind": "cc", "number": 1, "event": "change", "value_min": 64}
        self.assertFalse(trigger_matches(t, MidiEvent("cc", 1, 1, 10, "change")))
        self.assertTrue(trigger_matches(t, MidiEvent("cc", 1, 1, 90, "change")))

    def test_conditions(self):
        self.assertEqual(parse_conditions("shift==1，mode!=2"), [("shift", "==", 1), ("mode", "!=", 2)])
        self.assertTrue(check_conditions("shift==1", {"shift": 1}))
        self.assertFalse(check_conditions("shift==1", {}))
        self.assertTrue(check_conditions("$value>64", {}, MidiEvent("cc", 1, 1, 90, "change")))
        with self.assertRaises(ValueError):
            parse_conditions("shift=1")

    def test_set_var(self):
        v = {}
        apply_set_var({"name": "mode", "op": "add", "value": 1, "min": 0, "max": 2, "wrap": True}, v)
        apply_set_var({"name": "mode", "op": "add", "value": 1, "min": 0, "max": 2, "wrap": True}, v)
        self.assertEqual(apply_set_var({"name": "mode", "op": "add", "value": 1, "min": 0, "max": 2,
                                        "wrap": True}, v), 0)
        self.assertEqual(apply_set_var({"name": "s", "op": "toggle", "value": 1}, v), 1)
        self.assertEqual(apply_set_var({"name": "s", "op": "toggle", "value": 1}, v), 0)

    def test_conditions_layer_and_stop(self):
        cfg = default_config()
        a, b = mapping(cond="shift==1", stop=True), mapping()
        cfg["profiles"][0]["mappings"] += [a, b]
        p = cfg["profiles"][0]
        self.assertEqual(find_matches(cfg, p, note("press"), {"shift": 1}), [a])
        self.assertEqual(find_matches(cfg, p, note("press"), {}), [b])


class GestureTests(unittest.TestCase):
    def setUp(self):
        self.fired = []
        self.g = GestureTracker(lambda maps, ev: self.fired.append([m["trigger"]["gesture"] for m in maps]))
        self.maps = [mapping(gesture="short"), mapping(gesture="long"), mapping(gesture="double")]

    def test_short(self):
        self.g.handle(note("press"), self.maps)
        self.g.handle(note("release"), self.maps)
        time.sleep(0.4)
        self.assertEqual(self.fired, [["short"]])

    def test_long(self):
        self.g.handle(note("press"), self.maps)
        time.sleep(0.2)
        self.g.handle(note("release"), self.maps)
        time.sleep(0.4)
        self.assertEqual(self.fired, [["long"]])

    def test_double(self):
        for _ in range(2):
            self.g.handle(note("press"), self.maps)
            self.g.handle(note("release"), self.maps)
        time.sleep(0.4)
        self.assertEqual(self.fired, [["double"]])

    def test_gesture_maps_excluded_from_immediate(self):
        cfg = default_config()
        cfg["profiles"][0]["mappings"] += self.maps
        p = cfg["profiles"][0]
        self.assertEqual(find_matches(cfg, p, note("press"), {}), [])
        self.assertEqual(len(find_matches(cfg, p, note("release"), {}, gestures=True)), 3)


class RepeatTests(unittest.TestCase):
    def test_repeat_key(self):
        from midictl.actions import ActionRunner
        r = ActionRunner()
        m = mapping()
        m["actions"] = [{"type": "repeat_key", "keys": "w", "interval_ms": 20}]
        with mock.patch.object(win_input, "key_tap") as tap:
            r.run(m, note("press"))
            time.sleep(0.15)
            r.run(m, note("release"))
            n = tap.call_count
            time.sleep(0.1)
            self.assertGreaterEqual(n, 3)
            self.assertEqual(tap.call_count, n)


if __name__ == "__main__":
    unittest.main()
