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


def new_id() -> str:
    return uuid.uuid4().hex[:8]


def default_mapping() -> dict:
    return {
        "id": new_id(),
        "name": "新映射",
        "enabled": True,
        # kind: note | cc | pitchwheel | program_change
        # event: press | release | both | change   (channel/number None = any)
        "trigger": {"kind": "note", "channel": None, "number": 60, "event": "press"},
        # extra trigger fields: value_min, value_max, encoding, gesture, long_ms
        "conditions": "",  # e.g. "shift==1, mode!=2"
        "actions": [{"type": "key", "keys": "space", "mode": "tap", "delay_ms": 0}],
        "stop": False,
    }


def default_config() -> dict:
    return {
        "settings": {
            "input_port": "",
            "output_port": "",
            "enabled": True,
            "app_switching": True,
            "close_to_tray": True,
        },
        "profiles": [
            {"name": GLOBAL_PROFILE, "apps": [], "mappings": []},
        ],
    }


def _normalize(cfg: dict) -> dict:
    base = default_config()
    base["settings"].update(cfg.get("settings", {}))
    profiles = cfg.get("profiles") or []
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
    base["profiles"] = profiles
    return base


def load_config(path: Path = CONFIG_PATH) -> dict:
    if not path.exists():
        return default_config()
    with path.open("r", encoding="utf-8") as f:
        return _normalize(json.load(f))


def save_config(cfg: dict, path: Path = CONFIG_PATH) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def clone(obj):
    return copy.deepcopy(obj)
