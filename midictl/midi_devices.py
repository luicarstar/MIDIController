"""Cached WinRT discovery and a nonblocking adapter for the existing input lifecycle."""
import threading
import time

from . import ble_midi


class WinRTCatalog:
    def __init__(self, publish):
        self.publish = publish
        self._lock = threading.Lock()
        self._busy = False
        self._next = 0.0

    def refresh(self, force=False):
        with self._lock:
            if self._busy or (not force and time.monotonic() < self._next):
                return
            self._busy = True
            self._next = time.monotonic() + 10.0
        threading.Thread(target=self._refresh, name="midictl-winrt-catalog", daemon=True).start()

    def _refresh(self):
        try:
            self.publish(ble_midi.list_inputs(), "")
        except Exception as error:
            self.publish(None, str(error))
        finally:
            with self._lock:
                self._busy = False


class WinRTInput:
    """Mirror MidiInput's name/port/closed shape without waiting on WinRT under Engine's lock."""
    def __init__(self, on_message, on_status):
        self.name = ""
        self.state = "closed"
        self.last_error = ""
        self._on_message = on_message
        self._on_status = on_status
        self._cancelled = threading.Event()
        self._backend = None

    @property
    def port(self):
        return self if not self._cancelled.is_set() and self.state in ("opening", "open") else None

    @property
    def closed(self):
        return self.port is None

    def open(self, source):
        self.name, self.state = source, "opening"
        self._backend = ble_midi.WinRTMidiInput(self._message, self._status)
        threading.Thread(target=self._open, args=(source[len("winrt:"):],),
                         name="midictl-winrt-open", daemon=True).start()

    def _open(self, device_id):
        if self._cancelled.is_set():
            return
        try:
            self._backend.open(device_id)
        except Exception as error:
            if not self._cancelled.is_set():
                self._status("error", str(error))
        finally:
            if self._cancelled.is_set():
                self._backend.close()

    def _message(self, message):
        if not self._cancelled.is_set():
            self._on_message(message)

    def _status(self, state, detail):
        if self._cancelled.is_set():
            if state == "opening":
                self._close_backend()
            return
        self.state, self.last_error = state, detail
        self._on_status(state, detail)

    def _close_backend(self):
        if self._backend is not None:
            threading.Thread(target=self._backend.close, name="midictl-winrt-close", daemon=True).start()

    def close(self):
        self._cancelled.set()
        self.state = "closed"
        self._close_backend()
