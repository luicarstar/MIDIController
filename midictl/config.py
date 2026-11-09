"""Config model: profiles -> mappings (trigger + action), stored as JSON."""
from __future__ import annotations

import copy
import json
import uuid
import sys
from pathlib import Path

CONFIG_PATH = (Path(sys.executable).resolve().parent if getattr(sys, "frozen", False)
               else Path(__file__).resolve().parent.parent) / "config.json"
GLOBAL_PROFILE = "全局"
SCHEMA_VERSION = 3


def input_ports(settings: dict) -> list[str]:
    """Exact requested names; an explicit empty list means no input."""
    ports = settings.get("input_ports")
    if ports is None and "input_ports" not in settings:
        legacy = settings.get("input_port", "")
        ports = [legacy] if legacy else []
    if (not isinstance(ports, list) or any(not isinstance(n, str) or not n for n in ports)
            or len(set(ports)) != len(ports)):
        raise ValueError("输入端口必须是不重复的完整名称列表")
    return list(ports)


def _set_input_view(settings: dict, ports: list[str]) -> None:
    settings["input_ports"] = list(ports)
    settings["input_port"] = ports[0] if ports else ""


def new_id() -> str:
    return uuid.uuid4().hex[:8]


def default_mapping() -> dict:
    return {
        "id": new_id(),
        "name": "新映射",
        "enabled": True,
        # kind: note | cc | pitchwheel | program_change | aftertouch | polytouch
        # event: press | release | both | change   (channel/number None = any)
        "trigger": {"kind": "note", "channel": None, "number": 60, "event": "press",
                    "source": None, "value_mode": "full", "edge_mode": "edge",
                    "threshold": 64, "invert": False},
        # extra trigger fields: value_min, value_max, encoding, gesture, long_ms
        "conditions": "",  # e.g. "shift==1, mode!=2"
        "actions": [{"type": "key", "keys": "space", "mode": "tap", "delay_ms": 0}],
        "stop": False,
    }


def default_feedback_binding() -> dict:
    return {"id": new_id(), "name": "新反馈", "enabled": True, "state": "enabled",
            "variable": "", "match": "", "mode": "on_off", "on_value": 127,
            "off_value": 0, "message": "control_change", "channel": 1, "number": 20}


def _feedback_bindings(value) -> list:
    if not isinstance(value, list) or len(value) > 128 or any(not isinstance(b, dict) for b in value):
        raise ValueError("反馈绑定必须是列表，最多128项")
    bindings = copy.deepcopy(value)
    for binding in bindings:
        binding.setdefault("id", new_id())
    return bindings


def default_config() -> dict:
    cfg = {
        "settings": {
            "input_port": "",
            "input_ports": [],
            "output_port": "",
            "enabled": True,
            "app_switching": True,
            "close_to_tray": True,
            "diagnostics": False,
        },
        "profiles": [
            {"name": GLOBAL_PROFILE, "apps": [], "mappings": []},
        ],
        "feedback_bindings": [],
    }
    keyboard = {"id": new_id(), "name": "默认键盘", "input_port": "", "input_ports": [], "output_port": "",
                "profiles": cfg["profiles"], "feedback_bindings": cfg["feedback_bindings"]}
    cfg.update(schema_version=SCHEMA_VERSION, keyboards=[keyboard], active_keyboard=keyboard["id"])
    return cfg


def _normalize_profiles(profiles: list) -> list:
    profiles = copy.deepcopy(profiles) or []
    if not any(p.get("name") == GLOBAL_PROFILE for p in profiles):
        profiles.insert(0, {"name": GLOBAL_PROFILE, "apps": [], "mappings": []})
    for p in profiles:
        p.setdefault("apps", [])
        p.setdefault("mappings", [])
        for m in p["mappings"]:
            m.setdefault("id", new_id())
            m.setdefault("enabled", True)
            if "actions" not in m:
                m["actions"] = [m.pop("action")] if "action" in m else []
            m.setdefault("conditions", "")
            m.setdefault("stop", False)
            trigger = m.get("trigger", {})
            trigger.setdefault("value_mode", "legacy")
            trigger.setdefault("edge_mode", "level")
            trigger.setdefault("threshold", 64)
            trigger.setdefault("invert", False)
    return profiles


def active_keyboard(cfg: dict) -> dict:
    return next(k for k in cfg["keyboards"] if k["id"] == cfg["active_keyboard"])


def sync_keyboard(cfg: dict) -> None:
    """Keep the existing settings/profiles API as the active keyboard view."""
    keyboard = active_keyboard(cfg)
    keyboard["profiles"] = cfg["profiles"]
    keyboard["feedback_bindings"] = cfg.get("feedback_bindings", [])
    ports = input_ports(cfg["settings"])
    _set_input_view(cfg["settings"], ports)
    _set_input_view(keyboard, ports)
    keyboard["output_port"] = cfg["settings"].get("output_port", "")


