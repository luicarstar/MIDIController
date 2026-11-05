"""Action execution. Each action is a dict with 'type'."""
from __future__ import annotations

import os
import subprocess
import threading
import time
import webbrowser

import mido

from . import rules, win_input
from .midi_io import MidiEvent, direction

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
    if ev.kind == "pitchwheel":
        return (ev.value + 64) / 128
    return ev.value / 127


def knob_step(a: dict, ev: MidiEvent, m: dict, follow: bool = False) -> int:
    """Signed step count for knob-driven actions: direction x acceleration."""
    d = 1
    if follow or a.get("follow_direction"):
        d = direction(m.get("trigger", {}), ev)
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
        self.held: dict[str, list[int]] = {}  # mapping id -> held vks
        self.repeaters: dict[str, threading.Event] = {}
        self.drags: dict[str, dict] = {}
        self._lock = threading.RLock()

    def run(self, mapping: dict, ev: MidiEvent) -> None:
        lst = mapping.get("actions") or ([mapping["action"]] if "action" in mapping else [])
        if any(int(a.get("delay_ms", 0) or 0) > 0 for a in lst):
            threading.Thread(target=self._run_list, args=(lst, ev, mapping), daemon=True).start()
        else:
            self._run_list(lst, ev, mapping)

    def _run_list(self, lst, ev, mapping):
        for a in lst:
            d = int(a.get("delay_ms", 0) or 0)
            if d > 0:
                time.sleep(d / 1000)
            try:
                with self._lock:
                    self.run_one(a, ev, mapping)
            except Exception as e:
                self.log(f"[{mapping.get('name')}] 动作失败: {e}")

    def run_one(self, a: dict, ev: MidiEvent, mapping: dict) -> None:
        t = a.get("type")
        handler = getattr(self, f"_do_{t}", None)
        if handler is None:
            return
        handler(a, ev, mapping)

    def release_all(self) -> None:
        with self._lock:
            for vks in self.held.values():
                win_input.key_up(vks)
            self.held.clear()
            for stop in self.repeaters.values():
                stop.set()
            self.repeaters.clear()
            for key in list(self.drags):
                self._end_drag(key)

    @staticmethod
    def _is_release(ev: MidiEvent) -> bool:
        return ev.event == "release" or (ev.kind == "cc" and ev.value < 64)

    # --- handlers ---
    def _do_none(self, a, ev, m):
        pass

    def _do_set_var(self, a, ev, m):
        new = rules.apply_set_var(a, self.variables, ev)
        self.log(f"变量 {a.get('name')} = {new}")

    def _do_repeat_key(self, a, ev, m):
        key = m["id"]
        if self._is_release(ev):
            stop = self.repeaters.pop(key, None)
            if stop:
                stop.set()
            return
        if key in self.repeaters:
            return
        vks = win_input.parse_combo(a.get("keys", ""))
        interval = max(10, int(a.get("interval_ms", 100))) / 1000
        stop = threading.Event()
        self.repeaters[key] = stop

        def loop():
            while not stop.is_set():
                win_input.key_tap(vks)
                stop.wait(interval)
        threading.Thread(target=loop, daemon=True).start()

    def _do_key(self, a, ev, m):
        vks = win_input.parse_combo(a.get("keys", ""))
        mode = a.get("mode", "tap")
        if mode == "hold":
            if self._is_release(ev):
                held = self.held.pop(m["id"], None)
                if held:
                    win_input.key_up(held)
            elif m["id"] not in self.held:
                self.held[m["id"]] = vks
                win_input.key_down(vks)
        elif mode == "down":
            win_input.key_down(vks)
        elif mode == "up":
            win_input.key_up(vks)
        else:
            win_input.key_tap(vks)

    def _do_mouse_button(self, a, ev, m):
        mode = a.get("mode", "click")
        if mode == "hold":
            pressed = ev.event == "press" or (ev.kind == "cc" and ev.value >= 64)
            mode = "down" if pressed else "up"
        win_input.mouse_button(a.get("button", "left"), mode)

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
        if mods:
            win_input.key_down(mods)
        try:
            win_input.mouse_wheel(int(a.get("amount", 1)) * s, bool(a.get("horizontal")))
        finally:
            if mods:
                win_input.key_up(mods)

    def _do_mouse_drag(self, a, ev, m):
        s = knob_step(a, ev, m, follow=True)
        if s == 0:
            return
        key = m["id"]
        st = self.drags.get(key)
        if st is None:
            mods = win_input.parse_combo(a["modifiers"]) if a.get("modifiers") else []
            button = a.get("button", "left")
            if mods:
                win_input.key_down(mods)
            if button != "none":
                win_input.mouse_button(button, "down")
            st = self.drags[key] = {"mods": mods, "button": button, "timer": None}
        win_input.mouse_move(round(float(a.get("dx", 0)) * s), round(float(a.get("dy", 0)) * s))
        if st["timer"]:
            st["timer"].cancel()
        st["timer"] = threading.Timer(max(50, int(a.get("idle_ms", 400))) / 1000, self._end_drag, (key,))
        st["timer"].daemon = True
        st["timer"].start()

    def _end_drag(self, key):
        with self._lock:
            st = self.drags.pop(key, None)
            if not st:
                return
            if st["timer"]:
                st["timer"].cancel()
            if st["button"] != "none":
                win_input.mouse_button(st["button"], "up")
            if st["mods"]:
                win_input.key_up(st["mods"])

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
        ch = max(1, min(16, int(a.get("channel", 1)))) - 1
        num = int(a.get("number", 0))
        val = int(a.get("value", 127))
        if a.get("pass_value") and ev.kind != "pitchwheel":
            val = ev.value
        kind = a.get("message", "note_on")
        if kind == "note_on":
            msg = mido.Message("note_on", channel=ch, note=num, velocity=val)
        elif kind == "note_off":
            msg = mido.Message("note_off", channel=ch, note=num, velocity=0)
        elif kind == "control_change":
            msg = mido.Message("control_change", channel=ch, control=num, value=val)
        else:
            msg = mido.Message("program_change", channel=ch, program=num)
        self.midi_out.send(msg)

    def _do_profile(self, a, ev, m):
        if self.on_profile and a.get("name"):
            self.on_profile(a["name"])
