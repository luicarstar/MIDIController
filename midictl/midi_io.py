"""MIDI port handling and message normalization (mido + python-rtmidi)."""
from __future__ import annotations

import threading
from dataclasses import dataclass, replace
from typing import Callable, Optional

import mido

NOTE_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


def note_name(n: int) -> str:
    return f"{NOTE_NAMES[n % 12]}{n // 12 - 1}"


@dataclass
class MidiEvent:
    """Normalized MIDI event.

    kind: note | cc | cc14 | rpn | nrpn | pitchwheel | program_change | aftertouch | polytouch
    event: press | release | change
    value: 0..127 (pitchwheel: -64..64, 0 = center)
    channel: 1..16
    """
    kind: str
    channel: int
    number: int
    value: Optional[int]
    event: str
    prev: Optional[int] = None  # previous value of same control (filled by engine)
    dt: Optional[float] = None  # ms since previous event of same control (filled by engine)
    source: str = ""  # exact input port name; empty for legacy synthetic events
    source_generation: int = 0  # connection identity, never reused after disconnect
    raw_value: Optional[int] = None
    raw_prev: Optional[int] = None
    coarse: Optional[int] = None
    fine: Optional[int] = None
    coarse_valid: bool = False
    fine_valid: bool = False
    delta: Optional[int] = None  # non-None denotes a relative operation, not an absolute value
    value_mode: str = "legacy"  # full pitch is signed; parameter full is transport-only raw14

    @property
    def key(self) -> tuple:
        return (self.source, self.kind, self.channel, self.number)

    def describe(self) -> str:
        if self.kind in ("cc14", "rpn", "nrpn"):
            value = f"delta={self.delta:+d}" if self.delta is not None else str(self.value)
            return f"{self.kind.upper()} {self.number} ch{self.channel} = {value}"
        if self.kind == "note":
            return f"Note {note_name(self.number)}({self.number}) ch{self.channel} {self.event} vel={self.value}"
        if self.kind == "cc":
            return f"CC {self.number} ch{self.channel} = {self.value}"
        if self.kind == "pitchwheel":
            return f"PitchBend ch{self.channel} = {self.value:+d}"
        if self.kind == "aftertouch":
            return f"Aftertouch ch{self.channel} = {self.value}"
        if self.kind == "polytouch":
            return f"Polytouch {note_name(self.number)}({self.number}) ch{self.channel} = {self.value}"
        return f"Program {self.number} ch{self.channel}"


def normalize(msg: mido.Message, source: str = "") -> Optional[MidiEvent]:
    """Normalize without opening ports; preserve legacy pitch and its exact raw value."""
    ev = _normalize(msg)
    if ev is not None:
        ev.source = source
        ev.raw_value = msg.pitch if msg.type == "pitchwheel" else ev.value
    return ev


def _normalize(msg: mido.Message) -> Optional[MidiEvent]:
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
    if t in ("aftertouch", "polytouch"):
        return MidiEvent(t, msg.channel + 1, msg.note if t == "polytouch" else 0, msg.value, "change")
    return None


def value_bounds(ev: MidiEvent) -> Optional[tuple[int, int]]:
    """Absolute transport bounds; delta-only events have no absolute domain."""
    if ev.value is None:
        return None
    if ev.kind == "pitchwheel":
        return (-8192, 8191) if ev.value_mode == "full" else (-64, 64)
    if ev.kind in ("cc14", "rpn", "nrpn"):
        return (0, 16383)
    if ev.kind in ("note", "cc", "aftertouch", "polytouch", "program_change"):
        return (0, 127)
    return None


def event_for_trigger(trigger: dict, ev: MidiEvent) -> MidiEvent:
    """Return an independent mapping view; the engine owns absolute raw_prev history.

    Missing value_mode means legacy. Unknown absolute values remain None, so callers
    must reject value-follow actions when value_bounds(view) is None.
    """
    mode = trigger.get("value_mode", "legacy")
    if mode == "full":
        return replace(ev, value=ev.raw_value if ev.raw_value is not None else ev.value,
                       prev=ev.raw_prev if ev.raw_value is not None else ev.prev, value_mode="full")
    if ev.kind == "pitchwheel" and ev.value_mode == "full":
        value = None if ev.value is None else max(-64, min(64, round(ev.value / 128)))
        prev = None if ev.prev is None else max(-64, min(64, round(ev.prev / 128)))
        return replace(ev, value=value, prev=prev, value_mode="legacy")
    return replace(ev, value_mode="legacy")


