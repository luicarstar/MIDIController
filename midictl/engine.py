"""Engine: MIDI events -> profile selection -> matching mappings -> actions."""
from __future__ import annotations

import queue
import threading
import time
import traceback
from collections import deque
from dataclasses import replace
from typing import Callable, Optional

from .actions import ActionRunner
from .config import GLOBAL_PROFILE, switch_keyboard, sync_keyboard, input_ports
from .foreground import foreground_exe
from .feedback import FeedbackBindings
from .midi_devices import WinRTCatalog, WinRTInput
from .midi_io import (MidiEvent, MidiInput, MidiOutput, MidiDecoder, trigger_matches,
                      list_inputs, list_outputs, event_for_trigger, button_edge)
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
    ev = event_for_trigger(m["trigger"], ev)
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
        self._lock = threading.RLock()

    def handle(self, ev: MidiEvent, maps: list[dict]) -> None:
        if ev.kind not in ("note", "cc"):
            return
        with self._lock:
            current = {m["id"]: m for m in maps}
            for key in [k for k in self.state if k[:4] == ev.key and k[4] not in current]:
                self._discard(key)
            for mapping in maps:
                self._handle_one(ev, mapping, maps)

    def has_control(self, ev: MidiEvent) -> bool:
        with self._lock:
            return any(key[:4] == ev.key for key in self.state)

    def _discard(self, key):
        st = self.state.pop(key, {})
        for timer in (st.get("timer"), st.get("long_t")):
            if timer:
                timer.cancel()

    def clear(self) -> None:
        with self._lock:
            for key in list(self.state):
                self._discard(key)

    def reset_source(self, source: str) -> None:
        with self._lock:
            for key in [key for key in self.state if key[0] == source]:
                self._discard(key)

    def _handle_one(self, ev: MidiEvent, mapping: dict, maps: list[dict]) -> None:
        trigger = mapping["trigger"]
        ev = event_for_trigger(trigger, ev)
        edge = button_edge(trigger, ev)
        if edge is None:
            return
        key = ev.key + (mapping["id"],)
        st = self.state.setdefault(key, {"down": None, "taps": 0, "timer": None, "long_fired": False})
        gesture = _gesture(mapping)
        def signature(t):
            threshold = t.get("threshold", 64)
            return ((64 if threshold in (None, "") else int(threshold), bool(t.get("invert", False)))
                    if ev.kind == "cc" else (None, False))
        st["signature"], st["gesture"] = signature(trigger), gesture
        if edge == "press":
            if st["down"] is not None:
                return
            if st.get("long_t"):
                st["long_t"].cancel()
            st["down"] = time.monotonic()
            st["long_fired"] = False
            if gesture == "long":
                ms = int(trigger.get("long_ms") or LONG_MS)
                t = threading.Timer(ms / 1000, self._long, (key, ev, [mapping], st))
                st["long_t"] = t
                t.daemon = True
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
        has_double = any(_gesture(m) == "double" and signature(m["trigger"]) == signature(trigger)
                         for m in maps)
        if gesture == "double" or (gesture == "short" and has_double):
            st["taps"] += 1
            if st["timer"]:
                st["timer"].cancel()
                st["timer"] = None
            if st["taps"] >= 2:
                st["taps"] = 0
                if gesture == "double":
                    self.fire([mapping], ev)
                return
            t = threading.Timer(DOUBLE_MS / 1000, self._single,
                                (key, ev, [mapping] if gesture == "short" else [], st))
            st["timer"] = t
            t.daemon = True
            t.start()
        elif gesture == "short":
            self.fire([mapping], ev)

    def _long(self, key, ev, maps, st):
        with self._lock:
            if self.state.get(key) is st and st["down"] is not None:
                st["long_fired"] = True
                # One physical long press suppresses short/double interpretations of
                # this press, while different threshold/polarity groups stay independent.
                for other_key, other in self.state.items():
                    if (other_key[:4] == key[:4] and other.get("signature") == st.get("signature")
                            and other.get("gesture") in ("short", "double") and other["down"] is not None):
                        other["long_fired"] = True
                        other["taps"] = 0
                        if other.get("timer"):
                            other["timer"].cancel()
                            other["timer"] = None
                self.fire(maps, ev)

    def _single(self, key, ev, maps, st):
        with self._lock:
            if self.state.get(key) is not st or st["long_fired"]:
                return
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
        self.learn_kind: Optional[str] = None
        self.learn_source: Optional[str] = None
        self.variables: dict[str, int] = {}
        self.on_variables: Callable[[dict], None] = lambda v: None
        self.on_hud: Callable[[str], None] = lambda text: None
        self.on_connection_changed: Callable[[str], None] = lambda text: None
        self.on_ports_changed: Callable[[list, list], None] = lambda inputs, outputs: None
        self._last_values: dict[tuple, int] = {}
        self._last_raw_values: dict[tuple, int] = {}
        self._last_times: dict[tuple, float] = {}
        self._state_lock = threading.RLock()
        self._generation = 0
        self._requested = (None, None)
        self._known_ports = None
        self._connection = ""
        self.decoder = MidiDecoder()
        self._ingress_lock = threading.Lock()
        self._raw_pending = 0
        self._overflow_sources: set[str] = set()
        self._diagnostics = deque(maxlen=64)
        self._last_diagnostic = 0.0
        self._state_notice = False
        self._winrt_names: dict[str, str] = {}
        self._winrt_error = ""
        self._input_retry: dict[str, float] = {}

        self.inputs: dict[str, MidiInput] = {}
        self._source_generations: dict[str, int] = {}
        self._idle_input = MidiInput(self._enqueue)
        self.midi_out = MidiOutput()
        self.runner = ActionRunner(self.midi_out, self._force_profile, self._log, self.variables)
        self.runner.on_profile_event = self._force_profile
        self.runner.on_state_change = self.request_feedback
        self._q: queue.Queue = queue.Queue()
        self.winrt_catalog = WinRTCatalog(lambda devices, error: self._q.put(("winrt_inventory", devices, error)))
        self.feedback = FeedbackBindings(cfg, self.midi_out, self._feedback_state, self._log)
        self.gestures = self._new_gestures()
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._loop, name="midictl-engine", daemon=True)
        self._watch = threading.Thread(target=self._watch_fg, name="midictl-fg", daemon=True)

    def _log(self, s: str) -> None:
        self.log(s)

    @property
    def midi_in(self) -> MidiInput:
        """Legacy read-only view of the first selected input, not the full collection."""
        for name in input_ports(self.cfg["settings"]):
            if name in self.inputs:
                return self.inputs[name]
        return self._idle_input

    def source_label(self, source: str) -> str:
        if source.startswith("winrt:"):
            return "[WinRT·系统MIDI/蓝牙] " + self._winrt_names.get(source, "未连接端点 " + source[6:54])
        return "[WinMM] " + source if source else "合成"

    def start(self) -> None:
        self._worker.start()
        self._watch.start()

    def stop(self) -> None:
        with self._state_lock:
            if self._stop.is_set():
                return
            self._stop.set()
            self._generation += 1
            self._reset_runtime(clear_variables=True)
            for source in list(self.inputs):
                self._drop_input(source)
            self._close_port(self.midi_out)
            self._q.put(None)
        for thread in (self._worker, self._watch):
            if thread.is_alive() and thread is not threading.current_thread():
                thread.join(timeout=2)

    def _new_gestures(self) -> GestureTracker:
        tracker = GestureTracker(lambda maps, ev: self._q.put(("fire", tracker, maps, ev)))
        return tracker

    def _reset_runtime(self, clear_variables=False) -> None:
        self.feedback.cancel()
        self.runner.release_all()
        self.gestures.clear()
        self.gestures = self._new_gestures()
        self.learn_callback = None
        self.learn_kind = self.learn_source = None
        self._last_values.clear()
        self._last_raw_values.clear()
        self._last_times.clear()
        self.decoder.clear()
        if clear_variables:
            self.variables.clear()
            self.on_variables({})

    def _close_port(self, port) -> None:
        try:
            port.close()
        except Exception as e:
            self.log(f"关闭 MIDI 端口失败: {e}")

    def reset_source(self, source: str) -> None:
        """Clear one physical input without cancelling any other input's work."""
        with self._state_lock:
            self.runner.release_source(source)
            self.gestures.reset_source(source)
            self.decoder.reset_source(source)
            for history in (self._last_values, self._last_raw_values, self._last_times):
                for key in [key for key in history if key[0] == source]:
                    del history[key]

    def _drop_input(self, source: str) -> None:
        # Invalidate before closing: a backend can deliver one final callback in close().
        self._source_generations[source] = self._source_generations.get(source, 0) + 1
        port = self.inputs.pop(source, None)
        self.reset_source(source)
        if port is not None:
            self._close_port(port)

    def _source_live(self, ev: MidiEvent) -> bool:
        return (not ev.source or (ev.source in self.inputs and
                ev.source_generation == self._source_generations.get(ev.source)))

    @staticmethod
    def _healthy(port, name: str, available: list) -> bool:
        return bool(name and name in available and port.name == name and port.port is not None
                    and not getattr(port.port, "closed", False))

    def open_ports(self, force=False) -> None:
        """Reconcile exact configured names. Healthy ports stay open."""
        with self._state_lock:
            if self._stop.is_set():
                return
            self.winrt_catalog.refresh()
            inputs, outputs = list_inputs() + list(self._winrt_names), list_outputs()
            inventory = (inputs, outputs)
            if inventory != self._known_ports:
                self._known_ports = inventory
                self.on_ports_changed(inputs, outputs)
            s = self.cfg["settings"]
            requested = (tuple(input_ports(s)), s.get("output_port") or "")
            previous = self._requested[0]
            replace_session = (force or (previous is not None and previous != requested[0]
                                         and not set(previous).intersection(requested[0])))
            output_ok = self._healthy(self.midi_out, requested[1], outputs)
            output_change = (force or requested[1] != self._requested[1]
                             or (self.midi_out.port is not None and not output_ok)
                             or (requested[1] in outputs and not output_ok))
            if replace_session:
                self._generation += 1
                self._reset_runtime(clear_variables=True)
                for source in list(self.inputs):
                    self._drop_input(source)
            elif output_change:
                self._reset_runtime()
            self._requested = requested
            errors = []
            for source, port in list(self.inputs.items()):
                if source not in requested[0] or not self._healthy(port, source, inputs):
                    self._drop_input(source)
            for source in requested[0]:
                if (source not in self.inputs and source in inputs
                        and time.monotonic() >= self._input_retry.get(source, 0)):
                    self._source_generations[source] = self._source_generations.get(source, 0) + 1
                    generation, connection = self._generation, self._source_generations[source]
                    consumer = lambda msg, g=generation, n=source, c=connection: self._receive_message(
                        msg, g, n, c)
                    if source.startswith("winrt:"):
                        port = WinRTInput(consumer, lambda state, detail, g=generation, n=source, c=connection:
                            self._q.put(("winrt_status", g, n, c, state, detail)))
                    else:
                        port = MidiInput(self._enqueue)
                        port.on_message = consumer
                    try:
                        port.open(source)
                        self.inputs[source] = port
                    except Exception as e:
                        self._close_port(port)
                        errors.append(f"输入打开失败 {source}: {e}")
            if output_change:
                self.feedback.cancel(clear=True)
                self._close_port(self.midi_out)
                if requested[1] and requested[1] in outputs:
                    try:
                        self.midi_out.open(requested[1])
                    except Exception as e:
                        errors.append(f"输出打开失败: {e}")
                if self.midi_out.port is not None:
                    if s.get("enabled", True):
                        self.feedback.request(force=True)
                    else:
                        self.feedback.deactivate(enabled_only=True)
            elif replace_session:
                self.feedback.request(force=True)
            states = []
            for source in requested[0]:
                port = self.inputs.get(source)
                state = ("连接中" if getattr(port, "state", "") == "opening" else
                         "已连接" if port is not None else "等待连接")
                states.append(f"输入{state}: {self.source_label(source)}")
            if not requested[0]:
                states.append("输入未选择")
            name = requested[1]
            state = "未选择" if not name else ("已连接" if self._healthy(self.midi_out, name, outputs)
                                               else "等待连接")
            states.append(f"输出{state}" + (f": {name}" if name else ""))
            text = " | ".join(states + errors)
            if text != self._connection:
                self._connection = text
                self.log(text)
                self.on_connection_changed(text)

    def poll_ports(self) -> None:
        self.open_ports()

    def refresh_ports(self) -> None:
        self.winrt_catalog.refresh(force=True)
        self.open_ports()

    def bind_ports(self, input_port: str | list[str], output_port: str) -> None:
        with self._state_lock:
            names = ([input_port] if input_port else []) if isinstance(input_port, str) else input_port
            names = input_ports({"input_ports": names})
            self.cfg["settings"].update(input_ports=names, input_port=names[0] if names else "",
                                        output_port=output_port)
            sync_keyboard(self.cfg)
            self.open_ports()

    def select_keyboard(self, keyboard_id: str) -> None:
        with self._state_lock:
            self.feedback.deactivate()
            switch_keyboard(self.cfg, keyboard_id)
            self.forced_profile = None
            self.active_profile = ""
            self.open_ports(force=True)
            self._update_profile()

    def set_enabled(self, enabled: bool) -> None:
        with self._state_lock:
            self.cfg["settings"]["enabled"] = enabled
            self.open_ports(force=True)

    def _enqueue(self, ev: MidiEvent) -> None:
        self._q.put(("midi", self._generation, replace(ev,
            source_generation=self._source_generations.get(ev.source, 0))))

    def _receive_message(self, msg, generation: int, source: str, connection: int) -> None:
        supported = msg.type in ("note_on", "note_off", "control_change", "pitchwheel",
                                  "program_change", "aftertouch", "polytouch")
        if supported and self.feedback.is_echo(msg):
            return
        if not supported or self.cfg["settings"].get("diagnostics", False):
            self._diagnostic(source, f"{'raw' if supported else '未支持'}: {str(msg)[:240]}")
        if not supported:
            return  # Unknown messages never enter decoding, learning or execution.
        with self._ingress_lock:
            if self._raw_pending >= 2048:
                self._overflow_sources.add(source)
                return
            self._raw_pending += 1
        self._q.put(("raw", generation, source, connection, msg.copy()))

    def _diagnostic(self, source: str, text: str) -> None:
        now = time.monotonic()
        with self._ingress_lock:
            if now - self._last_diagnostic >= 0.1:
                self._last_diagnostic = now
                self._diagnostics.append(f"[{source or '合成'}] {text[:300]}")

    def drain_diagnostics(self) -> list[str]:
        with self._ingress_lock:
            lines = list(self._diagnostics)
            self._diagnostics.clear()
            return lines

    def mappings_changed(self) -> None:
        with self._state_lock:
            self._reset_runtime()
            self.feedback.request()

    def _feedback_state(self) -> dict:
        return {"enabled": self.cfg["settings"].get("enabled", True),
                "keyboard": self.cfg["active_keyboard"],
                "profile": select_profile(self.cfg, self.foreground, self.forced_profile)["name"],
                "variables": dict(self.variables)}

    def request_feedback(self) -> None:
        # A delayed macro can call this under the runner lock; never acquire engine's lock here.
        with self._ingress_lock:
            if self._state_notice or self._stop.is_set():
                return
            self._state_notice = True
        self._q.put(("state", self._generation))

    def set_feedback_bindings(self, bindings: list[dict]) -> None:
        with self._state_lock:
            self.feedback.deactivate()
            self.cfg["feedback_bindings"] = bindings
            sync_keyboard(self.cfg)
            self.feedback.request(force=True)

    def inject(self, ev: MidiEvent) -> None:
        """Feed a synthetic event (tests / virtual keyboard)."""
        self._enqueue(ev)

    def _force_profile(self, name: str, ev: MidiEvent | None = None) -> None:
        # A delayed macro may own runner._lock; never invert the engine/runner locks.
        self._q.put(("profile", self._generation, self.runner.epoch, name, ev))

    def _update_profile(self) -> None:
        p = select_profile(self.cfg, self.foreground, self.forced_profile)
        if p["name"] != self.active_profile:
            self._reset_runtime()
            self.active_profile = p["name"]
            self.on_profile_changed(p["name"], self.foreground)
            self.feedback.request(force=True)

    def _watch_fg(self) -> None:
        while not self._stop.is_set():
            exe = foreground_exe()
            if exe and exe != self.foreground and exe not in ("python.exe", "pythonw.exe", "midictl.exe", "midicontroller.exe"):
                self._q.put(("fg", exe))
            self._stop.wait(0.3)

    def _run_maps(self, maps: list[dict], ev: MidiEvent) -> None:
        before = dict(self.variables)
        for m in maps:
            try:
                view = event_for_trigger(m["trigger"], ev)
                self.runner.run(m, view)
            except Exception as e:
                self.log(f"[{m.get('name')}] 动作失败: {e}")
            if m.get("hud"):
                self.on_hud(self._hud_text(m, event_for_trigger(m["trigger"], ev)))
        self.runner.reconcile_holds()
        if self.variables != before:
            self.on_variables(dict(self.variables))
            self.feedback.request()

    def _hud_text(self, m: dict, ev: MidiEvent) -> str:
        text = m.get("name", "")
        if ev.value is None:
            text += f"  Δ{ev.delta:+d}"
        elif ev.kind in ("cc", "cc14", "rpn", "nrpn", "pitchwheel", "aftertouch", "polytouch"):
            text += f"  {ev.value:+d}" if ev.kind == "pitchwheel" else f"  {ev.value}"
        if self.variables:
            text += "\n" + "  ".join(f"{k}={v}" for k, v in sorted(self.variables.items()))
        return text

    def _loop(self) -> None:
        next_poll = time.monotonic() + 1
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=0.1 if self.feedback._due is not None else 0.2)
                if item is None:
                    break
                self._dispatch(item)
            except queue.Empty:
                pass
            except Exception:
                self.log(traceback.format_exc())
            if time.monotonic() >= next_poll:
                try:
                    self.poll_ports()
                except Exception:
                    self.log(traceback.format_exc())
                next_poll = time.monotonic() + 1
            with self._state_lock:
                if not self._stop.is_set():
                    self.feedback.tick()

    def _dispatch(self, item) -> None:
        if isinstance(item, tuple) and item[0] == "raw":
            with self._ingress_lock:
                self._raw_pending = max(0, self._raw_pending - 1)
        if isinstance(item, tuple) and item[0] == "state":
            with self._ingress_lock:
                self._state_notice = False
        with self._state_lock:
            if self._stop.is_set() or item is None:
                return
            if item == "fg":
                self._update_profile()
                return
            kind = item[0]
            if kind == "winrt_inventory":
                devices, error = item[1:]
                if devices is not None:
                    self._winrt_names = {"winrt:" + device_id: label for device_id, label in devices}
                    self._winrt_error = ""
                    self.open_ports()
                elif error != self._winrt_error:
                    self._winrt_error = error
                    self.log("WinRT MIDI设备列表不可用: " + error)
            elif kind == "winrt_status":
                generation, source, connection, state, detail = item[1:]
                if (generation == self._generation and source in self.inputs
                        and connection == self._source_generations.get(source)):
                    if state in ("closed", "disconnected", "error"):
                        self._input_retry[source] = time.monotonic() + 3.0
                        self._drop_input(source)
                    if detail:
                        self.log(f"{self.source_label(source)}: {state} {detail}")
                    self.open_ports()
            elif kind == "fg":
                self.foreground = item[1]
                self._update_profile()
            elif kind == "state" and item[1] == self._generation:
                self.on_variables(dict(self.variables))
                self._update_profile()
                self.feedback.request()
            elif kind == "profile" and item[1] == self._generation and item[2] == self.runner.epoch:
                if len(item) < 5 or item[4] is None or self._source_live(item[4]):
                    self.forced_profile = None if item[3] == "自动" else item[3]
                    self._update_profile()
            elif kind == "fire":
                if (item[1] is self.gestures and self._source_live(item[3])
                        and self.cfg["settings"].get("enabled", True)):
                    profile = select_profile(self.cfg, self.foreground, self.forced_profile)
                    eligible = find_matches(self.cfg, profile, item[3], self.variables, gestures=True)
                    ids = {m["id"] for m in item[2]}
                    self._run_maps([m for m in eligible if m["id"] in ids], item[3])
            elif kind == "raw" and item[1] == self._generation:
                source, connection, msg = item[2:]
                with self._ingress_lock:
                    overflow = source in self._overflow_sources
                    self._overflow_sources.discard(source)
                if overflow and source in self.inputs:
                    self._drop_input(source)
                    self._diagnostic(source, "输入积压，已释放该来源并等待精确名称重连")
                if source in self.inputs and connection == self._source_generations.get(source):
                    self._process_message(msg, source, connection)
            elif kind == "midi" and item[1] == self._generation:
                if self._source_live(item[2]):
                    self._process_midi(item[2])

    def _process_message(self, msg, source: str, connection: int) -> None:
        learning = self.learn_callback is not None
        for ev in self.decoder.feed(msg, source):
            ev.source, ev.source_generation = source, connection
            self._process_midi(ev, suppress_actions=learning)

    def _process_midi(self, ev: MidiEvent, suppress_actions=False) -> None:
        ev.prev = self._last_values.get(ev.key) if ev.value is not None else None
        ev.raw_prev = self._last_raw_values.get(ev.key) if ev.value is not None else None
        if ev.value is not None:
            self._last_values[ev.key] = ev.value
            if ev.raw_value is not None:
                self._last_raw_values[ev.key] = ev.raw_value
        now = time.monotonic()
        last = self._last_times.get(ev.key)
        ev.dt = None if last is None else (now - last) * 1000
        if ev.value is not None:
            self._last_times[ev.key] = now
        self.on_midi(ev)
        self.runner.release_for_event(ev)
        if self.learn_callback is not None:
            if ((self.learn_kind is None or self.learn_kind == ev.kind)
                    and (not self.learn_source or self.learn_source == ev.source)):
                cb, self.learn_callback = self.learn_callback, None
                cb(ev)
            return
        if suppress_actions or not self.cfg["settings"].get("enabled", True):
            return
        profile = select_profile(self.cfg, self.foreground, self.forced_profile)
        self._run_maps(find_matches(self.cfg, profile, ev, self.variables), ev)
        gmaps = find_matches(self.cfg, profile, ev, self.variables, gestures=True)
        if gmaps or self.gestures.has_control(ev):
            self.gestures.handle(ev, gmaps)
