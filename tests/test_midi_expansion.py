"""Selected expansion contracts; all hardware/process boundaries are intercepted."""
import copy
import json
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import mido
from midictl import config, midi_io, win_input
from midictl.actions import ActionRunner, knob_step, _norm_value
from midictl.engine import Engine
from tests.test_lifecycle import FakePort, drain


def cc(number, value, channel=0):
    return mido.Message("control_change", channel=channel, control=number, value=value)


def mapping(kind, number, actions, **trigger):
    m = config.default_mapping()
    m["trigger"].update(kind=kind, number=number, event="change")
    m["trigger"].update(trigger)
    m["actions"] = actions
    return m


class ProtocolTests(unittest.TestCase):
    def test_cc14_pairing_clears_lsb_and_isolates_source_channel(self):
        d = midi_io.MidiDecoder()
        self.assertEqual([e.kind for e in d.feed(cc(33, 5), "A")], ["cc"])
        coarse = d.feed(cc(1, 2), "A")[-1]
        self.assertEqual((coarse.kind, coarse.value, coarse.fine_valid), ("cc14", 256, False))
        self.assertEqual(d.feed(cc(33, 7), "A")[-1].value, 263)
        self.assertEqual([e.kind for e in d.feed(cc(33, 9), "B")], ["cc"])
        self.assertEqual([e.kind for e in d.feed(cc(33, 9, 1), "A")], ["cc"])
        self.assertEqual(d.feed(cc(1, 3), "A")[-1].value, 384)
        d.reset_source("A")
        self.assertEqual([e.kind for e in d.feed(cc(33, 1), "A")], ["cc"])

    def test_parameter_selection_change_kind_null_and_increment(self):
        d = midi_io.MidiDecoder()
        params = lambda n, v: [e for e in d.feed(cc(n, v), "A") if e.kind in ("rpn", "nrpn")]
        self.assertEqual(params(6, 12), [])  # Unselected data cannot be replayed.
        params(101, 0)
        params(100, 1)
        self.assertEqual(params(38, 3), [])
        first = params(6, 2)[0]
        self.assertEqual((first.kind, first.number, first.value, first.fine_valid), ("rpn", 1, 256, False))
        params(100, 1)  # Repeating the selected parameter preserves coarse assembly.
        self.assertEqual(params(38, 1)[0].value, 257)
        params(100, 2)
        self.assertEqual(params(38, 9), [])  # A different parameter cannot reuse coarse.
        params(6, 4)
        delta = params(96, 127)[0]
        self.assertEqual((delta.value, delta.raw_value, delta.delta), (None, None, 1))
        self.assertIsNone(midi_io.value_bounds(delta))
        self.assertEqual(params(38, 7), [])
        params(99, 0)  # Crossing to NRPN immediately disables the old selection.
        self.assertEqual(params(6, 5), [])
        params(98, 3)
        self.assertEqual((params(6, 5)[0].kind, params(97, 0)[0].delta), ("nrpn", -1))
        params(101, 127)
        params(100, 127)
        self.assertEqual(params(6, 9), [])
        self.assertEqual(params(96, 0), [])

    def test_new_relative_modes_preserve_steps_without_time_acceleration(self):
        samples = [("rel_signed", 67, -3), ("rel_offset", 68, 4),
                   ("rel_arturia3", 19, 3), ("rel_arturia3", 127, 0)]
        for encoding, raw, expected in samples:
            with self.subTest(encoding=encoding, raw=raw):
                m = mapping("cc", 20, [], encoding=encoding)
                ev = midi_io.MidiEvent("cc", 1, 20, raw, "change", dt=1)
                self.assertEqual(knob_step({"accel": True}, ev, m, follow=True), expected)
                m["trigger"]["reverse"] = True
                self.assertEqual(knob_step({}, ev, m, follow=True), -expected)
        old = mapping("cc", 20, [], encoding="rel_2c")
        ev = midi_io.MidiEvent("cc", 1, 20, 3, "change", dt=1)
        self.assertEqual(knob_step({}, ev, old, follow=True), 1)
        old["trigger"]["relative_steps"] = True
        self.assertEqual(knob_step({"accel": True}, ev, old, follow=True), 3)

    def test_v2_migration_and_explicit_empty_inputs_are_lossless(self):
        old = config.default_config()
        old["schema_version"] = 2
        keyboard = old["keyboards"][0]
        keyboard.pop("input_ports")
        keyboard["input_port"] = "Exact 0"
        m = mapping("pitchwheel", 0, [{"type": "none"}])
        m["trigger"].pop("value_mode")
        m["trigger"].pop("edge_mode")
        keyboard["profiles"][0]["mappings"] = [m]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            raw = json.dumps(old, ensure_ascii=False).encode("utf-8") + b"\n"
            path.write_bytes(raw)
            cfg = config.load_config(path)
            self.assertEqual(path.with_name("config.json.pre-midi-expansion.bak").read_bytes(), raw)
            self.assertEqual(cfg["settings"]["input_ports"], ["Exact 0"])
            trigger = cfg["profiles"][0]["mappings"][0]["trigger"]
            self.assertEqual((trigger["value_mode"], trigger["edge_mode"]), ("legacy", "level"))
            cfg["settings"].update(input_ports=[], input_port="must not silently return")
            config.save_config(cfg, path)
            self.assertEqual(config.load_config(path)["settings"]["input_ports"], [])
            damaged = copy.deepcopy(cfg)
            del damaged["keyboards"][0]["input_ports"]
            with self.assertRaises(ValueError):
                config._normalize(damaged)


class MultiInputTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.available, self.opened, self.effects, self.logs = ["A 0", "B 0"], [], [], []
        self.stack.enter_context(patch("mido.get_input_names", side_effect=lambda: self.available[:]))
        self.stack.enter_context(patch("mido.get_output_names", return_value=[]))
        def open_input(name, callback):
            port = FakePort(name, callback)
            self.opened.append(port)
            return port
        self.stack.enter_context(patch("mido.open_input", side_effect=open_input))
        for method in ("key_down", "key_up", "key_tap", "mouse_button", "mouse_move", "mouse_wheel"):
            self.stack.enter_context(patch.object(win_input, method,
                side_effect=lambda *args, method=method: self.effects.append((method, args))))
        self.cfg = config.default_config()
        self.cfg["settings"]["input_ports"] = self.available[:]
        self.engine = Engine(self.cfg, log=self.logs.append)
        self.addCleanup(self.engine.stop)
        self.engine.open_ports()
        self.observed = []
        self.engine.on_midi = self.observed.append

    def send(self, message, source="A 0"):
        self.engine.inputs[source].port.callback(message)
        drain(self.engine)

    def test_partial_disconnect_preserves_other_source_and_shared_hold(self):
        self.cfg["profiles"][0]["mappings"] = [mapping("note", 60,
            [{"type": "key", "keys": "w", "mode": "hold"},
             {"type": "mouse_button", "button": "left", "mode": "hold"}], event="both")]
        press = mido.Message("note_on", note=60, velocity=100)
        self.send(press)
        self.send(press, "B 0")
        self.assertEqual(self.effects, [("key_down", ([ord("W")],)), ("mouse_button", ("left", "down"))])
        self.assertIsNone(self.observed[-1].prev)  # B must not borrow A's prior value.
        old_a, healthy_b = self.engine.inputs["A 0"].port, self.engine.inputs["B 0"].port
        old_a.callback(press)  # Queued before disconnect, delivered after it.
        self.available[:] = ["A 1", "B 0"]
        self.engine.poll_ports()
        old_a.callback(press)  # Late backend callback too.
        drain(self.engine)
        self.assertTrue(old_a.closed)
        self.assertIs(self.engine.inputs["B 0"].port, healthy_b)
        self.assertEqual(len(self.effects), 2)
        self.assertFalse(any(key[0] == "A 0" for key in self.engine._last_values))
        self.send(mido.Message("note_off", note=60), "B 0")
        self.assertEqual(self.effects[-2:], [("key_up", ([ord("W")],)), ("mouse_button", ("left", "up"))])
        self.available.append("A 0")
        self.engine.poll_ports()
        self.assertEqual([p.name for p in self.opened], ["A 0", "B 0", "A 0"])

    def test_disconnect_cancels_only_its_delayed_macro(self):
        self.cfg["profiles"][0]["mappings"] = [mapping("note", 60,
            [{"type": "key", "keys": key, "delay_ms": 40}], source=source, event="press")
            for source, key in (("A 0", "a"), ("B 0", "b"))]
        self.send(mido.Message("note_on", note=60, velocity=100))
        self.send(mido.Message("note_on", note=60, velocity=100), "B 0")
        self.available.remove("A 0")
        self.engine.poll_ports()
        time.sleep(0.08)
        self.assertEqual(self.effects, [("key_tap", ([ord("B")],))])

    def test_full_pitch_and_legacy_views_keep_precision_and_previous_values(self):
        self.cfg["profiles"][0]["mappings"] = [mapping("pitchwheel", 0,
            [{"type": "set_var", "name": mode, "op": "from_value"}], value_mode=mode)
            for mode in ("legacy", "full")]
        for raw, legacy in ((-8192, -64), (1, 0), (2, 0), (8191, 64)):
            self.send(mido.Message("pitchwheel", pitch=raw))
            self.assertEqual(self.engine.variables, {"legacy": legacy, "full": raw})
        self.assertEqual((self.observed[-1].prev, self.observed[-1].raw_prev), (0, 2))
        view = midi_io.event_for_trigger({"value_mode": "full"}, self.observed[-1])
        self.assertEqual((view.value, view.prev, _norm_value(view)), (8191, 2, 1.0))

    def test_delta_only_never_replaces_absolute_history_or_drives_absolute_actions(self):
        steps = {"type": "knob_keys", "keys_inc": "right", "keys_dec": "left", "accel": True}
        absolute = [{"type": "volume", "mode": "set_from_value"},
                    {"type": "set_var", "name": "bad", "op": "from_value"},
                    {"type": "midi_out", "pass_value": True}]
        base = mapping("rpn", 1, [steps] + absolute, event="increase")
        ranged = mapping("rpn", 1, [{"type": "key", "keys": "x"}], value_min=0)
        conditioned = mapping("rpn", 1, [{"type": "key", "keys": "y"}])
        conditioned["conditions"] = "$value>=0"
        self.cfg["profiles"][0]["mappings"] = [base, ranged, conditioned]
        for message in (cc(101, 0), cc(100, 1), cc(6, 2)):
            self.send(message)
        self.effects.clear()
        self.engine.variables.clear()
        self.send(cc(96, 127))
        self.assertEqual(self.effects, [("key_tap", ([0x27],))])
        ev = self.observed[-1]
        self.assertEqual((ev.value, ev.prev, ev.raw_value, ev.raw_prev, ev.delta), (None, None, None, None, 1))
        self.assertEqual(self.engine._last_values[ev.key], 256)
        self.assertEqual(self.engine._last_raw_values[ev.key], 256)
        self.assertEqual(self.engine.variables, {})

    def test_cc14_mapping_runs_once_per_composite_not_original_cc(self):
        self.cfg["profiles"][0]["mappings"] = [mapping("cc14", 1,
            [{"type": "set_var", "name": "count", "op": "add", "value": 1}])]
        self.send(cc(1, 2))
        self.send(cc(33, 3))
        self.assertEqual(self.engine.variables, {"count": 2})

    def test_pedal_edges_polarity_and_condition_range_release(self):
        pedal = mapping("cc", 20, [{"type": "key", "keys": "space"}], event="both", threshold=100)
        self.cfg["profiles"][0]["mappings"] = [pedal]
        for value in (0, 90, 100, 120, 99, 100):
            self.send(cc(20, value))
        self.assertEqual(len(self.effects), 3)
        self.engine.mappings_changed()
        self.effects.clear()
        pedal["trigger"].update(threshold=64, invert=True, event="press", value_min=0, value_max=40)
        pedal["actions"] = [{"type": "key", "keys": "w", "mode": "hold"}]
        pedal["conditions"] = "shift==1"
        self.engine.variables["shift"] = 1
        self.send(cc(20, 127))  # Inverted unpressed state.
        self.send(cc(20, 30))
        self.assertEqual(self.effects, [("key_down", ([ord("W")],))])
        self.send(cc(20, 45))  # Still pressed, but now outside the range.
        self.assertEqual(self.effects[-1], ("key_up", ([ord("W")],)))
        self.send(cc(20, 127))
        self.send(cc(20, 30))
        self.engine.variables["shift"] = 0
        self.send(cc(20, 30))
        self.assertEqual(self.engine.runner.held, {})
        self.assertEqual(self.effects[-1], ("key_up", ([ord("W")],)))
