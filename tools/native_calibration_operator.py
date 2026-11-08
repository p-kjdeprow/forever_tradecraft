"""Bounded offline candidate for the native construction operator.

The candidate deliberately owns no desktop lease and creates no second Guardian.
It speaks the closed native-construction IPC vocabulary, derives click points only
from fresh template matches, and uses the existing sealed-input watchdog capability
when a real backend is supplied.  Native dependency acceptance is still required
before this module can be used for integration or live input.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import math
import multiprocessing
import os
import re
import secrets
import select
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from PIL import Image, ImageOps

if not __package__:
    _PROJECT_ROOT = Path(__file__).resolve().parents[1]
    if str(_PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(_PROJECT_ROOT))

from crowley.native import build as native_build
from crowley.native import pins
from crowley.native.matcher import MatcherError, TemplateMatcherRuntime
from crowley.native.publication import PublicationReadError, read_current_publication
from crowley.session.cli import _socket_request
from crowley.session.config import load_config
from crowley.session.decoder import PixelDecoder
from crowley.session.driver import DriverError, X11Backend, _proc_start
from crowley.session.executor import BindingMetadata, MotionProfile, Point, PointerButton, PointerTarget, Rect
from crowley.session.guardian import _watchdog_entry
from crowley.session.input_tool import NativeReleaseSpec, PinnedInputTool
from crowley.session.pointer_execution import (
    ClickRatePermit,
    HumanPointerPlan,
    PointerExecutionError,
    acquire_click_rate_permit,
    native_pointer_motion_action,
    plan_human_pointer,
    pointer_action_timeout,
)

MODE_CONTROL_PREFLIGHT = "control-preflight"
MODE_MEASURE_DEFERRED = "measure-deferred"
MODE_RECOVER = "recover"
MODES = frozenset({MODE_CONTROL_PREFLIGHT, MODE_MEASURE_DEFERRED, MODE_RECOVER})

FIXED_COMMANDS = (
    "/crowley agent defer",
    "/crowley agent release",
    "/crowley agent recover",
    "/crowley agent hold",
    "/crowley agent measure",
    "/crowley agent resume",
    "/crowley agent finalize",
    "/reload",
)
FIXED_COMMAND_SET = frozenset(FIXED_COMMANDS)
_NATIVE_POINTER_PROFILE = MotionProfile(min_duration=0.1, sample_hz=30.0)
_POINTER_CLICK_ACK_RESERVE_SECONDS = 1.5
# The production profile captures this fixed chat editor rectangle from the
# 1920x1080 client.  In the measured crop the Say label spans x17..39, the
# command body begins at x45, and the right border is at x440.  The body box
# starts at x40 to retain the leading slash and ends before that border; y7
# intentionally includes the top rule.  Rows 20..21 are clipped by this
# empirically qualified geometry, which passes every fixed command capture.
CHAT_CROP_SIZE = (460, 37)
# Read the command body directly, excluding the channel label and frame rules.
# The cursor is removed by bounded preprocessing.
CHAT_OCR_BOX = (40, 7, 400, 13)
CHAT_OCR_THRESHOLD = 45
# The body background is dimmer than the insertion cursor.  A probe at the
# body threshold mistakes one antialiased slash column for the cursor; keep a
# stricter, preprocessing-only threshold so only the measured full-height
# cursor columns are removed.
CHAT_CURSOR_THRESHOLD = 60
CHAT_OCR_SCALE = 3
CHAT_EDITOR_ACTIVE = "active"
CHAT_EDITOR_INACTIVE = "inactive"
IPC_COMMANDS = frozenset(
    {
        "native_construction_prepare",
        "native_construction_control_preflight",
        "native_construction_bootstrap",
        "native_construction_recheck",
        "native_construction_finalize_admit",
        "native_construction_recovery_begin",
        "native_construction_recovery_complete",
        "native_construction_recovery_cancel",
    }
)
CONTROL_COMMANDS = frozenset({"native_calibrate", "calibration_status", "status", "pause"})
COMMANDS = IPC_COMMANDS | CONTROL_COMMANDS
IPC_FIELDS = {
    "native_construction_prepare": frozenset({"command", "generation"}),
    "native_construction_control_preflight": frozenset({"command", "generation"}),
    "native_construction_bootstrap": frozenset(
        {"command", "generation", "reservationId", "expectedConstructorBoot"}
    ),
    "native_construction_recheck": frozenset({"command", "generation", "reservationId"}),
    "native_construction_finalize_admit": frozenset({"command", "generation", "reservationId"}),
    "native_construction_recovery_begin": frozenset({"command", "generation", "reservationId"}),
    "native_construction_recovery_complete": frozenset({"command", "generation", "reservationId"}),
    "native_construction_recovery_cancel": frozenset({"command", "generation", "reservationId"}),
    "native_calibrate": frozenset({"command", "generation"}),
    "calibration_status": frozenset({"command"}),
    "status": frozenset({"command"}),
    "pause": frozenset({"command", "generation", "reason"}),
}
CANONICAL_RESERVATION_FIELDS = (
    "reservationId",
    "reservationKind",
    "reservationState",
    "controllerGeneration",
    "desktopLeaseHeld",
    "guardianGeneration",
    "guardianArmed",
    "ownerPid",
    "ownerProcessStart",
    "profileFingerprint",
    "identityFingerprint",
    "B0",
    "expectedConstructorBoot",
    "expiresMonotonic",
)
HANDSHAKE_STATES = frozenset(
    {
        "construction_prepared",
        "running",
        "construction_reset_archived",
        "baseline_bound",
        "construction_released",
        "hold_required",
        "updates_reset_archived",
        "measure_ready",
        "resume_required",
        "resume_ready",
        "finalize_required",
        "finalized",
        "recovery_required",
        "recovered",
        "cancelled",
        "error",
    }
)
HANDSHAKE_PREDECESSOR = {
    "running": "construction_prepared",
    "construction_reset_archived": "running",
    "baseline_bound": "construction_reset_archived",
    "construction_released": "baseline_bound",
    "hold_required": "construction_released",
    "updates_reset_archived": "hold_required",
    "measure_ready": "updates_reset_archived",
    "resume_required": "measure_ready",
    "resume_ready": "resume_required",
    "finalize_required": "resume_ready",
    "finalized": "finalize_required",
}
RESERVATION_KINDS = frozenset({"measure", "preflight", "recovery"})
RESERVATION_STATES = frozenset({"prepared", "consumed", "recovery", "finalized", "recovered", "cancelled"})
RESERVATION_TERMINAL_STATES = frozenset({"finalized", "recovered", "cancelled"})
MATCH_THRESHOLD = 5.0
MAX_COMMAND_BYTES = 128
MAX_RECEIPT_EVENTS = 256
MAX_STATE_HISTORY = 16
STATE_HISTORY_KEYS = frozenset({"state", "nativeClock"})
STATE_CHAIN = (
    "construction_prepared",
    "running",
    "construction_reset_archived",
    "baseline_bound",
    "construction_released",
    "hold_required",
    "updates_reset_archived",
    "measure_ready",
    "resume_required",
    "resume_ready",
    "finalize_required",
    "finalized",
)
RECOVERY_CHAIN = ("recovery_required", "recovered")
TERMINAL_HANDSHAKE_STATES = frozenset({"recovered", "cancelled"})
CALIBRATION_TERMINAL_STATES = frozenset({"complete", "error"})
TERMINAL_FAILURE_HANDSHAKES = frozenset({"error", "restoration_required", "recovery_required"})


def _is_normal_recovery_reply(
    handshake: Mapping[str, Any] | None, reservation: Mapping[str, Any] | None
) -> bool:
    """Distinguish a live recovery handshake from a terminal measure failure."""

    return bool(
        isinstance(handshake, Mapping)
        and handshake.get("state") == "recovery_required"
        and isinstance(reservation, Mapping)
        and reservation.get("reservationKind") == "recovery"
        and reservation.get("reservationState") == "recovery"
    )


# A known reservation ID remains cancellable for this bounded private grace
# after an admission response is indeterminate.  This is cleanup protection,
# never a claimed server expiry or a retry window for a new admission.
KNOWN_ADMISSION_CLEANUP_GRACE = 5.0


class OperatorError(RuntimeError):
    """A bounded refusal or terminal helper failure."""


class OperatorInterrupted(OperatorError):
    """The helper received a handled termination signal."""


class ReceiptError(OperatorError):
    """Private receipt publication failed."""


class AdmissionIndeterminate(OperatorError):
    """An actor-owned admission may have committed before its reply was lost."""


class ClientTransport(Protocol):
    def request(self, request: dict[str, Any], *, deadline: float | None = None) -> dict[str, Any]: ...


class ScreenAdapter(Protocol):
    def resolve_identity(
        self, *, require_foreground: bool = True, deadline_monotonic: float | None = None
    ) -> Any: ...

    def capture(self, path: Path, *, identity: Any, deadline_monotonic: float | None = None) -> Any: ...

    def pointer_position(self, *, deadline_monotonic: float | None = None) -> tuple[int, int, int]: ...


class InputAdapter(Protocol):
    def key_return(self, *, deadline_monotonic: float | None = None) -> None: ...

    def key_escape(self, *, deadline_monotonic: float | None = None) -> None: ...

    def type_command(self, command: str, *, deadline_monotonic: float | None = None) -> None: ...

    def move_pointer(
        self,
        point: tuple[int, int],
        *,
        identity: Any,
        before_press: Callable[[], None] | None = None,
        deadline_monotonic: float | None = None,
    ) -> None: ...

    def click(
        self,
        match: TemplateMatch,
        *,
        identity: Any,
        before_press: Callable[[], None] | None = None,
        deadline_monotonic: float | None = None,
    ) -> None: ...

    def close(self) -> None: ...


class WatchdogAdapter(Protocol):
    def publish(
        self,
        *,
        groups: set[int],
        buttons: set[int],
        keys: set[str] = frozenset(),
        deadline_monotonic: float | None = None,
    ) -> None: ...

    def clear(self) -> None: ...

    def close(self) -> None: ...


@dataclass(frozen=True)
class ReservationBinding:
    reservation_id: str
    reservation_kind: str
    reservation_state: str
    controller_generation: int
    desktop_lease_held: bool
    guardian_generation: int
    guardian_armed: bool
    owner_pid: int
    owner_process_start: str
    profile_fingerprint: str
    identity_fingerprint: str
    b0: str
    expected_constructor_boot: str
    expires_monotonic: float

    @classmethod
    def from_record(
        cls,
        record: Mapping[str, Any],
        *,
        expected_generation: int | None = None,
        now: float | None = None,
    ) -> ReservationBinding:
        if not isinstance(record, Mapping) or set(record) != set(CANONICAL_RESERVATION_FIELDS):
            raise OperatorError("native reservation binding fields are not canonical")

        def positive_int(name: str) -> int:
            value = record[name]
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise OperatorError(f"native reservation {name} is malformed")
            return value

        reservation_id = record["reservationId"]
        if (
            not isinstance(reservation_id, str)
            or not reservation_id
            or len(reservation_id) > 128
            or "\x00" in reservation_id
        ):
            raise OperatorError("native reservation id is malformed")
        kind = record["reservationKind"]
        state = record["reservationState"]
        if kind not in RESERVATION_KINDS or state not in RESERVATION_STATES:
            raise OperatorError("native reservation kind or state is malformed")
        if state == "finalized" and kind != "measure":
            raise OperatorError("finalized native reservation must be a measure record")
        if state == "recovered" and kind != "recovery":
            raise OperatorError("recovered native reservation must be a recovery record")
        generation = positive_int("controllerGeneration")
        if expected_generation is not None and generation != expected_generation:
            raise OperatorError("native reservation generation changed")
        if not isinstance(record["desktopLeaseHeld"], bool) or not record["desktopLeaseHeld"]:
            raise OperatorError("native construction requires the daemon desktop lease")
        guardian_generation_value = record["guardianGeneration"]
        if (
            isinstance(guardian_generation_value, bool)
            or not isinstance(guardian_generation_value, int)
            or guardian_generation_value < 0
        ):
            raise OperatorError("native reservation guardianGeneration is malformed")
        guardian_generation = guardian_generation_value
        if not isinstance(record["guardianArmed"], bool) or record["guardianArmed"]:
            raise OperatorError("native construction requires an unarmed Guardian")
        owner_pid = positive_int("ownerPid")
        owner_process_start = record["ownerProcessStart"]
        if (
            not isinstance(owner_process_start, str)
            or not owner_process_start
            or len(owner_process_start) > 128
        ):
            raise OperatorError("native reservation owner process start is malformed")
        fingerprints = ("profileFingerprint", "identityFingerprint")
        for name in fingerprints:
            value = record[name]
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(c not in "0123456789abcdef" for c in value)
            ):
                raise OperatorError(f"native reservation {name} is malformed")
        b0 = record["B0"]
        expected_boot = record["expectedConstructorBoot"]
        if not isinstance(b0, str) or not b0 or len(b0.encode()) > 96:
            raise OperatorError("native reservation B0 is malformed")
        if (
            not isinstance(expected_boot, str)
            or not expected_boot.startswith("native-construction:")
            or len(expected_boot.encode()) > 96
            or expected_boot.removeprefix("native-construction:") != b0
        ):
            raise OperatorError("native reservation expected constructor boot is malformed")
        expires = record["expiresMonotonic"]
        if (
            isinstance(expires, bool)
            or not isinstance(expires, (int, float))
            or not math.isfinite(float(expires))
        ):
            raise OperatorError("native reservation expiry is malformed")
        if now is not None and float(expires) <= float(now):
            raise OperatorError("native reservation has expired")
        return cls(
            reservation_id,
            kind,
            state,
            generation,
            record["desktopLeaseHeld"],
            guardian_generation,
            record["guardianArmed"],
            owner_pid,
            owner_process_start,
            record["profileFingerprint"],
            record["identityFingerprint"],
            b0,
            expected_boot,
            float(expires),
        )

    def compare_immutable(self, other: ReservationBinding) -> None:
        fields = (
            "controller_generation",
            "desktop_lease_held",
            "guardian_generation",
            "guardian_armed",
            "owner_pid",
            "owner_process_start",
            "profile_fingerprint",
            "identity_fingerprint",
            "b0",
            "expected_constructor_boot",
        )
        for field_name in fields:
            if getattr(self, field_name) != getattr(other, field_name):
                raise OperatorError(f"native reservation binding changed: {field_name}")

    def validate_transition(self, other: ReservationBinding, *, handshake: str) -> None:
        """Accept reservation-state changes only at their owning boundary."""

        if self.reservation_id != other.reservation_id or self.reservation_kind != other.reservation_kind:
            raise OperatorError("native reservation identity changed")
        if self.reservation_state == other.reservation_state:
            return
        allowed = {
            ("prepared", "consumed", "running"),
            ("consumed", "finalized", "finalized"),
            ("recovery", "recovered", "recovered"),
            ("recovery", "cancelled", "cancelled"),
        }
        if (self.reservation_state, other.reservation_state, handshake) not in allowed:
            raise OperatorError("native reservation changed outside its explicit lifecycle boundary")
        if other.expires_monotonic != self.expires_monotonic and handshake != "running":
            raise OperatorError("native reservation expiry changed outside bootstrap")


@dataclass(frozen=True)
class TemplateRole:
    path: Path
    sha256: str
    threshold: float

    def verify(self) -> None:
        try:
            info = self.path.lstat()
        except OSError as exc:
            raise OperatorError("template artifact is unavailable") from exc
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise OperatorError("template artifact is not a user-owned regular file")
        if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            raise OperatorError("template artifact is group/other writable")
        try:
            digest = hashlib.sha256(self.path.read_bytes()).hexdigest()
        except OSError as exc:
            raise OperatorError("template artifact cannot be read") from exc
        if digest != self.sha256:
            raise OperatorError("template artifact digest changed")


@dataclass(frozen=True)
class TemplateManifest:
    construct: TemplateRole
    restore: TemplateRole

    @classmethod
    def load(cls, path: str | os.PathLike[str]) -> TemplateManifest:
        target = Path(path)
        try:
            info = target.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
                raise OperatorError("template manifest is not a private regular file")
            raw = json.loads(target.read_text(encoding="utf-8"))
        except OperatorError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise OperatorError("template manifest is malformed") from exc
        if not isinstance(raw, dict) or set(raw) != {"construct", "restore"}:
            raise OperatorError("template manifest roles are not canonical")
        roles: dict[str, TemplateRole] = {}
        for name in ("construct", "restore"):
            value = raw[name]
            if not isinstance(value, dict) or set(value) != {"path", "sha256", "threshold"}:
                raise OperatorError("template manifest role fields are not canonical")
            artifact = value["path"]
            digest = value["sha256"]
            threshold = value["threshold"]
            if not isinstance(artifact, str) or not Path(artifact).is_absolute():
                raise OperatorError("template path must be absolute")
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(c not in "0123456789abcdef" for c in digest)
            ):
                raise OperatorError("template digest is malformed")
            if isinstance(threshold, bool) or not isinstance(threshold, (int, float)):
                raise OperatorError("template threshold is malformed")
            if float(threshold) != MATCH_THRESHOLD:
                raise OperatorError("template threshold is not the reviewed fixed value")
            roles[name] = TemplateRole(Path(artifact), digest, float(threshold))
        manifest = cls(roles["construct"], roles["restore"])
        manifest.verify()
        return manifest

    def verify(self) -> None:
        self.construct.verify()
        self.restore.verify()
        if self.construct.path == self.restore.path or self.construct.sha256 == self.restore.sha256:
            raise OperatorError("construct and restore templates must be distinct")
        try:
            with Image.open(self.construct.path) as construct, Image.open(self.restore.path) as restore:
                if construct.size != restore.size:
                    raise OperatorError("construct and restore templates must have the same size")
        except OperatorError:
            raise
        except (OSError, ValueError) as exc:
            raise OperatorError("template artifacts cannot be decoded") from exc


@dataclass(frozen=True)
class TemplateMatch:
    role: str
    accepted: bool
    score: float
    box: tuple[int, int, int, int]
    click: tuple[int, int]
    capture_sha256: str


def normalize_ocr(text: str) -> str:
    if not isinstance(text, str):
        raise OperatorError("OCR result is not text")
    return " ".join(text.lower().split())


def _rectangles_intersect(left: tuple[int, int, int, int], right: tuple[int, int, int, int]) -> bool:
    lx, ly, lw, lh = left
    rx, ry, rw, rh = right
    return lx < rx + rw and rx < lx + lw and ly < ry + rh and ry < ly + lh


class TemplateMatcher:
    """Verified Rust-backed matcher; click coordinates come from its result."""

    def __init__(
        self,
        manifest: TemplateManifest,
        *,
        runtime: TemplateMatcherRuntime | None = None,
        artifact_root: Path | os.PathLike[str] | None = None,
    ):
        if runtime is not None and artifact_root is not None:
            raise OperatorError("template matcher runtime and artifact root are mutually exclusive")
        manifest.verify()
        self.manifest = manifest
        self._runtime = runtime or TemplateMatcherRuntime(artifact_root)

    @property
    def source_bindings(self) -> dict[str, str]:
        return self._runtime.source_bindings

    def close(self) -> None:
        closer = getattr(self._runtime, "close", None)
        if callable(closer):
            closer()

    @staticmethod
    def _validate_result(
        result: Any,
        *,
        role: str,
        region: tuple[int, int, int, int],
        template_size: tuple[int, int],
    ) -> tuple[str, int, tuple[int, int, int, int]]:
        status = getattr(result, "status", None)
        sad = getattr(result, "sad", None)
        count = getattr(result, "count", None)
        box = getattr(result, "box", None)
        if not isinstance(status, str):
            raise OperatorError("native template matcher returned an unknown status")
        if status == "ambiguous":
            raise OperatorError(f"template role {role} has an ambiguous match")
        if status not in {"matched", "rejected"}:
            raise OperatorError("native template matcher returned an unknown status")
        if (
            isinstance(sad, bool)
            or not isinstance(sad, int)
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count <= 0
            or not isinstance(box, tuple)
            or len(box) != 4
            or any(isinstance(value, bool) or not isinstance(value, int) for value in box)
        ):
            raise OperatorError("native template matcher returned malformed result")
        expected_count = template_size[0] * template_size[1] * 3
        if count != expected_count or sad < 0 or sad > 255 * count:
            raise OperatorError("native template matcher returned an inconsistent result")
        x, y, width, height = box
        left, top, region_width, region_height = region
        if (width, height) != template_size or x < left or y < top:
            raise OperatorError("native template matcher returned inconsistent geometry")
        if x + width > left + region_width or y + height > top + region_height:
            raise OperatorError("native template matcher returned out-of-region geometry")
        threshold = 5 * count
        if status == "matched" and sad > threshold or status == "rejected" and sad <= threshold:
            raise OperatorError("native template matcher returned an inconsistent decision")
        return status, sad, box

    def match(
        self,
        image_path: Path,
        role: str,
        *,
        region: tuple[int, int, int, int] | None = None,
        deadline_monotonic: float,
    ) -> TemplateMatch:
        if role not in {"construct", "restore"}:
            raise OperatorError("template role is not closed")
        deadline = float(deadline_monotonic)
        expected = self.manifest.construct if role == "construct" else self.manifest.restore
        expected.verify()
        try:
            with Image.open(expected.path) as template_raw, Image.open(image_path) as image_raw:
                template = template_raw.convert("RGB")
                image = image_raw.convert("RGB")
                if template.width <= 0 or template.height <= 0:
                    raise OperatorError("template has no pixels")
                if template.width > image.width or template.height > image.height:
                    raise OperatorError("template is larger than the fresh capture")
                if region is None:
                    left, top, region_width, region_height = 0, 0, image.width, image.height
                else:
                    left, top, region_width, region_height = region
                    if (
                        left < 0
                        or top < 0
                        or region_width <= 0
                        or region_height <= 0
                        or left + region_width > image.width
                        or top + region_height > image.height
                    ):
                        raise OperatorError("template search region is outside the fresh capture")
                if template.width > region_width or template.height > region_height:
                    raise OperatorError("template is larger than the bounded search region")
                result = self._runtime.search(
                    image.tobytes(),
                    image.size,
                    template.tobytes(),
                    template.size,
                    (left, top, region_width, region_height),
                    deadline_monotonic=deadline,
                )
                status, sad, box = self._validate_result(
                    result,
                    role=role,
                    region=(left, top, region_width, region_height),
                    template_size=template.size,
                )
                digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
                x, y, width, height = box
                return TemplateMatch(
                    role,
                    status == "matched",
                    sad / (width * height * 3),
                    box,
                    (x + width // 2, y + height // 2),
                    digest,
                )
        except OperatorError:
            raise
        except MatcherError as exc:
            raise OperatorError("native template matching failed") from exc
        except (OSError, ValueError) as exc:
            raise OperatorError("fresh template matching failed") from exc

    def require_exclusive(
        self,
        image_path: Path,
        positive_role: str,
        *,
        region: tuple[int, int, int, int] | None = None,
        deadline_monotonic: float,
    ) -> TemplateMatch:
        if positive_role not in {"construct", "restore"}:
            raise OperatorError("template role is not closed")
        deadline = float(deadline_monotonic)
        positive = self.match(
            image_path,
            positive_role,
            region=region,
            deadline_monotonic=deadline,
        )
        other_role = "restore" if positive_role == "construct" else "construct"
        # Both fixed controls are visible on the accepted panel.  The rival
        # proof therefore scores only the same candidate box; a global rival
        # search would reject every legitimate two-button panel.
        negative = self._match_at(
            image_path,
            other_role,
            positive.box,
            deadline_monotonic=deadline,
        )
        if not positive.accepted or negative.accepted:
            raise OperatorError("fresh target template is not role-exclusive")
        return positive

    def _match_at(
        self,
        image_path: Path,
        role: str,
        box: tuple[int, int, int, int],
        *,
        deadline_monotonic: float,
    ) -> TemplateMatch:
        expected = self.manifest.construct if role == "construct" else self.manifest.restore
        expected.verify()
        deadline = float(deadline_monotonic)
        left, top, width, height = box
        try:
            with Image.open(expected.path) as template_raw, Image.open(image_path) as image_raw:
                template = template_raw.convert("RGB")
                image = image_raw.convert("RGB")
                if (width, height) != template.size or left < 0 or top < 0:
                    raise OperatorError("template candidate box is malformed")
                if left + width > image.width or top + height > image.height:
                    raise OperatorError("template candidate box is outside the capture")
                result = self._runtime.score_at(
                    image.tobytes(),
                    image.size,
                    template.tobytes(),
                    template.size,
                    box,
                    deadline_monotonic=deadline,
                )
                status, sad, result_box = self._validate_result(
                    result,
                    role=role,
                    region=box,
                    template_size=template.size,
                )
                if result_box != box:
                    raise OperatorError("native SCORE_AT geometry differs from its request")
                digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
                return TemplateMatch(
                    role,
                    status == "matched",
                    sad / (width * height * 3),
                    box,
                    (left + width // 2, top + height // 2),
                    digest,
                )
        except OperatorError:
            raise
        except MatcherError as exc:
            raise OperatorError("native candidate-role matching failed") from exc
        except (OSError, ValueError) as exc:
            raise OperatorError("fresh candidate-role matching failed") from exc

    def require_dual_positive(
        self,
        image_path: Path,
        *,
        region: tuple[int, int, int, int] | None = None,
        deadline_monotonic: float,
    ) -> tuple[TemplateMatch, TemplateMatch]:
        deadline = float(deadline_monotonic)
        construct = self.match(
            image_path,
            "construct",
            region=region,
            deadline_monotonic=deadline,
        )
        restore = self.match(
            image_path,
            "restore",
            region=region,
            deadline_monotonic=deadline,
        )
        if not construct.accepted or not restore.accepted:
            raise OperatorError("fresh post-reload controls did not both match")
        if _rectangles_intersect(construct.box, restore.box):
            raise OperatorError("fresh post-reload controls overlap")
        return construct, restore


class DaemonClient:
    """Closed native IPC client with no retry or arbitrary request surface."""

    def __init__(
        self,
        socket_path: Path,
        *,
        request_fn: Callable[..., dict[str, Any]] = _socket_request,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = 45.0,
    ) -> None:
        self.socket_path = Path(socket_path)
        self._request_fn = request_fn
        self._clock = clock
        self._timeout = timeout

    @classmethod
    def from_config(cls, config_path: str | os.PathLike[str]) -> DaemonClient:
        config = load_config(config_path)
        return cls(config.socket)

    def request(self, request: dict[str, Any], *, deadline: float | None = None) -> dict[str, Any]:
        if not isinstance(request, dict) or request.get("command") not in COMMANDS:
            raise OperatorError("native helper IPC command is not closed")
        command = request["command"]
        if set(request) != IPC_FIELDS[command]:
            raise OperatorError("native helper IPC request fields are not canonical")
        generation = request.get("generation")
        if command not in {"calibration_status", "status"}:
            if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
                raise OperatorError("native helper IPC generation is malformed")
        if command in IPC_COMMANDS:
            reservation_id = request.get("reservationId")
            if command == "native_construction_recovery_begin":
                if not isinstance(reservation_id, str) or len(reservation_id) > 128:
                    raise OperatorError("native helper recovery reservation is malformed")
            elif command not in {"native_construction_prepare", "native_construction_control_preflight"}:
                if not isinstance(reservation_id, str) or not reservation_id or len(reservation_id) > 128:
                    raise OperatorError("native helper reservation is malformed")
            expected_boot = request.get("expectedConstructorBoot")
            if command == "native_construction_bootstrap" and (
                not isinstance(expected_boot, str) or not expected_boot or len(expected_boot.encode()) > 96
            ):
                raise OperatorError("native helper expected constructor boot is malformed")
        if command == "pause" and request.get("reason") != "native construction recovery":
            raise OperatorError("native helper pause reason is not fixed")
        if deadline is None:
            deadline = self._clock() + self._timeout
        remaining = float(deadline) - self._clock()
        if remaining <= 0:
            raise OperatorError("native helper IPC deadline expired")
        try:
            transport_kwargs: dict[str, Any] = {"timeout": remaining}
            if self._request_fn is _socket_request:
                transport_kwargs["response_deadline"] = float(deadline)
            response = self._request_fn(self.socket_path, request, **transport_kwargs)
        except Exception as exc:
            raise OperatorError("native helper IPC transport failed") from exc
        if not isinstance(response, dict) or response.get("ok") is not True:
            raise OperatorError("native helper IPC refused the operation")
        return response

    def prepare(self, generation: int, *, preflight: bool = False) -> dict[str, Any]:
        command = "native_construction_control_preflight" if preflight else "native_construction_prepare"
        return self.request({"command": command, "generation": generation})

    def bootstrap(self, generation: int, reservation: ReservationBinding) -> dict[str, Any]:
        return self.request(
            {
                "command": "native_construction_bootstrap",
                "generation": generation,
                "reservationId": reservation.reservation_id,
                "expectedConstructorBoot": reservation.expected_constructor_boot,
            }
        )

    def recheck(
        self, generation: int, reservation_id: str, *, deadline: float | None = None
    ) -> dict[str, Any]:
        return self.request(
            {
                "command": "native_construction_recheck",
                "generation": generation,
                "reservationId": reservation_id,
            },
            deadline=deadline,
        )

    def finalize_admit(self, generation: int, reservation_id: str) -> dict[str, Any]:
        return self.request(
            {
                "command": "native_construction_finalize_admit",
                "generation": generation,
                "reservationId": reservation_id,
            }
        )

    def recovery_begin(self, generation: int, reservation_id: str = "") -> dict[str, Any]:
        return self.request(
            {
                "command": "native_construction_recovery_begin",
                "generation": generation,
                "reservationId": reservation_id,
            }
        )

    def recovery_complete(self, generation: int, reservation_id: str) -> dict[str, Any]:
        return self.request(
            {
                "command": "native_construction_recovery_complete",
                "generation": generation,
                "reservationId": reservation_id,
            }
        )

    def recovery_cancel(self, generation: int, reservation_id: str) -> dict[str, Any]:
        return self.request(
            {
                "command": "native_construction_recovery_cancel",
                "generation": generation,
                "reservationId": reservation_id,
            }
        )

    def native_calibrate(self, generation: int, *, deadline: float | None = None) -> dict[str, Any]:
        return self.request({"command": "native_calibrate", "generation": generation}, deadline=deadline)

    def calibration_status(self, *, deadline: float | None = None) -> dict[str, Any]:
        return self.request({"command": "calibration_status"}, deadline=deadline)

    def status(self, *, deadline: float | None = None) -> dict[str, Any]:
        return self.request({"command": "status"}, deadline=deadline)

    def pause(self, generation: int, *, deadline: float | None = None) -> dict[str, Any]:
        return self.request(
            {
                "command": "pause",
                "generation": generation,
                "reason": "native construction recovery",
            },
            deadline=deadline,
        )


class X11Screen:
    """Production screen adapter; no lease or Guardian is acquired here."""

    def __init__(self, config_path: str | os.PathLike[str], root: Path):
        self.config_path = Path(config_path)
        self.tool: PinnedInputTool | None = None
        self.backend: X11Backend | None = None
        try:
            self.config = load_config(config_path)
            # The production factory checks the observer pins before loading
            # the input image, so an incomplete observer configuration fails
            # before any input/capture resource is acquired.
            self.backend = X11Backend.production(self.config.profile)
            self.tool = self.backend.input_tool
            self.input_environment = {
                "DISPLAY": self.config.profile.display,
                "XAUTHORITY": self.backend.xauthority,
            }
            if not self.input_environment["XAUTHORITY"]:
                raise OperatorError("profile Xauthority is unavailable for owned input")
        except BaseException:
            if self.backend is not None:
                try:
                    self.backend.close()
                finally:
                    self.backend.input_tool.close()
                    self.backend = None
                    self.tool = None
            raise
        self.root = root
        self.chat_crop = (30, 938, *CHAT_CROP_SIZE)
        self.agent_crop = self.config.profile.canvas_crop

    def resolve_identity(
        self, *, require_foreground: bool = True, deadline_monotonic: float | None = None
    ) -> Any:
        return self.backend.resolve_identity(
            require_foreground=require_foreground, deadline_monotonic=deadline_monotonic
        )

    def capture(self, path: Path, *, identity: Any, deadline_monotonic: float | None = None) -> Any:
        return self.backend.capture(path, identity=identity, deadline_monotonic=deadline_monotonic)

    def pointer_position(self, *, deadline_monotonic: float | None = None) -> tuple[int, int, int]:
        return self.backend.pointer_position(deadline_monotonic=deadline_monotonic)

    def close(self) -> None:
        if self.backend is not None:
            self.backend.close()
            self.backend = None
        if self.tool is not None:
            self.tool.close()
            self.tool = None

    def wait_reload_boundary(
        self, *, deadline_monotonic: float | None = None, proof: str = "ordinary"
    ) -> Any:
        """Wait for one fresh profile-bound absence or ordinary status frame."""

        if proof not in {"absence", "ordinary"}:
            raise OperatorError("native reload readiness proof is not closed")
        deadline = deadline_monotonic
        if deadline is None or not math.isfinite(float(deadline)):
            raise OperatorError("native reload readiness deadline is unavailable")
        crop = self.config.profile.canvas_crop
        if (
            not isinstance(crop, tuple)
            or len(crop) != 4
            or any(isinstance(value, bool) or not isinstance(value, int) for value in crop)
            or crop[2] <= 0
            or crop[3] <= 0
        ):
            raise OperatorError("native reload canvas crop is malformed")
        last_error: Exception | None = None
        while time.monotonic() < float(deadline):
            path: Path | None = None
            try:
                identity_before = self.resolve_identity(
                    require_foreground=True, deadline_monotonic=float(deadline)
                )
                self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
                os.chmod(self.root, 0o700)
                fd, name = tempfile.mkstemp(prefix=".reload-boundary-", suffix=".png", dir=self.root)
                os.close(fd)
                path = Path(name)
                os.chmod(path, 0o600)
                captured = self.capture(path, identity=identity_before, deadline_monotonic=float(deadline))
                identity_after = self.resolve_identity(
                    require_foreground=True, deadline_monotonic=float(deadline)
                )
                before_fingerprint = getattr(identity_before, "fingerprint", None)
                if (
                    getattr(captured, "fingerprint", before_fingerprint) != before_fingerprint
                    or getattr(identity_after, "fingerprint", before_fingerprint) != before_fingerprint
                ):
                    raise OperatorError("reload boundary identity changed during capture")
                with Image.open(path) as raw:
                    image = raw.convert("RGB")
                    if image.size != (
                        self.config.profile.client_width,
                        self.config.profile.client_height,
                    ):
                        raise OperatorError("reload boundary capture size is not profile-bound")
                    left, top, width, height = crop
                    if left < 0 or top < 0 or left + width > image.width or top + height > image.height:
                        raise OperatorError("reload boundary crop is outside the profile capture")
                    cropped = image.crop((left, top, left + width, top + height))
                    if proof == "absence":
                        temporary = path.with_name(f".{path.name}.crop")
                        try:
                            cropped.save(temporary, format="PNG")
                            os.chmod(temporary, 0o600)
                            if not _strict_native_absence_image(temporary, expected_size=(width, height)):
                                raise OperatorError("reload boundary Agent absence was not proven")
                        finally:
                            try:
                                temporary.unlink()
                            except OSError:
                                pass
                        return identity_after
                    decoder = PixelDecoder(crop=(0, 0, width, height))
                    sample = decoder.decode(
                        cropped,
                        monotonic=time.monotonic(),
                        window_identity=before_fingerprint or "",
                    )
                    frame = sample.frame
                    if sample.kind != "FRESH" or frame is None or not frame.boot:
                        raise OperatorError("reload boundary ordinary frame is not fresh")
                    if frame.boot.startswith("native-construction:"):
                        raise OperatorError("reload boundary ordinary frame has a construction boot")
                    snapshot = frame.snapshot.value
                    render = snapshot.get("render")
                    scanner = snapshot.get("scanner")
                    fence = scanner.get("scheduleFence") if isinstance(scanner, dict) else None
                    if (
                        not isinstance(render, dict)
                        or render.get("nativePhase") != "normal"
                        or render.get("nativeOrdinal") != 0
                        or not isinstance(fence, dict)
                        or fence.get("state") != "inactive"
                    ):
                        raise OperatorError("reload boundary ordinary frame is not idle")
                    return identity_after
            except (DriverError, OperatorError, OSError, ValueError, TypeError) as exc:
                last_error = exc
            finally:
                if path is not None:
                    try:
                        path.unlink()
                    except OSError:
                        pass
            remaining = float(deadline) - time.monotonic()
            if remaining > 0:
                time.sleep(min(0.1, remaining))
        raise last_error or OperatorError("reload boundary did not settle before its deadline")


def _strict_native_absence_image(path: Path, *, expected_size: tuple[int, int]) -> bool:
    """Call the accepted daemon's pure image-only absence classifier lazily."""

    try:
        daemon = importlib.import_module("crowley.session.daemon")
        checker = daemon._strict_native_absence_image
    except (ImportError, AttributeError) as exc:
        raise OperatorError("accepted native absence classifier is unavailable") from exc
    try:
        result = checker(path, expected_size=expected_size)
    except Exception as exc:
        raise OperatorError("native Agent absence proof failed") from exc
    if not isinstance(result, bool):
        raise OperatorError("native Agent absence classifier returned a malformed result")
    return result


