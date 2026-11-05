"""TourBox-style knob actions: modifier wheel, drag, stepped keys, acceleration."""
import time
import unittest
from unittest import mock

from midictl import win_input
from midictl.actions import ActionRunner, knob_step
from midictl.config import default_mapping
from midictl.midi_io import MidiEvent


def knob(v, prev, dt=200):
    return MidiEvent("cc", 1, 20, v, "change", prev=prev, dt=dt)


def mapping(action):
    m = default_mapping()
    m["trigger"] = {"kind": "cc", "channel": 1, "number": 20, "event": "change", "encoding": "absolute"}
    m["actions"] = [action]
    return m


class KnobTests(unittest.TestCase):
    def test_step_direction_and_accel(self):
        m = mapping({})
        self.assertEqual(knob_step({}, knob(10, 9), m, follow=True), 1)
        self.assertEqual(knob_step({}, knob(8, 9), m, follow=True), -1)
        self.assertEqual(knob_step({}, knob(9, 9), m, follow=True), 0)
        self.assertEqual(knob_step({"accel": True}, knob(15, 10, dt=300), m, follow=True), 5)
        self.assertEqual(knob_step({"accel": True}, knob(11, 10, dt=10), m, follow=True), 4)
        self.assertEqual(knob_step({}, knob(15, 10), m), 1)  # no follow: always +1

    def test_modifier_wheel(self):
        a = {"type": "mouse_wheel", "amount": 1, "modifiers": "ctrl", "follow_direction": True}
        r = ActionRunner()
        with mock.patch.object(win_input, "key_down") as down, mock.patch.object(win_input, "key_up") as up, \
                mock.patch.object(win_input, "mouse_wheel") as wheel:
            r.run(mapping(a), knob(8, 9))
            down.assert_called_once_with([0x11])
            wheel.assert_called_once_with(-1, False)
            up.assert_called_once_with([0x11])

    def test_drag_holds_then_releases(self):
        a = {"type": "mouse_drag", "modifiers": "alt", "button": "right", "dx": 4, "dy": 0, "idle_ms": 80}
        r = ActionRunner()
        m = mapping(a)
        with mock.patch.object(win_input, "key_down") as down, mock.patch.object(win_input, "key_up") as up, \
                mock.patch.object(win_input, "mouse_button") as btn, \
                mock.patch.object(win_input, "mouse_move") as move:
            r.run(m, knob(10, 9))
            r.run(m, knob(11, 10))
            r.run(m, knob(10, 11))
            self.assertEqual(down.call_count, 1)
            btn.assert_called_once_with("right", "down")
            self.assertEqual([c.args for c in move.call_args_list], [(4, 0), (4, 0), (-4, 0)])
            up.assert_not_called()
            time.sleep(0.2)
            btn.assert_called_with("right", "up")
            up.assert_called_once_with([0x12])
            self.assertEqual(r.drags, {})

    def test_knob_keys(self):
        a = {"type": "knob_keys", "keys_inc": "]", "keys_dec": "[", "accel": True}
        r = ActionRunner()
        with mock.patch.object(win_input, "key_tap") as tap:
            r.run(mapping(a), knob(13, 10, dt=300))
            self.assertEqual(tap.call_count, 3)
            self.assertEqual(tap.call_args.args[0], [0xDD])
            tap.reset_mock()
            r.run(mapping(a), knob(12, 13, dt=300))
            self.assertEqual(tap.call_args.args[0], [0xDB])


if __name__ == "__main__":
    unittest.main()