def button_state(trigger: dict, ev: MidiEvent) -> Optional[bool]:
    """Physical button state shared by matching, gestures and owner release."""
    if ev.kind == "note":
        return ev.event != "release"
    if ev.kind != "cc" or ev.value is None or ev.delta is not None:
        return None
    threshold = trigger.get("threshold", 64)
    threshold = 64 if threshold in (None, "") else int(threshold)
    return (ev.value >= threshold) != bool(trigger.get("invert", False))


def button_edge(trigger: dict, ev: MidiEvent) -> Optional[str]:
    """Default edge mode; level explicitly preserves migrated repeated CC presses."""
    state = button_state(trigger, ev)
    if state is None:
        return None
    if ev.kind == "note" or trigger.get("edge_mode", "edge") == "level":
        return "press" if state else "release"
    if ev.prev is None:
        return "press" if state else None
    previous = button_state(trigger, replace(ev, value=ev.prev))
    return ("press" if state else "release") if state != previous else None


def relative_delta(trigger: dict, ev: MidiEvent) -> int:
    """Signed logical steps; legacy relative encodings stay single-step by default.

    relative_steps=True opts old rel_2c/rel_64 into magnitude. New encodings always
    retain magnitude. Arturia3 accepts only 13..19, plus neutral 0 as an app policy.
    Absolute endpoints intentionally repeat direction; change events are not deduped.
    """
    enc = trigger.get("encoding", "absolute")
    v = ev.value
    if ev.delta is not None:
        d = ev.delta
    elif v is None:
        d = 0
    elif enc == "rel_2c":
        d = v if 1 <= v <= 63 else (v - 128 if 65 <= v <= 127 else 0)
    elif enc in ("rel_64", "rel_offset"):
        d = v - 64 if 0 <= v <= 127 else 0
    elif enc == "rel_signed":
        d = (v if v <= 63 else -(v & 63)) if 0 <= v <= 127 else 0
    elif enc == "rel_arturia3":
        d = v - 16 if 13 <= v <= 19 else 0
    elif ev.prev is None:
        d = 0
    elif ev.kind == "pitchwheel" and ev.value_mode != "full":
        # Legacy absolute direction used 7-bit endpoint sentinels even for pitch.
        # Preserve repeated zero -> -1 and repeated +64 -> 0 for existing mappings.
        d = (1 if v > ev.prev or v == ev.prev == 127 else
             -1 if v < ev.prev or v == ev.prev == 0 else 0)
    else:
        bounds = value_bounds(ev)
        d = (1 if v > ev.prev or (bounds and v == ev.prev == bounds[1]) else
             -1 if v < ev.prev or (bounds and v == ev.prev == bounds[0]) else 0)
    if ev.delta is None and enc in ("rel_2c", "rel_64") and not trigger.get("relative_steps", False):
        d = (d > 0) - (d < 0)
    return -d if trigger.get("reverse", False) else d


def direction(trigger: dict, ev: MidiEvent) -> int:
    """Compatibility direction, always -1 / 0 / +1."""
    d = relative_delta(trigger, ev)
    return (d > 0) - (d < 0)


