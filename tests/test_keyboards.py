"""Keyboard storage/pressure contracts; no ports or real input are opened."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

import mido
from midictl import config, midi_io
from midictl.engine import find_matches, select_profile


def legacy_config():
    return {"settings": {"input_port": "SMK25 0", "output_port": "Loop 1", "enabled": False},
            "profiles": [{"name": "全局", "apps": [], "mappings": [
                {"id": "stable-id", "name": "C3(48)", "enabled": False,
                 "trigger": {"kind": "note", "channel": 1, "number": 52, "event": "press",
                             "gesture": "long", "encoding": "absolute", "long_ms": 800},
                 "action": {"type": "key", "keys": "space", "delay_ms": 35},
                 "conditions": "shift==1", "stop": True, "hud": True},
                {"id": "second", "enabled": True,
                 "trigger": {"kind": "cc", "channel": None, "number": 7, "event": "change"},
                 "actions": [{"type": "volume", "mode": "set_from_value"},
                             {"type": "none", "delay_ms": 10}]}]},
                {"name": "浏览器", "apps": ["chrome.exe"], "mappings": []}]}


class KeyboardConfigTests(unittest.TestCase):
    def require_helpers(self):
        for name in ("active_keyboard", "add_keyboard", "switch_keyboard", "sync_keyboard"):
            self.assertTrue(hasattr(config, name), name + " must exist")

    def test_legacy_load_backs_up_exact_bytes_and_is_idempotent(self):
        original = legacy_config()
        raw = json.dumps(original, ensure_ascii=False, indent=4).encode("utf-8") + b"\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_bytes(raw)
            cfg = config.load_config(path)
            self.assertEqual(cfg.get("schema_version"), 3)
            backup = path.with_name("config.json.pre-keyboards.bak")
            self.assertEqual(backup.read_bytes(), raw)
            migrated = path.read_bytes()
            self.assertEqual(config.load_config(path), cfg)
            self.assertEqual(path.read_bytes(), migrated)
            config.save_config(cfg, path)
            self.assertEqual(config.load_config(path), cfg)
            self.assertEqual(backup.read_bytes(), raw)
        maps = cfg["profiles"][0]["mappings"]
        self.assertEqual([m["id"] for m in maps], ["stable-id", "second"])
        first = copy.deepcopy(original["profiles"][0]["mappings"][0])
        first["actions"] = [first.pop("action")]
        first["trigger"].update(value_mode="legacy", edge_mode="level", threshold=64, invert=False)
        self.assertEqual(maps[0], first)
        self.assertEqual(maps[1]["actions"], original["profiles"][0]["mappings"][1]["actions"])
        self.assertEqual(cfg["settings"]["output_port"], "Loop 1")

    def test_switch_round_trip_and_duplicate_are_independent(self):
        self.require_helpers()
        cfg = config._normalize(legacy_config())
        first = cfg["active_keyboard"]
        other = config.add_keyboard(cfg, "键盘 B", copy_current=True)
        config.switch_keyboard(cfg, other["id"])
        cfg["profiles"][0]["mappings"][0]["trigger"]["number"] = 60
        cfg["settings"]["input_ports"] = ["Keyboard B 1"]
        cfg["settings"]["output_port"] = "B Out 2"
        config.switch_keyboard(cfg, first)
        self.assertEqual(cfg["profiles"][0]["mappings"][0]["trigger"]["number"], 52)
        self.assertEqual(cfg["settings"]["input_port"], "SMK25 0")
        config.switch_keyboard(cfg, other["id"])
        self.assertEqual(cfg["profiles"][0]["mappings"][0]["trigger"]["number"], 60)
        self.assertEqual(cfg["settings"]["output_port"], "B Out 2")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            config.save_config(cfg, path)
            loaded = config.load_config(path)
            config.switch_keyboard(loaded, first)
            self.assertEqual(loaded["settings"]["input_port"], "SMK25 0")

    def test_new_keyboard_starts_unbound_and_app_fallback_is_preserved(self):
        self.require_helpers()
        cfg = config._normalize(legacy_config())
        first = cfg["active_keyboard"]
        other = config.add_keyboard(cfg, "空白键盘")
        config.switch_keyboard(cfg, other["id"])
        self.assertEqual(cfg["settings"]["input_port"], "")
        self.assertEqual(cfg["settings"]["output_port"], "")
        self.assertEqual(cfg["profiles"], [{"name": "全局", "apps": [], "mappings": []}])
        config.switch_keyboard(cfg, first)
        profile = select_profile(cfg, "chrome.exe")
        event = midi_io.MidiEvent("cc", 3, 7, 100, "change")
        self.assertEqual(find_matches(cfg, profile, event)[0]["id"], "second")

    def test_unknown_keyboard_and_future_schema_do_not_destroy_config(self):
        self.require_helpers()
        cfg = config._normalize(legacy_config())
        before = copy.deepcopy(cfg)
        with self.assertRaises(ValueError):
            config.switch_keyboard(cfg, "missing")
        self.assertEqual(cfg, before)
        with self.assertRaises(ValueError):
            config._normalize({"schema_version": 999})

    def test_damaged_v2_is_rejected_without_rewriting_or_losing_root_mappings(self):
        valid = config._normalize(legacy_config())
        for missing in ("keyboards", "keyboard_profiles"):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as tmp:
                damaged = copy.deepcopy(valid)
                if missing == "keyboards":
                    del damaged["keyboards"]
                else:
                    del damaged["keyboards"][0]["profiles"]
                path = Path(tmp) / "config.json"
                raw = json.dumps(damaged, ensure_ascii=False).encode("utf-8")
                path.write_bytes(raw)
                with self.assertRaises(ValueError):
                    config.load_config(path)
                self.assertEqual(path.read_bytes(), raw)
                self.assertEqual(list(Path(tmp).iterdir()), [path])


class PressureTests(unittest.TestCase):
    def test_pressure_normalize_learn_match_and_describe(self):
        for msg, kind, number in [(mido.Message("aftertouch", channel=4, value=0), "aftertouch", 0),
                                  (mido.Message("polytouch", channel=9, note=72, value=91), "polytouch", 72)]:
            with self.subTest(kind=kind):
                ev = midi_io.normalize(msg)
                self.assertIsNotNone(ev)
                self.assertEqual((ev.kind, ev.channel, ev.number, ev.value, ev.event),
                                 (kind, msg.channel + 1, number, msg.value, "change"))
                trigger = midi_io.trigger_from_event(ev)
                self.assertTrue(midi_io.trigger_matches(trigger, ev))
                self.assertIn(kind.lower(), ev.describe().lower())
                self.assertNotIn("Program", midi_io.describe_trigger(trigger))
                trigger["number"] = 10
                self.assertEqual(midi_io.trigger_matches(trigger, ev), kind == "aftertouch")

    def test_pitch_and_note_release_semantics_stay_unchanged(self):
        self.assertEqual([midi_io.normalize(mido.Message("pitchwheel", pitch=p)).value
                          for p in (-8192, -7680, 0, 8191)], [-64, -60, 0, 64])
        self.assertEqual(midi_io.normalize(mido.Message("note_on", note=52, velocity=0)).event, "release")
