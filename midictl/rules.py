"""Variables and simple conditions: 'shift==1, mode!=2, $value>64'."""
from __future__ import annotations

import re

_COND = re.compile(r"^\s*(\$?[A-Za-z_][\w]*)\s*(==|!=|>=|<=|>|<)\s*(-?\d+)\s*$")


def parse_conditions(text: str) -> list[tuple[str, str, int]]:
    out = []
    for part in (text or "").replace("，", ",").split(","):
        if not part.strip():
            continue
        m = _COND.match(part)
        if not m:
            raise ValueError(f"条件格式错误: {part.strip()}  (示例: shift==1)")
        out.append((m.group(1), m.group(2), int(m.group(3))))
    return out


def _cmp(a: int, op: str, b: int) -> bool:
    return {"==": a == b, "!=": a != b, ">": a > b, "<": a < b, ">=": a >= b, "<=": a <= b}[op]


def check_conditions(text: str, variables: dict, ev=None) -> bool:
    try:
        conds = parse_conditions(text)
    except ValueError:
        return False
    for name, op, val in conds:
        if name == "$value":
            cur = ev.value if ev is not None else 0
            if cur is None:
                return False  # A parameter increment is not an absolute parameter value.
        elif name == "$number":
            cur = ev.number if ev is not None else 0
        else:
            cur = int(variables.get(name, 0))
        if not _cmp(cur, op, val):
            return False
    return True


def apply_set_var(a: dict, variables: dict, ev=None) -> int:
    """op: set | add | toggle | from_value"""
    name = a.get("name", "").strip()
    if not name:
        return 0
    op = a.get("op", "set")
    v = int(a.get("value", 0))
    cur = int(variables.get(name, 0))
    if op == "add":
        new = cur + v
        lo, hi = a.get("min"), a.get("max")
        if a.get("wrap") and lo not in (None, "") and hi not in (None, ""):
            span = int(hi) - int(lo) + 1
            new = int(lo) + (new - int(lo)) % span
        else:
            if lo not in (None, ""):
                new = max(int(lo), new)
            if hi not in (None, ""):
                new = min(int(hi), new)
    elif op == "toggle":
        new = 0 if cur else (v or 1)
    elif op == "from_value":
        if ev is not None and ev.value is None:
            return cur
        new = ev.value if ev is not None else 0
    else:
        new = v
    variables[name] = new
    return new
