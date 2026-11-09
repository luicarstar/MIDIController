"""Action execution. Each action is a dict with 'type'."""
from __future__ import annotations

import os
import subprocess
import threading
import time
import webbrowser

import mido

from . import rules, win_input
from .midi_io import (MidiEvent, direction, relative_delta, value_bounds,
                      event_for_trigger, button_state, trigger_matches)

ACTION_TYPES = {
    "key": "键盘按键/组合键",
    "mouse_button": "鼠标按键",
    "mouse_move": "鼠标移动",
    "mouse_wheel": "鼠标滚轮",
    "mouse_drag": "修饰键+拖动（旋钮）",
    "knob_keys": "旋钮步进按键",
    "media": "媒体键",
    "volume": "系统音量",
    "launch": "启动程序/文件",
    "url": "打开网址",
    "command": "运行命令",
    "midi_out": "发送 MIDI",
    "set_var": "设置变量",
    "repeat_key": "按住连发按键",
    "profile": "切换配置",
    "none": "无动作",
}
MEDIA_KEYS = {"playpause": "播放/暂停", "nexttrack": "下一曲", "prevtrack": "上一曲",
              "stop": "停止", "volumeup": "音量+", "volumedown": "音量-", "volumemute": "静音"}


def make_midi_message(action: dict, value: int | None = None) -> mido.Message:
    """Shared encoding for action output and application-state feedback."""
    channel = max(1, min(16, int(action.get("channel", 1)))) - 1
    number = max(0, min(127, int(action.get("number", 0))))
    value = max(0, min(127, int(action.get("value", 127) if value is None else value)))
    kind = action.get("message", "note_on")
    if kind == "note_on":
        return mido.Message(kind, channel=channel, note=number, velocity=value)
    if kind == "note_off":
        return mido.Message(kind, channel=channel, note=number, velocity=0)
    if kind == "control_change":
        return mido.Message(kind, channel=channel, control=number, value=value)
    if kind == "program_change":
        return mido.Message(kind, channel=channel, program=number)
    raise ValueError("不支持的 MIDI 输出类型")


def default_action(t: str) -> dict:
    return {
        "key": {"type": "key", "keys": "space", "mode": "tap"},
        "mouse_button": {"type": "mouse_button", "button": "left", "mode": "click"},
        "mouse_move": {"type": "mouse_move", "dx": 10, "dy": 0, "scale_by_value": False},
        "mouse_wheel": {"type": "mouse_wheel", "amount": 1, "horizontal": False, "modifiers": "",
                        "follow_direction": False, "accel": False},
        "mouse_drag": {"type": "mouse_drag", "modifiers": "alt", "button": "right", "dx": 4, "dy": 0,
                       "idle_ms": 400, "accel": True},
        "knob_keys": {"type": "knob_keys", "keys_inc": "]", "keys_dec": "[", "accel": True},
        "media": {"type": "media", "key": "playpause"},
        "volume": {"type": "volume", "mode": "set_from_value", "step": 5},
        "launch": {"type": "launch", "path": "", "args": ""},
        "url": {"type": "url", "url": "https://"},
        "command": {"type": "command", "command": ""},
        "midi_out": {"type": "midi_out", "message": "note_on", "channel": 1, "number": 60,
                     "value": 127, "pass_value": True},
        "profile": {"type": "profile", "name": ""},
        "set_var": {"type": "set_var", "name": "shift", "op": "set", "value": 1,
                    "min": "", "max": "", "wrap": False},
        "repeat_key": {"type": "repeat_key", "keys": "w", "interval_ms": 100},
        "none": {"type": "none"},
    }[t]


