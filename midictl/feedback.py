"""Bounded MIDI feedback for this application's four supported state sources."""
from collections import deque
import threading
import time

from .actions import make_midi_message


class FeedbackBindings:
    def __init__(self, cfg, output, snapshot, log):
        self.cfg, self.output, self.snapshot, self.log = cfg, output, snapshot, log
        self._due = None
        self._last = {}
        self._pending = {}
        self._dirty = False
        self._echo = deque(maxlen=256)
        self._echo_lock = threading.Lock()

    def cancel(self, clear=False):
        self._due = None
        self._pending.clear()
        self._dirty = False
        if clear:
            self._last.clear()

    def request(self, force=False):
        if force:
            self._last.clear()
        if (not self.cfg.get("feedback_bindings") or self.output.port is None
                or not self.cfg["settings"].get("enabled", True)):
            self.cancel()
            return
        self._dirty = True
        if self._due is None:
            self._due = time.monotonic() + 0.1

    @staticmethod
    def _signature(msg):
        if msg.type in ("note_on", "note_off"):
            return ("note", msg.channel, msg.note, msg.velocity if msg.type == "note_on" else 0)
        return tuple(msg.bytes())

    def is_echo(self, msg):
        signature, now = self._signature(msg), time.monotonic()
        with self._echo_lock:
            while self._echo and self._echo[0][0] < now:
                self._echo.popleft()
            return any(item[1] == signature for item in self._echo)

    def _send(self, msg):
        # Register before sending: some virtual drivers echo synchronously in send().
        with self._echo_lock:
            self._echo.append((time.monotonic() + 1.0, self._signature(msg)))
        self.output.send(msg)

    @staticmethod
    def _message(binding, state, off=False):
        kind = binding.get("state", "enabled")
        if kind == "variable":
            value = state["variables"].get(binding.get("variable", ""), 0)
        elif kind in ("enabled", "keyboard", "profile"):
            value = state[kind]
        else:
            raise ValueError("未知反馈状态")
        if binding.get("mode", "on_off") == "value" and kind in ("variable", "enabled"):
            value = int(value)
        else:
            target = binding.get("match", "")
            on = str(value) == str(target) if target != "" else bool(value)
            value = binding.get("on_value", 127) if on and not off else binding.get("off_value", 0)
        value = max(0, min(127, int(value)))
        action = dict(binding, value=value)
        if action.get("message") == "program_change":
            action["number"] = value
        return make_midi_message(action, value)

    def tick(self):
        if self._due is None or time.monotonic() < self._due:
            return
        if self.output.port is None or not self.cfg["settings"].get("enabled", True):
            self.cancel(clear=True)
            return
        if self._dirty:
            self._dirty = False
            state = self.snapshot()
            self._pending.clear()
            for binding in self.cfg.get("feedback_bindings", [])[:128]:
                if not binding.get("enabled", True):
                    continue
                try:
                    msg = self._message(binding, state)
                    key = binding["id"]
                    if self._last.get(key) != self._signature(msg):
                        self._pending[key] = msg
                except (ValueError, TypeError, KeyError) as error:
                    self.log(f"反馈 {binding.get('name', '')}: {error}")
        for key in list(self._pending)[:8]:
            msg = self._pending.pop(key)
            try:
                self._send(msg)
                self._last[key] = self._signature(msg)
            except Exception as error:
                self.log(f"反馈输出失败: {error}")
                self.cancel(clear=True)
                return
        self._due = time.monotonic() + 0.1 if self._pending else None

    def deactivate(self, enabled_only=False):
        """One final off snapshot before disabling or leaving this keyboard."""
        self.cancel(clear=True)
        if self.output.port is None:
            return
        state = self.snapshot()
        for binding in self.cfg.get("feedback_bindings", [])[:128]:
            if (not binding.get("enabled", True) or (binding.get("mode", "on_off") != "on_off" and not enabled_only)
                    or (enabled_only and binding.get("state") != "enabled")):
                continue
            try:
                self._send(self._message(binding, state, off=True))
            except Exception as error:
                self.log(f"反馈关闭失败: {error}")
                break
