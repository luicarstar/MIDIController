"""Engine: MIDI events -> profile selection -> matching mappings -> actions."""
from __future__ import annotations

import queue
import threading
import time
import traceback
from typing import Callable, Optional

from .actions import ActionRunner
from .config import GLOBAL_PROFILE
from .foreground import foreground_exe
from .midi_io import MidiEvent, MidiInput, MidiOutput, trigger_matches
from .rules import check_conditions

DOUBLE_MS = 300
LONG_MS = 500


def select_profile(cfg: dict, exe: str, forced: Optional[str] = None) -> dict:
    profiles = cfg["profiles"]
    if forced:
        for p in profiles:
            if p["name"] == forced:
                return p
    if cfg["settings"].get("app_switching", True) and exe:
        for p in profiles:
            if p["name"] != GLOBAL_PROFILE and exe in [a.lower() for a in p.get("apps", [])]:
                return p
    return next(p for p in profiles if p["name"] == GLOBAL_PROFILE)


def _usable(m: dict, ev: MidiEvent, variables: dict, gesture_ok=False) -> bool:
    return (m.get("enabled", True) and trigger_matches(m["trigger"], ev, gesture_ok)
            and check_conditions(m.get("conditions", ""), variables, ev))


def _gesture(m: dict) -> str:
    return m["trigger"].get("gesture", "any") or "any"


def find_matches(cfg: dict, profile: dict, ev: MidiEvent, variables: dict | None = None,
                 gestures: bool = False) -> list[dict]:
    """Mappings in the active profile win; global mappings fill in the rest."""
    variables = variables if variables is not None else {}
    want_g = (lambda m: _gesture(m) != "any") if gestures else (lambda m: _gesture(m) == "any")

    def collect(maps):
        out = []
        for m in maps:
            if want_g(m) and _usable(m, ev, variables, gesture_ok=gestures):
                out.append(m)
                if m.get("stop"):
                    break
        return out

    hits = collect(profile["mappings"])
    if hits or profile["name"] == GLOBAL_PROFILE:
        return hits
    glob = next(p for p in cfg["profiles"] if p["name"] == GLOBAL_PROFILE)
    return collect(glob["mappings"])


class GestureTracker:
    """Detect short / long / double press per control. fire(mapping_list, ev) is called."""

    def __init__(self, fire):
        self.fire = fire
        self.state: dict[tuple, dict] = {}

    def handle(self, ev: MidiEvent, maps: list[dict]) -> None:
        is_press = ev.event == "press" or (ev.kind == "cc" and ev.value >= 64)
        if ev.kind == "cc" and ev.prev is not None and (ev.prev >= 64) == is_press:
            return  # CC held steady, not a new edge
        st = self.state.setdefault(ev.key, {"down": None, "taps": 0, "timer": None, "long_fired": False})
        has = {g: [m for m in maps if _gesture(m) == g] for g in ("short", "long", "double")}
        if is_press:
            st["down"] = time.monotonic()
            st["long_fired"] = False
            if has["long"]:
                ms = max(int(m["trigger"].get("long_ms") or LONG_MS) for m in has["long"])
                t = threading.Timer(ms / 1000, self._long, (ev, has["long"], st))
                st["long_t"] = t
                t.start()
            return
        # release
        lt = st.pop("long_t", None)
        if lt:
            lt.cancel()
        if st["long_fired"] or st["down"] is None:
            st["down"] = None
            return
        st["down"] = None
        if has["double"]:
            st["taps"] += 1
            if st["timer"]:
                st["timer"].cancel()
                st["timer"] = None
            if st["taps"] >= 2:
                st["taps"] = 0
                self.fire(has["double"], ev)
                return
            t = threading.Timer(DOUBLE_MS / 1000, self._single, (ev, has["short"], st))
            st["timer"] = t
            t.start()
        elif has["short"]:
            self.fire(has["short"], ev)

    def _long(self, ev, maps, st):
        if st["down"] is not None:
            st["long_fired"] = True
            self.fire(maps, ev)

    def _single(self, ev, maps, st):
        st["taps"] = 0
        st["timer"] = None
        if maps:
            self.fire(maps, ev)