def describe_action(a: dict) -> str:
    t = a.get("type")
    if t == "key":
        mode = {"tap": "点按", "hold": "跟随按住", "down": "仅按下", "up": "仅松开"}.get(a.get("mode"), "")
        return f"按键 {a.get('keys')} ({mode})"
    if t == "mouse_button":
        return f"鼠标 {a.get('button')} {a.get('mode')}"
    if t == "mouse_move":
        return f"鼠标移动 ({a.get('dx')},{a.get('dy')})"
    if t == "mouse_wheel":
        mod = f"{a['modifiers']}+" if a.get("modifiers") else ""
        extra = (" 水平" if a.get("horizontal") else "") + (" 随方向" if a.get("follow_direction") else "") \
            + (" 加速" if a.get("accel") else "")
        return f"{mod}滚轮 {a.get('amount')}{extra}"
    if t == "mouse_drag":
        mod = f"{a['modifiers']}+" if a.get("modifiers") else ""
        btn = {"left": "左键", "right": "右键", "middle": "中键", "none": ""}.get(a.get("button"), "")
        return f"{mod}{btn}拖动 ({a.get('dx')},{a.get('dy')})/格"
    if t == "knob_keys":
        return f"右转 {a.get('keys_inc')} / 左转 {a.get('keys_dec')}" + (" 加速" if a.get("accel") else "")
    if t == "media":
        return f"媒体 {MEDIA_KEYS.get(a.get('key'), a.get('key'))}"
    if t == "volume":
        return "音量跟随数值" if a.get("mode") == "set_from_value" else f"音量 {a.get('mode')} {a.get('step')}%"
    if t == "launch":
        return f"启动 {a.get('path')}"
    if t == "url":
        return f"网址 {a.get('url')}"
    if t == "command":
        return f"命令 {a.get('command')}"
    if t == "midi_out":
        return f"MIDI {a.get('message')} ch{a.get('channel')} #{a.get('number')}"
    if t == "profile":
        return f"切换到配置 {a.get('name')}"
    if t == "set_var":
        op = {"set": "=", "add": "+=", "toggle": "切换", "from_value": "= 输入值"}.get(a.get("op"), a.get("op"))
        val = "" if a.get("op") in ("toggle", "from_value") else f" {a.get('value')}"
        return f"变量 {a.get('name')} {op}{val}"
    if t == "repeat_key":
        return f"按住连发 {a.get('keys')} / {a.get('interval_ms')}ms"
    return "无"


def describe_actions(lst: list[dict]) -> str:
    parts = []
    for a in lst:
        d = describe_action(a)
        if int(a.get("delay_ms", 0) or 0):
            d = f"[+{a['delay_ms']}ms] " + d
        parts.append(d)
    return "  →  ".join(parts) or "无"


class Volume:
    """Master volume via pycaw; lazily initialized on the calling thread."""

    def __init__(self):
        self._ep = None

    def _endpoint(self):
        if self._ep is None:
            import comtypes
            comtypes.CoInitialize()
            from pycaw.pycaw import AudioUtilities
            dev = AudioUtilities.GetSpeakers()
            self._ep = dev.EndpointVolume
        return self._ep

    def set(self, level: float) -> None:
        self._endpoint().SetMasterVolumeLevelScalar(max(0.0, min(1.0, level)), None)

    def get(self) -> float:
        return self._endpoint().GetMasterVolumeLevelScalar()

    def toggle_mute(self) -> None:
        ep = self._endpoint()
        ep.SetMute(0 if ep.GetMute() else 1, None)


def _norm_value(ev: MidiEvent) -> float:
    bounds = value_bounds(ev)
    if ev.value is None or bounds is None:
        raise ValueError("增减消息没有绝对数值，不能用于数值跟随")
    lo, hi = bounds
    # Preserve the old pitch normalization exactly, including its +64 endpoint.
    span = 128 if ev.kind == "pitchwheel" and ev.value_mode != "full" else hi - lo
    return max(0.0, min(1.0, (ev.value - lo) / span))


