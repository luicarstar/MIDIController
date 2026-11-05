"""Windows SendInput wrapper: keyboard (scan codes) and mouse."""
from __future__ import annotations

import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)

INPUT_MOUSE, INPUT_KEYBOARD = 0, 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
MAPVK_VK_TO_VSC = 0

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP = 0x0002, 0x0004
MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP = 0x0008, 0x0010
MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP = 0x0020, 0x0040
MOUSEEVENTF_WHEEL, MOUSEEVENTF_HWHEEL = 0x0800, 0x1000

ULONG_PTR = ctypes.c_size_t


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]


class _U(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.MapVirtualKeyW.argtypes = (wintypes.UINT, wintypes.UINT)

VK = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "shift": 0x10, "ctrl": 0x11, "alt": 0x12,
    "pause": 0x13, "capslock": 0x14, "esc": 0x1B, "space": 0x20, "pageup": 0x21, "pagedown": 0x22,
    "end": 0x23, "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "printscreen": 0x2C, "insert": 0x2D, "delete": 0x2E, "win": 0x5B, "apps": 0x5D,
    "numlock": 0x90, "scrolllock": 0x91,
    "lshift": 0xA0, "rshift": 0xA1, "lctrl": 0xA2, "rctrl": 0xA3, "lalt": 0xA4, "ralt": 0xA5,
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF, "`": 0xC0,
    "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
    "volumemute": 0xAD, "volumedown": 0xAE, "volumeup": 0xAF,
    "nexttrack": 0xB0, "prevtrack": 0xB1, "stop": 0xB2, "playpause": 0xB3,
}
ALIASES = {"control": "ctrl", "return": "enter", "escape": "esc", "del": "delete", "ins": "insert",
           "pgup": "pageup", "pgdn": "pagedown", "windows": "win", "menu": "alt", "plus": "="}
VK.update({f"f{i}": 0x6F + i for i in range(1, 25)})
VK.update({f"num{i}": 0x60 + i for i in range(10)})
VK.update({chr(c): c for c in range(ord("0"), ord("9") + 1)})
VK.update({chr(c).lower(): c for c in range(ord("A"), ord("Z") + 1)})

EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2C, 0x2D, 0x2E, 0x5B, 0x5D,
            0xA3, 0xA5, 0x90, 0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB2, 0xB3}
MEDIA = {0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB2, 0xB3}
MOUSE_BUTTONS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


def key_names() -> list[str]:
    return sorted(VK)


def parse_combo(text: str) -> list[int]:
    """'ctrl+shift+s' -> [vk, ...]. Raises ValueError on unknown key."""
    vks = []
    for raw in text.replace(" ", "").lower().split("+"):
        if not raw:
            continue
        name = ALIASES.get(raw, raw)
        if name not in VK:
            raise ValueError(f"未知按键: {raw}")
        vks.append(VK[name])
    if not vks:
        raise ValueError("按键为空")
    return vks


def _send(inputs: list[INPUT]) -> int:
    arr = (INPUT * len(inputs))(*inputs)
    return user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))


def _key_input(vk: int, up: bool) -> INPUT:
    flags = KEYEVENTF_KEYUP if up else 0
    if vk in MEDIA:
        # Media keys only work reliably as virtual-key events
        ki = KEYBDINPUT(vk, 0, flags | KEYEVENTF_EXTENDEDKEY, 0, 0)
    else:
        scan = user32.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)
        flags |= KEYEVENTF_SCANCODE
        if vk in EXTENDED:
            flags |= KEYEVENTF_EXTENDEDKEY
        ki = KEYBDINPUT(0, scan, flags, 0, 0)
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.u.ki = ki
    return inp


def key_down(vks: list[int]) -> int:
    return _send([_key_input(v, False) for v in vks])


def key_up(vks: list[int]) -> int:
    return _send([_key_input(v, True) for v in reversed(vks)])


def key_tap(vks: list[int]) -> int:
    return _send([_key_input(v, False) for v in vks] + [_key_input(v, True) for v in reversed(vks)])


def _mouse(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> int:
    inp = INPUT(type=INPUT_MOUSE)
    inp.u.mi = MOUSEINPUT(dx, dy, ctypes.c_ulong(data & 0xFFFFFFFF).value, flags, 0, 0)
    return _send([inp])


def mouse_button(button: str, mode: str = "click") -> None:
    down, up = MOUSE_BUTTONS[button]
    if mode in ("click", "down"):
        _mouse(down)
    if mode in ("click", "up"):
        _mouse(up)


def mouse_move(dx: int, dy: int) -> None:
    _mouse(MOUSEEVENTF_MOVE, int(dx), int(dy))


def mouse_wheel(amount: int, horizontal: bool = False) -> None:
    _mouse(MOUSEEVENTF_HWHEEL if horizontal else MOUSEEVENTF_WHEEL, data=int(amount) * 120)


def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False
