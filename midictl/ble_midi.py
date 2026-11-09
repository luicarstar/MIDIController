"""Windows WinRT MIDI 1.0 input, including system-paired Bluetooth MIDI.

Endpoints are selected by MidiInPort.GetDeviceSelector, not by a guessed BLE
name/ID filter. Label this backend "WinRT MIDI (including system Bluetooth)".
Pair devices in Windows Settings. This module never pairs or uses raw GATT.
Callbacks run off the UI thread; bridge them to a Qt signal at integration.
"""
from __future__ import annotations

import queue
import sys
import threading
from concurrent.futures import Future
from contextlib import ExitStack, suppress
from dataclasses import dataclass, field
from typing import Callable

import mido

BACKEND_LABEL = "WinRT MIDI（含系统蓝牙）"
_api = None
_api_lock = threading.Lock()


class WinRTMidiError(RuntimeError):
    """Missing runtime, unavailable endpoint, canceled open, or native error."""


def _load_api():
    global _api
    with _api_lock:
        if _api is None:
            if sys.platform != "win32":
                raise WinRTMidiError("WinRT MIDI requires Windows 10 or later")
            try:
                from winrt.runtime import ApartmentType, init_apartment, uninit_apartment
                from winrt.windows.devices.enumeration import DeviceInformation
                from winrt.windows.devices.midi import MidiInPort
                from winrt.windows.storage.streams import DataReader
            except (ImportError, OSError) as exc:
                raise WinRTMidiError(
                    "WinRT MIDI runtime unavailable; install requirements-ble.txt: "
                    + str(exc)
                ) from exc
            _api = (ApartmentType, init_apartment, uninit_apartment,
                    DeviceInformation, MidiInPort, DataReader)
        return _api


def _dispose(value):
    if value is not None and hasattr(value, "close"):
        with suppress(Exception):
            value.close()


def _resolve(operation, wake=None, stopped=None, timeout=15.0):
    """Wait once using Completed; reclaim a port arriving after cancellation."""
    wake = wake if wake is not None else threading.Event()
    gate = threading.Lock()
    result = {}
    abandoned = False

    def completed(op, status):
        nonlocal abandoned
        value = None
        try:
            value = op.get_results()
            error = None
        except Exception as exc:
            error = exc
        with gate:
            if not abandoned:
                result.update(value=value, error=error)
                value = None
                wake.set()
        _dispose(value)

    operation.completed = completed
    try:
        signaled = wake.wait(timeout)
        with gate:
            if stopped is not None and stopped.is_set():
                raise WinRTMidiError("WinRT MIDI open canceled")
            if not signaled or not result:
                raise WinRTMidiError("WinRT MIDI operation timed out")
            if result["error"] is not None:
                raise result["error"]
            return result.pop("value")
    finally:
        with gate:
            abandoned = True
            leftover = result.pop("value", None)
        _dispose(leftover)
        if not result or (stopped is not None and stopped.is_set()):
            with suppress(Exception):
                operation.cancel()


def list_inputs() -> list[tuple[str, str]]:
    """Return enabled WinRT MIDI endpoints, including BLE exposed by Windows.

    IDs are opaque DeviceInformation.id values, stable across list refreshes
    while the Windows device registration exists. Re-pairing can change them.
    This synchronous call uses an MTA worker and raises WinRTMidiError on failure.
    It does not open ports, pair devices, or scan Bluetooth advertisements.
    """
    outcome = Future()

    def enumerate_inputs():
        uninit = None
        try:
            apartment, init, uninit_fn, info, midi, _ = _load_api()
            init(apartment.MULTI_THREADED)
            uninit = uninit_fn
            devices = _resolve(info.find_all_async(midi.get_device_selector(), []))
            unique = {d.id: d.name or d.id for d in devices if d.is_enabled}
            outcome.set_result(sorted(unique.items(), key=lambda item: (item[1].casefold(), item[0])))
        except Exception as exc:
            outcome.set_exception(WinRTMidiError(str(exc)))
        finally:
            if uninit is not None:
                uninit()

    threading.Thread(target=enumerate_inputs, name="WinRT MIDI list", daemon=True).start()
    return outcome.result()


@dataclass
class _Session:
    device_id: str
    wake: threading.Event = field(default_factory=threading.Event)
    stopped: threading.Event = field(default_factory=threading.Event)
    opened: Future = field(default_factory=Future)
    packets: queue.Queue = field(default_factory=lambda: queue.Queue(maxsize=2048))
    thread: threading.Thread | None = None
    failure: tuple[str, str] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def fail(self, state, detail):
        with self.lock:
            if self.failure is None:
                self.failure = (state, detail)
        self.wake.set()