def _emitter_entry(
    image_fd: Any,
    gate_read: Any,
    status_write: Any,
    action: tuple[str, ...] = ("mousedown", "1"),
    release_action: tuple[str, ...] = ("mouseup", "1"),
    environment: Mapping[str, str] | None = None,
    action_timeout: float = 2.0,
    deadline_monotonic: float | None = None,
) -> None:
    """Wait behind an owner gate, then emit one fixed pinned-tool action."""

    if hasattr(image_fd, "detach"):
        image_fd = image_fd.detach()
    if hasattr(gate_read, "detach"):
        gate_read = gate_read.detach()
    if hasattr(status_write, "detach"):
        status_write = status_write.detach()
    try:
        os.setsid()
        image = f"/proc/self/fd/{image_fd}"
        ready_timeout = _bounded_timeout(deadline_monotonic, 2.0)
        _write_bounded(status_write, b"READY\n", timeout=ready_timeout)
        command = _read_bounded(gate_read, 32, timeout=_bounded_timeout(deadline_monotonic, 2.0))
        if command != b"GO\n":
            return
        subprocess.run(
            [image, *action],
            check=True,
            timeout=_bounded_timeout(deadline_monotonic, action_timeout),
            pass_fds=(image_fd,),
            env=dict(environment or {}),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        _write_bounded(status_write, b"PRESSED\n", timeout=_bounded_timeout(deadline_monotonic, 2.0))
        # Safety release remains available after the causal phase deadline.
        command = _read_bounded(gate_read, 32, timeout=2.0)
        if command != b"RELEASE\n":
            return
        if release_action:
            subprocess.run(
                [image, *release_action],
                check=True,
                timeout=action_timeout,
                pass_fds=(image_fd,),
                env=dict(environment or {}),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        _write_bounded(status_write, b"RELEASED\n", timeout=2.0)
    except (OSError, TimeoutError, EOFError, ValueError, subprocess.SubprocessError):
        try:
            _write_bounded(status_write, b"ERROR\n", timeout=2.0)
        except (OSError, TimeoutError, ValueError):
            pass
    finally:
        for descriptor in (gate_read, status_write, image_fd):
            try:
                os.close(descriptor)
            except OSError:
                pass


def _read_bounded(fd: int, size: int, *, timeout: float) -> bytes:
    ready, _write, _error = select.select([fd], [], [], timeout)
    if not ready:
        raise TimeoutError("bounded native helper pipe read expired")
    value = os.read(fd, size)
    if not value:
        raise EOFError("native helper pipe closed")
    return value


def _read_record_bounded(fd: int, limit: int, *, timeout: float) -> bytes:
    """Read one newline-framed private actor response with a hard bound."""

    deadline = time.monotonic() + timeout
    data = bytearray()
    while len(data) <= limit:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("bounded actor response read expired")
        ready, _write, _error = select.select([fd], [], [], remaining)
        if not ready:
            raise TimeoutError("bounded actor response read expired")
        chunk = os.read(fd, min(4096, limit + 1 - len(data)))
        if not chunk:
            raise EOFError("actor response pipe closed")
        data.extend(chunk)
        newline = data.find(b"\n")
        if newline >= 0:
            if newline != len(data) - 1:
                raise OperatorError("actor response contains trailing data")
            return bytes(data[:newline])
    raise ValueError("actor response exceeds its bound")


def _write_bounded(fd: int, value: bytes, *, timeout: float) -> None:
    _read, writable, _error = select.select([], [fd], [], timeout)
    if not writable:
        raise TimeoutError("bounded native helper pipe write expired")
    written = os.write(fd, value)
    if written != len(value):
        raise OSError("native helper pipe write was partial")


def _bounded_timeout(deadline: float | None, maximum: float) -> float:
    if deadline is None:
        return maximum
    remaining = float(deadline) - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("native input deadline expired")
    return min(maximum, remaining)


def _publish_watchdog(
    watchdog: WatchdogAdapter,
    *,
    groups: set[int],
    buttons: set[int],
    keys: set[str],
    deadline_monotonic: float | None,
) -> None:
    """Publish ownership with the selected phase bound when supported."""

    try:
        watchdog.publish(
            groups=groups,
            buttons=buttons,
            keys=keys,
            deadline_monotonic=deadline_monotonic,
        )
    except TypeError as exc:
        # Narrow test adapters from earlier helper fixtures predate the
        # optional deadline argument.  Production Watchdog always accepts it;
        # retain compatibility only for that explicit signature mismatch.
        if "deadline_monotonic" not in str(exc):
            raise
        watchdog.publish(groups=groups, buttons=buttons, keys=keys)


def _bounded_process_join(process: multiprocessing.Process, timeout: float) -> bool:
    """Wait for a child without letting unrelated SIGCHLD wakeups extend it."""

    deadline = time.monotonic() + timeout
    while process.is_alive():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.05, remaining))
    process.join(timeout=0.0)
    return True


def _action_timeout(action: Sequence[str], *, deadline: float | None = None) -> float:
    """Bound one fixed input action using its vendored timing contract."""

    action = tuple(action)
    timeout = 2.0
    if len(action) == 4 and tuple(action[:3]) == ("type", "--delay", "130"):
        command = action[3]
        if not isinstance(command, str) or command not in FIXED_COMMAND_SET:
            raise OperatorError("typed input action is not fixed and allowlisted")
        timeout = min(8.0, 0.130 * len(command) + 1.0)
    elif action and action[0] == "sleep":
        try:
            timeout = pointer_action_timeout(tuple(action))
        except PointerExecutionError as exc:
            raise OperatorError(f"pointer motion action is not bounded: {exc}") from exc
    elif (
        len(action) == 6
        and action[0:2] == ("mousedown", "1")
        and action[2] == "sleep"
        and action[4:] == ("mouseup", "1")
    ):
        try:
            dwell = float(action[3])
        except (TypeError, ValueError) as exc:
            raise OperatorError("pointer click dwell is malformed") from exc
        if not math.isfinite(dwell) or not 0.05 <= dwell <= 0.35:
            raise OperatorError("pointer click dwell is outside its human gesture bound")
        # The pinned tool waits through the planned dwell, then reports
        # completion to the owned parent after the desktop has accepted the
        # button sequence. Reserve bounded subprocess/desktop ACK overhead in
        # addition to dwell; the complete plan is checked against the
        # constructor/evidence deadline before any pointer motion begins.
        timeout = dwell + _POINTER_CLICK_ACK_RESERVE_SECONDS
    if deadline is not None:
        remaining = float(deadline) - time.monotonic()
        if remaining < timeout:
            raise OperatorError("native input action cannot fit before its deadline")
    return timeout


def _text_keysyms(command: str) -> set[str]:
    """Return the exact closed key names a fixed text action can hold."""

    keysyms = {"space" if char == " " else "slash" if char == "/" else char for char in command}
    if not keysyms:
        raise OperatorError("typed input cannot publish an empty key ownership set")
    return keysyms


def _detach_standard_fds() -> None:
    null_fd = os.open(os.devnull, os.O_RDWR)
    try:
        for target in (0, 1, 2):
            os.dup2(null_fd, target)
    finally:
        if null_fd > 2:
            os.close(null_fd)


def _close_inherited_nonprotocol_fds(keep: set[int]) -> None:
    """Close descriptors inherited by a helper child outside its protocol."""

    try:
        descriptors = os.listdir("/proc/self/fd")
    except OSError:
        return
    for name in descriptors:
        try:
            descriptor = int(name)
        except ValueError:
            continue
        if descriptor in keep or descriptor <= 2:
            continue
        try:
            os.close(descriptor)
        except OSError:
            pass


def _watchdog_process_entry(*args: Any) -> None:
    os.setsid()
    _detach_standard_fds()
    _watchdog_entry(*args)


def _cancellation_process_entry(*args: Any) -> None:
    os.setsid()
    _detach_standard_fds()
    normalized = list(args)
    protocol_fds: set[int] = set()
    for index in (2, 3, 4, 9):
        descriptor = normalized[index]
        if hasattr(descriptor, "detach"):
            descriptor = descriptor.detach()
            normalized[index] = descriptor
        if isinstance(descriptor, int) and descriptor >= 0:
            protocol_fds.add(descriptor)
    _close_inherited_nonprotocol_fds(protocol_fds)
    _cancellation_entry(*normalized)


def _discard_spawn_child(process: multiprocessing.Process) -> None:
    """Detach a retained actor from multiprocessing's implicit atexit join."""

    from multiprocessing import process as multiprocessing_process

    children = multiprocessing_process._children
    if not hasattr(children, "discard"):
        raise OperatorError("multiprocessing child registry is unavailable")
    children.discard(process)


