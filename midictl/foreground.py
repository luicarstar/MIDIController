"""Detect foreground process executable name."""
from __future__ import annotations

import ctypes
from ctypes import wintypes

import psutil

user32 = ctypes.WinDLL("user32")
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))


def foreground_exe() -> str:
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    try:
        return psutil.Process(pid.value).name().lower()
    except (psutil.Error, ValueError):
        return ""
