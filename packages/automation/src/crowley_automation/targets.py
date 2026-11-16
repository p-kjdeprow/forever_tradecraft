"""Content-bound, reviewed interaction profiles stored under XDG config."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from crowley_common.house import House
from crowley_common.platform.paths import XdgDirs

from .config import AutomationConfig
from .movement import RouteProfile

MAX_PROFILE_BYTES = 16_384


class TargetProfileError(ValueError):
    """A referenced measured target profile is absent, malformed or mismatched."""


def _exact(value: object, keys: set[str], name: str) -> dict[str, object]:
    if type(value) is not dict or set(value) != keys:
        raise TargetProfileError(f"{name} has unexpected fields")
    return cast(dict[str, object], value)


def _text(value: object, name: str, maximum: int = 128) -> str:
    if type(value) is not str or not value or len(value.encode("utf-8")) > maximum:
        raise TargetProfileError(f"{name} must be bounded nonempty text")
    return value


def _integer(value: object, name: str, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise TargetProfileError(f"{name} is outside its integer bound")
    return value


def _number(value: object, name: str, *, positive: bool = False) -> float:
    if type(value) not in (int, float) or not math.isfinite(cast(int | float, value)):
        raise TargetProfileError(f"{name} must be finite")
    result = float(cast(int | float, value))
    if positive and result <= 0:
        raise TargetProfileError(f"{name} must be positive")
    return result


def _house(value: str) -> None:
    try:
        if House.parse(value).key != value:
            raise ValueError("non-canonical")
    except ValueError as exc:
        raise TargetProfileError("house must be a canonical house key") from exc


def _rect(value: object) -> tuple[int, int, int, int]:
    row = _exact(value, {"x", "y", "width", "height"}, "rect")
    fields = tuple(row[name] for name in ("x", "y", "width", "height"))
    if any(type(item) is not int for item in fields):
        raise TargetProfileError("rect coordinates must be exact integers")
    x, y, width, height = cast(tuple[int, int, int, int], fields)
    if x < 0 or y < 0 or not 3 <= width <= 2048 or not 3 <= height <= 2048:
        raise TargetProfileError("rect lies outside the measured target bounds")
    return x, y, width, height


def _pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise TargetProfileError(f"duplicate target profile field: {key}")
        value[key] = item
    return value


@dataclass(frozen=True, slots=True)
class TargetProfile:
    """A selected NPC or measured mailbox bound to one exact client identity."""

    kind: str
    house: str
    character: str
    client_build: str
    instance_id: int
    expected_ui: str
    name: str
    guid: str = ""
    rect: tuple[int, int, int, int] | None = None
    anchor_x: float | None = None
    anchor_y: float | None = None
    max_distance: float | None = None
    schema: int = 1

    def __post_init__(self) -> None:
        if self.kind not in {"npc", "mailbox"} or type(self.schema) is not int or self.schema != 1:
            raise TargetProfileError("target profile kind or schema is unsupported")
        _text(self.house, "house")
        _house(self.house)
        _text(self.character, "character")
        _text(self.client_build, "clientBuild", 64)
        _text(self.name, "name")
        if type(self.instance_id) is not int or not 0 <= self.instance_id <= 2_147_483_647:
            raise TargetProfileError("instanceId is outside its integer bound")
        if self.kind == "npc":
            if self.expected_ui != "ah":
                raise TargetProfileError("NPC profiles must expect auction-house UI")
            _text(self.guid, "guid")
            if any(
                value is not None for value in (self.rect, self.anchor_x, self.anchor_y, self.max_distance)
            ):
                raise TargetProfileError("NPC profiles cannot contain mailbox measurements")
            return
        if self.name != "Mailbox" or self.guid or type(self.rect) is not tuple or len(self.rect) != 4:
            raise TargetProfileError("mailbox profiles require the exact hover name and a measured rect")
        if self.expected_ui != "mail":
            raise TargetProfileError("mailbox profiles must expect mail UI")
        _rect(dict(zip(("x", "y", "width", "height"), self.rect, strict=True)))
        _number(self.anchor_x, "anchor.x")
        _number(self.anchor_y, "anchor.y")
        if _number(self.max_distance, "anchor.maxDistance", positive=True) > 5.0:
            raise TargetProfileError("mailbox anchor radius must be at most five world units")

    def object(self) -> dict[str, object]:
        value: dict[str, object] = {
            "schema": self.schema,
            "kind": self.kind,
            "house": self.house,
            "character": self.character,
            "clientBuild": self.client_build,
            "instanceId": self.instance_id,
            "expectedUi": self.expected_ui,
            "name": self.name,
        }
        if self.kind == "npc":
            value["guid"] = self.guid
        else:
            assert self.rect is not None
            value["rect"] = dict(zip(("x", "y", "width", "height"), self.rect, strict=True))
            value["anchor"] = {
                "x": self.anchor_x,
                "y": self.anchor_y,
                "maxDistance": self.max_distance,
            }
        return value

    @property
    def profile_id(self) -> str:
        canonical = json.dumps(self.object(), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return "target-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:57]

    @classmethod
    def from_json(cls, source: bytes | str) -> TargetProfile:
        try:
            raw = source.encode("utf-8") if isinstance(source, str) else source
            if len(raw) > MAX_PROFILE_BYTES:
                raise TargetProfileError("target profile exceeds its size bound")
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
        except TargetProfileError:
            raise
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise TargetProfileError("target profile is not valid UTF-8 JSON") from exc
        if type(value) is not dict or value.get("schema") != 1:
            raise TargetProfileError("target profile schema is invalid")
        kind = value.get("kind")
        common = {"schema", "kind", "house", "character", "clientBuild", "instanceId", "expectedUi", "name"}
        if kind == "npc":
            row = _exact(value, common | {"guid"}, "NPC profile")
            return cls(
                "npc",
                _text(row["house"], "house"),
                _text(row["character"], "character"),
                _text(row["clientBuild"], "clientBuild", 64),
                _integer(row["instanceId"], "instanceId", 2_147_483_647),
                _text(row["expectedUi"], "expectedUi", 8),
                _text(row["name"], "name"),
                _text(row["guid"], "guid"),
                schema=_integer(row["schema"], "schema", 1),
            )
        if kind == "mailbox":
            row = _exact(value, common | {"rect", "anchor"}, "mailbox profile")
            anchor = _exact(row["anchor"], {"x", "y", "maxDistance"}, "mailbox anchor")
            return cls(
                "mailbox",
                _text(row["house"], "house"),
                _text(row["character"], "character"),
                _text(row["clientBuild"], "clientBuild", 64),
                _integer(row["instanceId"], "instanceId", 2_147_483_647),
                _text(row["expectedUi"], "expectedUi", 8),
                _text(row["name"], "name"),
                rect=_rect(row["rect"]),
                anchor_x=_number(anchor["x"], "anchor.x"),
                anchor_y=_number(anchor["y"], "anchor.y"),
                max_distance=_number(anchor["maxDistance"], "anchor.maxDistance", positive=True),
                schema=_integer(row["schema"], "schema", 1),
            )
        raise TargetProfileError("target profile kind is unsupported")


def _read_profile(path: Path) -> bytes:
    try:
        info = path.lstat()
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            or info.st_size > MAX_PROFILE_BYTES
        ):
            raise TargetProfileError(f"profile file {path.name} is not a bounded owned regular file")
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            data = os.read(descriptor, MAX_PROFILE_BYTES + 1)
            opened = os.fstat(descriptor)
            if (opened.st_ino, opened.st_dev, opened.st_size) != (info.st_ino, info.st_dev, info.st_size):
                raise TargetProfileError(f"profile file {path.name} changed during read")
            return data
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise TargetProfileError(f"profile file {path.name} cannot be read") from exc


def _profile_dir(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise TargetProfileError("profile directory must be a real directory")
    return True


def load_profiles(
    config: AutomationConfig, *, root: Path | None = None
) -> tuple[dict[str, TargetProfile], dict[str, RouteProfile]]:
    """Load existing content-addressed profiles; absent directories mean no profiles."""

    base = (XdgDirs.from_environ(os.environ).crowley_config / "automation") if root is None else root
    identity = (config.house, config.character, config.build.rsplit(".", 1)[1])
    targets: dict[str, TargetProfile] = {}
    target_dir = base / "targets"
    if _profile_dir(target_dir):
        for path in sorted(target_dir.glob("target-*.json")):
            profile = TargetProfile.from_json(_read_profile(path))
            if path.stem != profile.profile_id:
                raise TargetProfileError(
                    f"target profile filename {path.name} does not match its content hash"
                )
            if (profile.house, profile.character, profile.client_build) != identity:
                raise TargetProfileError(f"target profile {path.stem} belongs to another configured identity")
            targets[profile.profile_id] = profile

    routes: dict[str, RouteProfile] = {}
    route_dir = base / "routes"
    if _profile_dir(route_dir):
        for path in sorted(route_dir.glob("route-*.json")):
            route = RouteProfile.from_json(_read_profile(path))
            if path.stem != route.route_id:
                raise TargetProfileError(
                    f"route profile filename {path.name} does not match its content hash"
                )
            if (route.house, route.character, route.client_build) != identity:
                raise TargetProfileError(f"route profile {path.stem} belongs to another configured identity")
            routes[route.route_id] = route
    return targets, routes


__all__ = ["TargetProfile", "TargetProfileError", "load_profiles"]
