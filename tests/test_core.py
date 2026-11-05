"""Offline tests (no MIDI device, no real input sent)."""
import unittest
from unittest import mock

import mido

from midictl import win_input
from midictl.actions import ActionRunner
from midictl.config import default_config, default_mapping, _normalize
from midictl.engine import find_matches, select_profile
from midictl.midi_io import normalize, trigger_from_event, trigger_matches


def cfg_with_maps():
    cfg = default_config()
    g = default_mapping()
    g["trigger"] = {"kind": "note", "channel": None, "number": 60, "event": "press"}
    cfg["profiles"][0]["mappings"].append(g)
    app = default_mapping()
    app["trigger"] = {"kind": "note", "channel": 1, "number": 62, "event": "press"}
    cfg["profiles"].append({"name": "浏览器", "apps": ["chrome.exe"], "mappings": [app]})
    return cfg


class MidiTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize(mido.Message("note_on", note=60, velocity=90)).event, "press")
        self.assertEqual(normalize(mido.Message("note_on", note=60, velocity=0)).event, "release")
        ev = normalize(mido.Message("control_change", channel=2, control=7, value=100))
        self.assertEqual((ev.kind, ev.channel, ev.number, ev.value), ("cc", 3, 7, 100))
        pb = [normalize(mido.Message("pitchwheel", pitch=p)).value for p in (-8192, -7680, 0, 8191)]
        self.assertEqual(pb, [-64, -60, 0, 64])

    def test_match(self):
        press = normalize(mido.Message("note_on", note=60, velocity=90))
        rel = normalize(mido.Message("note_off", note=60))
        t = trigger_from_event(press)
        self.assertTrue(trigger_matches(t, press))
        self.assertFalse(trigger_matches(t, rel))
        t["event"] = "both"
        self.assertTrue(trigger_matches(t, rel))
        t["channel"] = 5
        self.assertFalse(trigger_matches(t, press))

    def test_profile_selection_and_fallback(self):
        cfg = cfg_with_maps()
        self.assertEqual(select_profile(cfg, "chrome.exe")["name"], "浏览器")
        self.assertEqual(select_profile(cfg, "notepad.exe")["name"], "全局")
        p = select_profile(cfg, "chrome.exe")
        c4 = normalize(mido.Message("note_on", note=60, velocity=1))
        self.assertEqual(len(find_matches(cfg, p, c4)), 1)  # falls back to global
        cfg["settings"]["app_switching"] = False
        self.assertEqual(select_profile(cfg, "chrome.exe")["name"], "全局")

    def test_normalize_config_adds_global(self):
        cfg = _normalize({"profiles": [{"name": "x"}]})
        self.assertEqual(cfg["profiles"][0]["name"], "全局")


class InputTests(unittest.TestCase):
    def test_parse_combo(self):
        self.assertEqual(win_input.parse_combo("Ctrl+Shift+S"), [0x11, 0x10, ord("S")])
        with self.assertRaises(ValueError):
            win_input.parse_combo("ctrl+nokey")

    def test_hold_mode_tracks_press_release(self):
        r = ActionRunner()
        m = default_mapping()
        m["actions"] = [{"type": "key", "keys": "w", "mode": "hold"}]
        press = normalize(mido.Message("note_on", note=60, velocity=90))
        rel = normalize(mido.Message("note_off", note=60))
        with mock.patch.object(win_input, "key_down") as down, mock.patch.object(win_input, "key_up") as up:
            r.run(m, press)
            r.run(m, press)
            self.assertEqual(down.call_count, 1)
            r.run(m, rel)
            up.assert_called_once_with([ord("W")])

    def test_tap_sends_inputs(self):
        with mock.patch.object(win_input, "_send", return_value=4) as send:
            win_input.key_tap(win_input.parse_combo("ctrl+c"))
            self.assertEqual(len(send.call_args[0][0]), 4)


if __name__ == "__main__":
    unittest.main()