def switch_keyboard(cfg: dict, keyboard_id: str) -> None:
    keyboard = next((k for k in cfg["keyboards"] if k["id"] == keyboard_id), None)
    if keyboard is None:
        raise ValueError("键盘配置不存在")
    sync_keyboard(cfg)
    cfg["active_keyboard"] = keyboard_id
    cfg["profiles"] = keyboard["profiles"]
    cfg["feedback_bindings"] = keyboard.setdefault("feedback_bindings", [])
    _set_input_view(cfg["settings"], input_ports(keyboard))
    cfg["settings"]["output_port"] = keyboard.get("output_port", "")


def add_keyboard(cfg: dict, name: str, copy_current: bool = False) -> dict:
    name = name.strip()
    if not name or any(k["name"] == name for k in cfg["keyboards"]):
        raise ValueError("请输入不重复的键盘名称")
    sync_keyboard(cfg)
    keyboard = (copy.deepcopy(active_keyboard(cfg)) if copy_current else
                {"profiles": [{"name": GLOBAL_PROFILE, "apps": [], "mappings": []}], "feedback_bindings": [],
                 "input_port": "", "input_ports": [], "output_port": ""})
    keyboard.update(id=new_id(), name=name)
    cfg["keyboards"].append(keyboard)
    return keyboard


def _normalize(cfg: dict) -> dict:
    if not isinstance(cfg, dict):
        raise ValueError("配置必须是 JSON 对象")
    version = cfg.get("schema_version", 1)
    if not isinstance(version, int) or isinstance(version, bool):
        raise ValueError("配置版本必须是整数")
    if version > SCHEMA_VERSION:
        raise ValueError("配置由更新版本创建，请使用对应版本程序")
    if not isinstance(cfg.get("settings", {}), dict):
        raise ValueError("settings 必须是对象")
    if version in (2, 3):
        if not isinstance(cfg.get("keyboards"), list) or not cfg.get("active_keyboard"):
            raise ValueError("新版配置缺少 keyboards 或 active_keyboard；原文件未修改")
        for keyboard in cfg["keyboards"]:
            if (not isinstance(keyboard, dict) or not keyboard.get("id") or not keyboard.get("name")
                    or not isinstance(keyboard.get("profiles"), list)
                    or not isinstance(keyboard.get("input_port"), str)
                    or not isinstance(keyboard.get("output_port"), str)):
                raise ValueError("新版键盘配置字段缺失或格式错误；原文件未修改")
            if version == 3 and "input_ports" not in keyboard:
                raise ValueError("新版键盘配置缺少 input_ports；原文件未修改")
            if version == 3:
                input_ports(keyboard)
    elif version != 1 or "keyboards" in cfg or "active_keyboard" in cfg:
        raise ValueError("配置版本与键盘字段不一致；原文件未修改")
    base = default_config()
    base["settings"].update(cfg.get("settings", {}))
    if "keyboards" in cfg:
        keyboards = copy.deepcopy(cfg["keyboards"])
        if not keyboards or len({k["id"] for k in keyboards}) != len(keyboards):
            raise ValueError("键盘配置为空或 ID 重复")
        for keyboard in keyboards:
            # Legacy single-port files cannot accidentally acquire multi-port semantics.
            if version == 2:
                keyboard.pop("input_ports", None)
            _set_input_view(keyboard, input_ports(keyboard))
            keyboard["profiles"] = _normalize_profiles(keyboard["profiles"])
            keyboard["feedback_bindings"] = _feedback_bindings(keyboard.get("feedback_bindings", []))
        selected = cfg.get("active_keyboard")
        if selected not in {k["id"] for k in keyboards}:
            raise ValueError("当前键盘配置不存在")
        base.update(keyboards=keyboards, active_keyboard=selected)
        keyboard = active_keyboard(base)
        base["profiles"] = keyboard["profiles"]
        base["feedback_bindings"] = keyboard["feedback_bindings"]
        _set_input_view(base["settings"], input_ports(keyboard))
        base["settings"]["output_port"] = keyboard.get("output_port", "")
    else:
        base["profiles"] = _normalize_profiles(cfg.get("profiles", []))
        base["feedback_bindings"] = _feedback_bindings(cfg.get("feedback_bindings", []))
        legacy = cfg.get("settings", {}).get("input_port", "")
        if not isinstance(legacy, str):
            raise ValueError("旧版输入端口必须是名称字符串")
        _set_input_view(base["settings"], [legacy] if legacy else [])
        active_keyboard(base)["name"] = base["settings"].get("input_port") or "默认键盘"
        sync_keyboard(base)
    return base


def load_config(path: Path = CONFIG_PATH) -> dict:
    if not path.exists():
        return default_config()
    raw = path.read_bytes()
    original = json.loads(raw.decode("utf-8"))
    cfg = _normalize(original)
    if original.get("schema_version", 1) < SCHEMA_VERSION:
        suffixes = [".pre-midi-expansion.bak"]
        if original.get("schema_version", 1) < 2:
            suffixes.append(".pre-keyboards.bak")
        for suffix in suffixes:
            backup = path.with_name(path.name + suffix)
            try:
                with backup.open("xb") as f:
                    f.write(raw)
            except FileExistsError:
                pass  # Never replace the first rollback copy.
        save_config(cfg, path)
    return cfg


def save_config(cfg: dict, path: Path = CONFIG_PATH) -> None:
    sync_keyboard(cfg)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def clone(obj):
    return copy.deepcopy(obj)