class OwnedEmitter:
    """A one-shot gated emitter whose process group is watchdog-owned."""

    def __init__(
        self,
        tool: PinnedInputTool,
        *,
        context: Any | None = None,
        action: Sequence[str] = ("mousedown", "1"),
        release_action: Sequence[str] = ("mouseup", "1"),
        environment: Mapping[str, str] | None = None,
        deadline_monotonic: float | None = None,
        ready_timeout: float = 2.0,
    ):
        self.tool = tool
        self._context = context
        self.action = tuple(action)
        self.release_action = tuple(release_action)
        self.environment = dict(environment or {})
        self._deadline_monotonic = deadline_monotonic
        self._action_timeout = _action_timeout(self.action, deadline=deadline_monotonic)
        self._ready_timeout = min(2.0, max(0.1, float(ready_timeout)))
        self._gate_read = self._gate_write = self._status_read = self._status_write = -1
        try:
            self._gate_read, self._gate_write = os.pipe()
            self._status_read, self._status_write = os.pipe()
        except BaseException:
            for descriptor in (self._gate_read, self._gate_write, self._status_read, self._status_write):
                if descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            raise
        self._process: multiprocessing.Process | None = None
        self.pgid: int | None = None

    def start(self) -> None:
        from multiprocessing.reduction import DupFd

        context = self._context or multiprocessing.get_context("spawn")
        self._process = context.Process(
            target=_emitter_entry,
            args=(
                DupFd(self.tool.image.fd),
                DupFd(self._gate_read),
                DupFd(self._status_write),
                self.action,
                self.release_action,
                self.environment,
                self._action_timeout,
                self._deadline_monotonic,
            ),
            name="crowley-native-calibration-emitter",
            daemon=False,
        )
        try:
            self._process.start()
            os.close(self._gate_read)
            os.close(self._status_write)
            self._gate_read = self._status_write = -1
        except BaseException:
            self.abort()
            raise
        try:
            ready = _read_bounded(
                self._status_read,
                64,
                timeout=_bounded_timeout(self._deadline_monotonic, self._ready_timeout),
            )
        except (OSError, TimeoutError, EOFError) as exc:
            self.abort()
            raise OperatorError("native input emitter readiness failed") from exc
        if ready != b"READY\n":
            self.abort()
            raise OperatorError("native input emitter did not become ready")
        try:
            self.pgid = os.getpgid(self._process.pid)
        except OSError as exc:
            self.abort()
            raise OperatorError("native input emitter process group is unavailable") from exc
        if self.pgid <= 0:
            self.abort()
            raise OperatorError("native input emitter process group is malformed")

    def press(self) -> None:
        if self._gate_write < 0:
            raise OperatorError("native input emitter is not open")
        try:
            # Re-evaluate immediately before GO.  Spawn/READY and watchdog
            # publication consume the phase budget; do not emit a physical
            # action that cannot finish inside the selected bound.
            _action_timeout(self.action, deadline=self._deadline_monotonic)
            _write_bounded(
                self._gate_write,
                b"GO\n",
                timeout=_bounded_timeout(self._deadline_monotonic, self._action_timeout),
            )
            status = _read_bounded(
                self._status_read,
                64,
                timeout=_bounded_timeout(self._deadline_monotonic, self._action_timeout),
            )
        except (OSError, TimeoutError, EOFError) as exc:
            raise OperatorError("native input emitter press acknowledgement failed") from exc
        if status != b"PRESSED\n":
            raise OperatorError("native input emitter did not confirm press")

    def release(self) -> None:
        if self._gate_write >= 0:
            try:
                _write_bounded(self._gate_write, b"RELEASE\n", timeout=self._action_timeout)
            except (OSError, TimeoutError, EOFError) as exc:
                raise OperatorError("native input emitter release dispatch failed") from exc
        try:
            status = _read_bounded(self._status_read, 64, timeout=self._action_timeout)
        except (OSError, TimeoutError, EOFError) as exc:
            raise OperatorError("native input emitter release acknowledgement failed") from exc
        if status != b"RELEASED\n":
            raise OperatorError("native input emitter did not confirm release")
        self._join()

    def abort(self) -> None:
        if self._gate_write >= 0:
            try:
                os.close(self._gate_write)
            except OSError:
                pass
            self._gate_write = -1
        self._join(deadline=self._deadline_monotonic)

    def _join(self, *, deadline: float | None = None) -> None:
        process = self._process
        if process is not None and getattr(process, "pid", None) is not None:
            timeout = 3.0
            if deadline is not None:
                timeout = max(0.0, min(timeout, float(deadline) - time.monotonic()))
            process.join(timeout=timeout)
            if process.is_alive():
                process.kill()
                process.join(timeout=1.0)
        for descriptor_name in ("_gate_read", "_gate_write", "_status_read", "_status_write"):
            descriptor = getattr(self, descriptor_name)
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                setattr(self, descriptor_name, -1)


class FixedInput:
    """Closed input vocabulary used by the helper."""

    def __init__(
        self,
        tool: PinnedInputTool,
        *,
        watchdog: WatchdogAdapter,
        run: Callable[..., Any] = subprocess.run,
        emitter_factory: Callable[[], OwnedEmitter] | None = None,
        environment: Mapping[str, str] | None = None,
    ):
        self.tool = tool
        self.watchdog = watchdog
        self._run = run
        self.environment = dict(environment or {})
        self._emitter_factory = emitter_factory
        self._closed = False
        self.cleanup_failures: list[str] = []

    def _new_emitter(
        self,
        action: Sequence[str],
        *,
        release_action: Sequence[str] = (),
        deadline_monotonic: float | None = None,
        ready_timeout: float = 2.0,
    ) -> OwnedEmitter:
        if self._emitter_factory is not None:
            emitter = self._emitter_factory()
            if not isinstance(emitter, OwnedEmitter):
                # Test adapters may provide the same narrow emitter protocol;
                # production still always uses the owned implementation.
                return emitter
            return emitter
        return OwnedEmitter(
            self.tool,
            action=action,
            release_action=release_action,
            environment=self.environment,
            deadline_monotonic=deadline_monotonic,
            ready_timeout=ready_timeout,
        )

    def _emit_keyboard(self, key: str, *, deadline_monotonic: float | None = None) -> None:
        if key not in {"Return", "Escape"}:
            raise OperatorError("input key is not closed")
        emitter = self._new_emitter(("key", key), deadline_monotonic=deadline_monotonic)
        go_attempted = False
        try:
            emitter.start()
            if emitter.pgid is None:
                raise OperatorError("owned keyboard emitter has no process group")
            _publish_watchdog(
                self.watchdog,
                groups={emitter.pgid},
                buttons=set(),
                keys={key},
                deadline_monotonic=deadline_monotonic,
            )
            _action_timeout(("key", key), deadline=deadline_monotonic)
            go_attempted = True
            emitter.press()
            emitter.release()
            self.watchdog.clear()
        except BaseException as primary:
            release = getattr(self.watchdog, "release_now", None)
            if go_attempted and callable(release):
                try:
                    release()
                except BaseException:
                    self.cleanup_failures.append("keyboard.release_now")
            elif not go_attempted:
                try:
                    self.watchdog.clear()
                except BaseException:
                    self.cleanup_failures.append("keyboard.clear_before_go")
            try:
                emitter.abort()
            except BaseException:
                self.cleanup_failures.append("keyboard.abort")
            raise primary.with_traceback(primary.__traceback__) from None

    def key_return(self, *, deadline_monotonic: float | None = None) -> None:
        self._emit_keyboard("Return", deadline_monotonic=deadline_monotonic)

    def key_escape(self, *, deadline_monotonic: float | None = None) -> None:
        self._emit_keyboard("Escape", deadline_monotonic=deadline_monotonic)

    def type_command(self, command: str, *, deadline_monotonic: float | None = None) -> None:
        if command not in FIXED_COMMAND_SET or len(command.encode()) > MAX_COMMAND_BYTES:
            raise OperatorError("chat command is not fixed and allowlisted")
        emitter = self._new_emitter(
            ("type", "--delay", "130", command), deadline_monotonic=deadline_monotonic
        )
        go_attempted = False
        try:
            emitter.start()
            if emitter.pgid is None:
                raise OperatorError("owned text emitter has no process group")
            _publish_watchdog(
                self.watchdog,
                groups={emitter.pgid},
                buttons=set(),
                keys=_text_keysyms(command),
                deadline_monotonic=deadline_monotonic,
            )
            _action_timeout(("type", "--delay", "130", command), deadline=deadline_monotonic)
            go_attempted = True
            emitter.press()
            emitter.release()
            self.watchdog.clear()
        except BaseException as primary:
            release = getattr(self.watchdog, "release_now", None)
            if go_attempted and callable(release):
                try:
                    release()
                except BaseException:
                    self.cleanup_failures.append("text.release_now")
            elif not go_attempted:
                try:
                    self.watchdog.clear()
                except BaseException:
                    self.cleanup_failures.append("text.clear_before_go")
            try:
                emitter.abort()
            except BaseException:
                self.cleanup_failures.append("text.abort")
            raise primary.with_traceback(primary.__traceback__) from None

    def _emit_pointer_motion(
        self,
        gesture: HumanPointerPlan,
        *,
        identity: Any,
        before_dispatch: Callable[[], None] | None,
        deadline_monotonic: float | None,
        label: str,
    ) -> dict[str, int]:
        geometry = getattr(identity, "child_geometry", None)
        try:
            action = native_pointer_motion_action(gesture.trajectory, geometry)
            action_timeout = pointer_action_timeout(action)
        except (PointerExecutionError, TypeError) as exc:
            raise OperatorError(f"{label} path is not a bounded gesture: {exc}") from exc
        if deadline_monotonic is not None:
            remaining = float(deadline_monotonic) - time.monotonic()
            # The child command has one plan-derived execution timeout, plus
            # its READY bound and a bounded ownership/ack margin. Do not
            # charge the same command timeout twice: press() already waits
            # for that command's completion acknowledgement.
            required = 0.75 + action_timeout + 0.25
            if remaining < required:
                raise OperatorError(f"{label} gesture cannot fit before its deadline")
        emitter = self._new_emitter(
            action,
            deadline_monotonic=deadline_monotonic,
            ready_timeout=0.75,
        )
        started_ns = time.monotonic_ns()
        try:
            emitter.start()
            if emitter.pgid is None:
                raise OperatorError(f"{label} emitter has no process group")
            _publish_watchdog(
                self.watchdog,
                groups={emitter.pgid},
                buttons=set(),
                keys=set(),
                deadline_monotonic=deadline_monotonic,
            )
            if before_dispatch is not None:
                before_dispatch()
            _action_timeout(action, deadline=deadline_monotonic)
            emitter.press()
            motion_completed_ns = time.monotonic_ns()
            emitter.release()
            released_ns = time.monotonic_ns()
            self.watchdog.clear()
            return {
                "motionStartedMonotonicNs": started_ns,
                "motionCompletedMonotonicNs": motion_completed_ns,
                "motionReleaseAckMonotonicNs": released_ns,
            }
        except BaseException as primary:
            try:
                emitter.abort()
            except BaseException:
                self.cleanup_failures.append(f"{label}.abort")
            try:
                self.watchdog.clear()
            except BaseException:
                self.cleanup_failures.append(f"{label}.clear")
            raise primary.with_traceback(primary.__traceback__) from None

    def move_pointer(
        self,
        gesture: HumanPointerPlan,
        *,
        identity: Any,
        before_press: Callable[[], None] | None = None,
        deadline_monotonic: float | None = None,
    ) -> dict[str, int]:
        if not isinstance(gesture, HumanPointerPlan) or gesture.click is not None:
            raise OperatorError("pointer parking requires a bounded movement-only gesture")
        return self._emit_pointer_motion(
            gesture,
            identity=identity,
            before_dispatch=before_press,
            deadline_monotonic=deadline_monotonic,
            label="pointer_park",
        )

    def click(
        self,
        match: TemplateMatch,
        *,
        identity: Any,
        gesture: HumanPointerPlan,
        rate_permit: ClickRatePermit | None = None,
        before_press: Callable[[], None] | None = None,
        deadline_monotonic: float | None = None,
    ) -> dict[str, int]:
        if match.role not in {"construct", "restore"} or not match.accepted:
            raise OperatorError("click requires an accepted fresh template match")
        if not isinstance(gesture, HumanPointerPlan) or gesture.click is None:
            raise OperatorError("click requires a bounded seeded pointer gesture")
        if rate_permit is None:
            try:
                with acquire_click_rate_permit(deadline_monotonic=deadline_monotonic) as permit:
                    return self.click(
                        match,
                        identity=identity,
                        gesture=gesture,
                        rate_permit=permit,
                        before_press=before_press,
                        deadline_monotonic=deadline_monotonic,
                    )
            except PointerExecutionError as exc:
                raise OperatorError(f"click rate admission failed: {exc}") from exc
        if not isinstance(rate_permit, ClickRatePermit) or not rate_permit.is_open:
            raise OperatorError("click has no open canonical session rate permit")
        left, top, width, height = identity.child_geometry
        endpoint = (left + int(gesture.endpoint.x), top + int(gesture.endpoint.y))
        box_x, box_y, box_width, box_height = match.box
        if not (
            left <= endpoint[0] < left + width
            and top <= endpoint[1] < top + height
            and box_x + 1 <= gesture.endpoint.x < box_x + box_width - 1
            and box_y + 1 <= gesture.endpoint.y < box_y + box_height - 1
        ):
            raise OperatorError("frozen gesture endpoint is outside the safe matched control")
        try:
            motion_action = native_pointer_motion_action(gesture.trajectory, identity.child_geometry)
            motion_timeout = pointer_action_timeout(motion_action)
        except (PointerExecutionError, TypeError) as exc:
            raise OperatorError(f"click motion is not a bounded gesture: {exc}") from exc
        click_action = (
            "mousedown",
            "1",
            "sleep",
            f"{gesture.click.dwell:.6f}",
            "mouseup",
            "1",
        )
        click_timeout = _action_timeout(click_action)
        ready_timeout = 0.75
        if deadline_monotonic is not None:
            remaining = float(deadline_monotonic) - time.monotonic()
            # Reserve both emitter READY waits, each plan-derived action
            # timeout, and one bounded watchdog/release margin. The click
            # action timeout already includes its dwell.
            required = 2 * ready_timeout + motion_timeout + click_timeout + 1.0
            if remaining < required:
                raise OperatorError("complete click gesture cannot fit before its deadline")
        motion_timing = self._emit_pointer_motion(
            gesture,
            identity=identity,
            before_dispatch=None,
            deadline_monotonic=deadline_monotonic,
            label="pointer_motion",
        )
        emitter = self._new_emitter(
            click_action,
            release_action=(),
            deadline_monotonic=deadline_monotonic,
            ready_timeout=ready_timeout,
        )
        go_attempted = False
        click_started_ns = time.monotonic_ns()
        precommit_ns: int | None = None
        try:
            emitter.start()
            if emitter.pgid is None:
                raise OperatorError("native pointer emitter has no process group")
            _publish_watchdog(
                self.watchdog,
                groups={emitter.pgid},
                buttons={1},
                keys=set(),
                deadline_monotonic=deadline_monotonic,
            )
            if before_press is not None:
                before_press()
            if deadline_monotonic is not None:
                remaining = float(deadline_monotonic) - time.monotonic()
                if remaining < ready_timeout + click_timeout + 0.5:
                    raise OperatorError("click press and exact release cannot fit before its deadline")
            _action_timeout(click_action, deadline=deadline_monotonic)
            precommit_ns = rate_permit.precommit()
            go_attempted = True
            emitter.press()
            click_completed_ns = time.monotonic_ns()
            recorded_press_ns = rate_permit.record_press()
            emitter.release()
            release_ack_ns = time.monotonic_ns()
        except BaseException as primary:
            # The watchdog releases exact button 1 after terminating the
            # emitter group; ownership is cleared only after that release.
            release = getattr(self.watchdog, "release_now", None)
            if go_attempted and callable(release):
                try:
                    release()
                except BaseException:
                    self.cleanup_failures.append("pointer.release_now")
            elif not go_attempted:
                try:
                    self.watchdog.clear()
                except BaseException:
                    self.cleanup_failures.append("pointer.clear_before_go")
            for child in (locals().get("move"), locals().get("emitter")):
                if child is not None:
                    try:
                        child.abort()
                    except BaseException:
                        self.cleanup_failures.append("pointer.abort")
            raise primary.with_traceback(primary.__traceback__) from None
        else:
            self.watchdog.clear()
            return {
                **motion_timing,
                "clickEmitterStartedMonotonicNs": click_started_ns,
                "clickPrecommitMonotonicNs": int(precommit_ns or 0),
                "clickActionCompletedMonotonicNs": click_completed_ns,
                "rateRecordMonotonicNs": recorded_press_ns,
                "clickReleaseAckMonotonicNs": release_ack_ns,
            }

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.watchdog.close()
        finally:
            self.tool.close()


class Watchdog:
    """Owner-death release channel using the existing guardian entry point."""

    def __init__(
        self,
        tool: PinnedInputTool,
        *,
        interval: float = 0.1,
        environment: Mapping[str, str] | None = None,
        owner_pid: int | None = None,
        owner_start: str | None = None,
    ):
        self.tool = tool
        self.interval = interval
        self.environment = dict(environment or {})
        self.owner_pid = os.getpid() if owner_pid is None else owner_pid
        if isinstance(self.owner_pid, bool) or not isinstance(self.owner_pid, int) or self.owner_pid <= 0:
            raise OperatorError("input watchdog owner pid is malformed")
        self.owner_start = _proc_start(self.owner_pid) if owner_start is None else owner_start
        if not isinstance(self.owner_start, str) or not self.owner_start:
            raise OperatorError("input watchdog owner start identity is unavailable")
        self._read = self._write = self._ack_read = self._ack_write = -1
        self._ready_read = -1
        ready_write = -1
        self._release_spec: NativeReleaseSpec | None = None
        try:
            self._read, self._write = os.pipe()
            self._ack_read, self._ack_write = os.pipe()
            self._ready_read, ready_write = os.pipe()
            self._release_spec = tool.duplicate_release_spec()
        except BaseException:
            for descriptor in (
                self._read,
                self._write,
                self._ack_read,
                self._ack_write,
                self._ready_read,
                ready_write,
            ):
                if descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            raise
        context = multiprocessing.get_context("spawn")
        from multiprocessing.reduction import DupFd

        self._process = context.Process(
            target=_watchdog_process_entry,
            args=(
                self.owner_pid,
                self.owner_start,
                DupFd(self._read),
                interval,
                (),
                self.environment,
                DupFd(self._write),
                None,
                DupFd(ready_write),
                NativeReleaseSpec(
                    DupFd(self._release_spec.fd), self._release_spec.digest, self._release_spec.version
                ),
                DupFd(self._ack_write),
            ),
            name="crowley-native-calibration-watchdog",
            daemon=False,
        )
        try:
            self._process.start()
            # A release actor must remain independent if owner cleanup cannot
            # confirm physical release. Removing it from multiprocessing's
            # implicit atexit join set keeps the owner bounded; explicit
            # close()/release_now() still joins it when release succeeds.
            _discard_spawn_child(self._process)
            os.close(ready_write)
            ready_write = -1
            os.close(self._read)
            os.close(self._ack_write)
            self._read = self._ack_write = -1
            if _read_bounded(self._ready_read, 64, timeout=2.0) != b"READY\n":
                raise OperatorError("input watchdog did not become ready")
            os.close(self._ready_read)
            self._ready_read = -1
        except Exception as exc:
            for descriptor in (
                ready_write,
                self._ready_read,
                self._read,
                self._write,
                self._ack_read,
                self._ack_write,
            ):
                if isinstance(descriptor, int) and descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            if getattr(self._process, "pid", None) is not None and self._process.is_alive():
                self._process.terminate()
                self._process.join(timeout=1.0)
            if self._release_spec is not None:
                try:
                    os.close(self._release_spec.fd)
                except OSError:
                    pass
                self._release_spec = None
            if isinstance(exc, OperatorError):
                raise
            raise OperatorError("input watchdog readiness failed") from exc

    def publish(
        self,
        *,
        groups: set[int],
        buttons: set[int],
        keys: set[str] = frozenset(),
        deadline_monotonic: float | None = None,
    ) -> None:
        if any(isinstance(v, bool) or not isinstance(v, int) or v <= 0 for v in groups | buttons):
            raise OperatorError("watchdog ownership set is malformed")
        if any(not isinstance(v, str) or not v for v in keys):
            raise OperatorError("watchdog key ownership set is malformed")
        payload = json.dumps(
            {"groups": sorted(groups), "keys": sorted(keys), "buttons": sorted(buttons)},
            separators=(",", ":"),
        ).encode()
        frame = b"T" + payload + b"\n"
        try:
            timeout = _bounded_timeout(deadline_monotonic, 2.0)
            _write_bounded(self._write, frame, timeout=timeout)
            ack = _read_bounded(self._ack_read, 4, timeout=_bounded_timeout(deadline_monotonic, 2.0))
        except (OSError, TimeoutError, EOFError, ValueError) as exc:
            raise OperatorError("input watchdog ownership acknowledgement failed") from exc
        if ack != b"ACK\n":
            raise OperatorError("input watchdog ownership was not acknowledged")

    def release_now(self) -> None:
        if self._write < 0:
            return
        try:
            _write_bounded(self._write, b"S\n", timeout=2.0)
        except (OSError, TimeoutError, ValueError):
            pass
        try:
            os.close(self._write)
        except OSError:
            pass
        self._write = -1
        if not _bounded_process_join(self._process, 3.0):
            raise OperatorError("input watchdog has not confirmed physical release")
        if self._process.exitcode != 0:
            raise OperatorError("input watchdog exited without confirming physical release")

    def clear(self) -> None:
        try:
            payload = json.dumps({"groups": [], "keys": [], "buttons": []}, separators=(",", ":")).encode()
            _write_bounded(self._write, b"T" + payload + b"\n", timeout=2.0)
            ack = _read_bounded(self._ack_read, 4, timeout=2.0)
        except (OSError, TimeoutError, EOFError, ValueError) as exc:
            raise OperatorError("input watchdog clear acknowledgement failed") from exc
        if ack != b"ACK\n":
            raise OperatorError("input watchdog clear was not acknowledged")

    def close(self) -> None:
        if self._write >= 0:
            try:
                _write_bounded(self._write, b"S\n", timeout=2.0)
            except (OSError, TimeoutError, ValueError):
                pass
            try:
                os.close(self._write)
            except OSError:
                pass
            self._write = -1
        if self._ack_read >= 0:
            try:
                os.close(self._ack_read)
            except OSError:
                pass
            self._ack_read = -1
        try:
            if not _bounded_process_join(self._process, 3.0):
                raise OperatorError("input watchdog close could not confirm release")
            if self._process.exitcode != 0:
                raise OperatorError("input watchdog exited without confirming physical release")
        finally:
            if self._release_spec is not None:
                try:
                    os.close(self._release_spec.fd)
                except OSError:
                    pass
                self._release_spec = None


