"""Passive runtime readiness and durable environment-bound takeover evidence."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from .calibration import XTestInputProfile

STARTUP_CONTRACT = "crowley-passive-startup-v1"
TAKEOVER_CONTRACT = "crowley-takeover-hover-v5"
PASSIVE_STARTUP_TIMEOUT_SECONDS = 15.0
TAKEOVER_DRILL_ARM_RESERVE_SECONDS = 30.0
TAKEOVER_DRILL_HUMAN_TIMEOUT_SECONDS = 120.0
TAKEOVER_DRILL_HOVER_COMMAND_TIMEOUT_SECONDS = 5.0
TAKEOVER_DRILL_HOVER_OBSERVATION_TIMEOUT_SECONDS = 3.0
TAKEOVER_DRILL_RELEASE_TIMEOUT_SECONDS = 3.0
TAKEOVER_DRILL_CLEANUP_RESERVE_SECONDS = 5.0
TAKEOVER_DRILL_TRANSPORT_MARGIN_SECONDS = 5.0
TAKEOVER_DRILL_OPERATION_TIMEOUT_SECONDS = (
    PASSIVE_STARTUP_TIMEOUT_SECONDS
    + TAKEOVER_DRILL_ARM_RESERVE_SECONDS
    + TAKEOVER_DRILL_HOVER_COMMAND_TIMEOUT_SECONDS
    + TAKEOVER_DRILL_HOVER_OBSERVATION_TIMEOUT_SECONDS
    + TAKEOVER_DRILL_HUMAN_TIMEOUT_SECONDS
    + TAKEOVER_DRILL_RELEASE_TIMEOUT_SECONDS
    + TAKEOVER_DRILL_CLEANUP_RESERVE_SECONDS
)
_PROCESS_KEY = secrets.token_bytes(32)
_COMPOSITOR_FORWARDED_DEVICE_PREFIXES = (
    ("xwayland-pointer:", "pointer"),
    ("xwayland-relative-pointer:", "pointer"),
    ("xwayland-pointer-gestures:", "pointer"),
    ("xwayland-keyboard:", "keyboard"),
)
_CANONICAL_FORWARDED_ATTACHMENT = (0, "compositor-forwarded")
_CANONICAL_FORWARDED_ECHO_SOURCES: tuple[int, ...] = ()


class InvalidationCause(StrEnum):
    """Typed causes at the boundary where readiness is no longer usable."""

    OBSERVER_LOST = "observer_lost"
    GAME_PROCESS_CHANGED = "game_process_changed"
    INPUT_ENVIRONMENT_CHANGED = "input_environment_changed"
    TELEMETRY_INVALID = "telemetry_invalid"
    UNKNOWN = "unknown"


def _jsonable(value: Any) -> Any:
    if hasattr(value, "canonical_payload"):
        return _jsonable(value.canonical_payload())
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in sorted(value.items())}
    if isinstance(value, (tuple, list, set, frozenset)):
        rows = [_jsonable(item) for item in value]
        return sorted(rows, key=repr) if isinstance(value, (set, frozenset)) else rows
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError(f"unsupported startup evidence value: {type(value).__name__}")


def _canonical(value: Any) -> bytes:
    return json.dumps(_jsonable(value), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _compositor_forwarded_device(name: str) -> tuple[str, str, str] | None:
    observed_kind, separator, device_name = name.partition(":")
    if separator != ":":
        return None
    for prefix, expected_kind in _COMPOSITOR_FORWARDED_DEVICE_PREFIXES:
        if device_name.startswith(prefix):
            return observed_kind, expected_kind, device_name
    return None


def _canonical_hierarchy(
    hierarchy: tuple[tuple[int, int, str, str], ...],
) -> tuple[tuple[int, int, str, str], ...]:
    """Keep forwarded device identity while removing only seat attachment state."""

    canonical: list[tuple[int, int, str, str]] = []
    for device_id, parent_id, role, name in hierarchy:
        forwarded = _compositor_forwarded_device(name)
        if forwarded is not None:
            observed_kind, expected_kind, device_name = forwarded
            attached = role == "slave" and parent_id > 0 and observed_kind == expected_kind
            floating = role == "floating" and parent_id == 0 and observed_kind == "slave"
        else:
            attached = floating = False
        if attached or floating:
            canonical.append(
                (
                    device_id,
                    _CANONICAL_FORWARDED_ATTACHMENT[0],
                    _CANONICAL_FORWARDED_ATTACHMENT[1],
                    f"{expected_kind}:{device_name}",
                )
            )
        else:
            canonical.append((device_id, parent_id, role, name))
    return tuple(canonical)


def _durable_xtest_profile(profile: XTestInputProfile) -> XTestInputProfile:
    """Canonicalise only the seat attachment facts in a durable XTEST profile."""

    return replace(profile, hierarchy=_canonical_hierarchy(profile.hierarchy))


@dataclass(frozen=True)
class StartupEnvironment:
    display: str
    hierarchy: tuple[tuple[int, int, str, str], ...]
    keyboard_source: int
    keyboard_master: int
    pointer_source: int
    pointer_master: int
    forwarded_echo_sources: tuple[int, ...]
    xtest_profile: XTestInputProfile
    keymap_levels: tuple[tuple[int, tuple[str, ...]], ...]
    modifier_masks: tuple[tuple[str, int], ...]
    input_tool_version: str
    input_tool_sha256: str

    def canonical_payload(self) -> dict[str, Any]:
        durable_xtest_profile = _durable_xtest_profile(self.xtest_profile)
        return {
            "contract": TAKEOVER_CONTRACT,
            "display": self.display,
            "hierarchy": _canonical_hierarchy(self.hierarchy),
            "keyboardSource": self.keyboard_source,
            "keyboardMaster": self.keyboard_master,
            "pointerSource": self.pointer_source,
            "pointerMaster": self.pointer_master,
            "forwardedEchoSources": _CANONICAL_FORWARDED_ECHO_SOURCES,
            "xtestProfile": durable_xtest_profile,
            "keymapLevels": self.keymap_levels,
            "modifierMasks": self.modifier_masks,
            "inputToolVersion": self.input_tool_version,
            "inputToolSha256": self.input_tool_sha256,
        }

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(_canonical(self.canonical_payload())).hexdigest()


@dataclass(frozen=True)
class RuntimeInputEvidence:
    """Fresh per-process native input contract; never reconstructed from journal."""

    process_id: int
    issued_monotonic_ns: int
    readiness_boot: str
    readiness_sequence: int
    environment: StartupEnvironment
    game_identity_fingerprint: str
    game_process_start: str
    game_geometry: tuple[int, int, int, int]
    keyboard_keymap: tuple[tuple[str, int], ...]
    native_modifiers: tuple[int, int, int]
    native_held_keycodes: tuple[int, ...]
    native_held_buttons: tuple[int, ...]
    keyboard_profile: tuple[tuple[str, str, tuple[tuple[Any, ...], ...]], ...]
    event_provenance: str
    _seal: str

    @property
    def display(self) -> str:
        return self.environment.display

    @property
    def xauthority(self) -> str:
        return ""

    @property
    def driver_version(self) -> str:
        return STARTUP_CONTRACT

    @property
    def xinput_hierarchy(self) -> tuple[tuple[int, int, str, str], ...]:
        return self.environment.hierarchy

    @property
    def xtest_profile(self) -> XTestInputProfile:
        return self.environment.xtest_profile

    @property
    def synthetic_key_source(self) -> int:
        return self.environment.keyboard_source

    @property
    def synthetic_pointer_source(self) -> int:
        return self.environment.pointer_source

    @property
    def synthetic_pointer_master(self) -> int:
        return self.environment.pointer_master

    @property
    def synthetic_modifiers(self) -> tuple[int, int, int]:
        return self.native_modifiers

    @property
    def forwarded_echo_sources(self) -> frozenset[int]:
        return frozenset(self.environment.forwarded_echo_sources)

    @property
    def environment_fingerprint(self) -> str:
        return self.environment.fingerprint

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(_canonical(self._payload())).hexdigest()

    @property
    def is_live(self) -> bool:
        try:
            self.verify_seal()
        except (TypeError, ValueError):
            return False
        return True

    def _payload(self) -> dict[str, Any]:
        return {
            "processId": self.process_id,
            "issuedMonotonicNs": self.issued_monotonic_ns,
            "readinessBoot": self.readiness_boot,
            "readinessSequence": self.readiness_sequence,
            "environment": self.environment.canonical_payload(),
            "runtimeHierarchy": self.environment.hierarchy,
            "runtimeForwardedEchoSources": self.environment.forwarded_echo_sources,
            "runtimeXtestProfile": self.environment.xtest_profile,
            "identity": self.game_identity_fingerprint,
            "processStart": self.game_process_start,
            "geometry": self.game_geometry,
            "keyboardKeymap": self.keyboard_keymap,
            "nativeModifiers": self.native_modifiers,
            "heldKeycodes": self.native_held_keycodes,
            "heldButtons": self.native_held_buttons,
            "keyboardProfile": self.keyboard_profile,
            "eventProvenance": self.event_provenance,
        }

    def verify_seal(self) -> None:
        if self.process_id != os.getpid() or not self.game_identity_fingerprint:
            raise ValueError("runtime input evidence belongs to another process or has no game identity")
        if (
            not isinstance(self.readiness_boot, str)
            or not self.readiness_boot
            or isinstance(self.readiness_sequence, bool)
            or not isinstance(self.readiness_sequence, int)
            or not 1 <= self.readiness_sequence <= 0xFFFFFFFF
        ):
            raise ValueError("runtime input evidence has no positive passive readiness provenance")
        expected = hmac.new(_PROCESS_KEY, _canonical(self._payload()), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, self._seal):
            raise ValueError("runtime input evidence seal is invalid")
        if self.native_held_keycodes or self.native_held_buttons:
            raise ValueError("runtime input evidence contains pre-existing held input")
        if self.environment.forwarded_echo_sources != resolve_forwarded_echo_sources(
            self.environment.hierarchy, self.environment.pointer_master
        ):
            raise ValueError("runtime forwarded echo source is not uniquely bound to the observed hierarchy")
        if len(self.native_modifiers) != 3 or any(value < 0 for value in self.native_modifiers):
            raise ValueError("runtime input modifier state is malformed")
        masks = self.environment.modifier_masks
        if (
            not isinstance(masks, tuple)
            or tuple(name for name, _mask in masks) != ("Shift_L", "Control_L")
            or any(
                isinstance(mask, bool) or not isinstance(mask, int) or mask <= 0 or mask & (mask - 1)
                for _name, mask in masks
            )
            or masks[0][1] == masks[1][1]
        ):
            raise ValueError("runtime keyboard modifier map is malformed")
        self.environment.xtest_profile.verify()


def resolve_forwarded_echo_sources(
    hierarchy: tuple[tuple[int, int, str, str], ...], pointer_master: int
) -> tuple[int, ...]:
    """Bind the sole observed Xwayland relative-pointer relay from hierarchy facts.

    Retained host traces show the XTEST slave attached to the virtual core
    pointer and its delivered echo on the unique ``xwayland-relative-pointer``
    slave attached to that same master. Other pointer slaves (including the
    absolute and gesture devices) are not forwarding evidence. A sole floating
    relay is accepted as having no echo source; a missing, duplicated, or
    differently attached relay blocks startup.
    """

    if isinstance(pointer_master, bool) or not isinstance(pointer_master, int) or pointer_master <= 0:
        raise ValueError("runtime pointer master is malformed")
    relays: list[tuple[int, int, str, str]] = []
    for row in hierarchy:
        if (
            not isinstance(row, tuple)
            or len(row) != 4
            or not isinstance(row[3], str)
            or isinstance(row[0], bool)
            or not isinstance(row[0], int)
            or row[0] <= 0
            or isinstance(row[1], bool)
            or not isinstance(row[1], int)
        ):
            continue
        forwarded = _compositor_forwarded_device(row[3])
        if (
            forwarded is not None
            and forwarded[1] == "pointer"
            and forwarded[2].casefold().startswith("xwayland-relative-pointer:")
        ):
            relays.append(row)
    if len(relays) != 1:
        raise ValueError("runtime forwarded Xwayland relative-pointer relay is absent or ambiguous")
    relay_id, parent_id, role, name = relays[0]
    forwarded = _compositor_forwarded_device(name)
    if forwarded is None:
        raise ValueError("runtime forwarded Xwayland relative-pointer relay is absent or ambiguous")
    observed_kind, expected_kind, _device_name = forwarded
    if role == "floating" and parent_id == 0 and observed_kind == "slave":
        return ()
    if role == "slave" and parent_id == pointer_master and observed_kind == expected_kind:
        return (relay_id,)
    raise ValueError("runtime forwarded Xwayland relative-pointer relay is absent or ambiguous")


def is_measured_human_action(event: Any, environment: StartupEnvironment) -> bool:
    """Accept only a non-XTEST action from a measured slave of the right role.

    An unknown source still reaches Guardian's ordinary revoke path, but it
    cannot satisfy the durable takeover drill.  Delivered XI2 action records
    identify their source in both device fields. Raw XI2 key/button records
    arrive first, with the measured slave in the source field and its attached
    virtual core master in the device field. The hierarchy supplies the role
    in both cases.
    """

    source = getattr(event, "source_device", None)
    master = getattr(event, "master_device", None)
    event_class = getattr(getattr(event, "event_class", None), "value", "")
    raw_classes = {
        "RawKeyPress",
        "RawKeyRelease",
        "RawButtonPress",
        "RawButtonRelease",
        "RawMotion",
    }
    keyboard_classes = {"KeyPress", "KeyRelease", "RawKeyPress", "RawKeyRelease"}
    pointer_classes = {
        "ButtonPress",
        "ButtonRelease",
        "Motion",
        "RawButtonPress",
        "RawButtonRelease",
        "RawMotion",
    }
    if (
        isinstance(source, bool)
        or not isinstance(source, int)
        or source <= 0
        or isinstance(master, bool)
        or not isinstance(master, int)
        or event_class not in keyboard_classes | pointer_classes
        or (event_class in raw_classes and not getattr(event, "is_raw", False))
        or (event_class not in raw_classes and not getattr(event, "is_action_event", False))
    ):
        return False
    excluded_sources = {
        environment.keyboard_source,
        environment.pointer_source,
        environment.keyboard_master,
        environment.pointer_master,
        *environment.forwarded_echo_sources,
    }
    if source in excluded_sources:
        return False
    rows = [row for row in environment.hierarchy if len(row) == 4 and row[0] == source]
    if len(rows) != 1:
        return False
    _source, attachment, kind, name = rows[0]
    if kind != "slave" or not isinstance(name, str) or "xtest" in name.casefold():
        return False
    if event_class in keyboard_classes:
        role_master = environment.keyboard_master
        role_name = "keyboard:"
    else:
        role_master = environment.pointer_master
        role_name = "pointer:"
    if attachment != role_master or not name.casefold().startswith(role_name):
        return False
    if event_class in raw_classes:
        return master == role_master
    return master == source


def mint_runtime_evidence(
    *,
    readiness_boot: str,
    readiness_sequence: int,
    environment: StartupEnvironment,
    identity: Any,
    keyboard_keymap: tuple[tuple[str, int], ...],
    native_modifiers: tuple[int, int, int],
    native_held_keycodes: tuple[int, ...],
    native_held_buttons: tuple[int, ...],
    keyboard_profile: tuple[tuple[str, str, tuple[tuple[Any, ...], ...]], ...],
) -> RuntimeInputEvidence:
    issued = time.monotonic_ns()
    values = {
        "process_id": os.getpid(),
        "issued_monotonic_ns": issued,
        "readiness_boot": readiness_boot,
        "readiness_sequence": readiness_sequence,
        "environment": environment,
        "game_identity_fingerprint": identity.fingerprint,
        "game_process_start": identity.process_start,
        "game_geometry": tuple(identity.child_geometry),
        "keyboard_keymap": keyboard_keymap,
        "native_modifiers": native_modifiers,
        "native_held_keycodes": native_held_keycodes,
        "native_held_buttons": native_held_buttons,
        "keyboard_profile": keyboard_profile,
        "event_provenance": "registered-xi2-observer;xtest-slave-to-distinct-master-forwarding-v2",
    }
    unsealed = RuntimeInputEvidence(**values, _seal="")
    seal = hmac.new(_PROCESS_KEY, _canonical(unsealed._payload()), hashlib.sha256).hexdigest()
    evidence = RuntimeInputEvidence(**values, _seal=seal)
    evidence.verify_seal()
    return evidence


@dataclass(frozen=True)
class TakeoverDrillRecord:
    schema: int
    contract: str
    environment_fingerprint: str
    observed_at: int
    release_latency_ms: int
    fingerprint: str

    def payload(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "contract": self.contract,
            "environmentFingerprint": self.environment_fingerprint,
            "observedAt": self.observed_at,
            "releaseLatencyMs": self.release_latency_ms,
        }

    @classmethod
    def create(cls, environment_fingerprint: str, latency_ms: int, *, observed_at: int | None = None):
        payload = {
            "schema": 1,
            "contract": TAKEOVER_CONTRACT,
            "environmentFingerprint": environment_fingerprint,
            "observedAt": int(time.time()) if observed_at is None else observed_at,
            "releaseLatencyMs": latency_ms,
        }
        digest = hashlib.sha256(_canonical(payload)).hexdigest()
        return cls(1, TAKEOVER_CONTRACT, environment_fingerprint, payload["observedAt"], latency_ms, digest)

    @classmethod
    def parse(cls, raw: Any) -> TakeoverDrillRecord | None:
        required = {
            "schema",
            "contract",
            "environmentFingerprint",
            "observedAt",
            "releaseLatencyMs",
            "fingerprint",
        }
        if not isinstance(raw, dict) or set(raw) != required:
            return None
        schema = raw.get("schema")
        contract = raw.get("contract")
        environment = raw.get("environmentFingerprint")
        observed = raw.get("observedAt")
        latency = raw.get("releaseLatencyMs")
        fingerprint = raw.get("fingerprint")
        if (
            schema != 1
            or isinstance(schema, bool)
            or contract != TAKEOVER_CONTRACT
            or not isinstance(environment, str)
            or len(environment) != 64
            or isinstance(observed, bool)
            or not isinstance(observed, int)
            or observed <= 0
            or isinstance(latency, bool)
            or not isinstance(latency, int)
            or not 0 <= latency <= 120_000
            or not isinstance(fingerprint, str)
            or len(fingerprint) != 64
        ):
            return None
        result = cls(schema, contract, environment, observed, latency, fingerprint)
        expected = hashlib.sha256(_canonical(result.payload())).hexdigest()
        return result if hmac.compare_digest(expected, fingerprint) else None

    def to_json(self) -> dict[str, Any]:
        return {**self.payload(), "fingerprint": self.fingerprint}

    def matches(self, environment: StartupEnvironment) -> bool:
        return self.environment_fingerprint == environment.fingerprint