def trigger_matches(trigger: dict, ev: MidiEvent, gesture_ok: bool = False) -> bool:
    """gesture_ok=True skips the press/release check (engine handles gestures)."""
    if trigger.get("kind") != ev.kind:
        return False
    ev = event_for_trigger(trigger, ev)
    if trigger.get("source") not in (None, "", ev.source):
        return False
    ch = trigger.get("channel")
    if ch not in (None, "", 0) and int(ch) != ev.channel:
        return False
    num = trigger.get("number")
    if ev.kind not in ("pitchwheel", "aftertouch") and num not in (None, "") and int(num) != ev.number:
        return False
    lo, hi = trigger.get("value_min"), trigger.get("value_max")
    if ev.value is None and (lo not in (None, "") or hi not in (None, "")):
        return False
    if ev.event != "release" or ev.kind != "note":
        if lo not in (None, "") and ev.value < int(lo):
            return False
        if hi not in (None, "") and ev.value > int(hi):
            return False
    if gesture_ok and ev.value is not None:
        return True
    want = trigger.get("event", "press")
    if want in ("increase", "decrease"):
        d = direction(trigger, ev)
        return d == (1 if want == "increase" else -1)
    if ev.value is None:
        return want == "change"
    if ev.kind == "note":
        return want == "both" or want == ev.event
    if ev.kind == "cc" and want in ("press", "release"):
        return button_edge(trigger, ev) == want
    if ev.kind == "cc" and want == "both":
        return button_edge(trigger, ev) is not None
    return True


def trigger_from_event(ev: MidiEvent) -> dict:
    event = "press" if ev.kind in ("note", "program_change") else "change"
    if ev.delta is not None:
        event = "increase" if ev.delta > 0 else "decrease" if ev.delta < 0 else "change"
    trigger = {"kind": ev.kind, "channel": ev.channel, "number": ev.number, "event": event,
               "source": ev.source or None, "edge_mode": "edge"}
    if ev.kind in ("pitchwheel", "cc14", "rpn", "nrpn"):
        trigger["value_mode"] = "full"
        bounds = value_bounds(event_for_trigger(trigger, ev))
        if bounds is not None:
            trigger["value_min"], trigger["value_max"] = bounds
    return trigger


@dataclass
class _ParameterState:
    kind: str
    msb: Optional[int] = None
    lsb: Optional[int] = None
    coarse: Optional[int] = None
    fine: Optional[int] = None

    @property
    def number(self) -> Optional[int]:
        if self.msb is None or self.lsb is None:
            return None
        if self.kind == "rpn" and self.msb == self.lsb == 127:
            return None
        return (self.msb << 7) | self.lsb


class MidiDecoder:
    """Serial, source/channel-isolated MIDI 1.0 transport decoder; no hardware I/O.

    feed returns original CC first, then independent cc14/rpn/nrpn events. Engine
    must reject stale connection callbacks BEFORE feed and stamp source_generation
    on every returned event. It must not update absolute history for value=None.

    Application policies: orphan LSB does not emit; kind switching discards both
    selector halves and awaits a complete new selector, in either arrival order.
    Same-kind selection can update one half after initial completion. Changing the
    selected parameter clears data assembly, not the device's parameter value.
    Data Entry emits raw transport snapshots (not semitones/cents/etc.); missing
    fine is represented by zero with fine_valid=False. Inc/dec emits only delta,
    ignores its value byte and invalidates data assembly because the new absolute
    value is unknown. NRPN 127/127 is not treated as standardized null.

    CC121 clears this channel's parser state as an application lifecycle policy;
    this does not emulate a synthesizer's complete Reset All Controllers response.
    """

    def __init__(self):
        self._cc: dict[tuple[str, int, int], tuple[Optional[int], Optional[int]]] = {}
        self._parameters: dict[tuple[str, int], _ParameterState] = {}

    def clear(self) -> None:
        self._cc.clear()
        self._parameters.clear()

    def reset_source(self, source: str) -> None:
        for store in (self._cc, self._parameters):
            for key in list(store):
                if key[0] == source:
                    del store[key]

    def feed(self, msg: mido.Message, source: str) -> list[MidiEvent]:
        ev = normalize(msg, source)
        if ev is None:
            return []
        events = [ev]
        if ev.kind != "cc":
            return events
        n, v, ch = msg.control, msg.value, ev.channel
        channel_key = (source, ch)
        if n == 121:
            self._parameters.pop(channel_key, None)
            for key in list(self._cc):
                if key[:2] == channel_key:
                    del self._cc[key]
            return events
        if 0 <= n <= 63:
            base = n if n < 32 else n - 32
            key = (source, ch, base)
            coarse, fine = self._cc.get(key, (None, None))
            if n < 32:
                coarse, fine = v, None  # every MSB clears the previous LSB
            else:
                fine = v
            self._cc[key] = (coarse, fine)
            if coarse is not None:
                value = (coarse << 7) | (fine if fine is not None else 0)
                events.append(MidiEvent("cc14", ch, base, value, "change", source=source,
                                        raw_value=value, coarse=coarse, fine=fine,
                                        coarse_valid=True, fine_valid=fine is not None,
                                        value_mode="full"))
        if n in (98, 99, 100, 101):
            kind = "rpn" if n in (100, 101) else "nrpn"
            state = self._parameters.get(channel_key)
            if state is None or state.kind != kind:
                state = _ParameterState(kind)
                self._parameters[channel_key] = state
            previous = state.number
            if n in (99, 101):
                state.msb = v
            else:
                state.lsb = v
            if state.number != previous or state.number is None:
                state.coarse = state.fine = None
            return events
        state = self._parameters.get(channel_key)
        if state is None or state.number is None:
            return events  # unselected data is never replayed after selection
        if n in (96, 97):
            events.append(MidiEvent(state.kind, ch, state.number, None, "change", source=source,
                                    delta=1 if n == 96 else -1, value_mode="full"))
            state.coarse = state.fine = None
        elif n in (6, 38):
            if n == 6:
                state.coarse, state.fine = v, None
            else:
                state.fine = v
            if state.coarse is not None:
                value = (state.coarse << 7) | (state.fine if state.fine is not None else 0)
                events.append(MidiEvent(state.kind, ch, state.number, value, "change", source=source,
                                        raw_value=value, coarse=state.coarse, fine=state.fine,
                                        coarse_valid=True, fine_valid=state.fine is not None,
                                        value_mode="full"))
        return events