def _cancellation_entry(
    owner_pid: int,
    owner_start: str | None,
    control_read: int,
    ready_write: int,
    ack_write: int,
    socket_path: str,
    generation: int,
    reservation_id: str,
    deadline: float,
    result_write: int | None = None,
) -> None:
    """Closed owner actor for admission, terminal operations, and cancellation."""

    if hasattr(control_read, "detach"):
        control_read = control_read.detach()
    if hasattr(ready_write, "detach"):
        ready_write = ready_write.detach()
    if hasattr(ack_write, "detach"):
        ack_write = ack_write.detach()
    if result_write is not None and hasattr(result_write, "detach"):
        result_write = result_write.detach()

    try:
        _write_bounded(ready_write, b"READY\n", timeout=2.0)
    except (OSError, TimeoutError, ValueError):
        return
    finally:
        try:
            os.close(ready_write)
        except OSError:
            pass

    current_generation = generation
    current_reservation_id = reservation_id
    current_deadline = float(deadline)
    known_binding: ReservationBinding | None = None

    def owner_start_now() -> str | None:
        try:
            return _proc_start(owner_pid)
        except Exception:
            return None

    def acknowledge() -> bool:
        try:
            return _write_bounded(ack_write, b"ACK\n", timeout=1.0) is None
        except (OSError, TimeoutError, ValueError):
            return False

    def respond(payload: Mapping[str, Any]) -> bool:
        if result_write is None:
            return False
        try:
            encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            _write_bounded(result_write, b"J" + encoded + b"\n", timeout=1.0)
            return True
        except (OSError, TimeoutError, ValueError, TypeError):
            return False

    def cancel_known() -> None:
        if not current_reservation_id:
            return
        remaining = current_deadline - time.monotonic()
        if remaining <= 0:
            return
        request = {
            "command": "native_construction_recovery_cancel",
            "generation": current_generation,
            "reservationId": current_reservation_id,
        }
        try:
            response = _socket_request(
                Path(socket_path),
                request,
                timeout=remaining,
                response_deadline=current_deadline,
            )
            if not isinstance(response, dict) or response.get("ok") is not True:
                return
            record = response.get("reservation")
            handshake = response.get("handshake")
            if not isinstance(handshake, Mapping) or handshake.get("state") != "cancelled":
                return
            terminal = ReservationBinding.from_record(
                record, expected_generation=current_generation, now=None
            )
            if terminal.reservation_id != current_reservation_id or terminal.reservation_state != "cancelled":
                return
        except (OSError, RuntimeError, TimeoutError, ValueError, TypeError):
            return

    def validate_operation(
        op: str, values: Mapping[str, Any], response: Mapping[str, Any]
    ) -> ReservationBinding:
        nonlocal current_generation, current_reservation_id, current_deadline, known_binding
        if response.get("ok") is not True:
            raise OperatorError("actor admission was refused")
        record = response.get("reservation")
        handshake = response.get("handshake")
        failure_state = handshake.get("state") if isinstance(handshake, Mapping) else None
        if (
            op == "recheck"
            and failure_state in TERMINAL_FAILURE_HANDSHAKES
            and not _is_normal_recovery_reply(handshake, record)
        ):
            if not isinstance(record, Mapping) or set(record) != set(CANONICAL_RESERVATION_FIELDS):
                raise OperatorError("actor terminal failure binding is malformed")
            if (
                known_binding is None
                or record.get("reservationId") != known_binding.reservation_id
                or record.get("controllerGeneration") != known_binding.controller_generation
                or record.get("reservationState") != failure_state
            ):
                raise OperatorError("actor terminal failure binding changed")
            candidate_record = dict(record)
            candidate_record["reservationState"] = known_binding.reservation_state
            candidate = ReservationBinding.from_record(
                candidate_record, expected_generation=known_binding.controller_generation, now=None
            )
            known_binding.compare_immutable(candidate)
            history = response.get("stateHistory")
            if history is None and isinstance(handshake, Mapping):
                history = handshake.get("stateHistory")
            if not isinstance(history, list) or not history or len(history) > MAX_STATE_HISTORY:
                raise OperatorError("actor terminal failure state history is malformed")
            if not isinstance(history[-1], Mapping) or history[-1].get("state") != failure_state:
                raise OperatorError("actor terminal failure state history is inconsistent")
            for entry in history:
                if (
                    not isinstance(entry, Mapping)
                    or set(entry) != STATE_HISTORY_KEYS
                    or entry.get("state") not in (HANDSHAKE_STATES | TERMINAL_FAILURE_HANDSHAKES)
                    or isinstance(entry.get("nativeClock"), bool)
                    or not isinstance(entry.get("nativeClock"), (int, float))
                    or not math.isfinite(float(entry.get("nativeClock")))
                ):
                    raise OperatorError("actor terminal failure state history is malformed")
            return known_binding
        binding = ReservationBinding.from_record(record, expected_generation=None, now=None)
        # ``ownerPid`` identifies the daemon's leased owner, whereas
        # ``owner_pid`` identifies this helper process that the actor watches.
        # They are intentionally different processes; the canonical binding
        # fields are validated by ReservationBinding and retained verbatim.
        if not isinstance(handshake, Mapping) or handshake.get("state") not in HANDSHAKE_STATES:
            raise OperatorError("actor admission handshake is malformed")
        if binding.expires_monotonic <= time.monotonic():
            raise OperatorError("actor admission expiry is not in the future")
        history = response.get("stateHistory")
        if history is None:
            history = handshake.get("stateHistory")
        if not isinstance(history, list) or not history or len(history) > MAX_STATE_HISTORY:
            raise OperatorError("actor admission state history is malformed")
        history_states: list[str] = []
        prior_clock: float | None = None
        for entry in history:
            if not isinstance(entry, Mapping) or set(entry) != STATE_HISTORY_KEYS:
                raise OperatorError("actor admission state history entry is malformed")
            history_state = entry.get("state")
            native_clock = entry.get("nativeClock")
            if (
                history_state not in (HANDSHAKE_STATES | TERMINAL_FAILURE_HANDSHAKES)
                or isinstance(native_clock, bool)
                or not isinstance(native_clock, (int, float))
                or not math.isfinite(float(native_clock))
            ):
                raise OperatorError("actor admission state history value is malformed")
            if prior_clock is not None and float(native_clock) < prior_clock:
                raise OperatorError("actor admission state history clock regressed")
            prior_clock = float(native_clock)
            history_states.append(history_state)
        if history_states[-1] != handshake["state"]:
            raise OperatorError("actor admission state history does not end at handshake state")
        if history_states[0] == "recovery_required":
            if any(
                state not in {"recovery_required", "recovered", "cancelled", "error"}
                for state in history_states
            ):
                raise OperatorError("actor recovery state history is malformed")
        else:
            active = (
                history_states[:-1]
                if history_states[-1] in ({"error", "cancelled"} | TERMINAL_FAILURE_HANDSHAKES)
                else history_states
            )
            try:
                indexes = [STATE_CHAIN.index(state) for state in active]
            except ValueError as exc:
                raise OperatorError("actor construction state history is malformed") from exc
            if not active or indexes != list(range(indexes[0], indexes[0] + len(indexes))) or indexes[0] != 0:
                raise OperatorError("actor construction state history is not contiguous")
        if binding.controller_generation != values["generation"]:
            raise OperatorError("actor admission generation changed before acknowledgement")
        state = handshake["state"]
        expected_states = {
            "prepare": "construction_prepared",
            "bootstrap": "running",
            "recovery_begin": "recovery_required",
            "recovery_complete": {"recovered", "cancelled"},
            "cancel": "cancelled",
            "finalize": "finalize_required",
            "recheck": HANDSHAKE_STATES - {"error"},
        }
        expected = expected_states[op]
        state_ok = state in expected if isinstance(expected, (set, frozenset)) else state == expected
        if not state_ok:
            raise OperatorError("actor admission handshake state is not expected")
        if op == "prepare":
            if values["preflight"] and binding.reservation_kind != "preflight":
                raise OperatorError("actor preflight returned the wrong reservation kind")
            if not values["preflight"] and binding.reservation_kind != "measure":
                raise OperatorError("actor prepare returned the wrong reservation kind")
        elif op in {"bootstrap", "recovery_complete", "cancel", "finalize", "recheck"}:
            if binding.reservation_id != values["reservationId"]:
                raise OperatorError("actor admission reservation identity changed")
        if known_binding is not None:
            if (
                op == "recovery_begin"
                and binding.controller_generation != known_binding.controller_generation
            ):
                # A pause/recovery boundary may advance the controller
                # generation. Preserve the sealed owner/profile/identity
                # fields while allowing that explicit generation handoff. A
                # real pause revokes Guardian, so its generation must advance
                # with the controller handoff rather than being copied.
                for field_name in (
                    "desktop_lease_held",
                    "guardian_armed",
                    "owner_pid",
                    "owner_process_start",
                    "profile_fingerprint",
                    "identity_fingerprint",
                    "b0",
                    "expected_constructor_boot",
                ):
                    if getattr(known_binding, field_name) != getattr(binding, field_name):
                        raise OperatorError("actor recovery binding changed outside the sealed handoff")
                if binding.controller_generation <= known_binding.controller_generation:
                    raise OperatorError("actor recovery generation did not advance")
                if binding.guardian_generation <= known_binding.guardian_generation:
                    raise OperatorError("actor recovery Guardian generation did not advance")
            else:
                known_binding.compare_immutable(binding)
        known_binding = binding
        current_generation = binding.controller_generation
        current_reservation_id = binding.reservation_id
        current_deadline = binding.expires_monotonic
        return binding

    def perform(values: Mapping[str, Any]) -> None:
        nonlocal current_generation, current_reservation_id, current_deadline
        op = values.get("op")
        allowed = {
            "prepare": {"op", "generation", "preflight", "deadline"},
            "bootstrap": {"op", "generation", "reservationId", "expectedConstructorBoot", "deadline"},
            "recovery_begin": {"op", "generation", "reservationId", "deadline"},
            "recovery_complete": {"op", "generation", "reservationId", "deadline"},
            "cancel": {"op", "generation", "reservationId", "deadline"},
            "finalize": {"op", "generation", "reservationId", "deadline"},
            "recheck": {"op", "generation", "reservationId", "deadline"},
        }
        if not isinstance(op, str) or op not in allowed or set(values) != allowed[op]:
            respond({"ok": False, "error": "actor_operation_fields", "indeterminate": False})
            return
        requested_generation = values["generation"]
        requested_deadline = values["deadline"]
        if (
            isinstance(requested_generation, bool)
            or not isinstance(requested_generation, int)
            or requested_generation <= 0
            or isinstance(requested_deadline, bool)
            or not isinstance(requested_deadline, (int, float))
            or not math.isfinite(float(requested_deadline))
            or float(requested_deadline) <= time.monotonic()
        ):
            respond({"ok": False, "error": "actor_operation_values", "indeterminate": False})
            return
        if op == "prepare":
            if not isinstance(values["preflight"], bool):
                respond({"ok": False, "error": "actor_operation_values", "indeterminate": False})
                return
            request = {
                "command": (
                    "native_construction_control_preflight"
                    if values["preflight"]
                    else "native_construction_prepare"
                ),
                "generation": requested_generation,
            }
        elif op == "bootstrap":
            if (
                not isinstance(values["reservationId"], str)
                or not values["reservationId"]
                or len(values["reservationId"]) > 128
                or not isinstance(values["expectedConstructorBoot"], str)
                or not values["expectedConstructorBoot"].startswith("native-construction:")
                or len(values["expectedConstructorBoot"].encode()) > 96
            ):
                respond({"ok": False, "error": "actor_operation_values", "indeterminate": False})
                return
            request = {
                "command": "native_construction_bootstrap",
                "generation": requested_generation,
                "reservationId": values["reservationId"],
                "expectedConstructorBoot": values["expectedConstructorBoot"],
            }
        else:
            reservation_id = values["reservationId"]
            if (
                not isinstance(reservation_id, str)
                or (op != "recovery_begin" and not reservation_id)
                or len(reservation_id) > 128
            ):
                respond({"ok": False, "error": "actor_operation_values", "indeterminate": False})
                return
            command = {
                "recovery_begin": "native_construction_recovery_begin",
                "recovery_complete": "native_construction_recovery_complete",
                "cancel": "native_construction_recovery_cancel",
                "finalize": "native_construction_finalize_admit",
                "recheck": "native_construction_recheck",
            }[op]
            request = {
                "command": command,
                "generation": requested_generation,
                "reservationId": reservation_id,
            }
        remaining = float(requested_deadline) - time.monotonic()
        if remaining <= 0:
            respond({"ok": False, "error": "actor_operation_deadline", "indeterminate": False})
            return
        try:
            response = _socket_request(
                Path(socket_path),
                request,
                timeout=remaining,
                response_deadline=float(requested_deadline),
            )
        except (OSError, RuntimeError, TimeoutError, ValueError, TypeError):
            # A lost response may follow a committed server operation. Keep the
            # previous known capability and prohibit the helper from guessing.
            # Once the server has already admitted a known ID, retain a short
            # private cleanup window even if the old prepare deadline expires;
            # this permits one exact cancellation without pretending that an
            # unobserved canonical extension was observed by the actor.
            known_id = current_reservation_id
            if not known_id and op in {
                "bootstrap",
                "recovery_complete",
                "cancel",
                "finalize",
                "recheck",
            }:
                candidate_id = values.get("reservationId")
                if isinstance(candidate_id, str) and candidate_id:
                    known_id = candidate_id
                    current_reservation_id = candidate_id
            if known_id:
                # Retain exact-ID cleanup strictly past the old admission
                # bound. This is cleanup protection only, never a retry or
                # dependent-input window.
                current_deadline = max(current_deadline, time.monotonic()) + KNOWN_ADMISSION_CLEANUP_GRACE
            respond({"ok": False, "error": "admission_indeterminate", "indeterminate": True})
            return
        try:
            validate_operation(op, values, response)
        except (OperatorError, TypeError, ValueError):
            respond({"ok": False, "error": "actor_admission_invalid", "indeterminate": False})
            return
        respond(response)

    try:
        os.set_blocking(control_read, False)
        os.set_blocking(ack_write, True)
        if result_write is not None:
            os.set_blocking(result_write, True)
        pending = b""
        while True:
            remaining = current_deadline - time.monotonic()
            if remaining <= 0:
                return
            readable, _writable, _errors = select.select([control_read], [], [], min(0.05, remaining))
            if readable:
                try:
                    control = os.read(control_read, 4096)
                except BlockingIOError:
                    control = None
                if control is not None:
                    if control == b"":
                        cancel_known()
                        return
                    pending += control
                    if len(pending) > 16384:
                        cancel_known()
                        return
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        if line == b"S":
                            acknowledge()
                            return
                        if line.startswith(b"U"):
                            try:
                                update = json.loads(line[1:].decode("utf-8"))
                                if not isinstance(update, dict) or set(update) != {
                                    "generation",
                                    "reservationId",
                                    "deadline",
                                }:
                                    raise ValueError("handoff fields")
                                new_generation = update["generation"]
                                new_reservation_id = update["reservationId"]
                                new_deadline = update["deadline"]
                                if (
                                    isinstance(new_generation, bool)
                                    or not isinstance(new_generation, int)
                                    or new_generation <= 0
                                    or not isinstance(new_reservation_id, str)
                                    or not new_reservation_id
                                    or len(new_reservation_id) > 128
                                    or not isinstance(new_deadline, (int, float))
                                    or isinstance(new_deadline, bool)
                                    or not math.isfinite(float(new_deadline))
                                ):
                                    raise ValueError("handoff values")
                                current_generation = new_generation
                                current_reservation_id = new_reservation_id
                                current_deadline = float(new_deadline)
                            except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
                                cancel_known()
                                return
                            if not acknowledge():
                                cancel_known()
                                return
                            continue
                        if line.startswith(b"A"):
                            try:
                                operation = json.loads(line[1:].decode("utf-8"))
                            except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
                                respond(
                                    {"ok": False, "error": "actor_operation_fields", "indeterminate": False}
                                )
                                continue
                            if isinstance(operation, dict):
                                perform(operation)
                            else:
                                respond(
                                    {"ok": False, "error": "actor_operation_fields", "indeterminate": False}
                                )
                            continue
                        cancel_known()
                        return
            owner_current = owner_start_now()
            owner_dead = owner_current is None or (owner_start is not None and owner_current != owner_start)
            if owner_dead:
                cancel_known()
                return
    except (OSError, RuntimeError, ValueError, TimeoutError):
        return
    finally:
        for descriptor in (control_read, ack_write, result_write):
            if isinstance(descriptor, int) and descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass


class CancellationActor:
    """Independent owner-death cancellation for one admitted reservation."""

    def __init__(
        self,
        socket_path: Path,
        *,
        generation: int,
        reservation_id: str,
        deadline: float,
        owner_pid: int | None = None,
        owner_start: str | None = None,
    ):
        if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
            raise OperatorError("cancellation generation is malformed")
        if not isinstance(reservation_id, str) or len(reservation_id) > 128:
            raise OperatorError("cancellation reservation id is malformed")
        if not math.isfinite(float(deadline)):
            raise OperatorError("cancellation deadline is malformed")
        self.socket_path = Path(socket_path)
        self.generation = generation
        self.reservation_id = reservation_id
        self.deadline = float(deadline)
        self.owner_pid = os.getpid() if owner_pid is None else owner_pid
        if isinstance(self.owner_pid, bool) or not isinstance(self.owner_pid, int) or self.owner_pid <= 0:
            raise OperatorError("cancellation owner pid is malformed")
        self.owner_start = _proc_start(self.owner_pid) if owner_start is None else owner_start
        if not isinstance(self.owner_start, str) or not self.owner_start:
            raise OperatorError("cancellation owner process start identity is unavailable")
        self._control_read = self._control_write = self._ready_read = -1
        self._result_read = -1
        ready_write = ack_write = result_write = -1
        self._ack_read = -1
        try:
            self._control_read, self._control_write = os.pipe()
            self._ready_read, ready_write = os.pipe()
            self._ack_read, ack_write = os.pipe()
            self._result_read, result_write = os.pipe()
        except BaseException:
            for descriptor in (
                self._control_read,
                self._control_write,
                self._ready_read,
                ready_write,
                self._ack_read,
                ack_write,
                self._result_read,
                result_write,
            ):
                if descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            raise
        self._process: multiprocessing.Process | None = None
        self._channel_poisoned = False
        self._ready_write = ready_write
        self._ack_write = ack_write
        self._result_write = result_write

    def start(self) -> None:
        from multiprocessing.reduction import DupFd

        context = multiprocessing.get_context("spawn")
        self._process = context.Process(
            target=_cancellation_process_entry,
            args=(
                self.owner_pid,
                self.owner_start,
                DupFd(self._control_read),
                DupFd(self._ready_write),
                DupFd(self._ack_write),
                str(self.socket_path),
                self.generation,
                self.reservation_id,
                self.deadline,
                DupFd(self._result_write),
            ),
            name="crowley-native-calibration-cancellation",
            daemon=False,
        )
        try:
            self._process.start()
            os.close(self._control_read)
            os.close(self._ready_write)
            os.close(self._ack_write)
            os.close(self._result_write)
            self._control_read = self._ready_write = self._ack_write = self._result_write = -1
            _discard_spawn_child(self._process)
            if _read_bounded(self._ready_read, 64, timeout=2.0) != b"READY\n":
                raise OperatorError("cancellation actor did not become ready")
            os.close(self._ready_read)
            self._ready_read = -1
        except Exception as exc:
            for descriptor in (
                self._control_read,
                self._control_write,
                self._ready_write,
                self._ready_read,
                self._ack_read,
                self._ack_write,
                self._result_read,
                self._result_write,
            ):
                if isinstance(descriptor, int) and descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            if getattr(self._process, "pid", None) is not None and self._process.is_alive():
                self._process.terminate()
                self._process.join(timeout=1.0)
            if isinstance(exc, OperatorError):
                raise
            raise OperatorError("cancellation actor readiness failed") from exc

    def update(self, *, generation: int, reservation_id: str, deadline: float) -> None:
        if self._channel_poisoned:
            raise OperatorError("cancellation actor private channel is unusable")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
            raise OperatorError("cancellation handoff generation is malformed")
        if not isinstance(reservation_id, str) or not reservation_id or len(reservation_id) > 128:
            raise OperatorError("cancellation handoff reservation is malformed")
        if (
            isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(float(deadline))
        ):
            raise OperatorError("cancellation handoff deadline is malformed")
        payload = json.dumps(
            {"generation": generation, "reservationId": reservation_id, "deadline": float(deadline)},
            separators=(",", ":"),
        ).encode("utf-8")
        try:
            _write_bounded(self._control_write, b"U" + payload + b"\n", timeout=1.0)
            if _read_bounded(self._ack_read, 4, timeout=1.0) != b"ACK\n":
                raise OperatorError("cancellation handoff was not acknowledged")
        except (OSError, TimeoutError, EOFError, ValueError) as exc:
            raise OperatorError("cancellation handoff acknowledgement failed") from exc
        self.generation = generation
        self.reservation_id = reservation_id
        self.deadline = float(deadline)

    def call(
        self,
        op: str,
        *,
        generation: int,
        deadline: float,
        reservation_id: str = "",
        preflight: bool | None = None,
        expected_constructor_boot: str | None = None,
    ) -> dict[str, Any]:
        """Ask the owner actor to perform one closed admission operation."""
        if self._channel_poisoned:
            raise OperatorError("cancellation actor private channel is poisoned")
        if self._control_write < 0 or self._result_read < 0:
            raise OperatorError("cancellation actor is not running")
        allowed = {
            "prepare",
            "bootstrap",
            "recovery_begin",
            "recovery_complete",
            "cancel",
            "finalize",
            "recheck",
        }
        if op not in allowed:
            raise OperatorError("cancellation actor operation is not closed")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
            raise OperatorError("cancellation actor generation is malformed")
        if (
            isinstance(deadline, bool)
            or not isinstance(deadline, (int, float))
            or not math.isfinite(float(deadline))
            or float(deadline) <= time.monotonic()
        ):
            raise OperatorError("cancellation actor deadline is malformed")
        if not isinstance(reservation_id, str) or len(reservation_id) > 128:
            raise OperatorError("cancellation actor reservation is malformed")
        if op == "prepare":
            if not isinstance(preflight, bool) or reservation_id or expected_constructor_boot is not None:
                raise OperatorError("cancellation actor prepare arguments are malformed")
            values: dict[str, Any] = {
                "op": op,
                "generation": generation,
                "preflight": preflight,
                "deadline": float(deadline),
            }
        elif op == "bootstrap":
            if not reservation_id or not isinstance(expected_constructor_boot, str):
                raise OperatorError("cancellation actor bootstrap arguments are malformed")
            values = {
                "op": op,
                "generation": generation,
                "reservationId": reservation_id,
                "expectedConstructorBoot": expected_constructor_boot,
                "deadline": float(deadline),
            }
        else:
            if (
                (op != "recovery_begin" and not reservation_id)
                or expected_constructor_boot is not None
                or preflight is not None
            ):
                raise OperatorError("cancellation actor operation arguments are malformed")
            values = {
                "op": op,
                "generation": generation,
                "reservationId": reservation_id,
                "deadline": float(deadline),
            }
        payload = json.dumps(values, sort_keys=True, separators=(",", ":")).encode("utf-8")
        remaining = float(deadline) - time.monotonic()
        if remaining <= 0:
            raise OperatorError("cancellation actor deadline expired")
        try:
            _write_bounded(self._control_write, b"A" + payload + b"\n", timeout=min(1.0, remaining))
            frame = _read_record_bounded(self._result_read, 524288, timeout=remaining)
        except (OSError, TimeoutError, EOFError, ValueError) as exc:
            self._poison_private_channel()
            raise AdmissionIndeterminate("actor admission response was lost") from exc
        except BaseException:
            # A signal or other asynchronous interruption leaves the response
            # pipe potentially containing a late reply.  Poison both ends and
            # let actor EOF perform the exact-ID cleanup; never let a later
            # cancel consume that stale frame as its acknowledgement.
            self._poison_private_channel()
            raise
        if not frame.startswith(b"J"):
            raise OperatorError("cancellation actor response framing is malformed")
        try:
            response = json.loads(frame[1:].decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError, TypeError) as exc:
            raise OperatorError("cancellation actor response is malformed") from exc
        if not isinstance(response, dict):
            raise OperatorError("cancellation actor response is malformed")
        if response.get("indeterminate") is True:
            self._mark_indeterminate_channel()
            raise AdmissionIndeterminate("actor admission response was indeterminate")
        if response.get("ok") is not True:
            error = response.get("error")
            raise OperatorError(
                error
                if isinstance(error, str)
                and error
                in {
                    "actor_admission_invalid",
                    "actor_admission_refused",
                    "actor_operation_fields",
                    "actor_operation_values",
                    "actor_operation_deadline",
                }
                else "actor admission was refused"
            )
        record = response.get("reservation")
        if isinstance(record, Mapping):
            new_generation = record.get("controllerGeneration")
            new_reservation = record.get("reservationId")
            new_deadline = record.get("expiresMonotonic")
            if (
                isinstance(new_generation, int)
                and not isinstance(new_generation, bool)
                and isinstance(new_reservation, str)
                and new_reservation
                and isinstance(new_deadline, (int, float))
                and math.isfinite(float(new_deadline))
            ):
                self.generation = new_generation
                self.reservation_id = new_reservation
                self.deadline = float(new_deadline)
        return response

    def _poison_private_channel(self) -> None:
        if self._channel_poisoned:
            return
        self._channel_poisoned = True
        for descriptor_name in ("_control_write", "_result_read"):
            descriptor = getattr(self, descriptor_name)
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                setattr(self, descriptor_name, -1)

    def _mark_indeterminate_channel(self) -> None:
        """Retain owner control while refusing further RPC on a complete loss."""

        self._channel_poisoned = True
        if self._result_read >= 0:
            try:
                os.close(self._result_read)
            except OSError:
                pass
            self._result_read = -1

    def close(self) -> None:
        """Disarm only after the actor acknowledges the terminal command."""

        failure: OperatorError | None = None
        if self._channel_poisoned:
            # A complete server-indeterminate result leaves ownership live but
            # makes the private RPC unusable.  Closing the owner end is the
            # only terminal action; sending S would disarm before EOF cleanup.
            if self._control_write >= 0:
                try:
                    os.close(self._control_write)
                except OSError:
                    pass
                self._control_write = -1
        elif self._control_write >= 0:
            try:
                _write_bounded(self._control_write, b"S\n", timeout=1.0)
                if _read_bounded(self._ack_read, 4, timeout=1.0) != b"ACK\n":
                    raise OperatorError("cancellation actor terminal acknowledgement failed")
            except (OSError, TimeoutError, EOFError, ValueError) as exc:
                failure = OperatorError("cancellation actor terminal acknowledgement failed")
                failure.__cause__ = exc
            finally:
                try:
                    os.close(self._control_write)
                except OSError:
                    pass
                self._control_write = -1
        for descriptor_name in ("_ready_read", "_ack_read", "_result_read"):
            descriptor = getattr(self, descriptor_name)
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                setattr(self, descriptor_name, -1)
        if self._process is not None:
            if not _bounded_process_join(self._process, 2.0):
                failure = failure or OperatorError(
                    "cancellation actor did not exit after terminal acknowledgement"
                )
            elif self._process.exitcode != 0:
                failure = failure or OperatorError(
                    "cancellation actor exited with an unconfirmed terminal state"
                )
        if failure is not None:
            raise failure

    def abandon(self) -> None:
        """Close only the owner control end; EOF keeps cancellation armed."""

        if self._control_write >= 0:
            try:
                os.close(self._control_write)
            except OSError:
                pass
            self._control_write = -1
        for descriptor_name in ("_ready_read", "_ack_read", "_result_read"):
            descriptor = getattr(self, descriptor_name)
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                setattr(self, descriptor_name, -1)