class WinRTMidiInput:
    """One explicitly selected endpoint; never opens a matching WinMM port.

    on_message(mido.Message) receives MIDI 1.0 messages. Optional
    on_status(state, detail) receives opening/open/closed/disconnected/error.
    open(id) blocks until ready or raises; call it off the Qt UI thread.
    close() cancels a concurrent open and is idempotent. A callback already
    executing can finish after close. No automatic pairing or reconnect occurs.
    """

    def __init__(self, on_message: Callable[[mido.Message], None],
                 on_status: Callable[[str, str], None] | None = None):
        self.on_message = on_message
        self.on_status = on_status
        self._lock = threading.RLock()
        self._session: _Session | None = None
        self._state = "closed"
        self._last_error = ""

    @property
    def is_open(self) -> bool:
        with self._lock:
            return (self._state == "open" and self._session is not None
                    and not self._session.stopped.is_set())

    @property
    def is_closed(self) -> bool:
        return not self.is_open

    @property
    def last_error(self) -> str:
        with self._lock:
            return self._last_error

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def device_id(self) -> str:
        with self._lock:
            return self._session.device_id if self._session else ""

    def _status(self, session, state, detail=""):
        with self._lock:
            if self._session is not session:
                return
            if session.stopped.is_set() and state in ("opening", "open"):
                return
            self._state = state
            if state in ("error", "disconnected"):
                self._last_error = detail
            callback = self.on_status
        if callback is not None:
            # Consumer errors must not strand a native port or its worker.
            with suppress(Exception):
                callback(state, detail)

    def open(self, device_id: str) -> None:
        if not isinstance(device_id, str) or not device_id:
            raise ValueError("Pass the exact nonempty ID returned by list_inputs()")
        # Serialize replacement without holding a lock across callbacks/joins.
        with self._lock:
            old = self._session
            if old is not None:
                old.stopped.set()
                old.wake.set()
            session = _Session(device_id)
            self._session = session
            self._state = "opening"
            self._last_error = ""
            session.thread = threading.Thread(target=self._run, args=(session, old),
                                              name="WinRT MIDI input", daemon=True)
            session.thread.start()
        session.opened.result()

    def close(self) -> None:
        with self._lock:
            session = self._session
            if session is None or session.stopped.is_set():
                return
            session.stopped.set()
            session.wake.set()
            self._state = "closed"
        if session.thread is not threading.current_thread():
            session.thread.join(timeout=2.0)

    def _run(self, session, previous):
        uninit = None
        terminal = ("closed", "")
        try:
            self._status(session, "opening")
            if previous is not None and previous.thread is not None:
                previous.thread.join(timeout=2.0)
                if previous.thread.is_alive():
                    raise WinRTMidiError("Previous WinRT input is still closing; retry after its callback returns")
            if session.stopped.is_set():
                raise WinRTMidiError("WinRT MIDI open canceled")
            apartment, init, uninit_fn, info, midi, reader_type = _load_api()
            init(apartment.MULTI_THREADED)
            uninit = uninit_fn
            with ExitStack() as cleanup:
                port = _resolve(midi.from_id_async(session.device_id),
                                session.wake, session.stopped)
                if port is None:
                    raise WinRTMidiError("Windows could not open the MIDI endpoint; check pairing, permissions and availability")
                cleanup.callback(_dispose, port)

                def received(sender, args):
                    if session.stopped.is_set() or session.failure is not None:
                        return
                    try:
                        with reader_type.from_buffer(args.message.raw_data) as reader:
                            data = bytearray(reader.unconsumed_buffer_length)
                            reader.read_bytes(data)
                        session.packets.put_nowait((data, args.message.timestamp.total_seconds()))
                        session.wake.set()
                    except Exception as exc:
                        session.fail("error", "WinRT MIDI receive failed: " + str(exc))

                def removed(sender, update):
                    if update.id == session.device_id:
                        session.fail("disconnected", "Windows removed the MIDI endpoint")

                def updated(sender, update):
                    if update.id == session.device_id:
                        props = update.properties
                        for key in ("System.Devices.InterfaceEnabled", "System.Devices.Aep.IsConnected"):
                            if key in props and props[key] is False:
                                session.fail("disconnected", "Windows disabled or disconnected the MIDI endpoint")

                def added(sender, device):
                    if device.id == session.device_id and not device.is_enabled:
                        session.fail("disconnected", "Windows disabled the MIDI endpoint")

                def watcher_stopped(sender, args):
                    if not session.stopped.is_set():
                        session.fail("error", "Windows stopped the MIDI endpoint watcher")

                watcher = info.create_watcher(midi.get_device_selector(),
                                             ["System.Devices.InterfaceEnabled", "System.Devices.Aep.IsConnected"])
                for event, handler in (("added", added), ("removed", removed),
                                       ("updated", updated), ("stopped", watcher_stopped)):
                    token = getattr(watcher, "add_" + event)(handler)
                    cleanup.callback(self._unsubscribe, watcher, event, token)
                watcher.start()
                cleanup.callback(self._stop_watcher, watcher)
                token = port.add_message_received(received)
                cleanup.callback(self._unsubscribe, port, "message_received", token)
                parser = mido.Parser()
                if session.stopped.is_set():
                    raise WinRTMidiError("WinRT MIDI open canceled")
                self._status(session, "open")
                if session.stopped.is_set():
                    raise WinRTMidiError("WinRT MIDI open canceled")
                session.opened.set_result(None)
                while not session.stopped.is_set():
                    session.wake.wait()
                    session.wake.clear()
                    if session.failure is not None:
                        terminal = session.failure
                        break
                    while not session.stopped.is_set() and session.failure is None:
                        try:
                            data, timestamp = session.packets.get_nowait()
                        except queue.Empty:
                            break
                        parser.feed(data)
                        for message in parser:
                            if session.stopped.is_set() or session.failure is not None:
                                break
                            message.time = timestamp
                            self.on_message(message)
        except Exception as exc:
            if not session.stopped.is_set():
                terminal = ("error", str(exc))
        finally:
            session.stopped.set()
            if uninit is not None:
                with suppress(Exception):
                    uninit()
            self._status(session, *terminal)
            if not session.opened.done():
                session.opened.set_exception(WinRTMidiError(terminal[1] or "WinRT MIDI open canceled"))

    @staticmethod
    def _unsubscribe(obj, event, token):
        with suppress(Exception):
            getattr(obj, "remove_" + event)(token)

    @staticmethod
    def _stop_watcher(watcher):
        with suppress(Exception):
            watcher.stop()