def describe_trigger(t: dict) -> str:
    kind = t.get("kind")
    ch = t.get("channel") or "任意"
    num = t.get("number")
    ev = EVENT_LABELS.get(t.get("event"), t.get("event")) or ""
    g = t.get("gesture", "any")
    if g and g != "any":
        ev = GESTURE_LABELS.get(g, g)
    lo, hi = t.get("value_min"), t.get("value_max")
    if lo not in (None, "") or hi not in (None, ""):
        lower, upper = ((-8192, 8191) if kind == "pitchwheel" and t.get("value_mode") == "full" else
                        (-64, 64) if kind == "pitchwheel" else
                        (0, 16383) if kind in ("cc14", "rpn", "nrpn") else (0, 127))
        ev += f" [{lo if lo not in (None, '') else lower}-{hi if hi not in (None, '') else upper}]"
    if kind == "note":
        n = note_name(int(num)) if num not in (None, "") else "任意"
        return f"Note {n} | ch {ch} | {ev}"
    if kind == "cc":
        return f"CC {num if num not in (None, '') else '任意'} | ch {ch} | {ev}"
    if kind in ("cc14", "rpn", "nrpn"):
        return f"{kind.upper()} {num if num not in (None, '') else '任意'} | ch {ch} | {ev}"
    if kind == "pitchwheel":
        return f"弯音轮 | ch {ch}"
    if kind == "aftertouch":
        return f"通道触后 | ch {ch} | {ev}"
    if kind == "polytouch":
        return f"复音触后 {num if num not in (None, '') else '任意'} | ch {ch} | {ev}"
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
        self.on_message: Optional[Callable[[mido.Message], None]] = None
        self.port = None
        self.name = ""
        self._lock = threading.Lock()

    def open(self, name: str) -> None:
        self.close()
        with self._lock:
            # Bind this port's consumer now; late callbacks keep their old session.
            consumer = self.on_event
            raw_consumer = self.on_message
            self.port = mido.open_input(name, callback=lambda msg: self._cb(msg, consumer, name, raw_consumer))
            self.name = name

    def close(self) -> None:
        with self._lock:
            if self.port is not None:
                try:
                    self.port.close()
                finally:
                    self.port = None
                    self.name = ""

    def _cb(self, msg, consumer=None, source="", raw_consumer=None):
        if raw_consumer is not None:
            raw_consumer(msg)
            return
        ev = normalize(msg)
        if ev is not None:
            ev.source = source
            (consumer or self.on_event)(ev)


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
