"""MIDI port handling and message normalization (mido + python-rtmidi)."""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable, Optional

import mido

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def note_name(n: int) -> str:
    return f"{NOTE_NAMES[n % 12]}{n // 12 - 1}"


@dataclass
class MidiEvent:
    """Normalized MIDI event.

    kind: note | cc | pitchwheel | program_change
    event: press | release | change
    value: 0..127 (pitchwheel: -64..64, 0 = center)
    channel: 1..16
    """
    kind: str
    channel: int
    number: int
    value: int
    event: str
    prev: Optional[int] = None  # previous value of same control (filled by engine)
    dt: Optional[float] = None  # ms since previous event of same control (filled by engine)

    @property
    def key(self) -> tuple:
        return (self.kind, self.channel, self.number)

    def describe(self) -> str:
        if self.kind == "note":
            return f"Note {note_name(self.number)}({self.number}) ch{self.channel} {self.event} vel={self.value}"
        if self.kind == "cc":
            return f"CC {self.number} ch{self.channel} = {self.value}"
        if self.kind == "pitchwheel":
            return f"PitchBend ch{self.channel} = {self.value:+d}"
        return f"Program {self.number} ch{self.channel}"


def normalize(msg: mido.Message) -> Optional[MidiEvent]:
    t = msg.type
    if t == "note_on" and msg.velocity > 0:
        return MidiEvent("note", msg.channel + 1, msg.note, msg.velocity, "press")
    if t == "note_off" or (t == "note_on" and msg.velocity == 0):
        return MidiEvent("note", msg.channel + 1, msg.note, 0, "release")
    if t == "control_change":
        return MidiEvent("cc", msg.channel + 1, msg.control, msg.value, "change")
    if t == "pitchwheel":
        v = max(-64, min(64, round(msg.pitch / 128)))
        return MidiEvent("pitchwheel", msg.channel + 1, 0, v, "change")
    if t == "program_change":
        return MidiEvent("program_change", msg.channel + 1, msg.program, 0, "press")
    return None


def direction(trigger: dict, ev: MidiEvent) -> int:
    """+1 / -1 / 0 for knob movement, depending on encoding."""
    enc = trigger.get("encoding", "absolute")
    v = ev.value
    if enc == "rel_2c":  # 1..63 up, 65..127 down
        return 1 if 1 <= v <= 63 else (-1 if v >= 65 else 0)
    if enc == "rel_64":  # 65.. up, ..63 down
        return 1 if v > 64 else (-1 if v < 64 else 0)
    if ev.prev is None:
        return 0
    if v > ev.prev or v == ev.prev == 127:
        return 1
    if v < ev.prev or v == ev.prev == 0:
        return -1
    return 0


def trigger_matches(trigger: dict, ev: MidiEvent, gesture_ok: bool = False) -> bool:
    """gesture_ok=True skips the press/release check (engine handles gestures)."""
    if trigger.get("kind") != ev.kind:
        return False
    ch = trigger.get("channel")
    if ch not in (None, "", 0) and int(ch) != ev.channel:
        return False
    num = trigger.get("number")
    if ev.kind not in ("pitchwheel",) and num not in (None, "") and int(num) != ev.number:
        return False
    lo, hi = trigger.get("value_min"), trigger.get("value_max")
    if ev.event != "release" or ev.kind != "note":
        if lo not in (None, "") and ev.value < int(lo):
            return False
        if hi not in (None, "") and ev.value > int(hi):
            return False
    if gesture_ok:
        return True
    want = trigger.get("event", "press")
    if want in ("increase", "decrease"):
        d = direction(trigger, ev)
        return d == (1 if want == "increase" else -1)
    if ev.kind == "note":
        return want == "both" or want == ev.event
    if ev.kind == "cc" and want in ("press", "release"):
        # Treat CC buttons: >=64 press, <64 release
        return (ev.value >= 64) == (want == "press")
    return True


def trigger_from_event(ev: MidiEvent) -> dict:
    event = "press" if ev.kind in ("note", "program_change") else "change"
    return {"kind": ev.kind, "channel": ev.channel, "number": ev.number, "event": event}


def describe_trigger(t: dict) -> str:
    kind = t.get("kind")
    ch = t.get("channel") or "任意"
    num = t.get("number")
    ev = EVENT_LABELS.get(t.get("event"), t.get("event"))
    g = t.get("gesture", "any")
    if g and g != "any":
        ev = GESTURE_LABELS.get(g, g)
    lo, hi = t.get("value_min"), t.get("value_max")
    if lo not in (None, "") or hi not in (None, ""):
        ev += f" [{lo if lo not in (None, '') else 0}-{hi if hi not in (None, '') else 127}]"
    if kind == "note":
        n = note_name(int(num)) if num not in (None, "") else "任意"
        return f"Note {n} | ch {ch} | {ev}"
    if kind == "cc":
        return f"CC {num if num not in (None, '') else '任意'} | ch {ch} | {ev}"
    if kind == "pitchwheel":
        return f"弯音轮 | ch {ch}"
    return f"Program {num} | ch {ch}"


EVENT_LABELS = {"press": "按下", "release": "松开", "both": "按下+松开", "change": "数值变化",
                "increase": "增大/右转", "decrease": "减小/左转"}
GESTURE_LABELS = {"any": "普通", "short": "短按", "long": "长按", "double": "双击"}


def list_inputs() -> list[str]:
    try:
        return mido.get_input_names()
    except Exception:
        return []


def list_outputs() -> list[str]:
    try:
        return mido.get_output_names()
    except Exception:
        return []


class MidiInput:
    """Opens an input port; callback runs on the rtmidi thread."""

    def __init__(self, on_event: Callable[[MidiEvent], None]):
        self.on_event = on_event
        self.port = None
        self.name = ""
        self._lock = threading.Lock()

    def open(self, name: str) -> None:
        self.close()
        with self._lock:
            self.port = mido.open_input(name, callback=self._cb)
            self.name = name

    def close(self) -> None:
        with self._lock:
            if self.port is not None:
                try:
                    self.port.close()
                finally:
                    self.port = None
                    self.name = ""

    def _cb(self, msg):
        ev = normalize(msg)
        if ev is not None:
            self.on_event(ev)


class MidiOutput:
    def __init__(self):
        self.port = None
        self.name = ""

    def open(self, name: str) -> None:
        self.close()
        if name:
            self.port = mido.open_output(name)
            self.name = name

    def close(self) -> None:
        if self.port is not None:
            self.port.close()
            self.port = None
            self.name = ""

    def send(self, msg: mido.Message) -> None:
        if self.port is not None:
            self.port.send(msg)
