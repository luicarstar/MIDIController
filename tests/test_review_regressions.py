"""Two concrete regressions found by the independent static review."""
import time
import unittest

from midictl.config import default_mapping
from midictl.engine import GestureTracker
from midictl.midi_io import MidiEvent, direction, event_for_trigger, normalize
import mido


class ReviewRegressions(unittest.TestCase):
    def test_long_suppresses_same_button_short_double_only(self):
        for separate_short in (False, True):
            with self.subTest(separate_threshold=separate_short):
                fired = []
                tracker = GestureTracker(lambda maps, ev: fired.extend(m["name"] for m in maps))
                self.addCleanup(tracker.clear)
                maps = []
                for gesture in ("long", "short", "double"):
                    m = default_mapping()
                    m["name"] = gesture
                    m["trigger"].update(kind="cc", number=20, event="both", gesture=gesture,
                                         long_ms=20, threshold=100 if separate_short and gesture == "short" else 64)
                    maps.append(m)
                tracker.handle(MidiEvent("cc", 1, 20, 127, "change", prev=0, source="A"), maps)
                time.sleep(0.06)
                tracker.handle(MidiEvent("cc", 1, 20, 0, "change", prev=127, source="A"), maps)
                self.assertEqual(fired, ["long", "short"] if separate_short else ["long"])
                self.assertTrue(all(st.get("timer") is None for st in tracker.state.values()))

    def test_legacy_pitch_direction_keeps_old_sentinels_full_uses_bounds(self):
        legacy = {"kind": "pitchwheel", "encoding": "absolute", "value_mode": "legacy"}
        full = {**legacy, "value_mode": "full"}
        for raw, expected_legacy, expected_full in ((8191, 0, 1), (0, -1, 0), (-8192, 0, -1)):
            with self.subTest(raw=raw):
                ev = normalize(mido.Message("pitchwheel", pitch=raw))
                ev.prev, ev.raw_prev = ev.value, raw
                self.assertEqual(direction(legacy, event_for_trigger(legacy, ev)), expected_legacy)
                self.assertEqual(direction(full, event_for_trigger(full, ev)), expected_full)
