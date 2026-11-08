"""Parser for the Lua that WoW writes to SavedVariables files.

Pure Python on purpose: these files come from the game directory and we never want to
execute them. The client writes `NAME = <value>` statements where tables always use
bracketed keys (`["key"] = v`, `[1] = v`) or positional values followed by `-- [n]`
comments, and strings are double-quoted with backslash escapes.
"""

from __future__ import annotations

import re
from typing import Any

_WS_OR_COMMENT = re.compile(r"(?:\s+|--[^\n]*)*")
_NUMBER = re.compile(r"-?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_STRING_STOP = re.compile(r'["\\]')
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "\n": "\n"}


class SavedVariablesError(ValueError):
    pass


class _Parser:
    def __init__(self, text: str):
        self.text = text
        self.pos = 0

    def error(self, message: str) -> SavedVariablesError:
        line = self.text.count("\n", 0, self.pos) + 1
        return SavedVariablesError(f"{message} at line {line}")

    def skip(self) -> None:
        match = _WS_OR_COMMENT.match(self.text, self.pos)
        if match is None:
            raise self.error("unable to skip whitespace or comments")
        self.pos = match.end()

    def expect(self, char: str) -> None:
        self.skip()
        if self.text[self.pos : self.pos + 1] != char:
            raise self.error(f"expected {char!r}, got {self.text[self.pos : self.pos + 1]!r}")
        self.pos += 1

    def parse_file(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        self.skip()
        while self.pos < len(self.text):
            match = _IDENT.match(self.text, self.pos)
            if not match:
                raise self.error("expected variable name")
            self.pos = match.end()
            self.expect("=")
            result[match.group()] = self.parse_value()
            self.skip()
        return result

    def parse_value(self) -> Any:
        self.skip()
        char = self.text[self.pos : self.pos + 1]
        if char == "{":
            return self.parse_table()
        if char == '"':
            return self.parse_string()
        for word, value in (("true", True), ("false", False), ("nil", None)):
            if self.text.startswith(word, self.pos):
                self.pos += len(word)
                return value
        match = _NUMBER.match(self.text, self.pos)
        if not match:
            raise self.error(f"unexpected character {char!r}")
        self.pos = match.end()
        raw = match.group()
        return float(raw) if any(c in raw for c in ".eE") else int(raw)

    def parse_string(self) -> str:
        self.pos += 1  # opening quote
        parts: list[str] = []
        text = self.text
        while True:
            match = _STRING_STOP.search(text, self.pos)
            if not match:
                raise self.error("unterminated string")
            parts.append(text[self.pos : match.start()])
            self.pos = match.end()
            if match.group() == '"':
                return "".join(parts)
            esc = text[self.pos]
            if esc.isdigit():
                digits_match = re.match(r"\d{1,3}", text[self.pos : self.pos + 3])
                if digits_match is None:
                    raise self.error("invalid numeric escape")
                digits = digits_match.group()
                parts.append(chr(int(digits)))
                self.pos += len(digits)
            elif esc in _ESCAPES:
                parts.append(_ESCAPES[esc])
                self.pos += 1
            else:
                raise self.error(f"invalid escape \\{esc}")

    def parse_table(self) -> dict[Any, Any] | list[Any]:
        self.pos += 1  # {
        entries: dict[Any, Any] = {}
        next_index = 1
        while True:
            self.skip()
            char = self.text[self.pos : self.pos + 1]
            if char == "}":
                self.pos += 1
                break
            if char == "[":
                self.pos += 1
                key = self.parse_value()
                self.expect("]")
                self.expect("=")
                entries[key] = self.parse_value()
            else:
                entries[next_index] = self.parse_value()
                next_index += 1
            self.skip()
            if self.text[self.pos : self.pos + 1] == ",":
                self.pos += 1

        keys = list(entries)
        if keys and keys == list(range(1, len(keys) + 1)):
            return [entries[k] for k in keys]
        return entries


def _bytes_to_text(value: Any) -> Any:
    if isinstance(value, str):
        return value.encode("latin-1").decode("utf-8")
    if isinstance(value, list):
        return [_bytes_to_text(v) for v in value]
    if isinstance(value, dict):
        return {_bytes_to_text(k): _bytes_to_text(v) for k, v in value.items()}
    return value


def loads(data: bytes) -> dict[str, Any]:
    """Parse SavedVariables content into a mapping of global name -> value.

    Lua strings are byte strings (and `\\ddd` escapes are bytes), so parsing happens on a
    latin-1 view of the raw bytes and strings are decoded as UTF-8 afterwards.
    Lua tables with keys exactly 1..n become lists; all other tables become dicts.
    """
    return _bytes_to_text(_Parser(data.decode("latin-1")).parse_file())


def load_file(path) -> dict[str, Any]:
    with open(path, "rb") as handle:
        return loads(handle.read())