def knob_step(a: dict, ev: MidiEvent, m: dict, follow: bool = False) -> int:
    """Signed step count for knob-driven actions: direction x acceleration."""
    d = 1
    if follow or a.get("follow_direction"):
        trigger = m.get("trigger", {})
        raw_steps = (ev.value is None or (trigger.get("relative_steps", False) and
                     trigger.get("encoding") in ("rel_2c", "rel_64")) or trigger.get("encoding") in
                     ("rel_signed", "rel_offset", "rel_arturia3"))
        if raw_steps:
            return relative_delta(trigger, ev)  # Encoded steps already carry speed.
        d = direction(trigger, ev)
        if d == 0:
            return 0
    mult = 1
    if a.get("accel"):
        dt = ev.dt if ev.dt is not None else 1000
        mult = 4 if dt < 20 else 2 if dt < 50 else 1
        enc = m.get("trigger", {}).get("encoding", "absolute")
        if ev.prev is not None and enc == "absolute":
            mult = max(mult, min(8, abs(ev.value - ev.prev)))
    return d * mult


class ActionRunner:
    """Executes action lists. Delayed lists run on a helper thread."""

    def __init__(self, midi_out=None, on_profile=None, log=print, variables=None):
        self.midi_out = midi_out
        self.on_profile = on_profile
        self.log = log
        self.variables = variables if variables is not None else {}
        self.volume = Volume()
        self.held: dict[tuple, list[int]] = {}
        self.repeaters: dict[tuple, threading.Event] = {}
        self.drags: dict[tuple, dict] = {}
        self._lock = threading.RLock()
        self._cancel = threading.Event()
        self.epoch = 0
        self._source_cancel: dict[str, threading.Event] = {}
        self._key_owners: dict[int, set[tuple]] = {}
        self._button_owners: dict[str, set[tuple]] = {}
        self._explicit_keys: dict[tuple, set[int]] = {}
        self._explicit_buttons: dict[tuple, set[str]] = {}
        self.mouse_held: dict[tuple, set[str]] = {}
        self._hold_sources: dict[tuple, tuple] = {}
        self._hold_triggers: dict[tuple, dict] = {}
        self._hold_mappings: dict[tuple, dict] = {}
        self._hold_events: dict[tuple, MidiEvent] = {}
        self.on_profile_event = None
        self.on_state_change = None

    @property
    def down_keys(self) -> set[int]:
        return set(self._key_owners)

    @property
    def down_buttons(self) -> set[str]:
        return set(self._button_owners)

    @staticmethod
    def _owner(m: dict, ev: MidiEvent, role: str) -> tuple:
        return (m["id"], ev.key, m.get("_action_index", 0), role)

    def _acquire_keys(self, owner: tuple, vks) -> None:
        fresh = [vk for vk in dict.fromkeys(vks) if vk not in self._key_owners]
        if fresh:
            win_input.key_down(fresh)
        for vk in vks:
            self._key_owners.setdefault(vk, set()).add(owner)

    def _release_keys(self, owner: tuple, vks=None) -> None:
        released = []
        for vk in list(self._key_owners) if vks is None else dict.fromkeys(vks):
            owners = self._key_owners.get(vk)
            if owners is not None and owner in owners:
                owners.remove(owner)
                if not owners:
                    del self._key_owners[vk]
                    released.append(vk)
        if released:
            win_input.key_up(released)

    def _acquire_button(self, owner: tuple, button: str) -> None:
        if button not in self._button_owners:
            win_input.mouse_button(button, "down")
        self._button_owners.setdefault(button, set()).add(owner)

    def _release_button(self, owner: tuple, button: str) -> None:
        owners = self._button_owners.get(button)
        if owners is not None and owner in owners:
            owners.remove(owner)
            if not owners:
                del self._button_owners[button]
                win_input.mouse_button(button, "up")

    def _remember_hold(self, owner, ev, mapping):
        self._hold_sources[owner] = ev.key
        self._hold_triggers[owner] = dict(mapping.get("trigger", {}))
        self._hold_mappings[owner] = mapping
        self._hold_events[owner] = ev

    def _release_hold(self, owner):
        self.held.pop(owner, None)
        self._release_keys(owner)
        for button in self.mouse_held.pop(owner, set()):
            self._release_button(owner, button)
        stop = self.repeaters.pop(owner, None)
        if stop:
            stop.set()
        self._hold_sources.pop(owner, None)
        self._hold_triggers.pop(owner, None)
        self._hold_mappings.pop(owner, None)
        self._hold_events.pop(owner, None)

    def run(self, mapping: dict, ev: MidiEvent) -> None:
        lst = mapping.get("actions") or ([mapping["action"]] if "action" in mapping else [])
        with self._lock:
            cancel = self._cancel
            source_cancel = self._source_cancel.setdefault(ev.source, threading.Event())
        if any(int(a.get("delay_ms", 0) or 0) > 0 for a in lst):
            threading.Thread(target=self._run_list, args=(lst, ev, mapping, cancel, source_cancel), daemon=True).start()
        else:
            self._run_list(lst, ev, mapping, cancel, source_cancel)

    def _run_list(self, lst, ev, mapping, cancel, source_cancel):
        for index, a in enumerate(lst):
            d = int(a.get("delay_ms", 0) or 0)
            if source_cancel.wait(max(0, d) / 1000) or cancel.is_set():
                return
            try:
                with self._lock:
                    if cancel.is_set() or source_cancel.is_set():
                        return
                    self.run_one(a, ev, {**mapping, "_action_index": index})
            except Exception as e:
                self.log(f"[{mapping.get('name')}] 动作失败: {e}")

    def run_one(self, a: dict, ev: MidiEvent, mapping: dict) -> None:
        t = a.get("type")
        if ev.value is None and not (t in ("knob_keys", "mouse_drag") or
                (t == "mouse_wheel" and a.get("follow_direction"))):
            return  # Delta-only input can drive direction/step actions, not absolute actions.
        handler = getattr(self, f"_do_{t}", None)
        if handler is None:
            return
        handler(a, ev, mapping)

    def release_all(self) -> None:
        with self._lock:
            self._cancel.set()
            self._cancel = threading.Event()
            self.epoch += 1
            for token in self._source_cancel.values():
                token.set()
            self._source_cancel.clear()
            for stop in self.repeaters.values():
                stop.set()
            self.repeaters.clear()
            for key in list(self.drags):
                self._end_drag(key)
            keys = sorted(self._key_owners)
            if keys:
                win_input.key_up(keys)
            self.held.clear()
            self._key_owners.clear()
            for button in sorted(self._button_owners):
                win_input.mouse_button(button, "up")
            self._button_owners.clear()
            self.mouse_held.clear()
            self._hold_sources.clear()
            self._hold_triggers.clear()
            self._hold_mappings.clear()
            self._hold_events.clear()
            self._explicit_keys.clear()
            self._explicit_buttons.clear()

    def release_source(self, source: str) -> None:
        """Cancel one input's macros and release only that input's owners."""
        with self._lock:
            token = self._source_cancel.pop(source, None)
            if token:
                token.set()
            for owner in [o for o in self._hold_sources if o[1][0] == source]:
                self._release_hold(owner)
            for owner in [o for o in self.drags if o[1][0] == source]:
                self._end_drag(owner)
            for owner in [o for o in self._explicit_keys if o[1][0] == source]:
                self._release_keys(owner)
                self._explicit_keys.pop(owner, None)
            for owner in [o for o in self._explicit_buttons if o[1][0] == source]:
                for button in self._explicit_buttons.pop(owner):
                    self._release_button(owner, button)

    def release_for_event(self, ev: MidiEvent) -> None:
        """Release the resources a physical control owns, even if conditions changed."""
        with self._lock:
            for key, source in list(self._hold_sources.items()):
                if source != ev.key:
                    continue
                trigger = self._hold_triggers[key]
                view = event_for_trigger(trigger, ev)
                self._hold_events[key] = view
                mapping = self._hold_mappings[key]
                if (button_state(trigger, view) is False or
                        not trigger_matches(trigger, view, gesture_ok=True) or
                        not rules.check_conditions(mapping.get("conditions", ""), self.variables, view)):
                    self._release_hold(key)

    def reconcile_holds(self) -> None:
        with self._lock:
            for owner, mapping in list(self._hold_mappings.items()):
                if not rules.check_conditions(mapping.get("conditions", ""), self.variables,
                                              self._hold_events[owner]):
                    self._release_hold(owner)

    @staticmethod
    def _is_release(ev: MidiEvent, mapping: dict | None = None) -> bool:
        return button_state((mapping or {}).get("trigger", {}), ev) is False

    # --- handlers ---
    def _do_none(self, a, ev, m):
        pass

    def _do_set_var(self, a, ev, m):
        previous = dict(self.variables)
        new = rules.apply_set_var(a, self.variables, ev)
        self.log(f"变量 {a.get('name')} = {new}")
        if self.variables != previous and self.on_state_change:
            self.on_state_change()

    def _do_repeat_key(self, a, ev, m):
        key = self._owner(m, ev, "repeat")
        if self._is_release(ev, m):
            self._release_hold(key)
            return
        if key in self.repeaters:
            return
        vks = win_input.parse_combo(a.get("keys", ""))
        interval = max(10, int(a.get("interval_ms", 100))) / 1000
        stop = threading.Event()
        self.repeaters[key] = stop
        self._remember_hold(key, ev, m)

        def loop():
            while not stop.is_set():
                with self._lock:
                    if stop.is_set():
                        return
                    win_input.key_tap(vks)
                stop.wait(interval)
        threading.Thread(target=loop, daemon=True).start()

    def _do_key(self, a, ev, m):
        vks = win_input.parse_combo(a.get("keys", ""))
        mode = a.get("mode", "tap")
        owner = self._owner(m, ev, "hold" if mode == "hold" else "down")
        if mode == "hold":
            if self._is_release(ev, m):
                self._release_hold(owner)
            elif owner not in self.held:
                self.held[owner] = vks
                self._remember_hold(owner, ev, m)
                self._acquire_keys(owner, vks)
        elif mode == "down":
            self._acquire_keys(owner, vks)
            self._explicit_keys.setdefault(owner, set()).update(vks)
        elif mode == "up":
            owned_before = self.down_keys
            for key, keys in list(self._explicit_keys.items()):
                if key[1][:1] == ev.key[:1] and key[1][2] == ev.channel:
                    self._release_keys(key, vks)
                    keys.difference_update(vks)
                    if not keys:
                        self._explicit_keys.pop(key, None)
            unowned = [vk for vk in vks if vk not in owned_before]
            if unowned:
                win_input.key_up(unowned)
        else:
            win_input.key_tap(vks)

    def _do_mouse_button(self, a, ev, m):
        mode = a.get("mode", "click")
        button = a.get("button", "left")
        owner = self._owner(m, ev, "mouse_hold" if mode == "hold" else "mouse_down")
        if mode == "hold":
            if self._is_release(ev, m):
                self._release_hold(owner)
            else:
                self.mouse_held.setdefault(owner, set()).add(button)
                self._remember_hold(owner, ev, m)
                self._acquire_button(owner, button)
        elif mode == "down":
            self._acquire_button(owner, button)
            self._explicit_buttons.setdefault(owner, set()).add(button)
        elif mode == "up":
            owned_before = button in self._button_owners
            for key, buttons in list(self._explicit_buttons.items()):
                if key[1][0] == ev.source and key[1][2] == ev.channel and button in buttons:
                    self._release_button(key, button)
                    buttons.discard(button)
                    if not buttons:
                        self._explicit_buttons.pop(key, None)
            if not owned_before:
                win_input.mouse_button(button, "up")
        else:
            win_input.mouse_button(button, mode)

    def _do_mouse_move(self, a, ev, m):
        dx, dy = float(a.get("dx", 0)), float(a.get("dy", 0))
        if a.get("scale_by_value"):
            # Knob centered at 64: below moves negative, above positive
            f = (_norm_value(ev) - 0.5) * 2
            dx, dy = dx * f, dy * f
        win_input.mouse_move(round(dx), round(dy))

    def _do_mouse_wheel(self, a, ev, m):
        s = knob_step(a, ev, m)
        if s == 0:
            return
        mods = win_input.parse_combo(a["modifiers"]) if a.get("modifiers") else []
        owner = self._owner(m, ev, "wheel_modifiers")
        if mods:
            self._acquire_keys(owner, mods)
        try:
            win_input.mouse_wheel(int(a.get("amount", 1)) * s, bool(a.get("horizontal")))
        finally:
            if mods:
                self._release_keys(owner)

    def _do_mouse_drag(self, a, ev, m):
        s = knob_step(a, ev, m, follow=True)
        if s == 0:
            return
        key = self._owner(m, ev, "drag")
        st = self.drags.get(key)
        if st is None:
            mods = win_input.parse_combo(a["modifiers"]) if a.get("modifiers") else []
            button = a.get("button", "left")
            if mods:
                self._acquire_keys(key, mods)
            if button != "none":
                self._acquire_button(key, button)
            st = self.drags[key] = {"mods": mods, "button": button, "timer": None, "revision": 0}
        win_input.mouse_move(round(float(a.get("dx", 0)) * s), round(float(a.get("dy", 0)) * s))
        if st["timer"]:
            st["timer"].cancel()
        st["revision"] += 1
        st["timer"] = threading.Timer(max(50, int(a.get("idle_ms", 400))) / 1000,
                                      self._end_drag, (key, st, st["revision"]))
        st["timer"].daemon = True
        st["timer"].start()

    def _end_drag(self, key, expected=None, revision=None):
        with self._lock:
            st = self.drags.get(key)
            if st is None or (expected is not None and (st is not expected or st["revision"] != revision)):
                return
            self.drags.pop(key)
            if st["timer"]:
                st["timer"].cancel()
            if st["button"] != "none":
                self._release_button(key, st["button"])
            if st["mods"]:
                self._release_keys(key)

    def _do_knob_keys(self, a, ev, m):
        s = knob_step(a, ev, m, follow=True)
        keys = a.get("keys_inc") if s > 0 else a.get("keys_dec")
        if s == 0 or not keys:
            return
        vks = win_input.parse_combo(keys)
        for _ in range(abs(s)):
            win_input.key_tap(vks)

    def _do_media(self, a, ev, m):
        win_input.key_tap([win_input.VK[a.get("key", "playpause")]])

    def _do_volume(self, a, ev, m):
        mode = a.get("mode", "set_from_value")
        step = float(a.get("step", 5)) / 100
        if mode == "set_from_value":
            self.volume.set(_norm_value(ev))
        elif mode == "up":
            self.volume.set(self.volume.get() + step)
        elif mode == "down":
            self.volume.set(self.volume.get() - step)
        elif mode == "mute":
            self.volume.toggle_mute()

    def _do_launch(self, a, ev, m):
        path = os.path.expandvars(a.get("path", "")).strip()
        if not path:
            return
        args = a.get("args", "").strip()
        if args:
            subprocess.Popen(f'"{path}" {args}', shell=False)
        else:
            os.startfile(path)

    def _do_url(self, a, ev, m):
        if a.get("url"):
            webbrowser.open(a["url"])

    def _do_command(self, a, ev, m):
        if a.get("command"):
            subprocess.Popen(a["command"], shell=True, creationflags=subprocess.CREATE_NO_WINDOW)

    def _do_midi_out(self, a, ev, m):
        if self.midi_out is None or self.midi_out.port is None:
            self.log("MIDI 输出端口未打开")
            return
        val = int(a.get("value", 127))
        if a.get("pass_value"):
            if ev.value is None:
                return
            if ev.kind in ("cc14", "rpn", "nrpn") or (ev.kind == "pitchwheel" and ev.value_mode == "full"):
                val = round(_norm_value(ev) * 127)
            elif ev.kind != "pitchwheel":
                val = ev.value
        val = max(0, min(127, val))
        self.midi_out.send(make_midi_message(a, val))

    def _do_profile(self, a, ev, m):
        if a.get("name"):
            if self.on_profile_event:
                self.on_profile_event(a["name"], ev)
            elif self.on_profile:
                self.on_profile(a["name"])