class Engine:
    def __init__(self, cfg: dict, log: Callable[[str], None] = print):
        self.cfg = cfg
        self.log = log
        self.forced_profile: Optional[str] = None
        self.active_profile = GLOBAL_PROFILE
        self.foreground = ""
        # UI hooks (called from worker thread)
        self.on_midi: Callable[[MidiEvent], None] = lambda ev: None
        self.on_profile_changed: Callable[[str, str], None] = lambda name, exe: None
        self.learn_callback: Optional[Callable[[MidiEvent], None]] = None
        self.variables: dict[str, int] = {}
        self.on_variables: Callable[[dict], None] = lambda v: None
        self.on_hud: Callable[[str], None] = lambda text: None
        self._last_values: dict[tuple, int] = {}
        self._last_times: dict[tuple, float] = {}

        self.midi_in = MidiInput(self._enqueue)
        self.midi_out = MidiOutput()
        self.runner = ActionRunner(self.midi_out, self._force_profile, self._log, self.variables)
        self.gestures = GestureTracker(lambda maps, ev: self._q.put(("fire", maps, ev)))
        self._q: queue.Queue = queue.Queue()
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._loop, name="midictl-engine", daemon=True)
        self._watch = threading.Thread(target=self._watch_fg, name="midictl-fg", daemon=True)

    def _log(self, s: str) -> None:
        self.log(s)

    def start(self) -> None:
        self._worker.start()
        self._watch.start()

    def stop(self) -> None:
        self._stop.set()
        self._q.put(None)
        self.midi_in.close()
        self.midi_out.close()

    def open_ports(self) -> None:
        s = self.cfg["settings"]
        for kind, port, name in (("输入", self.midi_in, s.get("input_port")),
                                 ("输出", self.midi_out, s.get("output_port"))):
            try:
                if name:
                    port.open(name)
                    self.log(f"已打开 MIDI {kind}: {name}")
                else:
                    port.close()
            except Exception as e:
                self.log(f"打开 MIDI {kind}失败: {name}: {e}")

    def _enqueue(self, ev: MidiEvent) -> None:
        self._q.put(ev)

    def inject(self, ev: MidiEvent) -> None:
        """Feed a synthetic event (tests / virtual keyboard)."""
        self._q.put(ev)

    def _force_profile(self, name: str) -> None:
        self.forced_profile = None if name == "自动" else name
        self._update_profile()

    def _update_profile(self) -> None:
        p = select_profile(self.cfg, self.foreground, self.forced_profile)
        if p["name"] != self.active_profile:
            self.runner.release_all()
            self.active_profile = p["name"]
            self.on_profile_changed(p["name"], self.foreground)

    def _watch_fg(self) -> None:
        while not self._stop.is_set():
            exe = foreground_exe()
            if exe and exe != self.foreground and exe not in ("python.exe", "pythonw.exe", "midictl.exe"):
                self.foreground = exe
                self._q.put("fg")
            time.sleep(0.3)

    def _run_maps(self, maps: list[dict], ev: MidiEvent) -> None:
        before = dict(self.variables)
        for m in maps:
            try:
                self.runner.run(m, ev)
            except Exception as e:
                self.log(f"[{m.get('name')}] 动作失败: {e}")
            if m.get("hud"):
                self.on_hud(self._hud_text(m, ev))
        if self.variables != before:
            self.on_variables(dict(self.variables))

    def _hud_text(self, m: dict, ev: MidiEvent) -> str:
        text = m.get("name", "")
        if ev.kind in ("cc", "pitchwheel"):
            text += f"  {ev.value:+d}" if ev.kind == "pitchwheel" else f"  {ev.value}"
        if self.variables:
            text += "\n" + "  ".join(f"{k}={v}" for k, v in sorted(self.variables.items()))
        return text

    def _loop(self) -> None:
        while True:
            ev = self._q.get()
            if ev is None:
                break
            try:
                if ev == "fg":
                    self._update_profile()
                    continue
                if isinstance(ev, tuple) and ev[0] == "fire":
                    self._run_maps(ev[1], ev[2])
                    continue
                ev.prev = self._last_values.get(ev.key)
                self._last_values[ev.key] = ev.value
                now = time.monotonic()
                last = self._last_times.get(ev.key)
                ev.dt = None if last is None else (now - last) * 1000
                self._last_times[ev.key] = now
                self.on_midi(ev)
                if self.learn_callback is not None:
                    cb, self.learn_callback = self.learn_callback, None
                    cb(ev)
                    continue
                if not self.cfg["settings"].get("enabled", True):
                    continue
                profile = select_profile(self.cfg, self.foreground, self.forced_profile)
                self._run_maps(find_matches(self.cfg, profile, ev, self.variables), ev)
                gmaps = find_matches(self.cfg, profile, ev, self.variables, gestures=True)
                if gmaps or ev.key in self.gestures.state:
                    self.gestures.handle(ev, gmaps)
            except Exception:
                self.log(traceback.format_exc())
        self.runner.release_all()
