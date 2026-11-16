"""Render Python data as a Lua literal for files the addon loads (Crowley_Data/Data.lua).

Output uses the same conventions as SavedVariables (bracketed keys, double-quoted strings),
so savedvars.loads can read it back.
"""

from __future__ import annotations

import re
from typing import Any

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _string(value: str) -> str:
    out = ['"']
    for byte in value.encode("utf-8"):
        if byte == ord('"'):
            out.append('\\"')
        elif byte == ord("\\"):
            out.append("\\\\")
        elif byte == ord("\n"):
            out.append("\\n")
        elif 32 <= byte < 127:
            out.append(chr(byte))
        else:
            # Control and non-ASCII bytes as decimal escapes keep the file pure ASCII.
            out.append(f"\\{byte:03d}")
    out.append('"')
    return "".join(out)


def to_lua(value: Any, indent: int = 0) -> str:
    pad = "  " * (indent + 1)
    close = "  " * indent
    if value is None:
        return "nil"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        return _string(value)
    if isinstance(value, list):
        if not value:
            return "{}"
        body = "".join(f"{pad}{to_lua(v, indent + 1)},\n" for v in value)
        return "{\n" + body + close + "}"
    if isinstance(value, dict):
        if not value:
            return "{}"
        body = "".join(f"{pad}[{to_lua(k)}] = {to_lua(v, indent + 1)},\n" for k, v in value.items())
        return "{\n" + body + close + "}"
    raise TypeError(f"cannot encode {type(value).__name__} as Lua")


def render_assignment(name: str, value: Any, header: str) -> str:
    if not _IDENT.match(name):
        raise ValueError(f"invalid Lua global name {name!r}")
    prefix = "".join(f"-- {line}\n" for line in header.splitlines())
    return f"{prefix}{name} = {to_lua(value)}\n"