@dataclass
class ReceiptWriter:
    root: Path
    mode: str
    helper_source_sha256: str = ""
    dependencies: dict[str, str] = field(default_factory=dict)
    clock: Callable[[], float] = time.monotonic
    attempt: Path | None = None
    document: dict[str, Any] = field(default_factory=dict)

    def begin(self) -> None:
        if self.mode not in MODES:
            raise ReceiptError("receipt mode is not closed")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.root, 0o700)
        name = (
            f"native-calibration-{self.mode}-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}-"
            f"{os.getpid()}-{secrets.token_hex(4)}"
        )
        self.attempt = self.root / name
        self.attempt.mkdir(mode=0o700)
        os.chmod(self.attempt, 0o700)
        self.document = {
            "schema": 1,
            "mode": self.mode,
            "startedUtc": datetime.now(UTC).isoformat(),
            "helperSourceSha256": self.helper_source_sha256,
            "dependencySha256": dict(sorted(self.dependencies.items())),
            "events": [],
            "outcome": "running",
            "failureCode": None,
        }
        self.write()

    def event(self, name: str, **fields: Any) -> None:
        events = self.document.setdefault("events", [])
        if not isinstance(events, list) or len(events) >= MAX_RECEIPT_EVENTS:
            raise ReceiptError("receipt event bound exceeded")
        safe = {"name": name}
        for key, value in fields.items():
            if key in {"path", "argv", "environment", "config", "secret"}:
                raise ReceiptError("receipt contains a forbidden private field")
            safe[key] = value
        safe["publishedMonotonic"] = self.clock()
        events.append(safe)
        self.write()

    def finish(self, outcome: str, failure_code: str | None = None) -> None:
        if outcome not in {"success", "failure", "refused"}:
            raise ReceiptError("receipt outcome is not closed")
        self.document["outcome"] = outcome
        self.document["failureCode"] = failure_code
        self.write()

    def write(self) -> None:
        if self.attempt is None:
            raise ReceiptError("receipt was not initialized")
        target = self.attempt / "receipt.json"
        temporary = self.attempt / ".receipt.json.tmp"
        data = (json.dumps(self.document, sort_keys=True, separators=(",", ":")) + "\n").encode()
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                offset = 0
                while offset < len(data):
                    written = os.write(fd, data[offset:])
                    if written <= 0:
                        raise OSError("private receipt short write")
                    offset += written
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temporary, target)
            os.chmod(target, 0o600)
            directory_fd = os.open(self.attempt, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            root_fd = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(root_fd)
            finally:
                os.close(root_fd)
        except OSError as exc:
            raise ReceiptError("private receipt publication failed") from exc


class NativeCalibrationOperator:
    """Three-mode bounded orchestration with injected offline boundaries."""

    def __init__(
        self,
        *,
        mode: str,
        generation: int,
        config_path: str | os.PathLike[str],
        template_manifest: TemplateManifest,
        client: DaemonClient,
        screen: ScreenAdapter,
        input_device: InputAdapter,
        clock: Callable[[], float] = time.monotonic,
        ocr: Callable[[Path], str] | None = None,
        chat_state: Callable[[Path], str] | None = None,
        receipt_root: Path | None = None,
        helper_source_sha256: str = "",
        dependencies: dict[str, str] | None = None,
        matcher_runtime: TemplateMatcherRuntime | None = None,
        matcher_artifact_root: Path | os.PathLike[str] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        absence_checker: Callable[..., bool] | None = None,
    ) -> None:
        if mode not in MODES:
            raise OperatorError("helper mode is not closed")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
            raise OperatorError("helper generation is malformed")
        self.mode = mode
        self.generation = generation
        self.config_path = Path(config_path)
        self.client = client
        self.screen = screen
        self.input = input_device
        self.clock = clock
        self.ocr = ocr or self._ocr_file
        self.chat_state = chat_state or self._chat_editor_state
        self.absence_checker = absence_checker or _strict_native_absence_image
        self.matcher = TemplateMatcher(
            template_manifest,
            runtime=matcher_runtime,
            artifact_root=matcher_artifact_root,
        )
        receipt_dependencies = dict(dependencies or {})
        for name, digest in self.matcher.source_bindings.items():
            prior = receipt_dependencies.get(name)
            if prior is not None and prior != digest:
                self.matcher.close()
                raise OperatorError(f"matcher dependency pin changed: {name}")
            receipt_dependencies[name] = digest
        try:
            self.receipt = ReceiptWriter(
                receipt_root or Path.home() / ".local" / "state" / "crowley" / "native-calibration-operator",
                mode,
                helper_source_sha256,
                receipt_dependencies,
                clock,
            )
        except BaseException:
            self.matcher.close()
            raise
        self.sleep = sleep
        self._binding: ReservationBinding | None = None
        self._handshake: str | None = None
        self._operation_deadline: float | None = None
        self._phase_deadlines: dict[str, float] = {}
        self._reservation_deadline: float | None = None
        self._release_dispatch: float | None = None
        self._last_capture_completed: float | None = None
        self._chat_capture_sequence = 0
        self._chat_retained_frames: list[dict[str, Any]] = []
        self._last_chat_capture_record: dict[str, Any] | None = None
        self._history_before_action = 0
        self._aborted = False
        self._cleanup_started = False
        self._first_failure: BaseException | None = None
        self._state_history: list[tuple[str, float]] = []
        self._poll_state: str | None = None
        self._poll_clock: float | None = None
        self._poll_count = 0
        self._calibration_started = False
        self._calibration_terminal_error = False
        self._terminal_failure_message = "native calibration failed"
        self._recovery_generation: int | None = None
        self._cancellation_actor: CancellationActor | None = None
        self._cancellation_actor_binding: tuple[int, str, float] | None = None
        self._old_handlers: dict[int, Any] = {}
        self._defer_signal_restore = False
        # A real DaemonClient must never perform an admission from the helper
        # process.  Offline fake clients retain their existing direct path.
        self._actor_admission = all(
            hasattr(client, attribute) for attribute in ("request", "socket_path", "_request_fn", "_timeout")
        )

    def _deadline(self, phase: str | None = None) -> float | None:
        candidates: list[float] = []
        if self._reservation_deadline is not None:
            candidates.append(self._reservation_deadline)
        if phase is not None and phase in self._phase_deadlines:
            candidates.append(self._phase_deadlines[phase])
        if self._operation_deadline is not None:
            candidates.append(self._operation_deadline)
        return min(candidates) if candidates else None

    def _remaining(self, phase: str | None = None) -> float:
        deadline = self._deadline(phase)
        if deadline is None:
            raise OperatorError("native operation deadline is unavailable")
        remaining = float(deadline) - self.clock()
        if remaining <= 0:
            raise OperatorError("native operation deadline expired")
        return remaining

    def _event(self, name: str, **fields: Any) -> None:
        try:
            self.receipt.event(name, **fields)
        except ReceiptError as exc:
            if self._first_failure is None:
                self._first_failure = exc
            if not self._cleanup_started and self.receipt.attempt is not None:
                raise

    def _flush_poll_safe(self) -> None:
        try:
            self._flush_poll()
        except ReceiptError as exc:
            if self._first_failure is None:
                self._first_failure = exc

    @staticmethod
    def _chat_editor_state(path: Path) -> str:
        """Classify the measured editor chrome before any chat OCR or input."""
        try:
            with Image.open(path) as image_raw:
                image = image_raw.convert("L")
                if image.size != CHAT_CROP_SIZE:
                    raise OperatorError("fixed chat editor crop geometry is malformed")
                left, _top, width, _height = CHAT_OCR_BOX
                rows = {
                    row: [image.getpixel((column, row)) for column in range(left, left + width)]
                    for row in (4, 7, 23, 25)
                }

            def median(row: int) -> int:
                values = sorted(rows[row])
                return values[len(values) // 2]

            def count_at_least(row: int, threshold: int) -> int:
                return sum(value >= threshold for value in rows[row])

            def median_delta(first: int, second: int) -> int:
                values = sorted(rows[first][index] - rows[second][index] for index in range(width))
                return values[len(values) // 2]

            def count_positive_delta(first: int, second: int) -> int:
                return sum(rows[first][index] > rows[second][index] for index in range(width))

            active = (
                median(4) >= 130
                and median(7) >= 180
                and median(23) >= 180
                and median(25) >= 120
                and count_at_least(7, 150) >= 380
                and count_at_least(23, 150) >= 380
            )
            # The closed editor is transparent over the world, so its absolute
            # luminance changes with the scene.  Its measured chrome profile is
            # stable: the top edge is lighter than the inset row, the separator
            # remains brighter than the inset, and the lower edge is nearly
            # level.  Requiring that profile across most columns rejects flat
            # dark crops and active/mixed chrome without pinning the background.
            top_edge = median_delta(4, 7)
            separator = median_delta(23, 7)
            lower_edge = median_delta(25, 23)
            inactive = (
                24 <= top_edge <= 45
                and 20 <= separator <= 36
                and -5 <= lower_edge <= 9
                and count_positive_delta(4, 7) >= width * 0.98
                and count_positive_delta(23, 7) >= width * 0.95
            )
            if active:
                return CHAT_EDITOR_ACTIVE
            if inactive:
                return CHAT_EDITOR_INACTIVE
        except OperatorError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            raise OperatorError("chat editor visual state is unavailable") from exc
        raise OperatorError("chat editor visual state is unknown")

    @staticmethod
    def _ocr_file(path: Path) -> str:
        temporary: Path | None = None
        try:
            with Image.open(path) as image_raw:
                image = image_raw.convert("L")
                if image.size != CHAT_CROP_SIZE:
                    raise OperatorError("fixed chat OCR crop geometry is malformed")
                left, top, width, height = CHAT_OCR_BOX
                if (
                    left < 0
                    or top < 0
                    or width <= 0
                    or height <= 0
                    or left + width > image.width
                    or top + height > image.height
                ):
                    raise OperatorError("fixed chat OCR interior geometry is malformed")
                prepared = image.crop((left, top, left + width, top + height))
                prepared = ImageOps.autocontrast(prepared, cutoff=1)
                # The focused chat editor draws a two-column insertion cursor
                # through the body.  Its probe height is 13/13; the tallest
                # glyph/rule probe is 11/13.  Drop columns at >=12/13, leaving
                # a one-row margin, while preserving grayscale command pixels.
                foreground_probe = prepared.point(lambda value: 255 if value > CHAT_CURSOR_THRESHOLD else 0)
                for column in range(width):
                    foreground = sum(foreground_probe.getpixel((column, row)) // 255 for row in range(height))
                    if foreground >= height - 1:
                        for row in range(height):
                            prepared.putpixel((column, row), 0)
                prepared = prepared.resize(
                    (width * CHAT_OCR_SCALE, height * CHAT_OCR_SCALE),
                    Image.Resampling.BICUBIC,
                )
                temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.ocr.png")
                prepared.save(temporary, format="PNG")
                os.chmod(temporary, 0o600)
            result = subprocess.run(
                ["tesseract", str(temporary), "stdout", "--psm", "7"],
                check=True,
                capture_output=True,
                text=True,
                timeout=2.0,
            )
        except OperatorError:
            raise
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise OperatorError("fixed chat OCR failed") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except OSError:
                    pass
        return result.stdout

    @contextmanager
    def _signals(self):
        def handle(signum: int, _frame: Any) -> None:
            self._aborted = True
            if self._cleanup_started:
                return
            raise OperatorInterrupted(f"handled signal {signum}")

        if threading_current_main():
            for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
                self._old_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, handle)
        try:
            yield
        finally:
            if not self._defer_signal_restore:
                self._restore_signals()

    def _restore_signals(self) -> None:
        for signum, handler in self._old_handlers.items():
            signal.signal(signum, handler)
        self._old_handlers.clear()

    def _check_not_aborted(self) -> None:
        if getattr(self, "_aborted", False):
            raise OperatorInterrupted("helper operation interrupted")

    def _reservation_from_reply(self, reply: Mapping[str, Any]) -> ReservationBinding:
        reservation = reply.get("reservation")
        parsed = ReservationBinding.from_record(
            reservation,
            expected_generation=self.generation,
            now=self.clock(),
        )
        handshake = reply.get("handshake")
        if not isinstance(handshake, dict):
            raise OperatorError("native construction reply has no handshake")
        state = handshake.get("state")
        if state not in HANDSHAKE_STATES:
            raise OperatorError("native construction handshake state is unknown")
        return parsed

    def _validate_state_history(
        self, reply: Mapping[str, Any], *, reset: bool = False
    ) -> list[tuple[str, float]]:
        handshake = reply.get("handshake")
        raw = reply.get("stateHistory")
        if raw is None and isinstance(handshake, Mapping):
            raw = handshake.get("stateHistory")
        if not isinstance(raw, list) or not raw or len(raw) > MAX_STATE_HISTORY:
            raise OperatorError("native stateHistory is missing or outside its bound")
        parsed: list[tuple[str, float]] = []
        for entry in raw:
            if not isinstance(entry, Mapping) or set(entry) != STATE_HISTORY_KEYS:
                raise OperatorError("native stateHistory entry fields are malformed")
            state = entry["state"]
            clock = entry["nativeClock"]
            if state not in (HANDSHAKE_STATES | TERMINAL_FAILURE_HANDSHAKES):
                raise OperatorError("native stateHistory contains an unknown state")
            if (
                isinstance(clock, bool)
                or not isinstance(clock, (int, float))
                or not math.isfinite(float(clock))
            ):
                raise OperatorError("native stateHistory clock is malformed")
            if parsed and float(clock) < parsed[-1][1]:
                raise OperatorError("native stateHistory clocks regressed")
            parsed.append((state, float(clock)))
        states = [state for state, _clock in parsed]
        if states[0] == "recovery_required":
            recovery_history_states = {"recovery_required", "recovered", "cancelled", "error"}
            recovery_history_states |= TERMINAL_FAILURE_HANDSHAKES
            if states[:1] != ["recovery_required"] or any(
                state not in recovery_history_states for state in states
            ):
                raise OperatorError("native recovery stateHistory is malformed")
            if len(states) > 2 or len(states) == 2 and states[-1] not in {"recovered", "cancelled", "error"}:
                raise OperatorError("native recovery stateHistory order is invalid")
        else:
            terminal_error = states[-1] in ({"error", "cancelled"} | TERMINAL_FAILURE_HANDSHAKES)
            active_states = states[:-1] if terminal_error else states
            if not active_states:
                raise OperatorError("native construction error history has no prior state")
            try:
                indexes = [STATE_CHAIN.index(state) for state in active_states]
            except ValueError as exc:
                raise OperatorError("native construction stateHistory contains a terminal state") from exc
            if indexes != list(range(indexes[0], indexes[0] + len(indexes))) or indexes[0] != 0:
                raise OperatorError("native construction stateHistory skipped or regressed")
        if not reset and self._state_history:
            prior = self._state_history
            if parsed[: len(prior)] != prior:
                raise OperatorError("native stateHistory prior prefix changed")
        return parsed

    def _adopt_reply(
        self, reply: Mapping[str, Any], *, expected_state: str | None = None
    ) -> ReservationBinding:
        failure_state = self._terminal_failure_state(reply)
        if failure_state is not None:
            # A worker failure closes the daemon reservation before the owner
            # can necessarily observe it through a normal recheck.  Validate
            # the exact canonical ID/binding, retain the old actor capability,
            # and surface the causal failure for the pause/recovery path.
            self._validate_terminal_failure(reply, failure_state)
            self._handshake = failure_state
            self._calibration_terminal_error = True
            handshake = reply.get("handshake")
            reason = handshake.get("failure") if isinstance(handshake, Mapping) else None
            self._terminal_failure_message = (
                reason
                if isinstance(reason, str) and reason
                else "native construction reached a terminal failure"
            )
            self._state_history = self._validate_state_history(reply)
            raise OperatorError(self._terminal_failure_message)
        parsed = self._reservation_from_reply(reply)
        if self._binding is not None:
            self._binding.compare_immutable(parsed)
            if parsed.reservation_id != self._binding.reservation_id:
                raise OperatorError("reservation changed outside an explicit handoff")
            if parsed.reservation_kind != self._binding.reservation_kind:
                raise OperatorError("reservation kind changed outside an explicit handoff")
        parsed_history = self._validate_state_history(reply)
        state = self._handshake
        observed = self._handshake_from_reply(reply)
        if self._binding is not None:
            self._binding.validate_transition(parsed, handshake=observed)
        if observed == "error":
            self._handshake = observed
            raise OperatorError("native construction reached a terminal error")
        if parsed_history[-1][0] != observed:
            raise OperatorError("native handshake state does not match stateHistory")
        if state is not None and observed != state and self._state_history:
            if parsed_history[: len(self._state_history)] != self._state_history:
                raise OperatorError("native handshake stateHistory changed its immutable prefix")
        self._handshake = observed
        self._state_history = parsed_history
        if observed == "hold_required":
            # providerDeadline is emitted only for the initial construction
            # frame.  Keeping it after the canonical held boundary would
            # incorrectly cap the 99 update frames and later chat actions.
            self._phase_deadlines.pop("provider", None)
        if expected_state is not None and self._handshake != expected_state:
            raise OperatorError(f"unexpected native construction state: {self._handshake}")
        # Install the independent ownership capability before exposing the
        # binding to the workflow.  Bootstrap may extend the expiry by hours;
        # the acknowledged update must precede any dependent input.
        self._arm_cancellation_actor(parsed)
        self._reservation_deadline = parsed.expires_monotonic
        self._binding = parsed
        return parsed

    @staticmethod
    def _handshake_from_reply(reply: Mapping[str, Any]) -> str:
        handshake = reply.get("handshake")
        state = handshake.get("state") if isinstance(handshake, dict) else None
        if state not in (HANDSHAKE_STATES | TERMINAL_FAILURE_HANDSHAKES):
            raise OperatorError("native construction handshake state is unknown")
        return state

    @staticmethod
    def _terminal_failure_state(reply: Mapping[str, Any]) -> str | None:
        handshake = reply.get("handshake")
        state = handshake.get("state") if isinstance(handshake, Mapping) else None
        reservation = reply.get("reservation")
        # recovery_required is the normal live handshake for a recovery
        # reservation; it is a failure only when the daemon has archived the
        # same exact ID with a terminal reservation state.
        if _is_normal_recovery_reply(handshake, reservation):
            return None
        return state if state in TERMINAL_FAILURE_HANDSHAKES else None

    def _validate_terminal_failure(self, reply: Mapping[str, Any], state: str) -> None:
        if self._binding is None:
            raise OperatorError("native terminal failure has no admitted reservation")
        record = reply.get("reservation")
        if not isinstance(record, Mapping) or set(record) != set(CANONICAL_RESERVATION_FIELDS):
            raise OperatorError("native terminal failure binding is malformed")
        if record.get("reservationId") != self._binding.reservation_id:
            raise OperatorError("native terminal failure reservation changed")
        if record.get("controllerGeneration") != self._binding.controller_generation:
            raise OperatorError("native terminal failure generation changed")
        if record.get("reservationState") != state:
            raise OperatorError("native terminal failure reservation state is inconsistent")
        # Validate all sealed fields and types without admitting the failure
        # state as a successful reservation lifecycle state.
        candidate = dict(record)
        candidate["reservationState"] = self._binding.reservation_state
        parsed = ReservationBinding.from_record(
            candidate, expected_generation=self._binding.controller_generation, now=None
        )
        self._binding.compare_immutable(parsed)
        history = reply.get("stateHistory")
        handshake = reply.get("handshake")
        if history is None and isinstance(handshake, Mapping):
            history = handshake.get("stateHistory")
        if (
            not isinstance(history, list)
            or not history
            or not isinstance(history[-1], Mapping)
            or history[-1].get("state") != state
        ):
            raise OperatorError("native terminal failure history is malformed")

    def _record_operation_deadline(self, reply: Mapping[str, Any]) -> None:
        handshake = reply.get("handshake")
        if not isinstance(handshake, Mapping):
            return
        values: dict[str, float] = {}
        for name in (
            "operationDeadline",
            "constructorClickDeadline",
            "providerDeadline",
            "finalizeHookDeadline",
            "finalizeDeadline",
        ):
            value = handshake.get(name)
            if value is None:
                continue
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise OperatorError(f"native {name} is malformed")
            values[name] = float(value)
        if "operationDeadline" in values:
            value = values["operationDeadline"]
            if self._operation_deadline is None:
                self._operation_deadline = value
            else:
                # A later status response must never extend the causal bound
                # already observed for this operation.
                self._operation_deadline = min(self._operation_deadline, value)
        if "constructorClickDeadline" in values:
            value = values["constructorClickDeadline"]
            existing = self._phase_deadlines.get("constructor")
            self._phase_deadlines["constructor"] = value if existing is None else min(existing, value)
        if "providerDeadline" in values and self._handshake in {"running", "construction_released"}:
            value = values["providerDeadline"]
            existing = self._phase_deadlines.get("provider")
            self._phase_deadlines["provider"] = value if existing is None else min(existing, value)
        if "finalizeHookDeadline" in values:
            value = values["finalizeHookDeadline"]
            existing = self._phase_deadlines.get("finalize_hook")
            self._phase_deadlines["finalize_hook"] = value if existing is None else min(existing, value)
        if "finalizeDeadline" in values:
            value = values["finalizeDeadline"]
            existing = self._phase_deadlines.get("finalize")
            self._phase_deadlines["finalize"] = value if existing is None else min(existing, value)

    def _await_state(self, expected_state: str, *, min_history_index: int | None = None) -> dict[str, Any]:
        if expected_state not in HANDSHAKE_STATES:
            raise OperatorError("native construction wait state is unknown")
        start_index = len(self._state_history) if min_history_index is None else min_history_index
        while self.clock() < self._deadline_for_state(expected_state):
            reply = self._recheck()
            if expected_state in [state for state, _clock in self._state_history[start_index:]]:
                return reply
            if self._handshake == "error":
                raise OperatorError("native construction reached a terminal error")
            remaining = self._deadline_for_state(expected_state) - self.clock()
            self.sleep(min(0.05, max(0.0, remaining)))
        raise OperatorError(f"native construction state {expected_state} did not appear before its deadline")

    def _deadline_for_state(self, state: str) -> float:
        if state in {"construction_released", "hold_required"} and "provider" in self._phase_deadlines:
            deadline = self._deadline("provider")
        elif state in {"finalize_required", "finalized"}:
            deadline = self._deadline("finalize")
        else:
            deadline = self._deadline()
        if deadline is None:
            raise OperatorError("native operation deadline is unavailable")
        return deadline

    def _adopt_handoff(self, reply: Mapping[str, Any], *, expected_state: str) -> ReservationBinding:
        parsed = ReservationBinding.from_record(
            reply.get("reservation"), expected_generation=None, now=self.clock()
        )
        if self._binding is not None:
            if parsed.controller_generation != self._binding.controller_generation:
                for field_name in (
                    "desktop_lease_held",
                    "guardian_armed",
                    "owner_pid",
                    "owner_process_start",
                    "profile_fingerprint",
                    "identity_fingerprint",
                    "b0",
                    "expected_constructor_boot",
                ):
                    if getattr(self._binding, field_name) != getattr(parsed, field_name):
                        raise OperatorError("native recovery binding changed outside the sealed handoff")
                if parsed.controller_generation <= self._binding.controller_generation:
                    raise OperatorError("native recovery generation did not advance")
                if parsed.guardian_generation <= self._binding.guardian_generation:
                    raise OperatorError("native recovery Guardian generation did not advance")
            else:
                self._binding.compare_immutable(parsed)
        elif parsed.controller_generation != self.generation:
            raise OperatorError("native recovery generation is not the requested generation")
        observed = self._handshake_from_reply(reply)
        parsed_history = self._validate_state_history(reply, reset=True)
        if observed != expected_state:
            raise OperatorError(f"unexpected native construction state: {observed}")
        if parsed_history[-1][0] != observed:
            raise OperatorError("native handoff state does not match stateHistory")
        self.generation = parsed.controller_generation
        self._arm_cancellation_actor(parsed)
        self._handshake = observed
        self._state_history = parsed_history
        self._binding = parsed
        return parsed

    def _reply_state(self, reply: Mapping[str, Any]) -> str:
        handshake = reply.get("handshake")
        if not isinstance(handshake, dict) or handshake.get("state") not in HANDSHAKE_STATES:
            raise OperatorError("native construction handshake is malformed")
        state = handshake["state"]
        if state == "error":
            raise OperatorError("native construction reached a terminal error")
        return state

    def _recheck(
        self, expected_state: str | None = None, *, deadline_monotonic: float | None = None
    ) -> dict[str, Any]:
        self._check_not_aborted()
        if self._binding is None:
            raise OperatorError("native reservation was not admitted")
        deadline = deadline_monotonic or self._deadline() or self._binding.expires_monotonic
        if self._actor_admission:
            reply = self._actor_call(
                "recheck",
                generation=self.generation,
                reservation_id=self._binding.reservation_id,
                deadline=deadline,
            )
        else:
            reply = self.client.recheck(
                self.generation,
                self._binding.reservation_id,
                deadline=deadline,
            )
        self._adopt_reply(reply, expected_state=expected_state)
        self._record_operation_deadline(reply)
        if self._poll_state == self._handshake:
            self._poll_count += 1
        else:
            self._flush_poll()
            self._poll_state = self._handshake
            self._poll_clock = self._state_history[-1][1]
            self._poll_count = 1
        return reply

    def _flush_poll(self) -> None:
        if self._poll_state is not None and self._poll_count:
            self._event(
                "daemon_recheck_summary",
                state=self._poll_state,
                nativeClock=self._poll_clock,
                count=self._poll_count,
                generation=self.generation,
            )
        self._poll_state = None
        self._poll_clock = None
        self._poll_count = 0

    def _identity_guard(self, deadline_monotonic: float | None = None) -> Any:
        self._check_not_aborted()
        identity = self.screen.resolve_identity(
            require_foreground=True,
            deadline_monotonic=deadline_monotonic if deadline_monotonic is not None else self._deadline(),
        )
        if (
            self._binding is not None
            and getattr(identity, "fingerprint", None) != self._binding.identity_fingerprint
        ):
            raise OperatorError("game identity changed before input")
        return identity

    def _capture(
        self,
        label: str,
        *,
        require_exclusive: str | None = None,
        require_dual: bool = False,
        crop: bool = False,
        retain_chat_frame: str | None = None,
        deadline_monotonic: float | None = None,
    ) -> tuple[Path, Any, TemplateMatch | None]:
        capture_started = self.clock()
        deadline = deadline_monotonic if deadline_monotonic is not None else self._deadline()
        identity = self._identity_guard(deadline)
        target_root = self.receipt.attempt
        if target_root is None:
            raise ReceiptError("receipt was not initialized")
        if require_dual and require_exclusive is not None:
            raise OperatorError("capture cannot request exclusive and dual template proofs")
        if require_dual or require_exclusive is not None:
            identity = self._park_pointer(identity, deadline_monotonic=deadline)
        capture_sequence: int | None = None
        if retain_chat_frame is not None:
            self._chat_capture_sequence += 1
            capture_sequence = self._chat_capture_sequence
            path = target_root / (f"chat-{capture_sequence:08d}-{self._safe_chat_label(label)}.png")
        else:
            path = target_root / f"{label}.png"
        before = getattr(identity, "fingerprint", None)
        captured = self.screen.capture(path, identity=identity, deadline_monotonic=deadline)
        after = self.screen.resolve_identity(require_foreground=True, deadline_monotonic=deadline)
        if (
            getattr(captured, "fingerprint", before) != before
            or getattr(after, "fingerprint", before) != before
        ):
            raise OperatorError("fresh capture identity changed")
        if retain_chat_frame is not None:
            self._last_chat_capture_record = self._retain_chat_fullframe(
                path,
                label=label,
                use=retain_chat_frame,
                sequence=capture_sequence if capture_sequence is not None else 0,
            )
        else:
            self._last_chat_capture_record = None
        if crop:
            crop_box = getattr(self.screen, "chat_crop", None)
            if not (
                isinstance(crop_box, tuple)
                and len(crop_box) == 4
                and all(isinstance(value, int) and not isinstance(value, bool) for value in crop_box)
            ):
                raise OperatorError("reviewed chat crop is unavailable")
            left, top, width, height = crop_box
            if left < 0 or top < 0 or width <= 0 or height <= 0:
                raise OperatorError("reviewed chat crop is malformed")
            temporary = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
            try:
                with Image.open(path) as image_raw:
                    image = image_raw.convert("RGB")
                    if left + width > image.width or top + height > image.height:
                        raise OperatorError("reviewed chat crop is outside the fresh capture")
                    image.crop((left, top, left + width, top + height)).save(temporary, format="PNG")
                os.chmod(temporary, 0o600)
                os.replace(temporary, path)
            except OperatorError:
                try:
                    temporary.unlink()
                except OSError:
                    pass
                raise
            except (OSError, ValueError) as exc:
                try:
                    temporary.unlink()
                except OSError:
                    pass
                raise OperatorError("fresh chat crop failed") from exc
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
                raise OperatorError("fresh capture is not private")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as exc:
            raise OperatorError("fresh capture is unavailable") from exc
        if retain_chat_frame is not None and self._last_chat_capture_record is not None:
            self._last_chat_capture_record["derived_crop_sha256"] = digest
            self._last_chat_capture_record["derived_crop_filename"] = path.name
            try:
                with Image.open(path) as derived:
                    self._last_chat_capture_record["derived_crop_width"] = derived.width
                    self._last_chat_capture_record["derived_crop_height"] = derived.height
            except (OSError, ValueError):
                self._last_chat_capture_record["derived_crop_width"] = None
                self._last_chat_capture_record["derived_crop_height"] = None
        self._event(
            "capture",
            label=label,
            sha256=digest,
            identity=before,
            captureStartedMonotonic=capture_started,
            captureCompletedMonotonic=self.clock(),
        )
        self._last_capture_completed = self.clock()
        region = self._control_region(after)
        if (require_dual or require_exclusive is not None) and region is None:
            raise OperatorError("measured control transform is unavailable")
        if require_dual:
            match, _restore_match = self.matcher.require_dual_positive(
                path,
                region=region,
                deadline_monotonic=deadline,
            )
        else:
            match = (
                self.matcher.require_exclusive(
                    path,
                    require_exclusive,
                    region=region,
                    deadline_monotonic=deadline,
                )
                if require_exclusive
                else None
            )
        return path, after, match

    @staticmethod
    def _control_region(identity: Any) -> tuple[int, int, int, int] | None:
        geometry = getattr(identity, "child_geometry", None)
        if not (isinstance(geometry, tuple) and len(geometry) == 4):
            return None
        left, top, width, height = geometry
        if not (
            all(isinstance(value, int) and not isinstance(value, bool) for value in geometry)
            and (left, top) == (0, 0)
            and width >= 1280
            and height >= 720
        ):
            return None
        scale_x = width / 1920.0
        scale_y = height / 1080.0
        if not (math.isfinite(scale_x) and math.isfinite(scale_y)) or abs(scale_x - scale_y) > 0.01:
            return None
        # Commands owns a fixed 220x84 UI-unit panel at TOPRIGHT (-24,-24).
        panel_width = round(220 * scale_x)
        panel_height = round(84 * scale_y)
        inset = round(24 * scale_x)
        return (width - inset - panel_width, inset, panel_width, panel_height)

    @staticmethod
    def _source_control_rect(identity: Any, role: str) -> tuple[int, int, int, int] | None:
        geometry = getattr(identity, "child_geometry", None)
        if role not in {"construct", "restore"} or not (
            isinstance(geometry, tuple)
            and len(geometry) == 4
            and all(isinstance(value, int) and not isinstance(value, bool) for value in geometry)
        ):
            return None
        left, top, width, height = geometry
        if (left, top) != (0, 0) or width < 1280 or height < 720:
            return None
        scale_x = width / 1920.0
        scale_y = height / 1080.0
        if not math.isfinite(scale_x) or not math.isfinite(scale_y) or abs(scale_x - scale_y) > 0.01:
            return None
        panel_x = width - round(24 * scale_x) - round(220 * scale_x)
        panel_y = round(24 * scale_y)
        offset_x = 8 if role == "construct" else 112
        return (
            panel_x + round(offset_x * scale_x),
            panel_y + round(8 * scale_y),
            round(100 * scale_x),
            round(28 * scale_y),
        )

    @staticmethod
    def _contains_point(rect: tuple[int, int, int, int], point: tuple[int, int]) -> bool:
        left, top, width, height = rect
        return left <= point[0] < left + width and top <= point[1] < top + height

    def _park_pointer(self, identity: Any, *, deadline_monotonic: float | None) -> Any:
        """Move an unsafe pointer away from controls before a fresh visual proof."""

        deadline = deadline_monotonic if deadline_monotonic is not None else self._deadline()
        if deadline is None or not math.isfinite(float(deadline)) or float(deadline) <= self.clock():
            raise OperatorError("pointer parking deadline is unavailable or expired")
        region = self._control_region(identity)
        if region is None:
            raise OperatorError("measured control transform is unavailable")
        geometry = getattr(identity, "child_geometry", None)
        if not (
            isinstance(geometry, tuple)
            and len(geometry) == 4
            and all(isinstance(value, int) and not isinstance(value, bool) for value in geometry)
        ):
            raise OperatorError("verified client geometry is malformed")
        client_left, client_top, client_width, client_height = geometry
        if client_width <= 0 or client_height <= 0:
            raise OperatorError("verified client geometry is malformed")
        canvas = getattr(self.screen, "agent_crop", None)
        if not (
            isinstance(canvas, tuple)
            and len(canvas) == 4
            and all(isinstance(value, int) and not isinstance(value, bool) for value in canvas)
        ):
            raise OperatorError("telemetry canvas geometry is unavailable")
        canvas_left, canvas_top, canvas_width, canvas_height = canvas
        if canvas_width <= 0 or canvas_height <= 0:
            raise OperatorError("telemetry canvas geometry is malformed")
        canvas_rect = (canvas_left, canvas_top, canvas_width, canvas_height)
        if (
            canvas_left < client_left
            or canvas_top < client_top
            or canvas_left + canvas_width > client_left + client_width
            or canvas_top + canvas_height > client_top + client_height
        ):
            raise OperatorError("telemetry canvas is outside the verified client")

        if getattr(self, "_binding", None) is not None:
            self._recheck()
            identity = self._identity_guard(deadline)
            if getattr(identity, "child_geometry", None) != geometry:
                raise OperatorError("client geometry changed before pointer parking")

        pointer = self.screen.pointer_position(deadline_monotonic=deadline)
        if (
            not isinstance(pointer, tuple)
            or len(pointer) != 3
            or any(isinstance(value, bool) or not isinstance(value, int) for value in pointer)
        ):
            raise OperatorError("pointer position is malformed")
        current_point = (pointer[0], pointer[1])
        if not self._contains_point(region, current_point) and not self._contains_point(
            canvas_rect, current_point
        ):
            return identity

        margin = max(1, min(client_width, client_height) // 64)
        x_values = (
            client_left + client_width // 2,
            client_left + client_width // 4,
            client_left + (client_width * 3) // 4,
            client_left + margin,
            client_left + client_width - margin - 1,
        )
        y_values = (
            client_top + client_height // 2,
            client_top + client_height // 4,
            client_top + (client_height * 3) // 4,
            client_top + margin,
            client_top + client_height - margin - 1,
        )
        parking_rect: tuple[int, int, int, int] | None = None
        parking_point: tuple[int, int] | None = None
        for y in y_values:
            for x in x_values:
                candidate = (x, y)
                proposed = (x - 5, y - 5, 11, 11)
                if (
                    client_left <= proposed[0]
                    and client_top <= proposed[1]
                    and proposed[0] + proposed[2] <= client_left + client_width
                    and proposed[1] + proposed[3] <= client_top + client_height
                    and not _rectangles_intersect(proposed, region)
                    and not _rectangles_intersect(proposed, canvas_rect)
                ):
                    parking_point = candidate
                    parking_rect = proposed
                    break
            if parking_point is not None:
                break
        if parking_point is None or parking_rect is None:
            raise OperatorError("no safe pointer parking point is available")
        move_pointer = getattr(self.input, "move_pointer", None)
        if not callable(move_pointer):
            raise OperatorError("owned pointer parking is unavailable")

        sequence = int(getattr(self, "_gesture_sequence", 0)) + 1
        self._gesture_sequence = sequence
        target_xids = getattr(identity, "target_event_xids", frozenset())
        child_xid = getattr(identity, "child_xid", next(iter(target_xids), 0))
        if not isinstance(child_xid, int) or child_xid <= 0:
            raise OperatorError("pointer parking identity has no child window")
        binding = BindingMetadata(
            str(getattr(identity, "fingerprint", "native-parking")),
            f"parking-{sequence}",
            sequence,
            max(1, int(getattr(self, "generation", 1))),
            child_xid,
            (child_xid,),
            source="native-parking",
        )
        try:
            gesture = plan_human_pointer(
                Point(pointer[0] - client_left, pointer[1] - client_top),
                PointerTarget(
                    "native-pointer-parking",
                    Rect(
                        parking_rect[0] - client_left,
                        parking_rect[1] - client_top,
                        parking_rect[2],
                        parking_rect[3],
                    ),
                    binding,
                    padding=2,
                ),
                Rect(0, 0, client_width, client_height),
                button=None,
                profile=_NATIVE_POINTER_PROFILE,
            )
        except (PointerExecutionError, ValueError) as exc:
            raise OperatorError(f"pointer parking gesture cannot be planned: {exc}") from exc

        def before_press() -> None:
            # This is the final admission boundary: the emitter is already
            # watchdog-owned, but no native input may be sent until the same
            # reservation and the exact foreground geometry remain current.
            if getattr(self, "_binding", None) is not None:
                self._recheck(deadline_monotonic=deadline)
            fresh = self._identity_guard(deadline)
            if getattr(fresh, "fingerprint", None) != getattr(identity, "fingerprint", None):
                raise OperatorError("game identity changed before pointer parking dispatch")
            if getattr(fresh, "child_geometry", None) != geometry:
                raise OperatorError("client geometry changed before pointer parking dispatch")

        timing = move_pointer(
            gesture,
            identity=identity,
            before_press=before_press,
            deadline_monotonic=deadline,
        )
        parked_identity = self._identity_guard(deadline)
        if getattr(parked_identity, "fingerprint", None) != getattr(identity, "fingerprint", None):
            raise OperatorError("game identity changed after pointer parking")
        endpoint = self.screen.pointer_position(deadline_monotonic=deadline)
        expected_endpoint = (
            client_left + int(gesture.endpoint.x),
            client_top + int(gesture.endpoint.y),
        )
        if endpoint[:2] != expected_endpoint:
            raise OperatorError("pointer parking endpoint changed")
        target_xids = getattr(parked_identity, "target_event_xids", frozenset())
        if target_xids and endpoint[2] not in target_xids:
            raise OperatorError("pointer parking target changed")
        path_digest = hashlib.sha256(
            repr(
                tuple((sample.at, sample.point.x, sample.point.y) for sample in gesture.trajectory.samples)
            ).encode("ascii")
        ).hexdigest()
        self._event(
            "pointer_parked",
            x=expected_endpoint[0],
            y=expected_endpoint[1],
            seed=gesture.seed,
            movementDuration=gesture.movement_duration,
            submovementCount=len(gesture.waypoints),
            pathSha256=path_digest,
            **(timing or {}),
        )
        return parked_identity

    def _post_reload_controls(self) -> None:
        deadline = self._deadline()
        if deadline is None:
            raise OperatorError("post-reload proof deadline is unavailable")
        last_error: Exception | None = None
        while self.clock() < deadline:
            try:
                path, _identity, _matches = self._capture("post-reload-controls", require_dual=True)
                crop = getattr(self.screen, "agent_crop", None)
                if not (
                    isinstance(crop, tuple)
                    and len(crop) == 4
                    and all(isinstance(value, int) and not isinstance(value, bool) for value in crop)
                ):
                    raise OperatorError("native Agent crop is unavailable")
                left, top, width, height = crop
                temporary = path.with_name(f".{path.name}.agent.tmp")
                try:
                    with Image.open(path) as image_raw:
                        image = image_raw.convert("RGB")
                        if left < 0 or top < 0 or width <= 0 or height <= 0:
                            raise OperatorError("native Agent crop is malformed")
                        if left + width > image.width or top + height > image.height:
                            raise OperatorError("native Agent crop is outside the capture")
                        image.crop((left, top, left + width, top + height)).save(temporary, format="PNG")
                    os.chmod(temporary, 0o600)
                    if not self.absence_checker(temporary, expected_size=(width, height)):
                        raise OperatorError("post-reload Agent absence was not proven")
                finally:
                    try:
                        temporary.unlink()
                    except OSError:
                        pass
                self._event("post_reload_proof", controls=True, agentAbsent=True)
                return
            except OperatorError as exc:
                if "classifier is unavailable" in str(exc):
                    raise
                last_error = exc
            remaining = deadline - self.clock()
            self.sleep(min(0.1, max(0.0, remaining)))
        raise last_error or OperatorError("post-reload proof did not settle")

    def _await_reload_boundary(self, *, proof: str = "ordinary") -> None:
        waiter = getattr(self.screen, "wait_reload_boundary", None)
        if not callable(waiter):
            raise OperatorError("native reload readiness boundary is unavailable")
        waiter(deadline_monotonic=self._deadline(), proof=proof)
        self._event("reload_boundary", monotonic=self.clock(), proof=proof)

    def _chat(
        self, command: str, *, expected_state: str | None = None, post_state: str | None = None
    ) -> None:
        if command not in FIXED_COMMAND_SET:
            raise OperatorError("chat command is not fixed")
        if command == "/crowley agent release" and "provider" in self._phase_deadlines:
            phase = "provider"
        elif command == "/crowley agent finalize":
            phase = "finalize"
        else:
            phase = None
        deadline = self._deadline(phase)
        if deadline is None:
            raise OperatorError("chat command deadline is unavailable")
        self._recheck(expected_state, deadline_monotonic=deadline)
        closed_path, _closed_identity, _ = self._capture(
            f"chat-closed-{hashlib.sha256(command.encode()).hexdigest()[:12]}",
            crop=True,
            retain_chat_frame="chat_editor_state",
            deadline_monotonic=deadline,
        )
        editor_state = self.chat_state(closed_path)
        if editor_state == CHAT_EDITOR_ACTIVE:
            raise OperatorError("chat editor is active")
        if editor_state != CHAT_EDITOR_INACTIVE:
            raise OperatorError("chat editor visual state is unknown")
        self._event("chat_editor_state", state=editor_state)
        self._recheck(expected_state, deadline_monotonic=deadline)
        self._identity_guard(deadline)
        # The complete fixed sequence must fit before the first physical
        # dispatch.  Each emitter retains its own independent release path.
        required = sum(
            _action_timeout(action)
            for action in (
                ("key", "Return"),
                ("type", "--delay", "130", command),
                ("key", "Return"),
            )
        )
        if deadline - time.monotonic() < required:
            raise OperatorError("chat command cannot fit before its deadline")
        self._event("key_dispatch", key="Return", edge="open")
        self.input.key_return(deadline_monotonic=deadline)
        self._recheck(expected_state, deadline_monotonic=deadline)
        self._identity_guard(deadline)
        self._event("text_dispatch", command=command)
        self.input.type_command(command, deadline_monotonic=deadline)
        buffer_path, _identity, _ = self._capture(
            f"chat-{hashlib.sha256(command.encode()).hexdigest()[:12]}",
            crop=True,
            retain_chat_frame="typed_chat_readback",
            deadline_monotonic=deadline,
        )
        observed_raw = self.ocr(buffer_path)
        observed = normalize_ocr(observed_raw)
        expected = normalize_ocr(command)
        self._event("chat_readback", command=command, accepted=observed == expected)
        if observed != expected:
            try:
                self._recheck(expected_state, deadline_monotonic=deadline)
                self._identity_guard(deadline)
                self.input.key_escape(deadline_monotonic=deadline)
                self._event("key_dispatch", key="Escape", edge="mismatch")
            except Exception:
                pass
            self._record_chat_readback_diagnostic(
                command=command,
                typed_path=buffer_path,
                observed_raw=observed_raw,
                observed=observed,
                expected=expected,
            )
            raise OperatorError("chat command readback mismatch")
        self._recheck(expected_state, deadline_monotonic=deadline)
        self._identity_guard(deadline)
        if deadline - time.monotonic() < _action_timeout(("key", "Return")):
            raise OperatorError("chat submit cannot fit before its deadline")
        self._history_before_action = len(self._state_history)
        self._event("key_dispatch", key="Return", edge="submit")
        self.input.key_return(deadline_monotonic=deadline)
        if command == "/crowley agent release":
            self._release_dispatch = self.clock()
            self._event("release_dispatch", monotonic=self._release_dispatch)
        self._event("chat_submitted", command=command)
        # A command may deliberately preserve the daemon phase (defer/reload).
        # Only an explicit post-state is a causal transition to await.
        target_state = post_state
        if target_state is not None:
            self._await_state(target_state, min_history_index=self._history_before_action)

    def _click(
        self,
        role: str,
        *,
        expected_state: str | None = None,
        _rate_permit: ClickRatePermit | None = None,
    ) -> None:
        if role == "construct" and self.mode == MODE_MEASURE_DEFERRED:
            if "constructor" not in self._phase_deadlines:
                raise OperatorError("native constructor click deadline is unavailable")
            if self._release_dispatch is None:
                raise OperatorError("native release dispatch boundary is unavailable")
            click_deadline = min(self._phase_deadlines["constructor"], self._release_dispatch + 10.0)
            self._phase_deadlines["constructor"] = click_deadline
            if click_deadline <= self.clock():
                raise OperatorError("native constructor click deadline expired")
        click_phase = "constructor" if role == "construct" else None
        dispatch_deadline = self._deadline(click_phase)
        if dispatch_deadline is None:
            raise OperatorError("click deadline is unavailable")
        if isinstance(self.input, FixedInput) and _rate_permit is None:
            try:
                with acquire_click_rate_permit(
                    deadline_monotonic=dispatch_deadline,
                    cancellation_check=self._check_not_aborted,
                ) as permit:
                    self._click(role, expected_state=expected_state, _rate_permit=permit)
            except PointerExecutionError as exc:
                raise OperatorError(f"click rate admission failed: {exc}") from exc
            return

        self._recheck(expected_state)
        _path, identity, match = self._capture(f"{role}-control", require_exclusive=role)
        if match is None:
            raise OperatorError("click has no fresh template match")
        current = self.screen.resolve_identity(require_foreground=True, deadline_monotonic=self._deadline())
        if getattr(current, "fingerprint", None) != getattr(identity, "fingerprint", None):
            raise OperatorError("identity changed before click")
        self._recheck(expected_state)

        source_rect = self._source_control_rect(identity, role)
        if source_rect is None:
            raise OperatorError("source-derived click control geometry is unavailable")
        match_x, match_y, match_width, match_height = match.box
        source_x, source_y, source_width, source_height = source_rect
        if not (
            match_width > 2
            and match_height > 2
            and source_x <= match_x
            and source_y <= match_y
            and match_x + match_width <= source_x + source_width
            and match_y + match_height <= source_y + source_height
        ):
            raise OperatorError("fresh template match is outside its source-derived button")
        geometry = getattr(current, "child_geometry", None)
        if not (
            isinstance(geometry, tuple)
            and len(geometry) == 4
            and all(isinstance(value, int) and not isinstance(value, bool) for value in geometry)
        ):
            raise OperatorError("click identity geometry is malformed")
        client_left, client_top, client_width, client_height = geometry
        pointer_before = self.screen.pointer_position(deadline_monotonic=dispatch_deadline)
        if (
            not isinstance(pointer_before, tuple)
            or len(pointer_before) != 3
            or any(isinstance(value, bool) or not isinstance(value, int) for value in pointer_before)
        ):
            raise OperatorError("pointer start is unavailable for the bounded click gesture")
        target_xids = getattr(current, "target_event_xids", frozenset())
        child_xid = getattr(current, "child_xid", next(iter(target_xids), 0))
        if not isinstance(child_xid, int) or child_xid <= 0:
            raise OperatorError("click identity has no target child window")
        sequence = int(getattr(self, "_gesture_sequence", 0)) + 1
        self._gesture_sequence = sequence
        binding = BindingMetadata(
            str(getattr(current, "fingerprint", "native-template")),
            match.capture_sha256,
            sequence,
            max(1, int(self.generation)),
            child_xid,
            (child_xid,),
            source="native-template-control",
        )
        try:
            gesture = plan_human_pointer(
                Point(pointer_before[0] - client_left, pointer_before[1] - client_top),
                PointerTarget(
                    f"native-{role}", Rect(match_x, match_y, match_width, match_height), binding, padding=1
                ),
                Rect(0, 0, client_width, client_height),
                button=PointerButton.LEFT,
                profile=_NATIVE_POINTER_PROFILE,
            )
        except (PointerExecutionError, ValueError) as exc:
            raise OperatorError(f"bounded click gesture cannot be planned: {exc}") from exc
        expected_endpoint = (
            client_left + int(gesture.endpoint.x),
            client_top + int(gesture.endpoint.y),
        )

        def before_press() -> None:
            if self._last_capture_completed is None or self.clock() - self._last_capture_completed > 5.0:
                raise OperatorError("fresh click evidence exceeded its five-second bound")
            self._recheck(expected_state)
            fresh = self._identity_guard()
            if getattr(fresh, "fingerprint", None) != getattr(identity, "fingerprint", None):
                raise OperatorError("identity changed after reposition")
            endpoint = self.screen.pointer_position(deadline_monotonic=self._deadline())
            if endpoint[:2] != expected_endpoint:
                raise OperatorError("pointer moved before guarded press")
            target = getattr(fresh, "target_event_xids", frozenset())
            if target and endpoint[2] not in target:
                raise OperatorError("pointer target changed before guarded press")

        path_digest = hashlib.sha256(
            repr(
                tuple((sample.at, sample.point.x, sample.point.y) for sample in gesture.trajectory.samples)
            ).encode("ascii")
        ).hexdigest()
        self._event(
            "click",
            role=role,
            box=match.box,
            score=match.score,
            sourceControlRect=source_rect,
            matchCaptureSha256=match.capture_sha256,
            seed=gesture.seed,
            frozenEndpoint=(int(gesture.endpoint.x), int(gesture.endpoint.y)),
            movementDuration=gesture.movement_duration,
            dwell=gesture.dwell,
            submovementCount=len(gesture.waypoints),
            pathSha256=path_digest,
            rateGate="canonical-user-session",
        )
        timing = self.input.click(
            match,
            identity=current,
            gesture=gesture,
            rate_permit=_rate_permit,
            before_press=before_press,
            deadline_monotonic=dispatch_deadline,
        )
        if isinstance(timing, Mapping):
            self._event("click_gesture_completed", role=role, seed=gesture.seed, **dict(timing))
        pointer = self.screen.pointer_position(deadline_monotonic=self._deadline())
        if target_xids and pointer[2] not in target_xids:
            raise OperatorError("pointer target is outside the verified client")
        if pointer[:2] != expected_endpoint:
            raise OperatorError("pointer endpoint changed after guarded click")
        if role == "construct" and self.mode == MODE_MEASURE_DEFERRED:
            self._phase_deadlines.pop("constructor", None)

    def _prepare(self, *, preflight: bool) -> ReservationBinding:
        if self._actor_admission:
            deadline = time.monotonic() + float(getattr(self.client, "_timeout", 45.0))
            reply = self._actor_call(
                "prepare",
                generation=self.generation,
                deadline=deadline,
                preflight=preflight,
            )
        else:
            reply = self.client.prepare(self.generation, preflight=preflight)
        binding = self._adopt_reply(reply, expected_state="construction_prepared")
        self._reservation_deadline = binding.expires_monotonic
        self._operation_deadline = None
        self._record_operation_deadline(reply)
        if preflight and binding.reservation_kind != "preflight":
            raise OperatorError("control preflight returned the wrong reservation kind")
        if not preflight and binding.reservation_kind != "measure":
            raise OperatorError("deferred measurement returned the wrong reservation kind")
        self._event("prepared", kind=binding.reservation_kind, generation=self.generation)
        return binding

    def _arm_cancellation_actor(self, binding: ReservationBinding) -> None:
        socket_path = getattr(self.client, "socket_path", None)
        if socket_path is None:
            if getattr(self.client, "_allow_cancellation_actor_bypass", False) is True:
                return
            raise OperatorError("native cancellation actor socket is unavailable")
        existing = self._cancellation_actor
        if existing is not None:
            if Path(socket_path) != existing.socket_path:
                raise OperatorError("native cancellation socket changed during handoff")
            prior_binding = self._cancellation_actor_binding
            next_binding = (self.generation, binding.reservation_id, float(binding.expires_monotonic))
            existing.update(
                generation=self.generation,
                reservation_id=binding.reservation_id,
                deadline=binding.expires_monotonic,
            )
            self._cancellation_actor_binding = next_binding
            if prior_binding != next_binding:
                self._event("cancellation_actor_updated", generation=self.generation)
            return
        actor = CancellationActor(
            Path(socket_path),
            generation=self.generation,
            reservation_id=binding.reservation_id,
            deadline=binding.expires_monotonic,
            owner_pid=os.getpid(),
            owner_start=_proc_start(os.getpid()),
        )
        actor.start()
        self._cancellation_actor = actor
        self._cancellation_actor_binding = (
            self.generation,
            binding.reservation_id,
            float(binding.expires_monotonic),
        )
        self._event("cancellation_actor_ready", generation=self.generation)

    def _actor_call(
        self,
        op: str,
        *,
        generation: int,
        deadline: float,
        reservation_id: str = "",
        preflight: bool | None = None,
        expected_constructor_boot: str | None = None,
    ) -> dict[str, Any]:
        if not self._actor_admission:
            raise OperatorError("actor admission is unavailable for this client")
        actor = self._cancellation_actor
        if actor is None:
            actor = self._start_admission_actor(deadline=deadline)
        if Path(getattr(self.client, "socket_path", "")) != actor.socket_path:
            raise OperatorError("native cancellation socket changed during admission")
        return actor.call(
            op,
            generation=generation,
            deadline=deadline,
            reservation_id=reservation_id,
            preflight=preflight,
            expected_constructor_boot=expected_constructor_boot,
        )

    def _start_admission_actor(self, *, deadline: float) -> CancellationActor:
        if self._cancellation_actor is not None:
            return self._cancellation_actor
        socket_path = getattr(self.client, "socket_path", None)
        if socket_path is None:
            raise OperatorError("native admission actor socket is unavailable")
        actor = CancellationActor(
            Path(socket_path),
            generation=self.generation,
            reservation_id="",
            deadline=max(float(deadline), time.monotonic() + 1.0),
            owner_pid=os.getpid(),
            owner_start=_proc_start(os.getpid()),
        )
        actor.start()
        self._cancellation_actor = actor
        self._cancellation_actor_binding = None
        self._event("cancellation_actor_ready", generation=self.generation)
        return actor

    def _stop_cancellation_actor(self) -> None:
        actor = self._cancellation_actor
        if actor is not None:
            actor.close()
            self._cancellation_actor = None
            self._cancellation_actor_binding = None

    @staticmethod
    def _status_payload(reply: Mapping[str, Any]) -> Mapping[str, Any]:
        status = reply.get("status")
        if not isinstance(status, Mapping):
            raise OperatorError("native calibration status is malformed")
        return status

    def _start_native_calibration(self) -> None:
        if self._binding is None or self._binding.reservation_kind != "measure":
            raise OperatorError("native calibration requires a measure reservation")
        reply = self.client.native_calibrate(
            self.generation, deadline=self._deadline() or self._binding.expires_monotonic
        )
        status = self._status_payload(reply)
        if status.get("generation") != self.generation or status.get("state") != "NATIVE_CALIBRATION":
            raise OperatorError("native calibration status generation/state is not admitted")
        operation = status.get("calibrationOperation")
        if (
            not isinstance(operation, Mapping)
            or operation.get("kind") != "native"
            or operation.get("generation") != self.generation
            or operation.get("state") not in {"starting", "running"}
        ):
            raise OperatorError("native calibration did not start a bounded operation")
        self._calibration_started = True
        self._event("native_calibrate_started", generation=self.generation)

    def _await_calibration_terminal(self) -> Mapping[str, Any]:
        if not self._calibration_started:
            raise OperatorError("native calibration was not started")
        deadline = self._deadline()
        if deadline is None:
            raise OperatorError("native calibration deadline is unavailable")
        last_state: str | None = None
        while self.clock() < deadline:
            status_reply = self.client.calibration_status(deadline=deadline)
            status = self._status_payload(status_reply)
            operation = status.get("calibrationOperation")
            if not isinstance(operation, Mapping):
                raise OperatorError("native calibration operation status is malformed")
            status_state = status.get("state")
            if status.get("generation") != self.generation:
                raise OperatorError("native calibration status generation/state changed")
            if operation.get("kind") != "native" or operation.get("generation") != self.generation:
                raise OperatorError("native calibration operation identity changed")
            state = operation.get("state")
            if state not in {"starting", "running", *CALIBRATION_TERMINAL_STATES}:
                raise OperatorError("native calibration operation state is unknown")
            if state != last_state:
                self._event("calibration_status", state=state)
                last_state = state
            if state == "error":
                self._calibration_terminal_error = True
                error = operation.get("error")
                self._terminal_failure_message = (
                    "native calibration failed" if not isinstance(error, str) or not error else error
                )
                # The daemon may already have archived the construction
                # terminal.  Never let a construction recheck mask this
                # validated worker failure.
                raise OperatorError(self._terminal_failure_message)
            if state == "complete":
                if status_state != "INPUT_CALIBRATION":
                    raise OperatorError("native calibration did not reach INPUT_CALIBRATION")
                finalized = self._recheck()
                if self._handshake != "finalized":
                    raise OperatorError("native calibration did not reach finalized")
                return finalized
            if status_state not in {"NATIVE_CALIBRATION", "PAUSED"}:
                raise OperatorError("native calibration status generation/state changed")
            self._recheck()
            remaining = deadline - self.clock()
            self.sleep(min(0.05, max(0.0, remaining)))
        raise OperatorError("native calibration operation exceeded its deadline")

    def _cancel_active_reservation(self) -> None:
        binding = self._binding
        if binding is None:
            # A prepare/recovery admission may have committed while its reply
            # was lost. Keep the actor independent so it can consume the late
            # canonical reply and cancel it after this owner exits; never
            # invent an ID and never disarm an unknown admission here.
            if self._cancellation_actor is not None:
                self._event("cancellation_actor_retained_unknown")
            return
        if binding.reservation_state in {"recovered", "cancelled"}:
            if self._cancellation_actor is not None:
                self._stop_cancellation_actor()
            return
        try:
            deadline = self._deadline() or binding.expires_monotonic
            if self._actor_admission:
                reply = self._actor_call(
                    "cancel",
                    generation=self.generation,
                    reservation_id=binding.reservation_id,
                    deadline=deadline,
                )
            else:
                reply = self.client.recovery_cancel(
                    self.generation,
                    binding.reservation_id,
                )
            state = self._handshake_from_reply(reply)
            terminal = ReservationBinding.from_record(
                reply.get("reservation"), expected_generation=self.generation, now=None
            )
            if state != "cancelled" or terminal.reservation_state != "cancelled":
                raise OperatorError("native reservation cancellation did not reach cancelled")
            self._binding = terminal
            self._event("reservation_cancelled", generation=self.generation)
            self._stop_cancellation_actor()
        except Exception as exc:
            self._event("reservation_cancel_failed", reason=_safe_reason(exc))
            # Keep the actor armed.  Closing its control end makes EOF an
            # independent cancellation request after this owner disappears.

    def _pause_after_terminal_error(self) -> bool:
        if not self._calibration_started or not self._calibration_terminal_error:
            return False
        # The construction reservation may already be terminal/expired.  A
        # bounded fresh request window is required for status, pause, and the
        # subsequent empty-ID recovery admission.
        timeout = float(getattr(self.client, "_timeout", 45.0))
        deadline = time.monotonic() + min(max(timeout, 1.0), 45.0)
        old_generation = self.generation
        old_guardian_generation = self._binding.guardian_generation if self._binding is not None else None
        try:
            status: Mapping[str, Any] | None = None
            while time.monotonic() < deadline:
                reply = self.client.status(deadline=deadline)
                candidate = self._status_payload(reply)
                if candidate.get("generation") != old_generation:
                    raise OperatorError("native terminal error status is stale")
                operation = candidate.get("calibrationOperation")
                if (
                    candidate.get("state") == "NATIVE_CALIBRATION"
                    and isinstance(operation, Mapping)
                    and operation.get("kind") == "native"
                    and operation.get("generation") == old_generation
                    and operation.get("state") in {"error", "complete", "idle"}
                ):
                    status = candidate
                    break
                if candidate.get("state") not in {"NATIVE_CALIBRATION", "PAUSED"}:
                    raise OperatorError("native terminal error status is stale")
                time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            if status is None:
                raise OperatorError("native terminal error did not settle before pause")
            paused = self.client.pause(old_generation, deadline=deadline)
            # The mutating pause response is an immediate controller status;
            # aggregate calibration facts are fetched from status afterwards.
            paused_status = self._status_payload(paused)
            next_generation = paused_status.get("generation")
            lease_generation = paused_status.get("leaseGeneration")
            if (
                paused_status.get("state") != "PAUSED"
                or isinstance(next_generation, bool)
                or not isinstance(next_generation, int)
                or next_generation <= old_generation
                or isinstance(lease_generation, bool)
                or not isinstance(lease_generation, int)
                or old_guardian_generation is None
                or lease_generation <= old_guardian_generation
            ):
                raise OperatorError("native recovery pause did not reach PAUSED")
            aggregate = self._status_payload(self.client.status(deadline=deadline))
            if aggregate.get("generation") != next_generation or aggregate.get("state") != "PAUSED":
                raise OperatorError("native recovery aggregate status is stale")
            paused_operation = aggregate.get("calibrationOperation")
            if (
                not isinstance(paused_operation, Mapping)
                or paused_operation.get("kind") != "native"
                or paused_operation.get("generation") != old_generation
                or paused_operation.get("state") in {"starting", "running"}
                or not isinstance(paused_operation.get("state"), str)
            ):
                raise OperatorError("native recovery pause left an invalid native operation")
            guardian = aggregate.get("guardian")
            if isinstance(guardian, Mapping) and guardian.get("armed") is not False:
                raise OperatorError("native recovery pause left Guardian armed or malformed")
            if guardian is None and self._binding is not None and self._binding.guardian_armed:
                raise OperatorError("native recovery Guardian state is unavailable")
            # The failed measure reservation is closed at the pause boundary.
            # Retain the immutable binding for the generation handoff check,
            # but force fresh recovery IPC to use the client's bounded timeout.
            self._reservation_deadline = None
            self._operation_deadline = None
            self._phase_deadlines.clear()
            self._release_dispatch = None
            if next_generation > self.generation:
                self._recovery_generation = next_generation
                self.generation = next_generation
            self._event("paused_for_recovery", generation=self.generation)
            return True
        except Exception as exc:
            self._event("pause_for_recovery_failed", reason=_safe_reason(exc))
            return False

    def _recover(self, reservation_id: str = "") -> None:
        deadline = self._deadline() or (time.monotonic() + float(getattr(self.client, "_timeout", 45.0)))
        if self._actor_admission:
            reply = self._actor_call(
                "recovery_begin",
                generation=self.generation,
                reservation_id=reservation_id,
                deadline=deadline,
            )
        else:
            reply = self.client.recovery_begin(self.generation, reservation_id)
        prior_binding = self._binding
        binding = self._adopt_handoff(reply, expected_state="recovery_required")
        if binding.guardian_armed or (
            prior_binding is not None
            and binding.controller_generation > prior_binding.controller_generation
            and binding.guardian_generation <= prior_binding.guardian_generation
        ):
            raise OperatorError("native recovery binding is not a fresh unarmed Guardian owner")
        self._reservation_deadline = binding.expires_monotonic
        self._operation_deadline = None
        handshake = reply.get("handshake", {})
        recovery_kind = handshake.get("recoveryKind")
        if recovery_kind not in {"restore", "ordinary_reload"}:
            raise OperatorError("recovery kind is not closed")
        self._event("recovery_begin", kind=recovery_kind)
        if recovery_kind == "restore":
            self._chat("/crowley agent recover", expected_state="recovery_required")
            self._click("restore", expected_state="recovery_required")
        else:
            self._chat("/reload", expected_state="recovery_required")
        self._await_reload_boundary(proof="ordinary")
        deadline = self._deadline() or binding.expires_monotonic
        if self._actor_admission:
            reply = self._actor_call(
                "recovery_complete",
                generation=self.generation,
                reservation_id=binding.reservation_id,
                deadline=deadline,
            )
        else:
            reply = self.client.recovery_complete(self.generation, binding.reservation_id)
        terminal = self._adopt_reply(reply)
        if self._handshake not in TERMINAL_HANDSHAKE_STATES or terminal.reservation_state not in {
            "recovered",
            "cancelled",
        }:
            raise OperatorError("native recovery did not return a terminal record")
        if self._handshake == "cancelled":
            raise OperatorError("native recovery was cancelled")
        self._event("recovery_complete", kind=recovery_kind)

    def run(self) -> dict[str, Any]:
        outcome = "failure"
        failure_code: str | None = None
        result: dict[str, Any] | None = None
        caught: BaseException | None = None
        self._defer_signal_restore = True
        try:
            self.receipt.begin()
            with self._signals():
                if self.mode == MODE_CONTROL_PREFLIGHT:
                    self._prepare(preflight=True)
                    self._chat("/crowley agent defer", expected_state="construction_prepared")
                    self._click("construct", expected_state="construction_prepared")
                    self._recover(self._binding.reservation_id if self._binding else "")
                elif self.mode == MODE_MEASURE_DEFERRED:
                    binding = self._prepare(preflight=False)
                    self._chat("/crowley agent defer", expected_state="construction_prepared")
                    self._chat("/reload", expected_state="construction_prepared")
                    self._await_reload_boundary(proof="absence")
                    self._post_reload_controls()
                    if self._actor_admission:
                        reply = self._actor_call(
                            "bootstrap",
                            generation=self.generation,
                            reservation_id=binding.reservation_id,
                            expected_constructor_boot=binding.expected_constructor_boot,
                            deadline=self._deadline() or binding.expires_monotonic,
                        )
                    else:
                        reply = self.client.bootstrap(self.generation, binding)
                    self._adopt_reply(reply, expected_state="running")
                    self._record_operation_deadline(reply)
                    self._start_native_calibration()
                    self._await_state("construction_released")
                    self._chat("/crowley agent release", expected_state="construction_released")
                    self._click("construct", expected_state="construction_released")
                    self._await_state("hold_required")
                    self._chat(
                        "/crowley agent hold",
                        expected_state="hold_required",
                        post_state="measure_ready",
                    )
                    self._chat(
                        "/crowley agent measure", expected_state="measure_ready", post_state="resume_required"
                    )
                    self._chat(
                        "/crowley agent resume", expected_state="resume_required", post_state="resume_ready"
                    )
                    if self._actor_admission:
                        finalize_reply = self._actor_call(
                            "finalize",
                            generation=self.generation,
                            reservation_id=binding.reservation_id,
                            deadline=self._deadline() or binding.expires_monotonic,
                        )
                    else:
                        finalize_reply = self.client.finalize_admit(self.generation, binding.reservation_id)
                    self._adopt_reply(finalize_reply, expected_state="finalize_required")
                    self._record_operation_deadline(finalize_reply)
                    self._chat(
                        "/crowley agent finalize", expected_state="finalize_required", post_state="finalized"
                    )
                    self._await_calibration_terminal()
                else:
                    self._recover()
            outcome = "success"
            self._event("terminal", state="success")
            result = {"ok": True, "mode": self.mode, "receipt": "private"}
        except BaseException as exc:
            caught = exc
            if self._first_failure is None:
                self._first_failure = exc
            failure_code = "interrupted" if isinstance(exc, OperatorInterrupted) else _failure_code(exc)
            # Protect release/cancel/reap before publishing the terminal
            # receipt event.  A second signal or receipt write failure at
            # this edge must never skip mandatory cleanup or replace `exc`.
            self._cleanup_started = True
            self._event(
                "terminal",
                state="interrupted" if isinstance(exc, OperatorInterrupted) else "failure",
                failureCode=failure_code,
            )
            # Signals delivered during recovery/cancel must be observed as
            # secondary cleanup events, never replace the causal failure.
            recovered = False
            if not isinstance(exc, OperatorInterrupted):
                recovered = self._pause_after_terminal_error()
            if recovered:
                # A terminal measure reservation is closed by pause.  Recovery
                # is a fresh admission; its empty reservationId is the only
                # accepted pre-admission shape and the actor adopts the new
                # canonical ID before any restore/reload input.
                self._state_history = []
                self._handshake = None
                try:
                    self._recover("")
                except BaseException as recovery_error:
                    self._event("recovery_after_failure_failed", reason=_safe_reason(recovery_error))
                    self._cancel_active_reservation()
            else:
                self._cancel_active_reservation()
        finally:
            self._cleanup_started = True
            self._flush_poll_safe()
            try:
                if (
                    self._binding is not None
                    and self._binding.reservation_state in RESERVATION_TERMINAL_STATES
                ):
                    self._stop_cancellation_actor()
                elif self._cancellation_actor is not None:
                    self._event("cancellation_actor_retained", generation=self.generation)
                    self._cancellation_actor.abandon()
                    self._cancellation_actor = None
            except BaseException as exc:
                self._event("mandatory_cleanup_failed", reason=_safe_reason(exc))
                if self._first_failure is None:
                    self._first_failure = exc
                outcome = "failure"
                failure_code = failure_code or "cleanup_failure"
            try:
                self.input.close()
            except BaseException as exc:
                if self._first_failure is None:
                    self._first_failure = exc
                outcome = "failure"
                if failure_code is None:
                    failure_code = "cleanup_failure"
                self._event("mandatory_cleanup_failed", stage="input.close", reason=_safe_reason(exc))
            for stage in tuple(getattr(self.input, "cleanup_failures", ())):
                self._event(
                    "mandatory_cleanup_failed",
                    stage=str(stage),
                    reason="input action cleanup failed",
                )
                if self._first_failure is None:
                    self._first_failure = OperatorError(f"mandatory input cleanup failed: {stage}")
                outcome = "failure"
                failure_code = failure_code or "cleanup_failure"
            try:
                self.matcher.close()
            except BaseException as exc:
                self._event("mandatory_cleanup_failed", stage="matcher.close", reason=_safe_reason(exc))
                if self._first_failure is None:
                    self._first_failure = exc
                outcome = "failure"
                failure_code = failure_code or "cleanup_failure"
            if self._first_failure is not None and outcome == "success":
                outcome = "failure"
                failure_code = failure_code or _failure_code(self._first_failure)
            try:
                self.receipt.finish(outcome, failure_code)
            except BaseException as exc:
                if self._first_failure is None:
                    self._first_failure = exc
                if failure_code is None:
                    failure_code = "receipt_failure"
            self._defer_signal_restore = False
            try:
                self._restore_signals()
            except BaseException as exc:
                self._event("mandatory_cleanup_failed", stage="signal_restore", reason=_safe_reason(exc))
                if self._first_failure is None:
                    self._first_failure = exc
        if caught is not None:
            raise caught.with_traceback(caught.__traceback__)
        if self._first_failure is not None:
            raise self._first_failure
        if result is None:
            raise OperatorError("native calibration did not produce a result")
        return result

    @staticmethod
    def _bounded_chat_text(value: Any) -> tuple[str, bool]:
        text = value if isinstance(value, str) else str(value)
        truncated = len(text) > 256
        return text[:256], truncated

    @staticmethod
    def _safe_chat_label(label: str) -> str:
        safe = "".join(character if character.isalnum() or character in "-_" else "_" for character in label)
        return safe[:64] or "capture"

    def _retain_chat_fullframe(self, path: Path, *, label: str, use: str, sequence: int) -> dict[str, Any]:
        record: dict[str, Any] = {
            "sequence": sequence,
            "label": label,
            "use": use,
            "retained": False,
        }
        try:
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
                raise OSError("chat fullframe is not private")
            original = path.read_bytes()
            with Image.open(path) as image:
                width, height = image.size
            digest = hashlib.sha256(original).hexdigest()
            record.update(
                {
                    "original_sha256": digest,
                    "width": width,
                    "height": height,
                }
            )
            target_root = self.receipt.attempt
            if target_root is None:
                raise OSError("receipt was not initialized")
            retention_root = target_root / "chat-fullframes"
            retention_root.mkdir(mode=0o700, exist_ok=True)
            os.chmod(retention_root, 0o700)
            if len(self._chat_retained_frames) >= 4:
                oldest = self._chat_retained_frames[0]
                old_name = oldest.get("filename")
                if not isinstance(old_name, str) or not old_name:
                    self._chat_retained_frames.pop(0)
                else:
                    try:
                        (retention_root / old_name).unlink()
                    except FileNotFoundError:
                        pass
                    except OSError as exc:
                        record["retention_error"] = type(exc).__name__
                        record["retention_skipped"] = True
                        return record
                    self._chat_retained_frames.pop(0)
            filename = f"{sequence:08d}-{self._safe_chat_label(label)}.png"
            destination = retention_root / filename
            temporary = retention_root / f".{filename}.{secrets.token_hex(4)}.tmp"
            try:
                temporary.write_bytes(original)
                os.chmod(temporary, 0o600)
                os.replace(temporary, destination)
                os.chmod(destination, 0o600)
            finally:
                try:
                    temporary.unlink()
                except OSError:
                    pass
            record.update(
                {
                    "filename": filename,
                    "original_sha256": digest,
                    "width": width,
                    "height": height,
                    "retained": True,
                }
            )
            self._chat_retained_frames.append(record)
        except OperatorInterrupted:
            raise
        except Exception as exc:
            record["retention_error"] = type(exc).__name__
        return record

    def _record_chat_readback_diagnostic(
        self,
        *,
        command: str,
        typed_path: Path,
        observed_raw: Any,
        observed: Any,
        expected: Any,
    ) -> None:
        """Best-effort private evidence for a failed typed chat readback."""
        try:
            raw, raw_truncated = self._bounded_chat_text(observed_raw)
            normalized, normalized_truncated = self._bounded_chat_text(observed)
            expected_text, expected_truncated = self._bounded_chat_text(expected)
            state: str | None = None
            state_error: str | None = None
            try:
                state = self.chat_state(typed_path)
            except Exception as exc:
                state_error = type(exc).__name__
            record = dict(self._last_chat_capture_record or {})
            try:
                record["derived_crop_sha256"] = hashlib.sha256(typed_path.read_bytes()).hexdigest()
            except OSError as exc:
                record["derived_crop_error"] = type(exc).__name__
            record["typed_editor_state"] = state
            if state_error is not None:
                record["typed_editor_state_error"] = state_error
            retained = [dict(item) for item in self._chat_retained_frames]
            payload = {
                "schema": 1,
                "command": command,
                "expected": expected_text,
                "expected_truncated": expected_truncated,
                "observed_raw": raw,
                "observed_raw_truncated": raw_truncated,
                "observed_normalized": normalized,
                "observed_normalized_truncated": normalized_truncated,
                "failed_typed_capture": record,
                "retained_frames": retained,
            }
            target_root = self.receipt.attempt
            if target_root is None:
                return
            sequence = record.get("sequence", self._chat_capture_sequence)
            filename = target_root / f"chat-readback-{int(sequence):08d}.json"
            temporary = target_root / f".{filename.name}.{secrets.token_hex(4)}.tmp"
            try:
                temporary.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
                os.chmod(temporary, 0o600)
                os.replace(temporary, filename)
                os.chmod(filename, 0o600)
            finally:
                try:
                    temporary.unlink()
                except OSError:
                    pass
        except Exception:
            # This evidence is strictly diagnostic; never replace the causal
            # readback refusal or alter the ordinary cleanup path.
            return


def _failure_code(error: BaseException) -> str:
    if isinstance(error, OperatorInterrupted):
        return "interrupted"
    text = str(error).lower()
    if "expired" in text or "deadline" in text:
        return "deadline"
    if "identity" in text or "focus" in text:
        return "identity_or_focus_drift"
    if "template" in text or "capture" in text:
        return "visual_evidence_refused"
    if "reservation" in text or "handshake" in text or "native" in text:
        return "native_admission_refused"
    if "chat" in text or "ocr" in text:
        return "chat_readback_refused"
    return "operator_failure"


def _safe_reason(error: BaseException) -> str:
    return type(error).__name__


def threading_current_main() -> bool:
    # Avoid importing threading in the common module path just for this check.
    return threading_current_ident() == threading_main_ident()


def threading_current_ident() -> int:
    import threading

    return threading.get_ident()


def threading_main_ident() -> int:
    import threading

    return threading.main_thread().ident or -1


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_private_json(path: Path, *, label: str, max_bytes: int = 256 * 1024) -> dict[str, Any]:
    """Read one private JSON artifact without following a replacement path."""

    def stat_signature(info: os.stat_result) -> tuple[int, int, int, int, int, int, int, int]:
        return (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_nlink,
            info.st_uid,
            info.st_size,
            info.st_mtime_ns,
            info.st_ctime_ns,
        )

    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid() or before.st_mode & 0o077:
            raise OperatorError(f"{label} is not a private regular file")
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(descriptor)
            if stat_signature(opened) != stat_signature(before):
                raise OperatorError(f"{label} changed before read")
            if opened.st_size > max_bytes:
                raise OperatorError(f"{label} is oversized")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(descriptor, min(64 * 1024, max_bytes - total + 1))
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise OperatorError(f"{label} is oversized")
                chunks.append(chunk)
            after = os.fstat(descriptor)
            if stat_signature(after) != stat_signature(opened) or total != opened.st_size:
                raise OperatorError(f"{label} changed during read")
        finally:
            os.close(descriptor)
        final_path = path.lstat()
        if stat_signature(final_path) != stat_signature(after):
            raise OperatorError(f"{label} changed after read")
    except OperatorError:
        raise
    except (OSError, UnicodeError) as exc:
        raise OperatorError(f"{label} is unavailable") from exc
    try:
        value = json.loads(b"".join(chunks).decode("utf-8"), object_pairs_hook=_unique_json_pairs)
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise OperatorError(f"{label} is malformed") from exc
    if not isinstance(value, dict):
        raise OperatorError(f"{label} is not an object")
    return value


def _unique_json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _verify_observer_manifest(screen: X11Screen, observer: Path, digest: str) -> dict[str, str]:
    """Verify the sealed observer against the current reviewed source bytes."""

    publication_root = observer.parent
    if publication_root.name == "current":
        publication_root = publication_root.parent
    try:
        publication = read_current_publication(
            publication_root,
            binary_name=observer.name,
            manifest_name=observer.with_suffix(".manifest.json").name,
            sidecar_names=(),
            validate_manifest=lambda _manifest, _digest: None,
        )
    except PublicationReadError as exc:
        raise OperatorError(str(exc)) from exc
    try:
        if publication.binary_digest != digest:
            raise OperatorError("configured identity observer digest changed")
        manifest = publication.manifest
    finally:
        os.close(publication.binary_fd)
    if set(manifest) != pins.OBSERVER_MANIFEST_KEYS:
        raise OperatorError("configured identity observer source manifest schema is malformed")
    if manifest["schema"] != pins.OBSERVER_MANIFEST_SCHEMA:
        raise OperatorError("configured identity observer source manifest schema is malformed")
    if manifest["target"] != pins.TARGET_TRIPLE:
        raise OperatorError("configured identity observer target is not pinned")
    if manifest["binary"] != pins.OBSERVER_ARTIFACT_NAME:
        raise OperatorError("configured identity observer manifest is not bound to the binary")
    if manifest["binary_sha256"] != digest:
        raise OperatorError("configured identity observer manifest is not bound to the binary")
    if manifest["workspace_sha256"] != pins.OBSERVER_WORKSPACE_SHA256:
        raise OperatorError("configured identity observer workspace pin is stale")
    if manifest["static_library_resolution"] != {
        "mechanism": "pkg-config",
        "package": "x11",
        "mode": "static",
        "libraries": [f"lib{name}.a" for name in pins.OBSERVER_STATIC_LIBRARY_NAMES],
    }:
        raise OperatorError("configured identity observer library resolution is not canonical")
    static_inputs = manifest["static_inputs"]
    expected_static_inputs = {f"lib{name}.a" for name in pins.OBSERVER_STATIC_LIBRARY_NAMES}
    if (
        not isinstance(static_inputs, dict)
        or set(static_inputs) != expected_static_inputs
        or any(
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in static_inputs.values()
        )
    ):
        raise OperatorError("configured identity observer static inputs are malformed")
    toolchain = manifest["toolchain"]
    if toolchain != {
        "rustc": pins.EXPECTED_RUSTC,
        "cargo": pins.EXPECTED_CARGO,
        "rustfmt": pins.EXPECTED_RUSTFMT,
    }:
        raise OperatorError("configured identity observer toolchain is not pinned")
    linker = manifest["linker"]
    if not isinstance(linker, dict) or set(linker) != {"driver", "ld"}:
        raise OperatorError("configured identity observer linker metadata is malformed")
    for name in ("driver", "ld"):
        record = linker[name]
        expected_name = "cc" if name == "driver" else "ld"
        if (
            not isinstance(record, dict)
            or set(record) != {"name", "sha256", "version"}
            or record["name"] != expected_name
            or not isinstance(record["sha256"], str)
            or len(record["sha256"]) != 64
            or any(character not in "0123456789abcdef" for character in record["sha256"])
            or not isinstance(record["version"], str)
            or not record["version"]
        ):
            raise OperatorError("configured identity observer linker metadata is malformed")
    source_hashes = manifest["source_sha256"]
    expected_sources = set(pins.OBSERVER_SOURCE_MANIFEST_FILES)
    if not isinstance(source_hashes, dict) or set(source_hashes) != expected_sources:
        raise OperatorError("configured identity observer source set is malformed")
    bindings: dict[str, str] = {
        "identity-observer-binary": digest,
        f"identity-observer-{observer.name}": digest,
    }
    for source_name in sorted(expected_sources):
        source_path = native_build.NATIVE_ROOT / source_name.removeprefix("native/")
        try:
            source_info = source_path.lstat()
            if not stat.S_ISREG(source_info.st_mode) or source_info.st_uid != os.getuid():
                raise OperatorError("configured identity observer source is not a regular owned file")
            source_digest = _sha256_file(source_path)
        except OperatorError:
            raise
        except OSError as exc:
            raise OperatorError("configured identity observer source is unavailable") from exc
        expected_digest = source_hashes[source_name]
        if (
            not isinstance(expected_digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", expected_digest)
            or source_digest != expected_digest
        ):
            raise OperatorError("configured identity observer source digest is stale")
        bindings[f"identity-observer-source-{Path(source_name).name}"] = source_digest
    # The configured screen object is part of the binding, but its expected
    # path is never copied from the manifest.
    if getattr(getattr(screen, "backend", None), "resolver_kind", None) != "pinned-rust-v1":
        raise OperatorError("configured identity observer production binding is unavailable")
    return bindings


def _source_bindings(
    screen: X11Screen,
    manifest: TemplateManifest,
    matcher_runtime: TemplateMatcherRuntime | None = None,
) -> dict[str, str]:
    """Return fixed source/producer/template pins without private paths."""

    bindings: dict[str, str] = {}
    modules = (
        "crowley.session.guardian",
        "crowley.session.input_tool",
        "crowley.session.config",
        "crowley.session.driver",
        "crowley.session.decoder",
        "crowley.session.cli",
        "crowley.session.daemon",
        "crowley.session.controller",
    )
    for module_name in modules:
        module = importlib.import_module(module_name)
        module_path = Path(getattr(module, "__file__", ""))
        if not module_path.is_file():
            raise OperatorError("native helper dependency source is unavailable")
        bindings[module_path.name] = _sha256_file(module_path)
    config_path = Path(screen.config_path) if hasattr(screen, "config_path") else None
    if config_path is not None and config_path.is_file():
        bindings["profile-config"] = _sha256_file(config_path)
    addon = Path(__file__).resolve().parents[1] / "addon" / "Crowley" / "Commands.lua"
    if addon.is_file():
        bindings["addon-Commands.lua"] = _sha256_file(addon)
    bindings["template-construct"] = manifest.construct.sha256
    bindings["template-restore"] = manifest.restore.sha256
    producer = screen.config.profile.telemetry_region_executable
    producer_digest = screen.config.profile.telemetry_region_executable_sha256
    if producer is None or producer_digest is None:
        raise OperatorError("configured native producer pin is unavailable")
    if _sha256_file(producer) != producer_digest:
        raise OperatorError("configured native producer digest changed")
    bindings[f"producer-{producer.name}"] = producer_digest
    bindings["producer-manifest"] = producer_digest
    bindings["pinned-input-tool"] = screen.tool.digest
    observer = screen.config.profile.identity_observer_executable
    observer_digest = screen.config.profile.identity_observer_executable_sha256
    if observer is not None or observer_digest is not None:
        if observer is None or observer_digest is None:
            raise OperatorError("configured identity observer pin is incomplete")
        bindings.update(_verify_observer_manifest(screen, observer, observer_digest))
        bindings["identity-observer-resolver"] = hashlib.sha256(
            screen.backend.resolver_kind.encode()
        ).hexdigest()
    if matcher_runtime is not None:
        bindings.update(matcher_runtime.source_bindings)
    return bindings


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="native-calibration-operator")
    parser.add_argument("mode", choices=sorted(MODES))
    parser.add_argument("--generation", type=int, required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--templates", required=True)
    parser.add_argument("--matcher-artifact-root", type=Path, default=None)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    screen: X11Screen | None = None
    watchdog: Watchdog | None = None
    input_device: FixedInput | None = None
    operator: NativeCalibrationOperator | None = None
    matcher_runtime: TemplateMatcherRuntime | None = None
    try:
        manifest = TemplateManifest.load(args.templates)
        root = Path.home() / ".local" / "state" / "crowley" / "native-calibration-operator"
        screen = X11Screen(args.config, root)
        matcher_runtime = TemplateMatcherRuntime(args.matcher_artifact_root)
        dependencies = _source_bindings(screen, manifest, matcher_runtime)
        watchdog = Watchdog(screen.tool, environment=screen.input_environment)
        input_device = FixedInput(
            screen.tool,
            watchdog=watchdog,
            environment=screen.input_environment,
        )
        client = DaemonClient(screen.config.socket)
        operator = NativeCalibrationOperator(
            mode=args.mode,
            generation=args.generation,
            config_path=args.config,
            template_manifest=manifest,
            client=client,
            screen=screen,
            input_device=input_device,
            helper_source_sha256=_sha256_file(Path(__file__)),
            dependencies=dependencies,
            matcher_runtime=matcher_runtime,
        )
        operator.run()
        return 0
    except (OperatorError, MatcherError, DriverError, OSError, ValueError) as exc:
        print(f"native calibration operator refused: {type(exc).__name__}", file=sys.stderr)
        return 2
    finally:
        # NativeCalibrationOperator.run owns cleanup after its receipt begins;
        # this covers setup failures before that point without double-closing
        # the pinned tool or watchdog.
        if input_device is not None and (operator is None or operator.receipt.attempt is None):
            try:
                input_device.close()
            except Exception:
                pass
        elif input_device is None and watchdog is not None:
            try:
                watchdog.close()
            except Exception:
                pass
        elif watchdog is None and screen is not None:
            try:
                screen.close()
            except Exception:
                pass
        if matcher_runtime is not None and (operator is None or operator.receipt.attempt is None):
            try:
                matcher_runtime.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
