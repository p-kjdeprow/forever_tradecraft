"""Owned native and X11 input calibration workflows.

Calibration is a live operation owned by this package.  Callers provide no
source IDs, booleans, or magic token.  The workflow creates a temporary GTK/X11
window, proves its process/window/focus identity, drives only that window via
the same XTEST executor used at runtime, and waits for deliberate human input
on the target before issuing a sealed calibration record.
"""

from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import select
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .driver import SyntheticPointerProbe, WindowIdentity

from .input_events import (
    RAW_MOTION_DETAIL,
    EventClass,
    InputEventError,
    RawMotionPairing,
    XI2Event,
    decode_xtest_motion_transition,
    parse_xi2_records,
    xi2_timestamp_elapsed,
    xi2_timestamp_forward,
    xi2_timestamp_valid,
)
from .process_admission import ADMISSION_WRAPPER, AdmissionGate
from .protocol import unsigned_delta


class CalibrationError(RuntimeError):
    """Calibration evidence is missing, stale, or does not prove ownership."""


_SEAL = object()
GTK_LAYOUT_MUTTER_FRAME = "mutter-managed-frame-v1"
GTK_LAYOUT_ROOT_CHILD = "x11-root-child-v1"
XTEST_ABSOLUTE_COMMAND_SCHEMA = "private-xdotool-absolute-pointer-v1"


@dataclass(frozen=True)
class XTestInputProfile:
    """Sealed read-only profile for the attached XTEST pointer source."""

    source_device: int
    parent_device: int
    name: str
    classes: int
    axes: tuple[tuple[int, str, str, float, float, int], ...]
    enabled: int
    xtest_device: int
    ctm: tuple[float, ...]
    root_xid: int
    root_geometry: tuple[int, int, int, int]
    hierarchy: tuple[tuple[int, int, str, str], ...]
    command_schema: str = XTEST_ABSOLUTE_COMMAND_SCHEMA

    def canonical_payload(self) -> tuple[Any, ...]:
        return (
            self.source_device,
            self.parent_device,
            self.name,
            self.classes,
            self.axes,
            self.enabled,
            self.xtest_device,
            self.ctm,
            self.root_xid,
            self.root_geometry,
            self.hierarchy,
            self.command_schema,
        )

    def verify(self) -> None:
        if (
            isinstance(self.source_device, bool)
            or not isinstance(self.source_device, int)
            or self.source_device <= 0
            or isinstance(self.parent_device, bool)
            or not isinstance(self.parent_device, int)
            or self.parent_device <= 0
            or not self.name
            or self.classes != 3
            or self.enabled != 1
            or self.xtest_device != 1
            or self.command_schema != XTEST_ABSOLUTE_COMMAND_SCHEMA
            or len(self.ctm) != 9
            or tuple(self.ctm) != (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
            or isinstance(self.root_xid, bool)
            or not isinstance(self.root_xid, int)
            or self.root_xid <= 0
            or len(self.root_geometry) != 4
            or self.root_geometry[2] <= 0
            or self.root_geometry[3] <= 0
        ):
            raise CalibrationError("XTEST input profile identity is malformed")
        expected_axes = (
            (0, "Rel X", "relative", -1.0, -1.0, 0),
            (1, "Rel Y", "relative", -1.0, -1.0, 0),
        )
        if tuple(self.axes) != expected_axes:
            raise CalibrationError("XTEST input profile axes changed")
        rows = {row[0]: row for row in self.hierarchy if row[0] == self.source_device}
        selected = rows.get(self.source_device)
        if selected is None or selected[1] != self.parent_device or selected[3] != self.name:
            raise CalibrationError("XTEST input profile hierarchy changed")


def _parse_xinput_profile(
    long_text: str,
    properties_text: str,
    hierarchy: tuple[tuple[int, int, str, str], ...],
    *,
    source_device: int,
    parent_device: int,
    root_xid: int,
    root_geometry: tuple[int, int, int, int],
) -> XTestInputProfile:
    """Parse only the measured XTEST device/profile fields."""

    header = re.search(r"^(.+?)\s+id=(\d+)\s+\[(?:slave)\s+pointer\s+\((\d+)\)\]$", long_text, re.MULTILINE)
    classes_match = re.search(r"Reporting\s+(\d+)\s+classes:", long_text)
    origins = re.findall(r"Class originated from:\s*(\d+)\.\s*Type:\s*(\w+)", long_text)
    buttons = re.search(r"Buttons supported:\s*(\d+)", long_text)
    axes: list[tuple[int, str, str, float, float, int]] = []
    for match in re.finditer(
        r"Detail for Valuator\s+(\d+):\s*\n\s*Label:\s*(.+?)\s*\n\s*Range:\s*"
        r"(-?\d+(?:\.\d+)?)\s+-\s+(-?\d+(?:\.\d+)?)\s*\n\s*Resolution:\s*(\d+)\s+units/m\s*\n\s*Mode:\s*(\w+)",
        long_text,
        re.MULTILINE,
    ):
        axes.append(
            (
                int(match.group(1)),
                match.group(2),
                match.group(6),
                float(match.group(3)),
                float(match.group(4)),
                int(match.group(5)),
            )
        )
    enabled = re.search(r"Device Enabled \(\d+\):\s*(\d+)", properties_text)
    xtest = re.search(r"XTEST Device \(\d+\):\s*(\d+)", properties_text)
    ctm_match = re.search(r"Coordinate Transformation Matrix \(\d+\):\s*([^\n]+)", properties_text)
    if not header or not classes_match or not buttons or not enabled or not xtest or not ctm_match:
        raise CalibrationError("XTEST input profile output is incomplete")
    ctm = tuple(float(value.strip()) for value in ctm_match.group(1).split(","))
    if len(ctm) != 9 or any(not math.isfinite(value) for value in ctm):
        raise CalibrationError("XTEST coordinate transformation matrix is malformed")
    hierarchy_row = next((row for row in hierarchy if row[0] == source_device), None)
    if hierarchy_row is None:
        raise CalibrationError("XTEST input profile source is absent from hierarchy")
    hierarchy_name = hierarchy_row[3]
    if header.group(1).strip() != hierarchy_name.split(":", 1)[-1]:
        raise CalibrationError("XTEST input profile name disagrees with hierarchy")
    profile = XTestInputProfile(
        source_device=source_device,
        parent_device=parent_device,
        name=hierarchy_name,
        classes=int(classes_match.group(1)),
        axes=tuple(sorted(axes)),
        enabled=int(enabled.group(1)),
        xtest_device=int(xtest.group(1)),
        ctm=ctm,
        root_xid=root_xid,
        root_geometry=tuple(root_geometry),
        hierarchy=tuple(hierarchy),
    )
    if int(header.group(2)) != source_device or int(header.group(3)) != parent_device:
        raise CalibrationError("XTEST input profile source/parent output disagrees with hierarchy")
    expected_origins = [
        (str(source_device), "XIButtonClass"),
        (str(source_device), "XIValuatorClass"),
        (str(source_device), "XIValuatorClass"),
    ]
    if origins != expected_origins or int(buttons.group(1)) != 10:
        raise CalibrationError("XTEST input profile classes are not the measured contract")
    profile.verify()
    return profile


@dataclass(frozen=True)
class KeyboardProbeStep:
    """One physical key transition in an owned keyboard probe.

    ``phase`` is supplied by the driver action list (``press`` or ``release``)
    and is deliberately kept beside the keysym.  This preserves modifier
    transitions such as Control press, A press, A release, Control release;
    collapsing those transitions into one global modifier tuple would make a
    runtime chord unverifiable.
    """

    phase: str
    keysym: str


@dataclass(frozen=True)
class KeyboardProbeOperation:
    """An exact finite keyboard emission used by calibration and dispatch."""

    operation: str
    payload: str
    steps: tuple[KeyboardProbeStep, ...]


KeyboardProfileRow = tuple[
    str,
    str,
    tuple[tuple[str, str, int, tuple[int, int, int], int], ...],
]

NATIVE_CONSTRUCTION_LIMIT_BYTES = 128 * 1024 * 1024
NATIVE_UPDATES_LIMIT_BYTES = 16 * 1024 * 1024


_GTK_SCRIPT = r"""
import json, os, sys
import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, GdkX11

window = Gtk.Window()
window.set_title("CROWLEY INPUT CALIBRATION ONLY")
window.set_decorated(False)
window.set_default_size(480, 240)
window.set_resizable(False)
box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
label = Gtk.Label(label="Crowley native input calibration\nVerifying owned target and XTEST path…")
entry = Gtk.Entry()
entry.set_name("crowley-calibration-private-entry")
entry.set_placeholder_text("Private owned keyboard probe")
box.pack_start(label, False, False, 0)
box.pack_start(entry, False, False, 0)
window.add(box)
window.connect("destroy", Gtk.main_quit)
window.set_position(Gtk.WindowPosition.MOUSE)
window.show_all()
Gtk.main_iteration_do(False)
entry.grab_focus()
xid = GdkX11.X11Window.get_xid(window.get_window())
print(json.dumps({"pid": os.getpid(), "xid": xid}), flush=True)

def ready_prompt(_source, _condition):
    data = os.read(sys.stdin.fileno(), 4096)
    if b"READY" in data:
        label.set_text("Crowley native input calibration\nPress one real key and click this window")
        print("PROMPT_ACK", flush=True)
    return True

GLib.io_add_watch(sys.stdin.fileno(), GLib.IO_IN, ready_prompt)
Gtk.main()
"""


@dataclass(frozen=True)
class CalibrationFingerprint:
    process_pid: int
    windows_xid: int
    child_xid: int
    executable: str
    display: str
    geometry: tuple[int, int, int, int]
    xinput_hierarchy: tuple[tuple[int, int, str, str], ...]
    driver_version: str
    xauthority: str = ""
    wine_prefix: str = ""
    process_start: str = ""

    @property
    def value(self) -> str:
        payload = {
            "pid": self.process_pid,
            "xid": self.windows_xid,
            "child": self.child_xid,
            "executable": self.executable,
            "display": self.display,
            "geometry": self.geometry,
            "processStart": self.process_start,
            "xinput": self.xinput_hierarchy,
            "driver": self.driver_version,
            "xauthority": self.xauthority,
            "winePrefix": self.wine_prefix,
        }
        return hashlib.sha256(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        ).hexdigest()


@dataclass(frozen=True)
class InputCalibrationEvidence:
    """Sealed evidence produced only by :class:`InputCalibrationWorkflow`."""

    fingerprint: str
    display: str
    target_pid: int
    target_xid: int
    target_child_xid: int
    synthetic_key_source: int
    synthetic_pointer_source: int
    human_key_source: int
    human_pointer_source: int
    xinput_hierarchy: tuple[tuple[int, int, str, str], ...]
    synthetic_events: tuple[tuple[str, int, int | None], ...]
    human_events: tuple[tuple[str, int, int | None], ...]
    forwarded_events: tuple[tuple[str, int, int | None], ...]
    driver_version: str
    xauthority: str = ""
    event_proof: tuple[tuple[Any, ...], ...] = ()
    game_identity_fingerprint: str = ""
    game_geometry: tuple[int, int, int, int] = ()
    target_executable: str = ""
    target_process_start: str = ""
    target_geometry: tuple[int, int, int, int] = ()
    managed_frame_xid: int = 0
    managed_frame_geometry: tuple[int, int, int, int] = ()
    target_event_xids: tuple[int, ...] = ()
    synthetic_modifiers: tuple[int, int, int] = ()
    synthetic_echo_proof: tuple[tuple[Any, ...], ...] = ()
    xtest_profile: XTestInputProfile | None = None
    synthetic_motion_transitions: tuple[tuple[Any, ...], ...] = ()
    # (origin_x, origin_y, origin_window, requested_x, requested_y,
    #  post_x, post_y, post_window), captured by the native producer around
    # the absolute XTEST move and included in the evidence seal.
    synthetic_motion_query: tuple[Any, ...] = ()
    synthetic_motion_seed: int = 0
    synthetic_motion_plan: tuple[tuple[float, int, int], ...] = ()
    target_layout_kind: str = GTK_LAYOUT_MUTTER_FRAME
    target_root_xid: int = 0
    target_parent_xid: int = 0
    target_descendant_xids: tuple[int, ...] = ()
    target_excluded_input_only_xids: tuple[int, ...] = ()
    keyboard_keymap: tuple[tuple[str, int], ...] = ()
    keyboard_profile: tuple[KeyboardProfileRow, ...] = ()
    provenance: str = "owned-live"
    is_live: bool = True
    evidence_hash: str = ""
    _seal: object | None = None

    @classmethod
    def _issue(cls, **kwargs: Any) -> InputCalibrationEvidence:
        issued = cls(**kwargs, _seal=_SEAL)
        return replace(issued, fingerprint=issued.content_fingerprint()).with_hash()

    def _canonical_payload(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "display": self.display,
            "target_pid": self.target_pid,
            "target_xid": self.target_xid,
            "target_child_xid": self.target_child_xid,
            "synthetic_key_source": self.synthetic_key_source,
            "synthetic_pointer_source": self.synthetic_pointer_source,
            "human_key_source": self.human_key_source,
            "human_pointer_source": self.human_pointer_source,
            "xinput_hierarchy": self.xinput_hierarchy,
            "synthetic_events": self.synthetic_events,
            "human_events": self.human_events,
            "forwarded_events": self.forwarded_events,
            "driver_version": self.driver_version,
            "xauthority": self.xauthority,
            "event_proof": self.event_proof,
            "game_identity_fingerprint": self.game_identity_fingerprint,
            "game_geometry": self.game_geometry,
            "target_executable": self.target_executable,
            "target_process_start": self.target_process_start,
            "target_geometry": self.target_geometry,
            "managed_frame_xid": self.managed_frame_xid,
            "managed_frame_geometry": self.managed_frame_geometry,
            "target_event_xids": self.target_event_xids,
            "synthetic_modifiers": self.synthetic_modifiers,
            "synthetic_echo_proof": self.synthetic_echo_proof,
            "xtest_profile": (
                self.xtest_profile.canonical_payload() if self.xtest_profile is not None else None
            ),
            "synthetic_motion_transitions": self.synthetic_motion_transitions,
            "synthetic_motion_query": self.synthetic_motion_query,
            "synthetic_motion_seed": self.synthetic_motion_seed,
            "synthetic_motion_plan": self.synthetic_motion_plan,
            "target_layout_kind": self.target_layout_kind,
            "target_root_xid": self.target_root_xid,
            "target_parent_xid": self.target_parent_xid,
            "target_descendant_xids": self.target_descendant_xids,
            "target_excluded_input_only_xids": self.target_excluded_input_only_xids,
            "keyboard_keymap": self.keyboard_keymap,
            "keyboard_profile": self.keyboard_profile,
            "provenance": self.provenance,
            "is_live": self.is_live,
        }

    def content_fingerprint(self) -> str:
        payload = self._canonical_payload()
        payload["fingerprint"] = ""
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), default=list).encode("utf-8")
        ).hexdigest()

    def with_hash(self) -> InputCalibrationEvidence:
        digest = hashlib.sha256(
            json.dumps(self._canonical_payload(), sort_keys=True, separators=(",", ":"), default=list).encode(
                "utf-8"
            )
        ).hexdigest()
        return replace(self, evidence_hash=digest)

    def _verify_keyboard_profile(self) -> None:
        """Validate the sealed live keymap and every measured key transition."""

        if not isinstance(self.keyboard_keymap, tuple) or not self.keyboard_keymap:
            raise CalibrationError("input evidence has no sealed live keyboard keymap")
        keymap: dict[str, int] = {}
        for entry in self.keyboard_keymap:
            if (
                not isinstance(entry, tuple)
                or len(entry) != 2
                or not isinstance(entry[0], str)
                or not entry[0]
                or isinstance(entry[1], bool)
                or not isinstance(entry[1], int)
                or entry[1] <= 0
                or entry[0] in keymap
            ):
                raise CalibrationError("input evidence keyboard keymap is malformed")
            keymap[entry[0]] = entry[1]
        if tuple(sorted(keymap.items())) != self.keyboard_keymap:
            raise CalibrationError("input evidence keyboard keymap is not canonical")
        if not isinstance(self.keyboard_profile, tuple) or not self.keyboard_profile:
            raise CalibrationError("input evidence has no sealed keyboard operation profile")
        operations: set[tuple[str, str]] = set()
        for row in self.keyboard_profile:
            if not isinstance(row, tuple) or len(row) != 3:
                raise CalibrationError("input evidence keyboard operation profile is malformed")
            operation, payload, steps = row
            if (
                not isinstance(operation, str)
                or not operation
                or not isinstance(payload, str)
                or not isinstance(steps, tuple)
                or not steps
                or (operation, payload) in operations
            ):
                raise CalibrationError("input evidence keyboard operation profile is incomplete")
            operations.add((operation, payload))
            for step in steps:
                if (
                    not isinstance(step, tuple)
                    or len(step) != 5
                    or step[0] not in {"press", "release"}
                    or not isinstance(step[1], str)
                    or not step[1]
                    or isinstance(step[2], bool)
                    or not isinstance(step[2], int)
                    or step[2] <= 0
                    or not isinstance(step[3], tuple)
                    or len(step[3]) != 3
                    or any(
                        isinstance(value, bool) or not isinstance(value, int) or value < 0
                        for value in step[3]
                    )
                    or isinstance(step[4], bool)
                    or not isinstance(step[4], int)
                    or step[4] < 0
                    or step[4] != step[3][0] | step[3][1] | step[3][2]
                    or keymap.get(step[1], keymap.get(step[1].rsplit("+", 1)[-1])) != step[2]
                ):
                    raise CalibrationError("input evidence keyboard transition is malformed")
        if not operations:
            raise CalibrationError("input evidence has no keyboard operation facts")

    def verify_seal(self) -> None:
        if self._seal is not _SEAL or self.provenance != "owned-live" or not self.is_live:
            raise CalibrationError("input evidence was not issued by the owned live workflow")
        if self.xtest_profile is None:
            raise CalibrationError("input evidence has no sealed XTEST input profile")
        self.xtest_profile.verify()
        if not self.synthetic_motion_transitions:
            raise CalibrationError("input evidence must contain a baseline XTEST motion sequence")
        if (
            not isinstance(self.synthetic_motion_query, tuple)
            or len(self.synthetic_motion_query) != 8
            or any(
                isinstance(value, bool) or not isinstance(value, int) for value in self.synthetic_motion_query
            )
            or self.synthetic_motion_query[2] <= 0
            or self.synthetic_motion_query[7] <= 0
            or self.synthetic_motion_query[2] not in self.target_event_xids
            or self.synthetic_motion_query[7] not in self.target_event_xids
            or self.synthetic_motion_query[5:7] != self.synthetic_motion_query[3:5]
        ):
            raise CalibrationError("input evidence XTEST motion query is malformed")
        query_origin = tuple(self.synthetic_motion_query[:2])
        query_endpoint = tuple(self.synthetic_motion_query[3:5])
        if query_origin == query_endpoint:
            raise CalibrationError("input evidence XTEST motion must prove nonzero displacement")
        if tuple(self.synthetic_motion_query[5:7]) != query_endpoint:
            raise CalibrationError("input evidence XTEST post-query does not equal its requested endpoint")
        if (
            isinstance(self.synthetic_motion_seed, bool)
            or not isinstance(self.synthetic_motion_seed, int)
            or not 0 <= self.synthetic_motion_seed < 2**64
            or not self.synthetic_motion_plan
        ):
            raise CalibrationError("input evidence XTEST motion seed or plan is malformed")
        if (
            not isinstance(self.target_geometry, tuple)
            or len(self.target_geometry) != 4
            or any(isinstance(value, bool) or not isinstance(value, int) for value in self.target_geometry)
            or self.target_geometry[2] <= 0
            or self.target_geometry[3] <= 0
        ):
            raise CalibrationError("input evidence target geometry is malformed")
        target_left, target_top, target_width, target_height = self.target_geometry

        def in_target(point: tuple[int, int]) -> bool:
            return (
                target_left <= point[0] < target_left + target_width
                and target_top <= point[1] < target_top + target_height
            )

        if not in_target(query_origin) or not in_target(query_endpoint):
            raise CalibrationError("input evidence XTEST motion endpoint is outside the certified target")
        previous_plan_point: tuple[int, int] | None = None
        planned_endpoints: list[tuple[int, int]] = []
        previous_time = -1.0
        for sample in self.synthetic_motion_plan:
            if (
                not isinstance(sample, tuple)
                or len(sample) != 3
                or isinstance(sample[0], bool)
                or not isinstance(sample[0], float)
                or not math.isfinite(sample[0])
                or sample[0] < 0
                or sample[0] <= previous_time
                or any(isinstance(value, bool) or not isinstance(value, int) for value in sample[1:])
            ):
                raise CalibrationError("input evidence XTEST motion plan is malformed")
            point = (sample[1], sample[2])
            if not in_target(point):
                raise CalibrationError("input evidence XTEST motion plan left the certified target")
            if previous_plan_point is not None and point != previous_plan_point:
                planned_endpoints.append(point)
            previous_plan_point = point
            previous_time = sample[0]
        if (
            tuple(self.synthetic_motion_plan[0][1:]) != query_origin
            or previous_plan_point != query_endpoint
            or not planned_endpoints
        ):
            raise CalibrationError("input evidence XTEST motion plan is not sealed to its query")
        motion_facts = {
            (
                proof[4],
                proof[1],
                proof[2],
                proof[7],
                proof[5],
                proof[6],
                tuple(proof[8]) if proof[8] is not None else None,
                tuple(proof[9]) if proof[9] is not None else None,
            )
            for proof in self.event_proof
            if isinstance(proof, tuple) and len(proof) >= 10 and proof[0] == "Motion"
        }
        # A slave XTEST Motion reports the pointer origin in its native root
        # coordinates while its valuators carry the delivered endpoint.  A
        # controlled echo is compared with that normalized endpoint, so use
        # the sealed transition facts for the synthetic source pair below.
        transition_facts = {
            (
                transition[1],
                transition[3],
                transition[5],
                tuple(transition[10]),
                tuple(transition[11]),
            )
            for transition in self.synthetic_motion_transitions
            if isinstance(transition, tuple)
            and len(transition) == 15
            and isinstance(transition[10], tuple)
            and isinstance(transition[11], tuple)
        }
        raw_motion_facts = {
            (proof[4], proof[1], proof[2], proof[3])
            for proof in self.event_proof
            if isinstance(proof, tuple) and len(proof) >= 5 and proof[0] == "RawMotion"
        }
        baseline_raw = [
            proof
            for proof in self.event_proof
            if isinstance(proof, tuple)
            and len(proof) == 12
            and proof[0] == "RawMotion"
            and proof[2] == self.xtest_profile.source_device
        ]
        baseline_motion = [
            proof
            for proof in self.event_proof
            if isinstance(proof, tuple)
            and len(proof) == 12
            and proof[0] == "Motion"
            and proof[2] == self.xtest_profile.source_device
        ]
        if len(baseline_raw) != len(self.synthetic_motion_transitions) or len(baseline_motion) != len(
            self.synthetic_motion_transitions
        ):
            raise CalibrationError("input evidence baseline XTEST motion ancestry is not exactly one-to-one")
        previous_endpoint: tuple[int, int] | None = None
        for transition_index, transition in enumerate(self.synthetic_motion_transitions):
            if not isinstance(transition, tuple) or len(transition) != 15:
                raise CalibrationError("input evidence XTEST transition proof is malformed")
            if transition[0] != "XTestMotionTransition":
                raise CalibrationError("input evidence XTEST transition class is malformed")
            if (
                transition[2] != self.xtest_profile.source_device
                or transition[3] != self.xtest_profile.source_device
                or transition[4] != self.xtest_profile.parent_device
                or transition[6] != self.xtest_profile.root_xid
                or transition[7] != self.xtest_profile.root_xid
                or not isinstance(transition[9], tuple)
                or not isinstance(transition[8], tuple)
                or transition[8] != transition[9]
                or not isinstance(transition[10], tuple)
                or len(transition[10]) != 2
                or transition[10] != transition[11]
                or (previous_endpoint is not None and transition[8] != previous_endpoint)
            ):
                raise CalibrationError("input evidence XTEST transition ancestry changed")
            if any(
                isinstance(value, bool) or not isinstance(value, int) or value <= 0
                for value in (
                    transition[2],
                    transition[3],
                    transition[4],
                    transition[5],
                    transition[6],
                    transition[7],
                )
            ):
                raise CalibrationError("input evidence XTEST transition identity is malformed")
            if transition_index == 0:
                if tuple(transition[8]) != query_origin:
                    raise CalibrationError(
                        "input evidence XTEST producer query is not sealed to its transition"
                    )
            if not xi2_timestamp_valid(transition[1]):
                raise CalibrationError("input evidence XTEST transition timestamp is malformed")
            if (
                transition[1],
                transition[2],
                transition[3],
                transition[5],
                transition[6],
                transition[7],
                transition[8],
                transition[9],
            ) not in motion_facts or (transition[1], transition[4], transition[3], 0) not in raw_motion_facts:
                raise CalibrationError("input evidence XTEST transition is absent from the event proof")
            endpoint = transition[10]
            if tuple(transition[8]) == tuple(endpoint):
                raise CalibrationError("input evidence XTEST transition must prove nonzero displacement")
            if any(isinstance(value, bool) or not isinstance(value, int) for value in endpoint):
                raise CalibrationError("input evidence XTEST transition endpoint is malformed")
            if not in_target(endpoint):
                raise CalibrationError("input evidence XTEST motion endpoint left the certified target")
            valuators = transition[12]
            if (
                not isinstance(valuators, tuple)
                or len(valuators) != 2
                or {row[0] for row in valuators if isinstance(row, tuple) and row} != {0, 1}
                or any(
                    not isinstance(row, tuple)
                    or len(row) != 3
                    or not isinstance(row[1], float)
                    or not math.isfinite(row[1])
                    or not row[1].is_integer()
                    for row in valuators
                )
                or tuple(int(row[1]) for row in valuators) != endpoint
            ):
                raise CalibrationError("input evidence XTEST transition valuators are malformed")
            matching_primary = [
                proof
                for proof in self.event_proof
                if isinstance(proof, tuple)
                and len(proof) == 12
                and proof[0] == "Motion"
                and proof[4] == transition[1]
                and proof[1] == transition[2]
                and proof[2] == transition[3]
                and proof[7] == transition[5]
                and tuple(proof[8]) == tuple(transition[8])
                and tuple(proof[9]) == tuple(transition[9])
            ]
            if len(matching_primary) != 1 or (
                tuple(matching_primary[0][10]) != tuple(transition[13])
                or matching_primary[0][11] != transition[14]
            ):
                raise CalibrationError("input evidence XTEST transition modifiers are not sealed to Motion")
            previous_endpoint = endpoint
        if (
            tuple(self.synthetic_motion_transitions[0][8]) != query_origin
            or tuple(self.synthetic_motion_transitions[-1][10]) != query_endpoint
        ):
            raise CalibrationError("input evidence XTEST motion sequence is not sealed to its query")
        actual_endpoints = tuple(tuple(transition[10]) for transition in self.synthetic_motion_transitions)
        if actual_endpoints != tuple(planned_endpoints):
            raise CalibrationError("input evidence XTEST motion sequence differs from its plan")
        if (
            not isinstance(self.fingerprint, str)
            or not re.fullmatch(r"[0-9a-f]{64}", self.fingerprint)
            or isinstance(self.target_pid, bool)
            or not isinstance(self.target_pid, int)
            or self.target_pid <= 1
            or isinstance(self.target_xid, bool)
            or not isinstance(self.target_xid, int)
            or self.target_xid <= 0
            or isinstance(self.target_child_xid, bool)
            or not isinstance(self.target_child_xid, int)
            or self.target_child_xid <= 0
            or not self.xinput_hierarchy
            or not self.xauthority
            or not self.display
            or not self.driver_version
            or not self.target_executable
            or not self.target_process_start
            or not self.game_identity_fingerprint
            or not isinstance(self.target_layout_kind, str)
            or self.target_layout_kind not in {GTK_LAYOUT_MUTTER_FRAME, GTK_LAYOUT_ROOT_CHILD}
            or isinstance(self.target_root_xid, bool)
            or not isinstance(self.target_root_xid, int)
            or self.target_root_xid < 0
            or isinstance(self.target_parent_xid, bool)
            or not isinstance(self.target_parent_xid, int)
            or self.target_parent_xid < 0
            or isinstance(self.managed_frame_xid, bool)
            or not isinstance(self.managed_frame_xid, int)
            or not self.target_event_xids
            or any(
                isinstance(xid, bool) or not isinstance(xid, int) or xid <= 0
                for xid in self.target_event_xids
            )
            or len(set(self.target_event_xids)) != len(self.target_event_xids)
            or self.target_xid not in self.target_event_xids
            or self.target_child_xid not in self.target_event_xids
            or not isinstance(self.target_descendant_xids, (tuple, list))
            or any(
                isinstance(xid, bool) or not isinstance(xid, int) or xid <= 0
                for xid in self.target_descendant_xids
            )
            or len(set(self.target_descendant_xids)) != len(self.target_descendant_xids)
            or not isinstance(self.target_excluded_input_only_xids, (tuple, list))
            or any(
                isinstance(xid, bool) or not isinstance(xid, int) or xid <= 0
                for xid in self.target_excluded_input_only_xids
            )
            or len(set(self.target_excluded_input_only_xids)) != len(self.target_excluded_input_only_xids)
            or set(self.target_event_xids) & set(self.target_excluded_input_only_xids)
            or not isinstance(self.synthetic_modifiers, (tuple, list))
            or len(self.synthetic_modifiers) != 3
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in self.synthetic_modifiers
            )
        ):
            raise CalibrationError("input evidence identity is incomplete")
        if self.target_layout_kind == GTK_LAYOUT_ROOT_CHILD:
            descendants = set(self.target_descendant_xids)
            excluded = set(self.target_excluded_input_only_xids)
            if (
                self.target_root_xid <= 0
                or self.target_parent_xid != self.target_root_xid
                or self.target_root_xid in self.target_event_xids
                or self.managed_frame_xid != 0
                or tuple(self.managed_frame_geometry) != ()
                or not descendants
                or self.target_xid not in descendants
                or self.target_xid in excluded
                or len(excluded) != 1
                or not excluded.issubset(descendants)
                or self.target_root_xid in descendants
                or descendants != {self.target_xid, *excluded}
                or set(self.target_event_xids) != {self.target_xid}
            ):
                raise CalibrationError("input evidence root-child target certificate is invalid")
        else:
            frame_geometry = tuple(self.managed_frame_geometry)
            if (
                self.managed_frame_xid <= 0
                or self.managed_frame_xid == self.target_xid
                or self.managed_frame_xid not in self.target_event_xids
                or len(frame_geometry) != 4
                or any(isinstance(value, bool) or not isinstance(value, int) for value in frame_geometry)
                or frame_geometry[2] <= 0
                or frame_geometry[3] <= 0
                or (self.target_root_xid > 0 and self.target_root_xid in self.target_event_xids)
                or (self.target_root_xid > 0 and self.target_root_xid in self.target_descendant_xids)
                or (self.target_root_xid > 0 and self.target_parent_xid != self.managed_frame_xid)
                or (
                    self.target_descendant_xids
                    and set(self.target_event_xids) != {self.managed_frame_xid, *self.target_descendant_xids}
                )
            ):
                raise CalibrationError("input evidence managed-frame target certificate is invalid")
        self._verify_keyboard_profile()
        for name, geometry in (
            ("target", self.target_geometry),
            ("game", self.game_geometry),
        ):
            if (
                not isinstance(geometry, (tuple, list))
                or len(geometry) != 4
                or any(isinstance(value, bool) or not isinstance(value, int) for value in geometry)
                or geometry[2] <= 0
                or geometry[3] <= 0
            ):
                raise CalibrationError(f"input evidence {name} geometry is incomplete")
        if self.target_layout_kind == GTK_LAYOUT_MUTTER_FRAME and not _rect_contains(
            tuple(self.managed_frame_geometry), tuple(self.target_geometry)
        ):
            raise CalibrationError("input evidence managed frame does not enclose its GTK client")
        if not self.synthetic_events or not self.human_events or not self.forwarded_events:
            raise CalibrationError("input evidence event proof is incomplete")
        for echo in self.synthetic_echo_proof:
            coordinate_facts = (echo[5], echo[6]) if isinstance(echo, tuple) and len(echo) == 10 else ()
            if (
                not isinstance(echo, tuple)
                or len(echo) != 10
                or echo[0] != "MotionEcho"
                or any(
                    isinstance(echo[index], bool) or not isinstance(echo[index], int) or echo[index] <= 0
                    for index in (2, 3, 4, 8, 9)
                )
                or echo[5] is None
                or echo[6] is None
                or any(
                    not isinstance(point, (tuple, list))
                    or len(point) != 2
                    or any(
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(value)
                        for value in point
                    )
                    for point in coordinate_facts
                )
                or xi2_timestamp_elapsed(echo[7], echo[1]) is None
                or xi2_timestamp_elapsed(echo[7], echo[1]) > 250
            ):
                raise CalibrationError("input evidence forwarded echo proof is malformed")
        for name, events in (("synthetic", self.synthetic_events), ("human", self.human_events)):
            if any(
                not isinstance(event, tuple)
                or len(event) != 3
                or not isinstance(event[0], str)
                or not isinstance(event[1], int)
                or event[1] <= 0
                or (event[2] is not None and (isinstance(event[2], bool) or not isinstance(event[2], int)))
                for event in events
            ):
                raise CalibrationError(f"input evidence {name} event facts are malformed")
        if any(
            not isinstance(event, tuple)
            or len(event) != 3
            or not isinstance(event[0], str)
            or not isinstance(event[1], int)
            or event[1] <= 0
            or (event[2] is not None and (isinstance(event[2], bool) or not isinstance(event[2], int)))
            for event in self.forwarded_events
        ):
            raise CalibrationError("input evidence forwarded event facts are malformed")
        if any(
            event[2] not in (None, 0) and event[2] not in self.target_event_xids
            for event in self.forwarded_events
        ):
            raise CalibrationError(
                "input evidence forwarded event target is outside the certified target set"
            )
        if not self.event_proof:
            raise CalibrationError("input evidence has no complete event proof")
        proof_sources: dict[int, set[str]] = {}
        synthetic_modifier_facts: dict[int, set[tuple[tuple[int, int, int], int]]] = {}
        known_classes = {
            "RawKeyPress",
            "KeyPress",
            "RawKeyRelease",
            "KeyRelease",
            "RawButtonPress",
            "ButtonPress",
            "RawButtonRelease",
            "ButtonRelease",
            "RawMotion",
            "Motion",
            "DeviceChanged",
            "Enter",
            "Leave",
        }
        for proof in self.event_proof:
            if not isinstance(proof, tuple) or len(proof) != 12:
                raise CalibrationError("input evidence contains an incomplete event record")
            if (
                not isinstance(proof[0], str)
                or not isinstance(proof[1], int)
                or not isinstance(proof[2], int)
            ):
                raise CalibrationError("input evidence event identity is malformed")
            if proof[0] not in known_classes:
                raise CalibrationError("input evidence contains an unknown event class")
            proof_sources.setdefault(proof[2], set()).add(proof[0])
            parent_device = next(
                (row[1] for row in self.xinput_hierarchy if row[0] == proof[2]),
                0,
            )
            delivered_class = proof[0] in {
                "KeyPress",
                "KeyRelease",
                "ButtonPress",
                "ButtonRelease",
                "Motion",
                "Enter",
                "Leave",
            }
            expected_device = proof[2] if delivered_class else parent_device
            if expected_device <= 0 or proof[1] != expected_device:
                raise CalibrationError("input evidence XI2 master/source semantics are malformed")
            if isinstance(proof[4], bool) or not isinstance(proof[4], int):
                raise CalibrationError("input evidence event timestamp is malformed")
            if proof[11] is not None and (
                isinstance(proof[11], bool) or not isinstance(proof[11], int) or proof[11] < 0
            ):
                raise CalibrationError("input evidence effective modifier state is malformed")
            if proof[0] in {
                "KeyPress",
                "KeyRelease",
                "ButtonPress",
                "ButtonRelease",
                "Motion",
            } and (proof[10] is None or proof[11] is None):
                raise CalibrationError("input evidence delivered modifier effects are incomplete")
            if proof[0] in {
                "KeyPress",
                "KeyRelease",
                "ButtonPress",
                "ButtonRelease",
                "Motion",
            }:
                modifiers = proof[10]
                if (
                    not isinstance(modifiers, (tuple, list))
                    or len(modifiers) != 3
                    or any(
                        isinstance(value, bool) or not isinstance(value, int) or value < 0
                        for value in modifiers
                    )
                    or proof[11] != modifiers[0] | modifiers[1] | modifiers[2]
                    or any(
                        isinstance(value, bool) or not isinstance(value, int) or value <= 0
                        for value in (proof[5], proof[6], proof[7])
                    )
                    or any(
                        not isinstance(point, (tuple, list))
                        or len(point) != 2
                        or any(
                            isinstance(value, bool)
                            or not isinstance(value, (int, float))
                            or not math.isfinite(value)
                            for value in point
                        )
                        for point in (proof[8], proof[9])
                    )
                ):
                    raise CalibrationError("input evidence delivered event facts are malformed")
                if proof[2] in {self.synthetic_key_source, self.synthetic_pointer_source}:
                    synthetic_modifier_facts.setdefault(proof[2], set()).add(
                        (tuple(modifiers), proof[11])  # type: ignore[arg-type]
                    )
        key_classes = {"RawKeyPress", "KeyPress", "RawKeyRelease", "KeyRelease"}
        pointer_classes = {"RawButtonPress", "ButtonPress", "RawButtonRelease", "ButtonRelease"}
        for source, expected in (
            (self.synthetic_key_source, key_classes),
            (self.synthetic_pointer_source, pointer_classes),
            (self.human_key_source, key_classes),
            (self.human_pointer_source, pointer_classes),
        ):
            if not expected.issubset(proof_sources.get(source, set())):
                raise CalibrationError("input evidence lacks complete press/release event facts")
        expected_synthetic_modifiers = tuple(self.synthetic_modifiers)
        expected_effective = (
            expected_synthetic_modifiers[0]
            | expected_synthetic_modifiers[1]
            | expected_synthetic_modifiers[2]
        )
        pointer_facts = synthetic_modifier_facts.get(self.synthetic_pointer_source)
        if pointer_facts != {(expected_synthetic_modifiers, expected_effective)}:
            raise CalibrationError(
                "input evidence synthetic pointer modifier facts are not sealed consistently"
            )
        key_delivered = [
            proof
            for proof in self.event_proof
            if proof[2] == self.synthetic_key_source and proof[0] in {"KeyPress", "KeyRelease"}
        ]
        profile_steps = [step for _operation, _payload, steps in self.keyboard_profile for step in steps]
        key_events = [
            proof
            for proof in self.event_proof
            if proof[2] == self.synthetic_key_source
            and proof[0] in {"RawKeyPress", "KeyPress", "RawKeyRelease", "KeyRelease"}
        ]
        if len(key_delivered) < 2 or len(key_events) != 4 + 2 * len(profile_steps):
            raise CalibrationError("input evidence synthetic key probe is incomplete")
        expected_key_events: list[tuple[str, int]] = [
            ("RawKeyPress", 8),
            ("KeyPress", 8),
            ("RawKeyRelease", 8),
            ("KeyRelease", 8),
        ]
        for step in profile_steps:
            phase, _keysym, detail, _modifiers, _effective = step
            expected_key_events.extend(
                (("RawKeyPress", detail), ("KeyPress", detail))
                if phase == "press"
                else (("RawKeyRelease", detail), ("KeyRelease", detail))
            )
        if [(proof[0], proof[3]) for proof in key_events] != expected_key_events:
            raise CalibrationError("input evidence synthetic key probe order is not sealed")
        if any(
            current[4] < previous[4] for previous, current in zip(key_events, key_events[1:], strict=False)
        ):
            raise CalibrationError("input evidence synthetic key timestamps are not ordered")
        if any(
            proof[0].startswith("Raw") and any(value is not None for value in proof[5:10])
            for proof in key_events
        ):
            raise CalibrationError("input evidence raw keyboard facts carry a delivered target")
        if any(
            tuple(proof[10]) != expected_synthetic_modifiers or proof[11] != expected_effective
            for proof in key_delivered[:2]
        ):
            raise CalibrationError("input evidence synthetic probe baseline modifiers changed")
        if len(key_delivered[2:]) != len(profile_steps):
            raise CalibrationError("input evidence keyboard profile does not cover synthetic key events")
        for proof, step in zip(key_delivered[2:], profile_steps, strict=True):
            phase, _keysym, detail, modifiers, effective = step
            expected_class = "KeyPress" if phase == "press" else "KeyRelease"
            if (
                proof[0] != expected_class
                or proof[3] != detail
                or tuple(proof[10]) != modifiers
                or proof[11] != effective
            ):
                raise CalibrationError("input evidence keyboard profile does not match event proof")
        proof_event_facts = {(proof[0], proof[2], proof[7]) for proof in self.event_proof}
        if any(tuple(event) not in proof_event_facts for event in self.forwarded_events):
            raise CalibrationError("input evidence forwarded facts are absent from the complete event proof")
        motion_facts = {
            (
                proof[4],
                proof[1],
                proof[2],
                proof[7],
                tuple(proof[8]),
                tuple(proof[9]),
            )
            for proof in self.event_proof
            if proof[0] == "Motion"
        }
        echo_facts = {
            (
                echo[1],
                echo[2],
                echo[3],
                echo[4],
                tuple(echo[5]),
                tuple(echo[6]),
            )
            for echo in self.synthetic_echo_proof
        }
        # The forwarded record stores its own (source, source) pair at
        # positions 2/3.  The synthetic delivered record is a separate
        # (source, source) pair; never reconstruct it with the forwarded
        # device (the host trace's 7(7) echo must not become 7(4)).
        for echo in self.synthetic_echo_proof:
            echo_records = [
                proof
                for proof in self.event_proof
                if proof[0] == "Motion"
                and proof[4] == echo[1]
                and proof[1] == echo[2]
                and proof[2] == echo[3]
                and proof[7] == echo[4]
                and tuple(proof[8]) == tuple(echo[5])
                and tuple(proof[9]) == tuple(echo[6])
            ]
            if len(echo_records) != 1 or any(
                proof[5] != self.xtest_profile.root_xid or proof[6] != self.xtest_profile.root_xid
                for proof in echo_records
            ):
                raise CalibrationError("input evidence forwarded echo root/event identity is not sealed")
            candidates = {
                fact
                for fact in transition_facts
                if fact[0] == echo[7]
                and fact[1] == echo[8]
                and fact[2] == echo[9]
                and fact[3] == tuple(echo[5])
                and fact[4] == tuple(echo[6])
            }
            if len(candidates) != 1:
                raise CalibrationError("input evidence synthetic motion echo lacks its delivered source pair")
            candidate = next(iter(candidates))
            if candidate[1] != self.xtest_profile.source_device:
                raise CalibrationError("input evidence synthetic delivered motion has an invalid source pair")
        for motion in motion_facts:
            if (
                motion[2]
                not in {
                    self.synthetic_pointer_source,
                    self.synthetic_key_source,
                    self.human_pointer_source,
                }
                and motion not in echo_facts
            ):
                raise CalibrationError("input evidence has an unproven forwarded motion")
        if not echo_facts.issubset(motion_facts):
            raise CalibrationError("input evidence controlled echo is absent from the complete event proof")
        if not re.fullmatch(r"[0-9a-f]{64}", self.evidence_hash):
            raise CalibrationError("input evidence has no canonical seal")
        if self.fingerprint != self.content_fingerprint():
            raise CalibrationError("input evidence fingerprint does not cover its facts")
        expected = self.with_hash().evidence_hash
        if self.evidence_hash != expected:
            raise CalibrationError("input evidence canonical seal does not match its facts")


@dataclass(frozen=True)
class NativeMemoryMeasurement:
    """One complete reset5-bounded native memory window.

    The historical HWM is retained before ``clear_refs`` is written.  The
    growth value is calculated only from the post-reset HWM to the final HWM
    in this same window; a lifetime peak is never used as a baseline.
    """

    method: str
    phase: str
    ordinal: int
    pid: int
    process_start: str
    process_executable: str
    identity_fingerprint: str
    uid: int
    kernel_release: str
    historical_hwm_bytes: int
    rss_before_reset_bytes: int
    reset_completed_monotonic: float
    post_reset_hwm_bytes: int
    rss_after_reset_bytes: int
    workload_start_monotonic: float
    workload_end_monotonic: float
    workload_start_ordinal: int
    workload_end_ordinal: int
    final_hwm_bytes: int
    final_rss_bytes: int
    growth_bytes: int
    limit_bytes: int
    reset_value: int = 5

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "phase": self.phase,
            "ordinal": self.ordinal,
            "pid": self.pid,
            "process_start": self.process_start,
            "process_executable": self.process_executable,
            "identity_fingerprint": self.identity_fingerprint,
            "uid": self.uid,
            "kernel_release": self.kernel_release,
            "historical_hwm_bytes": self.historical_hwm_bytes,
            "rss_before_reset_bytes": self.rss_before_reset_bytes,
            "reset_completed_monotonic": self.reset_completed_monotonic,
            "post_reset_hwm_bytes": self.post_reset_hwm_bytes,
            "rss_after_reset_bytes": self.rss_after_reset_bytes,
            "workload_start_monotonic": self.workload_start_monotonic,
            "workload_end_monotonic": self.workload_end_monotonic,
            "workload_start_ordinal": self.workload_start_ordinal,
            "workload_end_ordinal": self.workload_end_ordinal,
            "final_hwm_bytes": self.final_hwm_bytes,
            "final_rss_bytes": self.final_rss_bytes,
            "growth_bytes": self.growth_bytes,
            "limit_bytes": self.limit_bytes,
            "reset_value": self.reset_value,
        }

    def verify(self) -> None:
        if self.method != "linux_proc_status_vmhwm_reset5" or self.reset_value != 5:
            raise CalibrationError("native memory method is not the reset5 contract")
        if self.phase not in {"construction", "updates"}:
            raise CalibrationError("native memory phase is invalid")
        if self.phase == "construction" and self.ordinal != 0:
            raise CalibrationError("native construction memory ordinal is not zero")
        if self.phase == "updates" and self.ordinal != 0:
            raise CalibrationError("native update memory window ordinal is not zero")
        ints = (
            self.ordinal,
            self.pid,
            self.uid,
            self.historical_hwm_bytes,
            self.rss_before_reset_bytes,
            self.post_reset_hwm_bytes,
            self.rss_after_reset_bytes,
            self.workload_start_ordinal,
            self.workload_end_ordinal,
            self.final_hwm_bytes,
            self.final_rss_bytes,
            self.growth_bytes,
            self.limit_bytes,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) for value in ints):
            raise CalibrationError("native memory accounting is not integer-valued")
        if self.pid <= 1 or self.uid < 0 or any(value < 0 for value in ints[3:]):
            raise CalibrationError("native memory accounting is negative or invalid")
        expected_limit = (
            NATIVE_CONSTRUCTION_LIMIT_BYTES if self.phase == "construction" else NATIVE_UPDATES_LIMIT_BYTES
        )
        if self.ordinal < 0 or self.limit_bytes != expected_limit:
            raise CalibrationError("native memory window bounds are invalid")
        if not self.process_start or not self.process_executable or not self.identity_fingerprint:
            raise CalibrationError("native memory identity is incomplete")
        times = (
            self.reset_completed_monotonic,
            self.workload_start_monotonic,
            self.workload_end_monotonic,
        )
        if any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value))
            for value in times
        ):
            raise CalibrationError("native memory timestamps are not finite")
        if self.workload_end_monotonic < self.workload_start_monotonic:
            raise CalibrationError("native memory workload time reversed")
        if self.reset_completed_monotonic > self.workload_start_monotonic:
            raise CalibrationError("native memory workload began before its reset completed")
        if self.growth_bytes != max(0, self.final_hwm_bytes - self.post_reset_hwm_bytes):
            raise CalibrationError("native memory growth is not derived from the reset window")
        if self.final_hwm_bytes < 0 or self.final_rss_bytes < 0:
            raise CalibrationError("native final memory accounting is negative")


@dataclass(frozen=True)
class NativeMemoryResetRecord:
    """Historical accounting that must be archived before clear_refs is written."""

    method: str
    phase: str
    ordinal: int
    pid: int
    process_start: str
    process_executable: str
    identity_fingerprint: str
    uid: int
    kernel_release: str
    historical_hwm_bytes: int
    rss_before_reset_bytes: int
    limit_bytes: int
    reset_value: int = 5

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "phase": self.phase,
            "ordinal": self.ordinal,
            "pid": self.pid,
            "process_start": self.process_start,
            "process_executable": self.process_executable,
            "identity_fingerprint": self.identity_fingerprint,
            "uid": self.uid,
            "kernel_release": self.kernel_release,
            "historical_hwm_bytes": self.historical_hwm_bytes,
            "rss_before_reset_bytes": self.rss_before_reset_bytes,
            "limit_bytes": self.limit_bytes,
            "reset_value": self.reset_value,
        }

    def verify(self) -> None:
        if (
            self.method != "linux_proc_status_vmhwm_reset5"
            or self.phase not in {"construction", "updates"}
            or self.reset_value != 5
        ):
            raise CalibrationError("native reset record method or phase is invalid")
        if (
            isinstance(self.ordinal, bool)
            or not isinstance(self.ordinal, int)
            or self.ordinal != 0
            or isinstance(self.pid, bool)
            or not isinstance(self.pid, int)
            or self.pid <= 1
            or isinstance(self.uid, bool)
            or not isinstance(self.uid, int)
            or self.uid < 0
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in (self.historical_hwm_bytes, self.rss_before_reset_bytes, self.limit_bytes)
            )
            or self.limit_bytes
            != (
                NATIVE_CONSTRUCTION_LIMIT_BYTES
                if self.phase == "construction"
                else NATIVE_UPDATES_LIMIT_BYTES
            )
            or not self.process_start
            or not self.process_executable
            or not self.identity_fingerprint
            or not self.kernel_release
        ):
            raise CalibrationError("native reset record is incomplete")


@dataclass(frozen=True)
class NativeMemoryArchiveReceipt:
    """Durability acknowledgement returned by the production journal hook."""

    archive_id: str
    record_sha256: str
    durable: bool
    fsynced: bool

    @classmethod
    def for_record(cls, record: NativeMemoryResetRecord, *, archive_id: str) -> NativeMemoryArchiveReceipt:
        digest = hashlib.sha256(
            json.dumps(record.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return cls(archive_id, digest, True, True)

    def verify(self, record: NativeMemoryResetRecord) -> None:
        expected = NativeMemoryArchiveReceipt.for_record(record, archive_id=self.archive_id)
        if (
            not self.archive_id
            or not re.fullmatch(r"[0-9a-f]{64}", self.record_sha256)
            or self.record_sha256 != expected.record_sha256
            or self.durable is not True
            or self.fsynced is not True
        ):
            raise CalibrationError("native historical memory archive was not durable")


def _native_memory_measurement_digest(measurement: NativeMemoryMeasurement) -> str:
    return hashlib.sha256(
        json.dumps(measurement.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _native_memory_failure_outcome_id(measurement: NativeMemoryMeasurement, failure: str) -> str:
    payload = {
        "kind": "native_memory_failures",
        "measurement": measurement.to_dict(),
        "failure": failure,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _native_memory_assessment_payload(measurement: NativeMemoryMeasurement) -> dict[str, Any]:
    """Return the canonical, failure-independent over-target assessment domain."""

    return {
        "kind": "native_memory_assessments",
        "schema": 1,
        "classification": "target_exceeded",
        "measurement": measurement.to_dict(),
    }


def _native_memory_assessment_outcome_id(measurement: NativeMemoryMeasurement) -> str:
    return hashlib.sha256(
        json.dumps(
            _native_memory_assessment_payload(measurement),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True)
class NativeMemoryAssessmentArchiveReceipt:
    """Durability acknowledgement for an over-target resource assessment."""

    outcome_id: str
    measurement_sha256: str
    archive_ordinal: int
    durable: bool

    @classmethod
    def from_archive(
        cls, measurement: NativeMemoryMeasurement, archived: dict[str, Any]
    ) -> NativeMemoryAssessmentArchiveReceipt:
        if not isinstance(archived, dict):
            raise CalibrationError("native resource assessment archive returned no record")
        expected_payload = _native_memory_assessment_payload(measurement)
        if any(archived.get(key) != value for key, value in expected_payload.items()):
            raise CalibrationError("native resource assessment archive domain is invalid")
        ordinal = archived.get("archiveOrdinal")
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal <= 0:
            raise CalibrationError("native resource assessment archive returned no ordinal")
        outcome_id = archived.get("outcomeId")
        if not isinstance(outcome_id, str):
            raise CalibrationError("native resource assessment archive returned no outcome identity")
        if outcome_id != _native_memory_assessment_outcome_id(measurement):
            raise CalibrationError("native resource assessment archive identity is invalid")
        return cls(
            outcome_id=outcome_id,
            measurement_sha256=_native_memory_measurement_digest(measurement),
            archive_ordinal=ordinal,
            durable=True,
        )

    def verify(self, measurement: NativeMemoryMeasurement) -> None:
        measurement.verify()
        if measurement.growth_bytes <= measurement.limit_bytes:
            raise CalibrationError("native resource assessment is not target-exceeded")
        if (
            not isinstance(self.outcome_id, str)
            or not re.fullmatch(r"[0-9a-f]{64}", self.outcome_id)
            or self.outcome_id != _native_memory_assessment_outcome_id(measurement)
            or not isinstance(self.measurement_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", self.measurement_sha256)
            or self.measurement_sha256 != _native_memory_measurement_digest(measurement)
            or isinstance(self.archive_ordinal, bool)
            or not isinstance(self.archive_ordinal, int)
            or self.archive_ordinal <= 0
            or self.durable is not True
        ):
            raise CalibrationError("native resource assessment receipt is invalid")


@dataclass(frozen=True)
class NativeMemoryMeasurementArchiveReceipt:
    """Durability acknowledgement for one rejected complete memory window."""

    outcome_id: str
    measurement_sha256: str
    archive_ordinal: int
    durable: bool

    @classmethod
    def from_archive(
        cls, measurement: NativeMemoryMeasurement, archived: dict[str, Any]
    ) -> NativeMemoryMeasurementArchiveReceipt:
        if not isinstance(archived, dict):
            raise CalibrationError("native failed-memory archive returned no record")
        ordinal = archived.get("archiveOrdinal")
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal <= 0:
            raise CalibrationError("native failed-memory archive returned no ordinal")
        outcome_id = archived.get("outcomeId")
        if not isinstance(outcome_id, str):
            raise CalibrationError("native failed-memory archive returned no outcome identity")
        return cls(
            outcome_id=outcome_id,
            measurement_sha256=_native_memory_measurement_digest(measurement),
            archive_ordinal=ordinal,
            durable=True,
        )

    def verify(self, measurement: NativeMemoryMeasurement) -> None:
        measurement.verify()
        failure = f"native {measurement.phase} memory growth exceeds its limit"
        if (
            not isinstance(self.outcome_id, str)
            or not re.fullmatch(r"[0-9a-f]{64}", self.outcome_id)
            or self.outcome_id != _native_memory_failure_outcome_id(measurement, failure)
            or not isinstance(self.measurement_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", self.measurement_sha256)
            or self.measurement_sha256 != _native_memory_measurement_digest(measurement)
            or isinstance(self.archive_ordinal, bool)
            or not isinstance(self.archive_ordinal, int)
            or self.archive_ordinal <= 0
            or self.durable is not True
        ):
            raise CalibrationError("native failed-memory archive receipt is invalid")


@dataclass(frozen=True)
class NativeRestorationReceipt:
    """Explicit Agent restoration status after a failed measurement."""

    state: str
    restored: bool
    reason: str
    durable: bool = False

    def verify(self) -> None:
        if not isinstance(self.reason, str) or not self.reason:
            raise CalibrationError("native measurement failure has no restoration status")
        if self.state != "restoration_required" or self.restored is not False or self.durable is not True:
            raise CalibrationError("native failure restoration-required state is not durable")


@dataclass(frozen=True)
class NativeResumeBoundary:
    """Validated held/100 proof passed before requesting normal publication."""

    identity_fingerprint: str
    process_start: str
    process_executable: str
    boot: str
    sequence: int
    crc32: int
    update_count: int
    capture_monotonic: float
    capture_sha256: str
    render: dict[str, Any]
    phase: str = "held"
    ordinal: int = 100
    _seal: object | None = None

    @classmethod
    def _issue(cls, **kwargs: Any) -> NativeResumeBoundary:
        return cls(**kwargs, _seal=_SEAL)

    def verify(self) -> None:
        if (
            self._seal is not _SEAL
            or not isinstance(self.identity_fingerprint, str)
            or not self.identity_fingerprint
            or not isinstance(self.process_start, str)
            or not self.process_start
            or not isinstance(self.process_executable, str)
            or not self.process_executable
            or not isinstance(self.boot, str)
            or not self.boot
            or not isinstance(self.phase, str)
            or self.phase != "held"
            or isinstance(self.ordinal, bool)
            or not isinstance(self.ordinal, int)
            or self.ordinal != 100
            or isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence <= 0
            or isinstance(self.crc32, bool)
            or not isinstance(self.crc32, int)
            or self.crc32 < 0
            or isinstance(self.update_count, bool)
            or not isinstance(self.update_count, int)
            or self.update_count < 0
            or isinstance(self.capture_monotonic, bool)
            or not isinstance(self.capture_monotonic, (int, float))
            or not math.isfinite(float(self.capture_monotonic))
            or self.capture_monotonic < 0
            or not isinstance(self.capture_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", self.capture_sha256)
            or not isinstance(self.render, dict)
        ):
            raise CalibrationError("native held boundary is incomplete")
        NativeCalibrationRunner._validate_metrics(self.render, minimum_updates=100)
        if self.render.get("nativePhase") != "held" or self.render.get("nativeOrdinal") != 100:
            raise CalibrationError("native held boundary phase is invalid")
        if self.render.get("updateCount") != self.update_count:
            raise CalibrationError("native held boundary count is not sealed")


@dataclass(frozen=True)
class _PendingNativeMemoryWindow:
    meter: LinuxProcStatusVmHwmReset5
    method: str
    phase: str
    ordinal: int
    pid: int
    process_start: str
    process_executable: str
    identity_fingerprint: str
    uid: int
    kernel_release: str
    historical_hwm_bytes: int
    rss_before_reset_bytes: int
    reset_completed_monotonic: float
    post_reset_hwm_bytes: int
    rss_after_reset_bytes: int
    limit_bytes: int


class LinuxProcStatusVmHwmReset5:
    """Pinned, identity-bound Linux ``VmHWM`` reset5 meter.

    Only ``status``, ``stat``, ``exe`` and ``clear_refs`` for the pinned PID
    are read or written.  The directory fd prevents a later path lookup from
    silently changing the process being measured.
    """

    METHOD = "linux_proc_status_vmhwm_reset5"

    def __init__(
        self,
        pid: int,
        *,
        process_start: str,
        process_executable: str,
        identity_fingerprint: str,
        expected_uid: int | None = None,
        proc_root: str | os.PathLike[str] = "/proc",
        monotonic: Callable[[], float] = time.monotonic,
        over_limit_archive: Callable[[NativeMemoryMeasurement], NativeMemoryAssessmentArchiveReceipt]
        | None = None,
    ) -> None:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
            raise CalibrationError("native memory PID is invalid")
        if not process_start or not process_executable or not identity_fingerprint:
            raise CalibrationError("native memory identity is incomplete")
        self.pid = pid
        self.process_start = str(process_start)
        self.process_executable = os.path.realpath(process_executable)
        self.identity_fingerprint = identity_fingerprint
        self.expected_uid = os.getuid() if expected_uid is None else expected_uid
        if (
            isinstance(self.expected_uid, bool)
            or not isinstance(self.expected_uid, int)
            or self.expected_uid < 0
        ):
            raise CalibrationError("native memory UID is invalid")
        if over_limit_archive is not None and not callable(over_limit_archive):
            raise CalibrationError("native failed-memory archive callback is invalid")
        self._over_limit_archive = over_limit_archive
        self._proc_root = Path(proc_root)
        try:
            self._pid_fd = os.open(self._proc_root / str(pid), os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except OSError as exc:
            raise CalibrationError("cannot pin the owned process proc directory") from exc
        self._monotonic = monotonic
        self.kernel_release = os.uname().release
        self._closed = False
        self._pending_window: _PendingNativeMemoryWindow | None = None
        self._completing_window: _PendingNativeMemoryWindow | None = None
        try:
            self._verify_identity()
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            os.close(self._pid_fd)

    def __enter__(self) -> LinuxProcStatusVmHwmReset5:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _open_pid(self, name: str, flags: int) -> int:
        if self._closed:
            raise CalibrationError("native memory meter is closed")
        try:
            return os.open(name, flags | os.O_CLOEXEC, dir_fd=self._pid_fd)
        except OSError as exc:
            raise CalibrationError(f"cannot open pinned proc entry {name}") from exc

    def _read_entry(self, name: str) -> bytes:
        fd = self._open_pid(name, os.O_RDONLY)
        try:
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 65536)
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
        finally:
            os.close(fd)

    def _status(self) -> tuple[int, int, int]:
        try:
            text = self._read_entry("status").decode("ascii")
            values: dict[str, int] = {}
            for line in text.splitlines():
                if line.startswith(("VmHWM:", "VmRSS:", "Uid:")):
                    key, value = line.split(":", 1)
                    values[key] = int(value.split()[0]) * 1024
            if not {"VmHWM", "VmRSS", "Uid"}.issubset(values):
                raise ValueError("required status fields are absent")
            uid = values["Uid"]
            # Uid is represented as four columns in proc; the first is the
            # real UID and is the only owner identity used here.
            uid = int(
                next(
                    line.split(":", 1)[1].split()[0] for line in text.splitlines() if line.startswith("Uid:")
                )
            )
            hwm, rss = values["VmHWM"], values["VmRSS"]
            if min(hwm, rss) < 0:
                raise ValueError("negative accounting")
            return hwm, rss, uid
        except (CalibrationError, OSError, UnicodeError, ValueError, StopIteration) as exc:
            if isinstance(exc, CalibrationError):
                raise
            raise CalibrationError("cannot parse pinned process status") from exc

    def _start(self) -> str:
        try:
            text = self._read_entry("stat").decode("ascii")
            closing = text.rfind(")")
            fields = text[closing + 2 :].split()
            if closing < 0 or len(fields) <= 19:
                raise ValueError("malformed stat")
            return fields[19]
        except (CalibrationError, OSError, UnicodeError, ValueError) as exc:
            if isinstance(exc, CalibrationError):
                raise
            raise CalibrationError("cannot read pinned process start time") from exc

    def _exe(self) -> str:
        if self._closed:
            raise CalibrationError("native memory meter is closed")
        try:
            return os.path.realpath(os.readlink("exe", dir_fd=self._pid_fd))
        except OSError as exc:
            raise CalibrationError("cannot read pinned process executable") from exc

    def _verify_identity(self) -> None:
        _hwm, _rss, uid = self._status()
        if (
            self._start() != self.process_start
            or self._exe() != self.process_executable
            or uid != self.expected_uid
        ):
            raise CalibrationError("native memory process identity changed")

    def begin_window(
        self,
        phase: str,
        ordinal: int,
        *,
        limit_bytes: int,
        before_reset: Callable[[NativeMemoryResetRecord], NativeMemoryArchiveReceipt],
    ) -> _PendingNativeMemoryWindow:
        if self._pending_window is not None:
            raise CalibrationError("native memory window is still awaiting final accounting")
        if (
            phase not in {"construction", "updates"}
            or isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
            or ordinal != 0
        ):
            raise CalibrationError("native memory window phase or ordinal is invalid")
        expected_limit = (
            NATIVE_CONSTRUCTION_LIMIT_BYTES if phase == "construction" else NATIVE_UPDATES_LIMIT_BYTES
        )
        if isinstance(limit_bytes, bool) or not isinstance(limit_bytes, int) or limit_bytes != expected_limit:
            raise CalibrationError("native memory limit is invalid")
        self._verify_identity()
        historical_hwm, rss_before, uid = self._status()
        record = NativeMemoryResetRecord(
            method=self.METHOD,
            phase=phase,
            ordinal=ordinal,
            pid=self.pid,
            process_start=self.process_start,
            process_executable=self.process_executable,
            identity_fingerprint=self.identity_fingerprint,
            uid=uid,
            kernel_release=self.kernel_release,
            historical_hwm_bytes=historical_hwm,
            rss_before_reset_bytes=rss_before,
            limit_bytes=limit_bytes,
        )
        record.verify()
        if not callable(before_reset):
            raise CalibrationError("native historical memory archive callback is not configured")
        try:
            receipt = before_reset(record)
            if not isinstance(receipt, NativeMemoryArchiveReceipt):
                raise CalibrationError("native historical memory archive returned no durability receipt")
            receipt.verify(record)
        except Exception as exc:
            raise CalibrationError("native historical memory archive failed before reset") from exc
        fd = self._open_pid("clear_refs", os.O_WRONLY)
        try:
            if os.write(fd, b"5") != 1:
                raise CalibrationError("native clear_refs reset was short")
            try:
                os.fsync(fd)
            except OSError as exc:
                # procfs clear_refs is a pseudo-file and Linux may reject
                # fsync with EINVAL after accepting the exact reset byte.
                # Other fsync failures remain a hard admission failure.
                if exc.errno != errno.EINVAL:
                    raise CalibrationError("native clear_refs reset was not durable") from exc
            reset_completed = float(self._monotonic())
        except OSError as exc:
            raise CalibrationError("native clear_refs reset was denied") from exc
        finally:
            os.close(fd)
        # Install the ownership token before any post-reset identity/status
        # read.  A reset may have succeeded even when that read observes a
        # PID replacement, and the caller must still be able to abort the
        # window and retry after releasing the token.
        pending = _PendingNativeMemoryWindow(
            self,
            self.METHOD,
            phase,
            ordinal,
            self.pid,
            self.process_start,
            self.process_executable,
            self.identity_fingerprint,
            uid,
            self.kernel_release,
            historical_hwm,
            rss_before,
            reset_completed,
            historical_hwm,
            rss_before,
            limit_bytes,
        )
        self._pending_window = pending
        self._verify_identity()
        post_hwm, rss_after, post_uid = self._status()
        if post_uid != uid:
            raise CalibrationError("native memory UID changed across reset")
        pending = replace(pending, post_reset_hwm_bytes=post_hwm, rss_after_reset_bytes=rss_after)
        self._pending_window = pending
        return pending

    def finish_window(
        self,
        pending: _PendingNativeMemoryWindow,
        *,
        workload_start_monotonic: float,
        workload_end_monotonic: float,
        workload_start_ordinal: int,
        workload_end_ordinal: int,
    ) -> NativeMemoryMeasurement:
        if pending.meter is not self:
            raise CalibrationError("native memory pending token belongs to another meter")
        if self._pending_window is not pending:
            raise CalibrationError("native memory pending token was already consumed")
        # Consume before the identity read.  A failed final read must not be
        # replayed with a different process state or a new workload interval.
        self._pending_window = None
        self._verify_identity()
        final_hwm, final_rss, uid = self._status()
        if uid != pending.uid:
            raise CalibrationError("native memory UID changed before final accounting")
        result = NativeMemoryMeasurement(
            method=pending.method,
            phase=pending.phase,
            ordinal=pending.ordinal,
            pid=pending.pid,
            process_start=pending.process_start,
            process_executable=pending.process_executable,
            identity_fingerprint=pending.identity_fingerprint,
            uid=pending.uid,
            kernel_release=pending.kernel_release,
            historical_hwm_bytes=pending.historical_hwm_bytes,
            rss_before_reset_bytes=pending.rss_before_reset_bytes,
            reset_completed_monotonic=pending.reset_completed_monotonic,
            post_reset_hwm_bytes=pending.post_reset_hwm_bytes,
            rss_after_reset_bytes=pending.rss_after_reset_bytes,
            workload_start_monotonic=workload_start_monotonic,
            workload_end_monotonic=workload_end_monotonic,
            workload_start_ordinal=workload_start_ordinal,
            workload_end_ordinal=workload_end_ordinal,
            final_hwm_bytes=final_hwm,
            final_rss_bytes=final_rss,
            growth_bytes=max(0, final_hwm - pending.post_reset_hwm_bytes),
            limit_bytes=pending.limit_bytes,
        )
        result.verify()
        if result.growth_bytes > result.limit_bytes:
            if self._over_limit_archive is not None:
                # The callback is only admitted while this exact, fully read
                # window is completing.  The daemon uses this boundary to
                # bind the archive to the meter's current ownership token.
                self._completing_window = pending
                try:
                    receipt = self._over_limit_archive(result)
                    if not isinstance(receipt, NativeMemoryAssessmentArchiveReceipt):
                        raise CalibrationError("native resource assessment archive returned no receipt")
                    receipt.verify(result)
                except Exception as exc:
                    raise CalibrationError(
                        "native resource assessment archive failed before over-target completion"
                    ) from exc
                finally:
                    self._completing_window = None
            else:
                raise CalibrationError("native resource assessment archive callback is not configured")
        return result

    def completion_matches(self, measurement: NativeMemoryMeasurement) -> bool:
        """Return whether a measurement is the exact window completing now."""

        pending = self._completing_window
        return bool(
            pending is not None
            and measurement.method == pending.method
            and measurement.phase == pending.phase
            and measurement.ordinal == pending.ordinal
            and measurement.pid == pending.pid
            and measurement.process_start == pending.process_start
            and measurement.process_executable == pending.process_executable
            and measurement.identity_fingerprint == pending.identity_fingerprint
            and measurement.uid == pending.uid
            and measurement.kernel_release == pending.kernel_release
            and measurement.historical_hwm_bytes == pending.historical_hwm_bytes
            and measurement.rss_before_reset_bytes == pending.rss_before_reset_bytes
            and measurement.reset_completed_monotonic == pending.reset_completed_monotonic
            and measurement.post_reset_hwm_bytes == pending.post_reset_hwm_bytes
            and measurement.rss_after_reset_bytes == pending.rss_after_reset_bytes
            and measurement.limit_bytes == pending.limit_bytes
        )

    def abort_window(self, pending: _PendingNativeMemoryWindow | None = None) -> None:
        """Consume an unfinished window so a later run can retry safely.

        The reset accounting cannot be undone; the archive callback has
        already retained its historical values.  Only the in-memory pending
        token is released here.
        """

        if pending is not None and self._pending_window is not pending:
            raise CalibrationError("native memory pending token was already consumed")
        self._pending_window = None


# This token is deliberately public because the main-engine archive hook must
# retain the historical values before the reset is allowed to proceed.
NativeMemoryWindowToken = _PendingNativeMemoryWindow


@dataclass(frozen=True)
class NativeCalibrationEvidence:
    """Lossless native status/render result tied to one process and boot."""

    fingerprint: str
    process_pid: int
    process_executable: str
    window_xid: int
    child_xid: int
    geometry: tuple[int, int, int, int]
    xinput_hierarchy: tuple[tuple[int, int, str, str], ...]
    crop: tuple[int, int, int, int]
    boot: str
    sequence: int
    crc32: int
    render: dict[str, Any]
    construction_update_count: int
    held_update_count: int
    resume_update_count: int
    memory_measurements: tuple[NativeMemoryMeasurement, ...]
    native_identity_fingerprint: str
    resume_sequence: int
    resume_crc32: int
    resume_render: dict[str, Any]
    resume_capture_monotonic: float
    capture_path: str
    xauthority: str = ""
    wine_prefix: str = ""
    provenance: str = "owned-live"
    is_live: bool = True
    process_start: str = ""
    capture_sha256: str = ""
    capture_monotonic: float = -1.0
    preconstruction_fingerprint: str = ""
    evidence_hash: str = ""
    _seal: object | None = None

    @classmethod
    def _issue(cls, **kwargs: Any) -> NativeCalibrationEvidence:
        issued = cls(**kwargs, _seal=_SEAL)
        return replace(issued, fingerprint=issued.content_fingerprint()).with_hash()

    def _canonical_payload(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "process_pid": self.process_pid,
            "process_executable": self.process_executable,
            "process_start": self.process_start,
            "window_xid": self.window_xid,
            "child_xid": self.child_xid,
            "geometry": self.geometry,
            "xinput_hierarchy": self.xinput_hierarchy,
            "crop": self.crop,
            "boot": self.boot,
            "sequence": self.sequence,
            "crc32": self.crc32,
            "render": self.render,
            "construction_update_count": self.construction_update_count,
            "held_update_count": self.held_update_count,
            "resume_update_count": self.resume_update_count,
            "memory_measurements": tuple(measurement.to_dict() for measurement in self.memory_measurements),
            "native_identity_fingerprint": self.native_identity_fingerprint,
            "resume_sequence": self.resume_sequence,
            "resume_crc32": self.resume_crc32,
            "resume_render": self.resume_render,
            "resume_capture_monotonic": self.resume_capture_monotonic,
            "capture_path": self.capture_path,
            "capture_sha256": self.capture_sha256,
            "capture_monotonic": self.capture_monotonic,
            "xauthority": self.xauthority,
            "wine_prefix": self.wine_prefix,
            "preconstruction_fingerprint": self.preconstruction_fingerprint,
            "provenance": self.provenance,
            "is_live": self.is_live,
        }

    def with_hash(self) -> NativeCalibrationEvidence:
        digest = hashlib.sha256(
            json.dumps(self._canonical_payload(), sort_keys=True, separators=(",", ":"), default=list).encode(
                "utf-8"
            )
        ).hexdigest()
        return replace(self, evidence_hash=digest)

    def content_fingerprint(self) -> str:
        payload = self._canonical_payload()
        payload["fingerprint"] = ""
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), default=list).encode("utf-8")
        ).hexdigest()

    def resource_assessment(self) -> dict[str, Any]:
        """Derive the bounded whole-process resource result from sealed windows."""

        if not isinstance(self.memory_measurements, tuple) or len(self.memory_measurements) != 2:
            raise CalibrationError("native resource assessment has incomplete measurements")
        construction, updates = self.memory_measurements
        for measurement in (construction, updates):
            if not isinstance(measurement, NativeMemoryMeasurement):
                raise CalibrationError("native resource assessment has an invalid measurement")
            measurement.verify()
        if construction.method != updates.method:
            raise CalibrationError("native resource assessment methods differ")
        return {
            "classification": (
                "target_exceeded"
                if construction.growth_bytes > construction.limit_bytes
                or updates.growth_bytes > updates.limit_bytes
                else "within_targets"
            ),
            "method": construction.method,
            "evidenceHash": self.evidence_hash,
            "construction": {
                "growthBytes": construction.growth_bytes,
                "targetBytes": construction.limit_bytes,
                "withinTarget": construction.growth_bytes <= construction.limit_bytes,
            },
            "updates": {
                "growthBytes": updates.growth_bytes,
                "targetBytes": updates.limit_bytes,
                "withinTarget": updates.growth_bytes <= updates.limit_bytes,
            },
        }

    def verify_seal(self) -> None:
        if self._seal is not _SEAL or self.provenance != "owned-live" or not self.is_live:
            raise CalibrationError("native evidence was not issued by the owned live workflow")
        if (
            not isinstance(self.fingerprint, str)
            or not re.fullmatch(r"[0-9a-f]{64}", self.fingerprint)
            or isinstance(self.process_pid, bool)
            or not isinstance(self.process_pid, int)
            or self.process_pid <= 1
            or not self.process_executable
            or not self.process_start
            or isinstance(self.window_xid, bool)
            or not isinstance(self.window_xid, int)
            or self.window_xid <= 0
            or isinstance(self.child_xid, bool)
            or not isinstance(self.child_xid, int)
            or self.child_xid <= 0
            or not isinstance(self.geometry, (tuple, list))
            or len(self.geometry) != 4
            or any(isinstance(value, bool) or not isinstance(value, int) for value in self.geometry)
            or self.geometry[2] <= 0
            or self.geometry[3] <= 0
            or not self.xinput_hierarchy
            or not self.xauthority
            or not self.wine_prefix
        ):
            raise CalibrationError("native evidence identity is incomplete")
        if (
            self.crop != (80, 48, 256, 256)
            or not self.boot
            or isinstance(self.sequence, bool)
            or not isinstance(self.sequence, int)
            or self.sequence <= 0
            or isinstance(self.crc32, bool)
            or not isinstance(self.crc32, int)
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for value in (
                    self.construction_update_count,
                    self.held_update_count,
                    self.resume_update_count,
                )
            )
            or not isinstance(self.memory_measurements, tuple)
            or len(self.memory_measurements) != 2
            or not self.native_identity_fingerprint
            or not self.preconstruction_fingerprint
            or not isinstance(self.capture_path, str)
            or not self.capture_path
            or not isinstance(self.capture_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", self.capture_sha256)
            or isinstance(self.capture_monotonic, bool)
            or not isinstance(self.capture_monotonic, (int, float))
            or not math.isfinite(float(self.capture_monotonic))
            or self.capture_monotonic < 0
        ):
            raise CalibrationError("native evidence render identity or boot proof is incomplete")
        # ``process_executable`` identifies the window/client (and therefore
        # remains the Windows ``WowB.exe`` identity).  Reset5 measurements
        # carry the separately pinned Linux ``/proc/PID/exe`` identity.  Do
        # not compare these two OS channels: bind every memory measurement to
        # one canonical executable from that measurement channel instead.
        first_memory_measurement = self.memory_measurements[0]
        if not isinstance(first_memory_measurement, NativeMemoryMeasurement):
            raise CalibrationError("native evidence memory archive is incomplete")
        memory_process_executable = first_memory_measurement.process_executable
        if not isinstance(memory_process_executable, str) or not memory_process_executable:
            raise CalibrationError("native evidence memory executable is incomplete")
        for measurement in self.memory_measurements:
            if not isinstance(measurement, NativeMemoryMeasurement):
                raise CalibrationError("native evidence memory archive is incomplete")
            measurement.verify()
            if (
                measurement.identity_fingerprint != self.native_identity_fingerprint
                or measurement.pid != self.process_pid
                or measurement.process_start != self.process_start
                or measurement.process_executable != memory_process_executable
            ):
                raise CalibrationError("native evidence memory identity is not bound to the client")
        construction, updates = self.memory_measurements
        if (
            construction.phase != "construction"
            or construction.ordinal != 0
            or construction.workload_start_ordinal != 0
            or construction.workload_end_ordinal != 0
            or updates.phase != "updates"
            or updates.ordinal != 0
            or updates.workload_start_ordinal != 1
            or updates.workload_end_ordinal != 100
        ):
            raise CalibrationError("native evidence memory phases are not ordered")
        if construction.limit_bytes != NATIVE_CONSTRUCTION_LIMIT_BYTES:
            raise CalibrationError("native evidence construction memory limit is invalid")
        if updates.limit_bytes != NATIVE_UPDATES_LIMIT_BYTES:
            raise CalibrationError("native evidence update memory limit is invalid")
        NativeCalibrationRunner._validate_metrics(self.render, minimum_updates=100)
        if (
            isinstance(self.resume_sequence, bool)
            or not isinstance(self.resume_sequence, int)
            or self.resume_sequence <= self.sequence
            or isinstance(self.resume_crc32, bool)
            or not isinstance(self.resume_crc32, int)
            or self.resume_crc32 < 0
            or not isinstance(self.resume_render, dict)
            or self.resume_render.get("nativePhase") != "normal"
            or self.resume_render.get("nativeOrdinal") != 0
            or isinstance(self.resume_update_count, bool)
            or not isinstance(self.resume_update_count, int)
            or self.resume_update_count < 0
            or self.resume_render.get("updateCount") != self.resume_update_count
            or self.construction_update_count >= self.held_update_count
            or self.render.get("updateCount") != self.held_update_count + 100
            or self.resume_update_count != self.render.get("updateCount", -1) + 1
            or self.resume_sequence != self.sequence + 1
            or isinstance(self.resume_capture_monotonic, bool)
            or not isinstance(self.resume_capture_monotonic, (int, float))
            or not math.isfinite(float(self.resume_capture_monotonic))
            or self.resume_capture_monotonic <= self.capture_monotonic
            or self.render.get("nativePhase") != "held"
            or self.render.get("nativeOrdinal") != 100
        ):
            raise CalibrationError("native resume frame is not a fresh normal/0 observation")
        NativeCalibrationRunner._validate_metrics(self.resume_render, minimum_updates=0)
        if float(self.resume_render["updateP99Ms"]) > 250.0:
            raise CalibrationError("native completed update p99 exceeds 250 ms")
        if (
            self.render.get("updateCount") != self.held_update_count + 100
            or float(self.render["constructionSeconds"]) > 5.0
        ):
            raise CalibrationError("native evidence final render count or time budget is invalid")
        capture = Path(self.capture_path)
        if (
            not capture.is_absolute()
            or not capture.is_file()
            or hashlib.sha256(capture.read_bytes()).hexdigest() != self.capture_sha256
        ):
            raise CalibrationError("native evidence capture provenance changed")
        if self.fingerprint != self.content_fingerprint():
            raise CalibrationError("native evidence fingerprint does not cover render facts")
        if not isinstance(self.evidence_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", self.evidence_hash):
            raise CalibrationError("native evidence has no canonical seal")
        expected = self.with_hash().evidence_hash
        if self.evidence_hash != expected:
            raise CalibrationError("native evidence canonical seal does not match its facts")


@dataclass(frozen=True)
class NativeRenderObservation:
    """One identity-bound capture returned by the live addon measurement hook."""

    metrics: dict[str, Any]
    identity_fingerprint: str
    boot: str | None = None
    sequence: int | None = None
    crc32: int | None = None
    capture_id: str | None = None
    capture_path: str | None = None
    capture_sha256: str | None = None
    capture_monotonic: float | None = None
    native_phase: str | None = None
    native_ordinal: int | None = None


@dataclass(frozen=True)
class NativePreconstructionEvidence:
    """External bootstrap handshake proving an unconstructed Agent surface."""

    identity_fingerprint: str
    process_start: str
    absent: bool
    baseline_id: str
    observed_monotonic: float
    bootstrap_release_monotonic: float = -1.0
    display: str = ""
    process_executable: str = ""
    window_xid: int = 0
    child_xid: int = 0
    geometry: tuple[int, int, int, int] = ()
    xauthority: str = ""
    wine_prefix: str = ""
    baseline_hwm_bytes: int = -1
    capture_path: str = ""
    capture_sha256: str = ""
    provenance: str = "owned-live"
    fingerprint: str = ""
    evidence_hash: str = ""
    _seal: object | None = None

    @classmethod
    def _issue(cls, **kwargs: Any) -> NativePreconstructionEvidence:
        issued = cls(**kwargs, _seal=_SEAL)
        return replace(issued, fingerprint=issued.content_fingerprint()).with_hash()

    def _canonical_payload(self) -> dict[str, Any]:
        return {
            "identity_fingerprint": self.identity_fingerprint,
            "process_start": self.process_start,
            "absent": self.absent,
            "baseline_id": self.baseline_id,
            "observed_monotonic": self.observed_monotonic,
            "bootstrap_release_monotonic": self.bootstrap_release_monotonic,
            "display": self.display,
            "process_executable": self.process_executable,
            "window_xid": self.window_xid,
            "child_xid": self.child_xid,
            "geometry": self.geometry,
            "xauthority": self.xauthority,
            "wine_prefix": self.wine_prefix,
            "baseline_hwm_bytes": self.baseline_hwm_bytes,
            "capture_path": self.capture_path,
            "capture_sha256": self.capture_sha256,
            "provenance": self.provenance,
        }

    def content_fingerprint(self) -> str:
        payload = self._canonical_payload()
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":"), default=list).encode("utf-8")
        ).hexdigest()

    def with_hash(self) -> NativePreconstructionEvidence:
        digest = hashlib.sha256(
            json.dumps(
                self._canonical_payload() | {"fingerprint": self.fingerprint},
                sort_keys=True,
                separators=(",", ":"),
                default=list,
            ).encode("utf-8")
        ).hexdigest()
        return replace(self, evidence_hash=digest)

    def verify_seal(self) -> None:
        if self._seal is not _SEAL or self.provenance != "owned-live" or not self.absent:
            raise CalibrationError("preconstruction evidence was not issued by the owned live workflow")
        if (
            not self.identity_fingerprint
            or not self.process_start
            or not self.process_executable
            or isinstance(self.window_xid, bool)
            or not isinstance(self.window_xid, int)
            or self.window_xid <= 0
            or isinstance(self.child_xid, bool)
            or not isinstance(self.child_xid, int)
            or self.child_xid <= 0
            or not isinstance(self.geometry, (tuple, list))
            or len(self.geometry) != 4
            or any(isinstance(value, bool) or not isinstance(value, int) for value in self.geometry)
            or self.geometry[2] <= 0
            or self.geometry[3] <= 0
            or not self.display
            or not self.xauthority
            or not self.wine_prefix
            or isinstance(self.baseline_hwm_bytes, bool)
            or not isinstance(self.baseline_hwm_bytes, int)
            or self.baseline_hwm_bytes < 0
            or not self.baseline_id
            or not isinstance(self.capture_path, str)
            or not self.capture_path
            or not isinstance(self.capture_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", self.capture_sha256)
            or isinstance(self.observed_monotonic, bool)
            or not isinstance(self.observed_monotonic, (int, float))
            or not math.isfinite(float(self.observed_monotonic))
            or self.observed_monotonic < 0
            or isinstance(self.bootstrap_release_monotonic, bool)
            or not isinstance(self.bootstrap_release_monotonic, (int, float))
            or not math.isfinite(float(self.bootstrap_release_monotonic))
            or self.bootstrap_release_monotonic < self.observed_monotonic
        ):
            raise CalibrationError("preconstruction evidence is incomplete")
        capture = Path(self.capture_path)
        if (
            not capture.is_absolute()
            or not capture.is_file()
            or hashlib.sha256(capture.read_bytes()).hexdigest() != self.capture_sha256
        ):
            raise CalibrationError("preconstruction capture provenance changed")
        if self.baseline_id != self.capture_sha256:
            raise CalibrationError("preconstruction baseline is not bound to its capture")
        if self.fingerprint != self.content_fingerprint():
            raise CalibrationError("preconstruction fingerprint does not cover its facts")
        if not re.fullmatch(r"[0-9a-f]{64}", self.evidence_hash) or self.evidence_hash != (
            self.with_hash().evidence_hash
        ):
            raise CalibrationError("preconstruction evidence canonical seal does not match its facts")


@dataclass(frozen=True)
class OwnedWindow:
    pid: int
    xid: int
    title: str
    geometry: tuple[int, int, int, int]
    process: subprocess.Popen[str]
    descendant_xids: frozenset[int] = frozenset()
    executable: str = ""
    process_start: str = ""
    managed_frame_xid: int | None = None
    managed_frame_geometry: tuple[int, int, int, int] = ()
    certified_event_target_xids: frozenset[int] = frozenset()
    layout_kind: str = GTK_LAYOUT_MUTTER_FRAME
    root_xid: int = 0
    parent_xid: int = 0
    excluded_input_only_xids: frozenset[int] = frozenset()

    @property
    def child_xid(self) -> int:
        return self.xid

    @property
    def allowed_event_target_xids(self) -> frozenset[int]:
        """Freshly certified X11 targets accepted by delivered XI2 events.

        ``descendant_xids`` remains the actual GTK client subtree.  A
        reparenting window manager may deliver the event child as its managed
        frame, so that one frame is admitted only when ``_verify`` established
        its structural relationship to this client.
        """

        return self.certified_event_target_xids

    @property
    def event_target_xids(self) -> frozenset[int]:
        """Alias used by dispatchers when admitting delivered event targets."""

        return self.allowed_event_target_xids

    def contains_xid(self, xid: int | None) -> bool:
        return xid is not None and xid in self.allowed_event_target_xids


@dataclass(frozen=True)
class CalibrationReadiness:
    """Owned target/observer proof exposed while awaiting human input."""

    target_pid: int
    target_xid: int
    geometry: tuple[int, int, int, int]
    synthetic_key_source: int
    synthetic_pointer_source: int
    xinput_hierarchy: tuple[tuple[int, int, str, str], ...]
    observed_monotonic: float
    synthetic_modifiers: tuple[int, int, int] = ()
    synthetic_echo_proof: tuple[tuple[Any, ...], ...] = ()
    keyboard_keymap: tuple[tuple[str, int], ...] = ()
    keyboard_profile: tuple[KeyboardProfileRow, ...] = ()


class CommandRunner(Protocol):
    def __call__(self, args: list[str], *, display: str, xauthority: str, timeout: float = 5.0) -> str: ...


def _run_command(args: list[str], *, display: str, xauthority: str, timeout: float = 5.0) -> str:
    env = os.environ.copy()
    env["DISPLAY"] = display
    if not xauthority:
        raise CalibrationError("Xauthority path must be explicitly configured")
    env["XAUTHORITY"] = xauthority
    try:
        result = subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        raise CalibrationError(f"calibration command failed: {' '.join(args)}: {exc}") from exc
    return result.stdout


def _parse_xwininfo_geometry(text: str) -> tuple[int, int, int, int]:
    """Read an absolute x,y,width,height rectangle from ``xwininfo`` output."""

    labels = ("Absolute upper-left X", "Absolute upper-left Y", "Width", "Height")
    field_lines: list[tuple[str, str]] = []
    for line in text.splitlines():
        stripped = line.lstrip()
        for label in labels:
            if stripped.startswith(f"{label}:"):
                field_lines.append((label, line))
                break
    if len(field_lines) != len(labels) or {label for label, _line in field_lines} != set(labels):
        raise CalibrationError("X11 window geometry stats are missing or duplicated")

    values: dict[str, int] = {}
    for label, line in field_lines:
        match = re.fullmatch(rf"[ \t]*{re.escape(label)}:[ \t]*(-?\d+)[ \t]*", line)
        if match is None:
            raise CalibrationError("X11 window geometry stats contain malformed fields")
        values[label] = int(match.group(1))
    if values["Width"] <= 0 or values["Height"] <= 0:
        raise CalibrationError("X11 window geometry stats contain non-positive dimensions")
    return (
        values["Absolute upper-left X"],
        values["Absolute upper-left Y"],
        values["Width"],
        values["Height"],
    )


def _parse_xwininfo_root_stats(text: str) -> tuple[int, tuple[int, int, int, int]]:
    match = re.search(r"Window id:\s*(0x[0-9a-fA-F]+)\s+\(the root window\)", text)
    if match is None:
        raise CalibrationError("X11 root stats do not identify the root window")
    return int(match.group(1), 16), _parse_xwininfo_geometry(text)


def _rect_contains(outer: tuple[int, int, int, int], inner: tuple[int, int, int, int]) -> bool:
    ox, oy, ow, oh = outer
    ix, iy, iw, ih = inner
    return ox <= ix and oy <= iy and ox + ow >= ix + iw and oy + oh >= iy + ih


def _proc_start_time(pid: int) -> str:
    """Read Linux starttime without splitting a process name incorrectly."""

    stat = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
    closing = stat.rfind(")")
    if closing < 0:
        raise CalibrationError("owned GTK process start time is malformed")
    fields = stat[closing + 2 :].split()
    # The suffix starts at field 3 (state); starttime is field 22.
    if len(fields) <= 19:
        raise CalibrationError("owned GTK process start time is missing")
    return fields[19]


def _process_identity(pid: int) -> tuple[str, str, str]:
    """Return the live executable, start time, and comm for one X11 owner."""

    try:
        executable = os.readlink(f"/proc/{pid}/exe")
        process_start = _proc_start_time(pid)
        comm = Path(f"/proc/{pid}/comm").read_text(encoding="ascii").strip()
    except (OSError, CalibrationError, UnicodeError) as exc:
        raise CalibrationError("owned X11 process identity is unavailable") from exc
    if not executable or not process_start or not comm:
        raise CalibrationError("owned X11 process identity is incomplete")
    return executable, process_start, comm


def _xprop_property_lines(text: str, property_name: str) -> list[str]:
    prefix = re.compile(rf"^[ \t]*{re.escape(property_name)}\(")
    return [line for line in text.splitlines() if prefix.match(line)]


def _xprop_single_window_id(text: str, property_name: str) -> int:
    lines = _xprop_property_lines(text, property_name)
    matches = [
        match.group(1)
        for line in lines
        if (
            match := re.fullmatch(
                rf"[ \t]*{re.escape(property_name)}\(WINDOW\):[ \t]*window id #[ \t]*(0x[0-9a-fA-F]+)[ \t]*",
                line,
            )
        )
    ]
    if len(lines) != 1 or len(matches) != 1:
        raise CalibrationError(f"X11 property {property_name} is missing or duplicated")
    return int(matches[0], 16)


def _xprop_optional_window_id(text: str, property_name: str) -> int | None:
    """Read one published WINDOW property, distinguishing absence from damage."""

    mentions = [
        line
        for line in text.splitlines()
        if re.match(rf"^[ \t]*{re.escape(property_name)}(?=$|[ \t(:=])", line)
    ]
    if not mentions:
        return None
    if len(mentions) != 1:
        raise CalibrationError(f"X11 property {property_name} is malformed or duplicated")
    if re.fullmatch(rf"[ \t]*{re.escape(property_name)}:[ \t]*not found\.[ \t]*", mentions[0]):
        return None
    xid = _xprop_single_window_id(text, property_name)
    if xid <= 0:
        raise CalibrationError(f"X11 property {property_name} has an invalid window ID")
    return xid


def _xprop_single_cardinal(text: str, property_name: str) -> int:
    lines = _xprop_property_lines(text, property_name)
    matches = [
        match.group(1)
        for line in lines
        if (
            match := re.fullmatch(
                rf"[ \t]*{re.escape(property_name)}\(CARDINAL\)[ \t]*=[ \t]*(\d+)[ \t]*",
                line,
            )
        )
    ]
    if len(lines) != 1 or len(matches) != 1:
        raise CalibrationError(f"X11 property {property_name} is missing or duplicated")
    return int(matches[0])


def _xprop_single_class(text: str) -> tuple[str, str]:
    lines = _xprop_property_lines(text, "WM_CLASS")
    matches = [
        match.groups()
        for line in lines
        if (
            match := re.fullmatch(
                r'[ \t]*WM_CLASS\(STRING\)[ \t]*=[ \t]*"([^"]*)",[ \t]*"([^"]*)"[ \t]*',
                line,
            )
        )
    ]
    if len(lines) != 1 or len(matches) != 1:
        raise CalibrationError("X11 WM_CLASS property is missing or duplicated")
    return matches[0]


def _xprop_single_name(text: str) -> str:
    lines = _xprop_property_lines(text, "WM_NAME")
    matches = [
        match.group(1)
        for line in lines
        if (match := re.fullmatch(r'[ \t]*WM_NAME\(STRING\)[ \t]*=[ \t]*"([^"]*)"[ \t]*', line))
    ]
    if len(lines) != 1 or len(matches) != 1:
        raise CalibrationError("X11 WM_NAME property is missing or duplicated")
    return matches[0]


def _xwininfo_single_stat(text: str, label: str) -> int:
    matches = re.findall(rf"^[ \t]*{re.escape(label)}:[ \t]*(-?\d+)[ \t]*$", text, re.MULTILINE)
    if len(matches) != 1:
        raise CalibrationError(f"xwininfo {label} stat is missing or duplicated")
    return int(matches[0])


def _xwininfo_single_class(text: str) -> str:
    matches = re.findall(r"^[ \t]*Class:[ \t]*([^\s]+)[ \t]*$", text, re.MULTILINE)
    if len(matches) != 1:
        raise CalibrationError("xwininfo Class stat is missing or duplicated")
    return matches[0]


class _GtkStartupPending(CalibrationError):
    """The GTK target has published identity but Mutter is not ready yet."""


def _xwininfo_single_map_state(text: str) -> str:
    lines = [line for line in text.splitlines() if re.match(r"^[ \t]*Map State:", line)]
    matches = [
        match.group(1)
        for line in lines
        if (match := re.fullmatch(r"[ \t]*Map State:[ \t]*(IsUnMapped|IsUnviewable|IsViewable)[ \t]*", line))
    ]
    if not lines:
        raise _GtkStartupPending("GTK client map state is not published yet")
    if len(lines) != 1 or len(matches) != 1:
        raise CalibrationError("GTK client map state is malformed or duplicated")
    return matches[0]


def _xprop_single_frame_extents(text: str) -> tuple[int, int, int, int]:
    lines = _xprop_property_lines(text, "_NET_FRAME_EXTENTS")
    matches = [
        match.groups()
        for line in lines
        if (
            match := re.fullmatch(
                r"[ \t]*_NET_FRAME_EXTENTS\(CARDINAL\)[ \t]*=[ \t]*"
                r"(\d+)[ \t]*,[ \t]*(\d+)[ \t]*,[ \t]*(\d+)[ \t]*,[ \t]*(\d+)[ \t]*",
                line,
            )
        )
    ]
    if not lines:
        raise _GtkStartupPending("GTK client frame extents are not published yet")
    if len(lines) != 1 or len(matches) != 1:
        raise CalibrationError("GTK client frame extents are malformed or duplicated")
    return tuple(int(value) for value in matches[0])  # type: ignore[return-value]


def _xprop_single_undecorated_motif_hints(text: str) -> tuple[int, int, int, int, int]:
    lines = _xprop_property_lines(text, "_MOTIF_WM_HINTS")
    pattern = re.compile(
        r"^[ \t]*_MOTIF_WM_HINTS\([^)]*\)[ \t]*=[ \t]*"
        r"(0x[0-9a-fA-F]+|\d+)[ \t]*,[ \t]*"
        r"(0x[0-9a-fA-F]+|\d+)[ \t]*,[ \t]*"
        r"(0x[0-9a-fA-F]+|\d+)[ \t]*,[ \t]*"
        r"(0x[0-9a-fA-F]+|\d+)[ \t]*,[ \t]*"
        r"(0x[0-9a-fA-F]+|\d+)[ \t]*$"
    )
    matches = [match.groups() for line in lines if (match := pattern.fullmatch(line))]
    if len(lines) != 1 or len(matches) != 1:
        raise CalibrationError("GTK undecorated Motif hints are missing or malformed")
    try:
        values = tuple(int(value, 0) for value in matches[0])
    except ValueError as exc:
        raise CalibrationError("GTK undecorated Motif hints are malformed") from exc
    if values != (0x2, 0, 0, 0, 0):
        raise CalibrationError("GTK client decoration hints changed from the observed undecorated layout")
    return values  # type: ignore[return-value]


def _xwininfo_tree_root_parent(text: str, expected_xid: int) -> tuple[int, int]:
    headers = re.findall(r"^xwininfo:\s+Window id:\s+(0x[0-9a-fA-F]+)\b", text, re.MULTILINE)
    roots = re.findall(r"^[ \t]*Root window id:[ \t]*(0x[0-9a-fA-F]+)(?:[ \t].*)?$", text, re.MULTILINE)
    parents = re.findall(r"^[ \t]*Parent window id:[ \t]*(0x[0-9a-fA-F]+)(?:[ \t].*)?$", text, re.MULTILINE)
    if len(headers) != 1 or int(headers[0], 16) != expected_xid or len(roots) != 1 or len(parents) != 1:
        raise CalibrationError("owned GTK client tree has incomplete root/parent identity")
    return int(roots[0], 16), int(parents[0], 16)


class OwnedGtkCalibrationWindow:
    """Create and verify the ephemeral GTK/X11 target used by input calibration."""

    TITLE = "CROWLEY INPUT CALIBRATION ONLY"
    LAYOUT_MUTTER_FRAME = GTK_LAYOUT_MUTTER_FRAME
    LAYOUT_ROOT_CHILD = GTK_LAYOUT_ROOT_CHILD

    def __init__(
        self,
        *,
        display: str = ":0",
        xauthority: str = "",
        python_executable: str = "/usr/bin/python3",
        command: CommandRunner = _run_command,
        size: tuple[int, int] = (480, 240),
        layout_kind: str = GTK_LAYOUT_MUTTER_FRAME,
    ) -> None:
        if layout_kind not in {GTK_LAYOUT_MUTTER_FRAME, GTK_LAYOUT_ROOT_CHILD}:
            raise CalibrationError("GTK calibration layout kind is unsupported")
        self.display = display
        self.xauthority = xauthority
        self.python_executable = python_executable
        self.command = command
        self.size = size
        self.layout_kind = layout_kind
        self.window: OwnedWindow | None = None

    def create(
        self,
        *,
        timeout: float = 10.0,
        startup_admission: Callable[[int, Callable[[], None]], Callable[[], None] | None] | None = None,
    ) -> OwnedWindow:
        if self.window is not None:
            raise CalibrationError("owned calibration target already exists")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise CalibrationError("GTK startup timeout must be a finite positive number")
        timeout_value = float(timeout)
        if not math.isfinite(timeout_value) or timeout_value <= 0:
            raise CalibrationError("GTK startup timeout must be a finite positive number")
        # This is the sole create budget.  Admission, identity publication,
        # every readiness query and the final certificate check all consume
        # this same absolute deadline.
        deadline = time.monotonic() + timeout_value
        if not self.xauthority:
            raise CalibrationError("Xauthority path must be explicitly configured")
        if not Path(self.python_executable).is_file():
            raise CalibrationError("configured GTK Python executable does not exist")
        env = os.environ.copy()
        env["DISPLAY"] = self.display
        if self.xauthority:
            env["XAUTHORITY"] = self.xauthority
        env["GDK_BACKEND"] = "x11"
        gate_read, gate_write = os.pipe()
        admission_gate = AdmissionGate(gate_write)
        process: subprocess.Popen[str] | None = None
        registration_cleanup: Callable[[], None] | None = None

        def cleanup_registration() -> None:
            nonlocal registration_cleanup
            cleanup = registration_cleanup
            registration_cleanup = None
            if cleanup is not None:
                cleanup()

        def reap_process() -> None:
            if process is None:
                return
            if process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass
                process.wait(timeout=2)
            if process.poll() is None:
                raise CalibrationError("GTK calibration target process was not reaped")

        try:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-I",
                    "-S",
                    "-c",
                    ADMISSION_WRAPPER,
                    str(gate_read),
                    self.python_executable,
                    "-c",
                    _GTK_SCRIPT,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
                start_new_session=True,
                pass_fds=(gate_read,),
            )
            os.close(gate_read)
            gate_read = -1
            if callable(startup_admission):
                cleanup = startup_admission(process.pid, admission_gate.release)
                if cleanup is not None and not callable(cleanup):
                    raise CalibrationError("GTK helper startup cleanup capability is malformed")
                registration_cleanup = cleanup
                gate_write = -1
                if time.monotonic() >= deadline:
                    raise CalibrationError("owned GTK target did not become ready before startup deadline")
            else:
                raise CalibrationError("GTK helper startup requires the guardian admission fence")
        except Exception as exc:
            if gate_read >= 0:
                try:
                    os.close(gate_read)
                except OSError:
                    pass
                gate_read = -1
            if gate_write >= 0:
                # AdmissionGate is the sole owner of this descriptor after
                # construction; close it exactly once on every failure path.
                admission_gate.close()
                gate_write = -1
            reap_error: Exception | None = None
            if process is not None:
                try:
                    reap_process()
                except Exception as reap_exc:
                    reap_error = reap_exc
            cleanup_error: Exception | None = None
            if process is None or process.poll() is not None:
                try:
                    cleanup_registration()
                except Exception as cleanup_exc:
                    cleanup_error = cleanup_exc
            if reap_error is not None:
                if cleanup_error is not None:
                    reap_error.add_note(f"GTK target cleanup also failed: {cleanup_error}")
                raise CalibrationError("cannot reap GTK target process") from reap_error
            if cleanup_error is not None:
                raise CalibrationError("cannot clean up GTK target startup admission") from cleanup_error
            raise CalibrationError(f"cannot create owned GTK calibration target: {exc}") from exc

        def abort() -> None:
            nonlocal gate_write
            if gate_write >= 0:
                try:
                    admission_gate.close()
                except OSError:
                    pass
                gate_write = -1
            reap_error: Exception | None = None
            try:
                reap_process()
            except Exception as reap_exc:
                reap_error = reap_exc
            cleanup_error: Exception | None = None
            if process.poll() is not None:
                try:
                    cleanup_registration()
                except Exception as cleanup_exc:
                    cleanup_error = cleanup_exc
            if reap_error is not None:
                if cleanup_error is not None:
                    reap_error.add_note(f"GTK target cleanup also failed: {cleanup_error}")
                raise reap_error
            if cleanup_error is not None:
                raise cleanup_error

        if process.stdout is None:
            abort()
            raise CalibrationError("GTK calibration target has no identity channel")
        identity: dict[str, Any] | None = None
        identity_buffer = b""
        try:
            identity_fd = process.stdout.fileno()
        except (AttributeError, OSError) as exc:
            abort()
            raise CalibrationError("GTK calibration target has no usable identity channel") from exc
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([identity_fd], [], [], remaining)
            if not ready:
                break
            try:
                chunk = os.read(identity_fd, 4096)
            except OSError as exc:
                abort()
                raise CalibrationError("GTK calibration identity channel failed") from exc
            if chunk:
                identity_buffer += chunk
            if b"\n" in identity_buffer:
                line, _separator, _remainder = identity_buffer.partition(b"\n")
                try:
                    identity = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    abort()
                    raise CalibrationError("GTK calibration identity was not JSON") from exc
                break
            if not chunk:
                if process.poll() is None:
                    abort()
                    raise CalibrationError("GTK calibration identity channel closed before identity")
                break
        if not identity:
            abort()
            if identity_buffer:
                raise CalibrationError("GTK calibration identity was incomplete")
            raise CalibrationError("GTK calibration target did not publish identity")
        try:
            pid = int(identity["pid"])
            xid = int(identity["xid"])
        except (KeyError, TypeError, ValueError) as exc:
            abort()
            raise CalibrationError("GTK calibration identity is incomplete") from exc
        if pid != process.pid or xid <= 0:
            abort()
            raise CalibrationError("GTK calibration identity is not owned by its process")
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise CalibrationError("owned GTK target did not become ready before startup deadline")
                try:
                    window = self._verify(
                        pid,
                        xid,
                        process,
                        deadline=deadline,
                        startup_readiness=True,
                    )
                except _GtkStartupPending:
                    if process.poll() is not None:
                        raise CalibrationError("owned GTK target exited during startup readiness") from None
                    time.sleep(min(0.01, remaining))
                    continue
                if time.monotonic() >= deadline:
                    raise CalibrationError("owned GTK target did not become ready before startup deadline")
                break
        except Exception as exc:
            reap_error: Exception | None = None
            try:
                reap_process()
            except Exception as reap_exc:
                reap_error = reap_exc
            cleanup_error: Exception | None = None
            if process.poll() is not None:
                try:
                    cleanup_registration()
                except Exception as cleanup_exc:
                    cleanup_error = cleanup_exc
            if reap_error is not None:
                if cleanup_error is not None:
                    reap_error.add_note(f"GTK target cleanup also failed: {cleanup_error}")
                raise CalibrationError("cannot reap GTK target process") from reap_error
            if cleanup_error is not None:
                raise CalibrationError("cannot clean up GTK target startup admission") from cleanup_error
            raise exc
        self.window = window
        return window

    def show_human_prompt(self) -> None:
        window = self.window
        if window is None or window.process.stdin is None:
            raise CalibrationError("owned GTK target has no prompt channel")
        poll = getattr(window.process, "poll", None)
        if callable(poll) and poll() is not None:
            raise CalibrationError("owned GTK target exited before the human prompt")
        try:
            window.process.stdin.write("READY\n")
            window.process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise CalibrationError("owned GTK target prompt channel failed") from exc
        if callable(poll) and poll() is not None:
            raise CalibrationError("owned GTK target exited during the human prompt")
        if window.process.stdout is None:
            raise CalibrationError("owned GTK target has no prompt acknowledgement channel")
        deadline = time.monotonic() + 2.0
        acknowledged = False
        while time.monotonic() < deadline:
            ready, _, _ = select.select(
                [window.process.stdout], [], [], max(0.0, deadline - time.monotonic())
            )
            if not ready:
                break
            line = window.process.stdout.readline()
            if not line:
                break
            if line.strip() == "PROMPT_ACK":
                acknowledged = True
                break
        if not acknowledged:
            raise CalibrationError("owned GTK target did not acknowledge the human prompt")
        # The label update and the ACK are both child-side state.  Recertify
        # the client/tree after that transition so readiness cannot rely on a
        # stale pre-prompt frame certificate.
        refreshed = self._verify(
            window.pid,
            window.xid,
            window.process,
            timeout=2.0,
        )
        if (
            refreshed.pid != window.pid
            or refreshed.xid != window.xid
            or refreshed.title != window.title
            or refreshed.geometry != window.geometry
            or refreshed.allowed_event_target_xids != window.allowed_event_target_xids
            or refreshed.descendant_xids != window.descendant_xids
            or refreshed.excluded_input_only_xids != window.excluded_input_only_xids
            or refreshed.layout_kind != window.layout_kind
            or refreshed.root_xid != window.root_xid
            or refreshed.parent_xid != window.parent_xid
            or refreshed.managed_frame_xid != window.managed_frame_xid
            or refreshed.managed_frame_geometry != window.managed_frame_geometry
            or refreshed.executable != window.executable
            or refreshed.process_start != window.process_start
        ):
            raise CalibrationError("owned GTK target certificate changed before readiness")
        self.window = refreshed

    def _verify(
        self,
        pid: int,
        xid: int,
        process: subprocess.Popen[str],
        *,
        timeout: float = 5.0,
        startup_readiness: bool = False,
        deadline: float | None = None,
    ) -> OwnedWindow:
        verify_deadline = time.monotonic() + timeout if deadline is None else deadline

        def pending_or_hard(message: str) -> None:
            if startup_readiness:
                raise _GtkStartupPending(message)
            raise CalibrationError(message)

        def run_command(argv: list[str]) -> str:
            remaining = verify_deadline - time.monotonic()
            if remaining <= 0:
                raise CalibrationError("GTK target verification exceeded its deadline")
            return self.command(
                argv,
                display=self.display,
                xauthority=self.xauthority,
                timeout=remaining,
            )

        listing = run_command(["wmctrl", "-lp"])
        client_matches: list[int] = []
        xid_rows: list[list[str]] = []
        for line in listing.splitlines():
            # wmctrl -lp is five columns: XID, desktop (commonly -1), PID,
            # client-machine, and the remainder of the title.
            fields = line.split(maxsplit=4)
            if not fields or not re.fullmatch(r"0x[0-9a-fA-F]+", fields[0]):
                continue
            row_xid = int(fields[0], 16)
            if len(fields) != 5:
                if row_xid == xid:
                    raise CalibrationError("owned GTK target title/PID row is malformed")
                continue
            if not re.fullmatch(r"-?\d+", fields[1]) or not re.fullmatch(r"\d+", fields[2]):
                if row_xid == xid:
                    raise CalibrationError("owned GTK target title/PID is not unique")
                continue
            row_pid = int(fields[2])
            row_title = fields[4].strip()
            if row_xid == xid:
                xid_rows.append(fields)
                if row_title != self.TITLE:
                    raise CalibrationError("owned GTK target title/PID is not unique")
                if row_pid != pid:
                    raise CalibrationError("owned GTK target title/PID does not match its process")
                client_matches.append(row_xid)
            elif row_pid == pid and row_title == self.TITLE:
                client_matches.append(row_xid)
        if client_matches != [xid]:
            if not xid_rows and not client_matches:
                pending_or_hard("owned GTK client is not in wmctrl yet")
            raise CalibrationError("owned GTK target title/PID is not unique")
        props = run_command(["xprop", "-id", hex(xid)])
        advertised_helper_xid: int | None = None
        if self.layout_kind == GTK_LAYOUT_ROOT_CHILD:
            advertised_helper_xid = _xprop_optional_window_id(props, "_NET_WM_USER_TIME_WINDOW")
            if advertised_helper_xid == xid:
                raise CalibrationError("GTK client advertises itself as its metadata helper")
        name_lines = _xprop_property_lines(props, "WM_NAME")
        pid_lines = _xprop_property_lines(props, "_NET_WM_PID")
        client_name: str | None = None
        client_pid: int | None = None
        if name_lines:
            client_name = _xprop_single_name(props)
        if pid_lines:
            client_pid = _xprop_single_cardinal(props, "_NET_WM_PID")
        if client_name is not None and client_name != self.TITLE:
            raise CalibrationError("owned GTK target XID/PID properties do not match")
        if client_pid is not None and client_pid != pid:
            raise CalibrationError("owned GTK target XID/PID properties do not match")
        if not name_lines or not pid_lines:
            pending_or_hard("owned GTK client properties are not published yet")
        assert client_name is not None and client_pid is not None
        if client_name != self.TITLE or client_pid != pid:
            raise CalibrationError("owned GTK target XID/PID properties do not match")
        try:
            executable, process_start, _ = _process_identity(pid)
        except CalibrationError as exc:
            raise CalibrationError("owned GTK process identity is unavailable") from exc
        geometry_text = run_command(["xwininfo", "-id", hex(xid), "-stats"])
        geometry = _parse_xwininfo_geometry(geometry_text)
        if geometry[2:] != self.size:
            raise CalibrationError("owned GTK target geometry changed")
        map_state: str | None = None
        map_pending: _GtkStartupPending | None = None
        try:
            map_state = _xwininfo_single_map_state(geometry_text)
        except _GtkStartupPending as exc:
            map_pending = exc
        if self.layout_kind == GTK_LAYOUT_ROOT_CHILD:
            if _xprop_property_lines(props, "_NET_FRAME_EXTENTS"):
                raise CalibrationError("GTK root-child client unexpectedly publishes frame extents")
            _xprop_single_undecorated_motif_hints(props)
            if _xwininfo_single_class(geometry_text) != "InputOutput":
                raise CalibrationError("GTK root-child target is not an InputOutput window")
            if advertised_helper_xid is None:
                pending_or_hard("GTK client metadata helper identity is not published yet")
            root_child_window = self._verify_root_child_layout(
                pid=pid,
                xid=xid,
                process=process,
                executable=executable,
                process_start=process_start,
                geometry=geometry,
                advertised_helper_xid=advertised_helper_xid,
                startup_readiness=startup_readiness,
                run_command=run_command,
            )
            if map_pending is not None:
                pending_or_hard(str(map_pending))
                raise AssertionError("unreachable")
            assert map_state is not None
            if map_state != "IsViewable":
                pending_or_hard(f"owned GTK client is {map_state}")
            return root_child_window
        else:
            extents_pending: _GtkStartupPending | None = None
            try:
                _xprop_single_frame_extents(props)
            except _GtkStartupPending as exc:
                extents_pending = exc
        if map_pending is not None:
            pending_or_hard(str(map_pending))
            raise AssertionError("unreachable")
        if extents_pending is not None:
            pending_or_hard(str(extents_pending))
            raise AssertionError("unreachable")
        assert map_state is not None
        if map_state != "IsViewable":
            pending_or_hard(f"owned GTK client is {map_state}")
        tree = run_command(["xwininfo", "-root", "-tree"])
        try:
            from .driver import _parse_xwininfo_tree

            root, parents, tree_geometries = _parse_xwininfo_tree(tree)
        except Exception as exc:
            raise CalibrationError("owned GTK target ancestry is not a structural X11 tree") from exc
        managed_frame_xid = parents.get(xid, 0)
        if root <= 0 or xid == root:
            raise CalibrationError("owned GTK target ancestry includes the X11 root")
        if managed_frame_xid == root or managed_frame_xid <= 0 or managed_frame_xid not in parents:
            pending_or_hard("owned GTK target is not reparented under its managed frame")
        if parents.get(managed_frame_xid) != root:
            raise CalibrationError("owned GTK target ancestry is not under its verified managed frame")
        frame_children = {child for child, parent in parents.items() if parent == managed_frame_xid}
        if len(frame_children - {xid}) > 1:
            raise CalibrationError("managed GTK frame children do not match its advertised helper")
        frame_props = run_command(["xprop", "-id", hex(managed_frame_xid)])
        frame_property_names = (
            "_MUTTER_FRAME_FOR",
            "_NET_WM_USER_TIME_WINDOW",
            "_NET_WM_PID",
            "WM_CLASS",
        )
        frame_property_lines = {
            name: _xprop_property_lines(frame_props, name) for name in frame_property_names
        }
        if frame_property_lines["_MUTTER_FRAME_FOR"]:
            frame_for = _xprop_single_window_id(frame_props, "_MUTTER_FRAME_FOR")
        else:
            frame_for = 0
        if frame_property_lines["_NET_WM_USER_TIME_WINDOW"]:
            helper_xid = _xprop_single_window_id(frame_props, "_NET_WM_USER_TIME_WINDOW")
        else:
            helper_xid = 0
        if helper_xid > 0:
            published_unexpected_children = frame_children - {xid, helper_xid}
            if published_unexpected_children:
                raise CalibrationError("managed GTK frame children do not match its advertised helper")
        if frame_property_lines["_NET_WM_PID"]:
            frame_pid = _xprop_single_cardinal(frame_props, "_NET_WM_PID")
        else:
            frame_pid = 0
        if frame_property_lines["WM_CLASS"]:
            frame_class = _xprop_single_class(frame_props)
        else:
            frame_class = ("", "")
        if frame_property_lines["_MUTTER_FRAME_FOR"] and frame_for != xid:
            raise CalibrationError("managed GTK frame identity or advertised helper is invalid")
        if frame_property_lines["_NET_WM_USER_TIME_WINDOW"] and (
            helper_xid <= 0 or helper_xid in {root, xid, managed_frame_xid}
        ):
            raise CalibrationError("managed GTK frame identity or advertised helper is invalid")
        if frame_property_lines["_NET_WM_PID"] and frame_pid == pid:
            raise CalibrationError("managed GTK frame identity or advertised helper is invalid")
        if frame_property_lines["WM_CLASS"] and frame_class != (
            "mutter-x11-frames",
            "mutter-x11-frames",
        ):
            raise CalibrationError("managed GTK frame identity or advertised helper is invalid")
        frame_identity: tuple[str, str, str] | None = None
        if frame_property_lines["_NET_WM_PID"]:
            if frame_pid <= 0 or frame_pid == pid:
                raise CalibrationError("managed GTK frame identity or advertised helper is invalid")
            try:
                frame_identity = _process_identity(frame_pid)
            except CalibrationError as exc:
                raise CalibrationError("managed GTK frame process identity is unavailable") from exc
            if (
                frame_identity[0] != "/usr/libexec/mutter-x11-frames"
                or frame_identity[2] != "mutter-x11-fram"
            ):
                raise CalibrationError("managed GTK frame is not the observed Mutter frame process")
        if any(not lines for lines in frame_property_lines.values()):
            pending_or_hard("managed GTK frame properties are not complete yet")
        if (
            frame_for != xid
            or helper_xid <= 0
            or helper_xid in {root, xid, managed_frame_xid}
            or frame_pid <= 0
            or frame_class != ("mutter-x11-frames", "mutter-x11-frames")
        ):
            raise CalibrationError("managed GTK frame identity or advertised helper is invalid")
        assert frame_identity is not None
        frame_geometry_text = run_command(["xwininfo", "-id", hex(managed_frame_xid), "-stats"])
        frame_geometry = _parse_xwininfo_geometry(frame_geometry_text)
        if not _rect_contains(frame_geometry, geometry):
            raise CalibrationError("managed GTK frame does not enclose its client geometry")
        unexpected_children = frame_children - {xid, helper_xid}
        if unexpected_children:
            raise CalibrationError("managed GTK frame children do not match its advertised helper")
        if not {xid, helper_xid}.issubset(frame_children):
            pending_or_hard("managed GTK frame children are not ready")
        if frame_children != {xid, helper_xid}:
            raise CalibrationError("managed GTK frame children do not match its advertised helper")
        helper_stats_text = run_command(["xwininfo", "-id", hex(helper_xid), "-stats"])
        helper_geometry = _parse_xwininfo_geometry(helper_stats_text)
        if (
            helper_geometry[2:] != (1, 1)
            or _xwininfo_single_stat(helper_stats_text, "Relative upper-left X") != -1
            or _xwininfo_single_stat(helper_stats_text, "Relative upper-left Y") != -1
            or _xwininfo_single_stat(helper_stats_text, "Depth") != 0
            or _xwininfo_single_stat(helper_stats_text, "Border width") != 0
            or _xwininfo_single_class(helper_stats_text) != "InputOnly"
        ):
            raise CalibrationError("managed GTK helper geometry or class is invalid")
        helper_props = run_command(["xprop", "-id", hex(helper_xid)])
        try:
            user_time = _xprop_single_cardinal(helper_props, "_NET_WM_USER_TIME")
        except CalibrationError as exc:
            raise CalibrationError("managed GTK helper user-time property is incomplete") from exc
        if user_time <= 0:
            raise CalibrationError("managed GTK helper user-time property is invalid")
        descendants: set[int] = {xid}
        pending = [xid]
        while pending:
            ancestor = pending.pop()
            children = [child for child, parent in parents.items() if parent == ancestor]
            for child in children:
                if child not in descendants:
                    descendants.add(child)
                    pending.append(child)
        if xid in tree_geometries and tree_geometries[xid][2:] != geometry[2:]:
            raise CalibrationError("owned GTK client tree geometry changed")
        if managed_frame_xid in tree_geometries:
            tree_frame = tree_geometries[managed_frame_xid]
            if tree_frame[2] < geometry[2] or tree_frame[3] < geometry[3]:
                raise CalibrationError("managed GTK frame tree geometry is too small")
        certified_targets = frozenset({managed_frame_xid, *descendants})
        return OwnedWindow(
            pid,
            xid,
            self.TITLE,
            geometry,
            process,
            frozenset(descendants),
            executable,
            process_start,
            managed_frame_xid,
            frame_geometry,
            certified_targets,
            GTK_LAYOUT_MUTTER_FRAME,
            root,
            managed_frame_xid,
            frozenset({helper_xid}),
        )

    def _verify_root_child_layout(
        self,
        *,
        pid: int,
        xid: int,
        process: subprocess.Popen[str],
        executable: str,
        process_start: str,
        geometry: tuple[int, int, int, int],
        advertised_helper_xid: int,
        startup_readiness: bool,
        run_command: Callable[[list[str]], str],
    ) -> OwnedWindow:
        """Certify the observed undecorated client as a direct root child."""

        tree = run_command(["xwininfo", "-id", hex(xid), "-tree"])
        root_xid, parent_xid = _xwininfo_tree_root_parent(tree, xid)
        if root_xid <= 0 or xid == root_xid or parent_xid != root_xid:
            raise CalibrationError("GTK root-child target is not directly parented by the X11 root")
        if advertised_helper_xid in {root_xid, xid}:
            raise CalibrationError("GTK client advertises an invalid metadata helper identity")
        try:
            from .driver import _parse_xwininfo_tree

            tree_client, parents, tree_geometries = _parse_xwininfo_tree(tree)
        except Exception as exc:
            if startup_readiness and str(exc) == "X11 window tree contains no structural window rows":
                raise _GtkStartupPending("GTK metadata helper subtree is not published yet") from exc
            raise CalibrationError("GTK root-child client tree is not structurally valid") from exc
        if tree_client != xid or xid in parents or xid in tree_geometries:
            raise CalibrationError("GTK root-child client tree identity changed")
        descendants: set[int] = {xid}
        pending = [xid]
        while pending:
            ancestor = pending.pop()
            children = [child for child, parent in parents.items() if parent == ancestor]
            for child in children:
                if child == root_xid or child in descendants:
                    raise CalibrationError("GTK root-child client tree contains a cycle or root target")
                descendants.add(child)
                pending.append(child)
        if len(descendants) == 1:
            if startup_readiness:
                raise _GtkStartupPending("GTK metadata helper subtree is not published yet")
            raise CalibrationError("GTK root-child client is missing its advertised metadata helper")
        if descendants != {xid, advertised_helper_xid}:
            raise CalibrationError("GTK metadata helper subtree does not match its advertised identity")

        excluded: set[int] = set()
        event_targets: set[int] = {xid}
        helper_xids: list[int] = []
        for child_xid in sorted(descendants - {xid}):
            stats = run_command(["xwininfo", "-id", hex(child_xid), "-stats"])
            headers = re.findall(r"^xwininfo:\s+Window id:\s+(0x[0-9a-fA-F]+)\b", stats, re.MULTILINE)
            if len(headers) != 1 or int(headers[0], 16) != child_xid:
                raise CalibrationError("GTK root-child descendant geometry identity changed")
            child_class = _xwininfo_single_class(stats)
            if child_class == "InputOutput":
                raise CalibrationError("GTK root-child layout has an unexpected client descendant")
            if child_class != "InputOnly":
                raise CalibrationError("GTK root-child descendant class is unsupported")
            child_geometry = _parse_xwininfo_geometry(stats)
            parent_relative = (
                _xwininfo_single_stat(stats, "Relative upper-left X"),
                _xwininfo_single_stat(stats, "Relative upper-left Y"),
            )
            if (
                parents.get(child_xid) != xid
                or child_geometry[2:] != (1, 1)
                or parent_relative != (-1, -1)
                or _xwininfo_single_stat(stats, "Depth") != 0
                or _xwininfo_single_stat(stats, "Border width") != 0
                or tree_geometries.get(child_xid) != (-1, -1, 1, 1)
            ):
                raise CalibrationError("GTK InputOnly metadata helper layout is invalid")
            helper_xids.append(child_xid)
            excluded.add(child_xid)
        if (
            len(helper_xids) != 1
            or descendants != {xid, helper_xids[0]}
            or event_targets != {xid}
            or event_targets & excluded
            or root_xid in event_targets
        ):
            raise CalibrationError("GTK root-child metadata helper certificate is incomplete")
        return OwnedWindow(
            pid=pid,
            xid=xid,
            title=self.TITLE,
            geometry=geometry,
            process=process,
            descendant_xids=frozenset(descendants),
            executable=executable,
            process_start=process_start,
            managed_frame_xid=None,
            managed_frame_geometry=(),
            certified_event_target_xids=frozenset(event_targets),
            layout_kind=GTK_LAYOUT_ROOT_CHILD,
            root_xid=root_xid,
            parent_xid=parent_xid,
            excluded_input_only_xids=frozenset(excluded),
        )

    def close(self) -> None:
        window = self.window
        if window is None:
            return
        process = window.process
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired as exc:
                # Keep the owned handle/certificate for a retry and for the
                # guardian owner-death fallback; clearing it would hide a
                # still-live GTK process group.
                raise CalibrationError("owned GTK target termination could not be confirmed") from exc
        if process.poll() is None:
            raise CalibrationError("owned GTK target termination could not be confirmed")
        self.window = None


def xinput_hierarchy(text: str) -> tuple[tuple[int, int, str, str], ...]:
    """Parse the actual ``xinput --list --short`` hierarchy."""

    rows: list[tuple[int, int, str, str]] = []
    for line in text.splitlines():
        match = re.search(
            r"id=(\d+)\s+\[(master|slave|floating)\s+"
            r"(pointer|keyboard|.*?)(?:\s+\((\d+)\))?\]",
            line,
        )
        if not match:
            continue
        ident = int(match.group(1))
        role = match.group(2)
        kind = match.group(3).strip()
        parent = int(match.group(4) or 0)
        name = line.split("\tid=", 1)[0].strip(" │⎡⎣⎜↳∼")
        rows.append((ident, parent, role, f"{kind}:{name}"))
    if not rows:
        raise CalibrationError("xinput hierarchy contains no devices")
    return tuple(sorted(rows))


def xtest_sources(hierarchy: tuple[tuple[int, int, str, str], ...]) -> tuple[int, int]:
    """Resolve XTEST keyboard/pointer IDs from the observed hierarchy."""

    pointer = [row[0] for row in hierarchy if "xtest" in row[3].casefold() and "pointer" in row[3].casefold()]
    keyboard = [
        row[0] for row in hierarchy if "xtest" in row[3].casefold() and "keyboard" in row[3].casefold()
    ]
    if len(pointer) != 1 or len(keyboard) != 1:
        raise CalibrationError("XTEST keyboard/pointer devices are not uniquely present")
    return keyboard[0], pointer[0]


class InputCalibrationWorkflow:
    """Run the complete closed-by-default non-game input calibration."""

    def __init__(
        self,
        driver: Any,
        *,
        display: str | None = None,
        window_factory: Callable[[], OwnedGtkCalibrationWindow] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        on_ready: Callable[[CalibrationReadiness], None] | None = None,
    ) -> None:
        self.driver = driver
        self.display = display or driver.profile.display
        xauthority = getattr(driver, "xauthority", "") or getattr(driver.profile, "xauthority", "")
        self.window_factory = window_factory or (
            lambda: OwnedGtkCalibrationWindow(
                display=self.display,
                xauthority=xauthority,
                layout_kind=GTK_LAYOUT_ROOT_CHILD,
            )
        )
        self.monotonic = monotonic
        self.on_ready = on_ready
        self.ready_for_human = threading.Event()
        self.readiness: CalibrationReadiness | None = None
        # Failure diagnostics are assembled only for a pre-human exception,
        # after the observer/GTK cleanup in ``run``.  They are deliberately
        # private workflow state and never participate in qualification.
        self.failure_context: dict[str, Any] | None = None
        self._failure_error: BaseException | None = None
        self._stage = "setup"
        self._readiness_published = False
        self._target: Any | None = None
        self._target_certificate: dict[str, Any] | None = None
        self._observer: Any | None = None
        self._hierarchy: tuple[tuple[int, int, str, str], ...] = ()
        self._xtest_profile: Any | None = None
        self._keyboard_operations: tuple[KeyboardProbeOperation, ...] = ()
        self._keyboard_keymap: tuple[tuple[str, int], ...] = ()
        self._game_identity: Any | None = None
        self._predicate_reasons: dict[str, Any] = {"count": 0, "first": None, "last": None}
        self._target_placement_diagnostic: dict[str, Any] | None = None
        self._synthetic_pointer_probe: SyntheticPointerProbe | None = None

    @staticmethod
    def _bounded_error(value: Any, *, limit: int = 1024) -> str:
        text = " ".join(str(value).split())
        return text[:limit]

    @staticmethod
    def _safe_target_facts(target: Any) -> dict[str, Any] | None:
        window = getattr(target, "window", target)
        names = (
            "pid",
            "process_start",
            "xid",
            "frame_xid",
            "child_xid",
            "geometry",
            "executable",
            "layout_kind",
            "root_xid",
            "parent_xid",
        )
        result: dict[str, Any] = {}
        for name in names:
            value = getattr(window, name, None)
            if isinstance(value, tuple):
                value = list(value)
            if value is None or isinstance(value, (str, int, float, bool, list)):
                result[name] = value
        for name in (
            "title",
            "managed_frame_xid",
            "managed_frame_geometry",
            "descendant_xids",
            "certified_event_target_xids",
            "excluded_input_only_xids",
            "allowed_event_target_xids",
            "event_target_xids",
        ):
            value = getattr(window, name, None)
            if isinstance(value, (set, frozenset, tuple)):
                value = sorted(value) if name.endswith("xids") else list(value)
            if value is None or isinstance(value, (str, int, float, bool, list)):
                result[name] = value
        return result or None

    @staticmethod
    def _safe_identity_facts(identity: Any) -> dict[str, Any] | None:
        if identity is None:
            return None
        result: dict[str, Any] = {}
        for name in ("fingerprint", "executable", "process_start", "child_geometry", "pid"):
            value = getattr(identity, name, None)
            if isinstance(value, tuple):
                value = list(value)
            if value is None or isinstance(value, (str, int, float, bool, list)):
                result[name] = value
        return result or None

    def _build_failure_context(self, error: BaseException) -> dict[str, Any]:
        """Build bounded, private-only context without raising over ``error``."""

        observer_snapshot: dict[str, Any] | None = None
        observer = self._observer
        try:
            snapshot = getattr(observer, "diagnostic_snapshot", None)
            if callable(snapshot):
                value = snapshot()
                if isinstance(value, dict):
                    observer_snapshot = value
        except Exception:
            observer_snapshot = {"snapshotError": "observer diagnostic snapshot failed"}
        xtest = self._xtest_profile
        try:
            xtest_value = xtest.canonical_payload() if xtest is not None else None
            if isinstance(xtest_value, tuple):
                xtest_value = list(xtest_value)
        except Exception:
            xtest_value = None
        tool_digest = None
        try:
            backend = getattr(self.driver, "backend", None)
            digest = getattr(backend, "input_tool_digest", None)
            if callable(digest):
                digest = digest()
            if isinstance(digest, str) and digest:
                tool_digest = digest
        except Exception:
            tool_digest = None
        observer_complete = bool(
            observer_snapshot
            and observer_snapshot.get("rawBlocks")
            and not observer_snapshot.get("partialRaw")
            and not observer_snapshot.get("rawTruncated")
            and observer_snapshot.get("parserFailure") is None
            and observer_snapshot.get("failureDiagnostic") is None
            and observer_snapshot.get("observerAlive") is False
        )
        observer_truncated = bool(observer_snapshot and observer_snapshot.get("rawTruncated"))
        synthetic_pointer_diagnostic = None
        probe = self._synthetic_pointer_probe
        if probe is not None:
            synthetic_pointer_diagnostic = {
                "producer": "DesktopDriver.synthetic_target_keyboard_profile",
                "origin": {
                    "x": probe.origin[0],
                    "y": probe.origin[1],
                    "windowXid": probe.origin[2],
                },
                "requested": list(probe.requested_endpoint),
                "post": {
                    "x": probe.final_observed[0],
                    "y": probe.final_observed[1],
                    "windowXid": probe.final_observed[2],
                },
                "seed": probe.seed,
                "plan": [list(sample) for sample in probe.plan],
                "transitions": [list(point) for point in probe.emitted_transitions],
            }
        target_placement_diagnostic = None
        if isinstance(self._target_placement_diagnostic, dict):
            target_placement_diagnostic = dict(self._target_placement_diagnostic)
        complete_binding = bool(
            self._target_certificate
            and self._game_identity
            and self._hierarchy
            and self._keyboard_operations
            and self._keyboard_keymap
            and tool_digest
        )
        return {
            "schema": "input-calibration-failure-v1",
            "phase": "pre_human",
            "stage": self._stage,
            "errorType": type(error).__name__,
            "error": self._bounded_error(error),
            "readinessPublished": bool(self._readiness_published),
            "target": self._target_certificate,
            "gameIdentity": self._safe_identity_facts(self._game_identity),
            "input": {
                "hierarchy": [list(row) for row in self._hierarchy],
                "xtestProfile": xtest_value,
                "keyboardOperations": [
                    {
                        "operation": operation.operation,
                        "payload": operation.payload,
                        "steps": [[step.phase, step.keysym] for step in operation.steps],
                    }
                    for operation in self._keyboard_operations
                ],
                "keyboardKeymap": [list(item) for item in self._keyboard_keymap],
                "inputToolDigest": tool_digest,
            },
            "syntheticPredicate": dict(self._predicate_reasons),
            "syntheticPointer": synthetic_pointer_diagnostic,
            "targetPlacement": target_placement_diagnostic,
            "observer": observer_snapshot,
            "replayable": bool(observer_complete and complete_binding and not observer_truncated),
        }

    def failure_context_for(self, error: BaseException) -> dict[str, Any]:
        """Return a setup-failure context when ``run`` failed before its body."""

        if self.failure_context is None:
            self._failure_error = error
            self.failure_context = self._build_failure_context(error)
        return self.failure_context

    def _keyboard_probe_spec(self) -> tuple[tuple[KeyboardProbeOperation, ...], tuple[tuple[str, int], ...]]:
        """Read the driver's finite keyboard action list and live keymap.

        The actual driver owns the action list because it is the only place
        that knows whether a command is emitted as text, a named key, or a
        chord.  Calibration refuses to manufacture a compatibility profile
        when that provider is absent.
        """

        operations_provider = getattr(self.driver, "calibration_keyboard_operations", None)
        keymap_provider = getattr(self.driver, "calibration_keyboard_keymap", None)
        if not callable(operations_provider) or not callable(keymap_provider):
            raise CalibrationError("input calibration keyboard providers are not configured")
        try:
            raw_operations = tuple(operations_provider())
            raw_keymap = keymap_provider()
        except Exception as exc:
            raise CalibrationError("input calibration keyboard facts could not be measured") from exc
        if not isinstance(raw_keymap, dict) or not raw_keymap:
            raise CalibrationError("input calibration live keyboard keymap is empty")
        keymap: dict[str, int] = {}
        for keysym, detail in raw_keymap.items():
            if (
                not isinstance(keysym, str)
                or not keysym
                or isinstance(detail, bool)
                or not isinstance(detail, int)
                or detail <= 0
                or keysym in keymap
            ):
                raise CalibrationError("input calibration live keyboard keymap is malformed")
            keymap[keysym] = detail
        operations: list[KeyboardProbeOperation] = []
        for operation in raw_operations:
            if not isinstance(operation, KeyboardProbeOperation):
                raise CalibrationError("input calibration keyboard action list is not owned")
            if (
                not operation.operation
                or not isinstance(operation.payload, str)
                or not operation.steps
                or any(
                    not isinstance(step, KeyboardProbeStep)
                    or step.phase not in {"press", "release"}
                    or not step.keysym
                    for step in operation.steps
                )
            ):
                raise CalibrationError("input calibration keyboard action list is incomplete")
            operations.append(operation)
        if not operations or len(
            {(operation.operation, operation.payload) for operation in operations}
        ) != len(operations):
            raise CalibrationError("input calibration keyboard action list is incomplete")
        for operation in operations:
            for step in operation.steps:
                base = step.keysym.rsplit("+", 1)[-1]
                if base not in keymap:
                    raise CalibrationError(f"input calibration keymap has no detail for {step.keysym!r}")
        return tuple(operations), tuple(sorted(keymap.items()))

    def run(self, *, human_timeout: float = 120.0) -> InputCalibrationEvidence:
        """Collect synthetic and deliberate real samples on the owned target.

        No callback can inject human source IDs.  ``xinput`` supplies the
        records, and the workflow classifies only complete delivered events
        whose target child is the owned XID.
        """

        from .driver import SyntheticPointerProbe, X11Backend

        backend = self.driver.backend
        self._synthetic_pointer_probe = None
        target = self.window_factory()
        self._target = target
        # Open the calibration epoch before any owned helper is created.  The
        # same guardian fence then covers target ownership, observer startup,
        # and every subsequent XTEST mutation.
        begin_operation = getattr(self.driver, "begin_calibration_operation", None)
        if not callable(begin_operation):
            raise CalibrationError("input calibration operation fence is not configured")
        operation_epoch = begin_operation()
        if isinstance(operation_epoch, bool) or not isinstance(operation_epoch, int) or operation_epoch <= 0:
            raise CalibrationError("input calibration operation fence returned an invalid epoch")
        startup_admission = getattr(self.driver, "admit_calibration_target_process", None)
        if callable(startup_admission) and isinstance(target, OwnedGtkCalibrationWindow):
            window = target.create(startup_admission=startup_admission)
        else:
            window = target.create()
        self._target_certificate = self._safe_target_facts(window)
        observer = None
        self._observer = None
        calibration_ticket: Any | None = None
        try:
            self._stage = "measure_input_profile"
            hierarchy = self.driver.input_hierarchy()
            if not hierarchy:
                raise CalibrationError("owned input calibration has no XInput hierarchy")
            xtest_profile = self.driver.xtest_input_profile(hierarchy)
            self._hierarchy = tuple(hierarchy)
            self._xtest_profile = xtest_profile
            if isinstance(backend, X11Backend) and xtest_profile is None:
                raise CalibrationError("owned XTEST input profile could not be measured")
            if xtest_profile is not None:
                xtest_profile.verify()
            keyboard_operations, keyboard_keymap = self._keyboard_probe_spec()
            self._keyboard_operations = keyboard_operations
            self._keyboard_keymap = keyboard_keymap
            resolve_identity = getattr(self.driver, "resolve_identity", None)
            if not callable(resolve_identity):
                raise CalibrationError("input calibration has no identity provider")
            try:
                game_identity = resolve_identity(require_foreground=False)
            except Exception as exc:
                raise CalibrationError("input calibration game identity could not be observed") from exc
            if (
                not getattr(game_identity, "fingerprint", "")
                or not getattr(game_identity, "child_geometry", ())
                or not getattr(game_identity, "executable", "")
                or not getattr(game_identity, "process_start", "")
            ):
                raise CalibrationError("input calibration game identity is incomplete")
            self._game_identity = game_identity
            synthetic_key_source, synthetic_pointer_source = xtest_sources(hierarchy)
            first_human = threading.Event()
            callback_error: list[str] = []
            synthetic_phase = True
            revoked = False

            def observe(event: XI2Event | None) -> None:
                nonlocal revoked
                if event is None:
                    callback_error.append("malformed XI2 event stream")
                    if not revoked:
                        revoked = True
                        self.driver.revoke("malformed XI2 event during calibration")
                    first_human.set()
                    return
                if event.event_class is EventClass.UNKNOWN:
                    if not revoked:
                        revoked = True
                        self.driver.revoke("unclassified XI2 event during calibration")
                    first_human.set()
                    callback_error.append("unclassified XI2 event during calibration")
                    return
                source_is_xtest = event.source_device in {synthetic_key_source, synthetic_pointer_source}
                if synthetic_phase and not source_is_xtest and event.event_class is EventClass.MOTION:
                    return
                outside_owned_target = event.child_xid not in (None, 0) and not window.contains_xid(
                    event.child_xid
                )
                if not source_is_xtest or outside_owned_target:
                    if not revoked:
                        revoked = True
                        self.driver.revoke("first human input during calibration")
                    first_human.set()
                    if outside_owned_target:
                        callback_error.append("input event targeted outside owned GTK ancestry")

            observer = self.driver.start_input_observer(calibration=True, callback=observe)
            self._observer = observer
            self._stage = "synthetic"
            issue_ticket = getattr(self.driver, "issue_calibration_ticket", None)
            queue_ticket = getattr(self.driver, "queue_delayed_calibration_action", None)
            if not callable(issue_ticket) or not callable(queue_ticket):
                raise CalibrationError("input calibration ticket issuer is not configured")
            # Publish the shared capability before any focus or synthetic
            # preparation.  Scheduling is intentionally delayed until the
            # entire synthetic proof and child prompt ACK complete, so prep
            # time cannot consume the human observation window.
            calibration_ticket = issue_ticket(window)
            if calibration_ticket is None:
                raise CalibrationError("input calibration ticket issuer returned no capability")
            focus_target = getattr(self.driver, "focus_calibration_target", None)
            if not callable(focus_target):
                raise CalibrationError("input calibration focus capability is not configured")
            focus_target(window, calibration_ticket=calibration_ticket)
            if isinstance(backend, X11Backend):
                self._stage = "target_pointer_preflight"
                pointer_position = backend.pointer_position
                try:
                    pointer_x, pointer_y, pointer_xid = pointer_position()
                except Exception as exc:
                    self._target_placement_diagnostic = {
                        "producer": "InputCalibrationWorkflow.target_pointer_preflight",
                        "targetXid": window.xid,
                        "targetGeometry": list(window.geometry),
                        "queryError": self._bounded_error(exc, limit=256),
                    }
                    raise CalibrationError(
                        "owned GTK target pointer cannot be verified before synthetic input"
                    ) from exc
                left, top, width, height = window.geometry
                pointer_certified = window.contains_xid(pointer_xid)
                pointer_inside = left <= pointer_x < left + width and top <= pointer_y < top + height
                self._target_placement_diagnostic = {
                    "producer": "InputCalibrationWorkflow.target_pointer_preflight",
                    "targetXid": window.xid,
                    "targetGeometry": [left, top, width, height],
                    "origin": {"x": pointer_x, "y": pointer_y, "windowXid": pointer_xid},
                    "predicates": {
                        "certifiedHitWindow": pointer_certified,
                        "insideTargetGeometry": pointer_inside,
                    },
                }
                if not pointer_certified or not pointer_inside:
                    raise CalibrationError("owned GTK target does not contain the current pointer")
                self._stage = "synthetic"
            synthetic_input = self.driver.synthetic_target_keyboard_profile
            if not callable(synthetic_input):
                raise CalibrationError("input calibration keyboard probe capability is not configured")
            synthetic_result = synthetic_input(
                window,
                operations=keyboard_operations,
                calibration_ticket=calibration_ticket,
            )
            synthetic_motion_query: tuple[Any, ...] = ()
            synthetic_motion_seed = 0
            synthetic_motion_plan: tuple[tuple[float, int, int], ...] = ()
            if isinstance(backend, X11Backend):
                if not isinstance(synthetic_result, SyntheticPointerProbe):
                    raise CalibrationError("owned XTEST producer returned no typed pointer probe")
                self._synthetic_pointer_probe = synthetic_result
                synthetic_motion_query = (
                    synthetic_result.origin[0],
                    synthetic_result.origin[1],
                    synthetic_result.origin[2],
                    synthetic_result.requested_endpoint[0],
                    synthetic_result.requested_endpoint[1],
                    synthetic_result.final_observed[0],
                    synthetic_result.final_observed[1],
                    synthetic_result.final_observed[2],
                )
                synthetic_motion_seed = synthetic_result.seed
                synthetic_motion_plan = synthetic_result.plan
                left, top, width, height = window.geometry
                origin_inside = (
                    left <= synthetic_result.origin[0] < left + width
                    and top <= synthetic_result.origin[1] < top + height
                )
                endpoint_inside = (
                    left <= synthetic_result.requested_endpoint[0] < left + width
                    and top <= synthetic_result.requested_endpoint[1] < top + height
                )
                if not window.contains_xid(synthetic_result.origin[2]) or not origin_inside:
                    raise CalibrationError("synthetic pointer probe origin is outside the owned target")
                if not window.contains_xid(synthetic_result.final_observed[2]) or not endpoint_inside:
                    raise CalibrationError("synthetic pointer probe endpoint is outside the owned target")
                if synthetic_result.final_observed[:2] != synthetic_result.requested_endpoint:
                    raise CalibrationError("synthetic pointer probe postcondition changed")
            trace = observer.collect_until(
                lambda events: self._has_synthetic(
                    events,
                    window,
                    synthetic_key_source,
                    synthetic_pointer_source,
                    hierarchy,
                    require_motion=isinstance(backend, X11Backend),
                    xtest_profile=xtest_profile,
                    synthetic_motion_probe=self._synthetic_pointer_probe,
                    keyboard_operations=keyboard_operations,
                    keyboard_keymap=keyboard_keymap,
                    failure_reasons=self._predicate_reasons,
                ),
                timeout=10.0,
            )
            synthetic_records = self._records(trace)
            synthetic_phase = False
            synthetic_motion_transitions: tuple[tuple[Any, ...], ...] = ()
            if isinstance(backend, X11Backend):
                if xtest_profile is None:
                    raise CalibrationError("owned XTEST proof has no sealed input profile")
                synthetic_motion_transitions = self._canonical_motion_transitions(
                    synthetic_records,
                    window,
                    xtest_profile,
                    synthetic_motion_probe=self._synthetic_pointer_probe,
                )
            synthetic_echo_proof = self._controlled_echo_proof(
                synthetic_records,
                window,
                synthetic_key_source,
                synthetic_pointer_source,
                hierarchy,
                xtest_profile if isinstance(backend, X11Backend) else None,
            )
            if revoked or callback_error:
                raise CalibrationError(
                    callback_error[0] if callback_error else "input arrived during synthetic calibration"
                )
            synthetic_key_baseline_records = [
                event
                for event in synthetic_records
                if event.source_device == synthetic_key_source
                and event.event_class
                in {
                    EventClass.RAW_KEY_PRESS,
                    EventClass.KEY_PRESS,
                    EventClass.RAW_KEY_RELEASE,
                    EventClass.KEY_RELEASE,
                }
            ][:4]
            synthetic_modifiers = self._sequence_modifiers(
                synthetic_key_baseline_records, synthetic_key_source
            )
            keyboard_profile = self._validate_keyboard_probe(
                synthetic_records,
                synthetic_key_source,
                window,
                hierarchy,
                keyboard_operations,
                dict(keyboard_keymap),
            )
            if synthetic_modifiers != self._sequence_modifiers(synthetic_records, synthetic_pointer_source):
                raise CalibrationError("synthetic key and pointer modifier states do not match")
            self._validate_sequence(
                synthetic_records,
                synthetic_pointer_source,
                window,
                hierarchy,
                pointer=True,
                require_motion=isinstance(backend, X11Backend),
                expected_modifiers=synthetic_modifiers,
                xtest_profile=xtest_profile,
                synthetic_motion_probe=self._synthetic_pointer_probe,
            )
            self.readiness = CalibrationReadiness(
                target_pid=window.pid,
                target_xid=window.xid,
                geometry=window.geometry,
                synthetic_key_source=synthetic_key_source,
                synthetic_pointer_source=synthetic_pointer_source,
                xinput_hierarchy=hierarchy,
                observed_monotonic=self.monotonic(),
                synthetic_modifiers=synthetic_modifiers,
                synthetic_echo_proof=synthetic_echo_proof,
                keyboard_keymap=keyboard_keymap,
                keyboard_profile=keyboard_profile,
            )
            prompt = getattr(target, "show_human_prompt", None)
            self._stage = "publish_human"
            if not callable(prompt):
                raise CalibrationError("owned GTK target prompt is not configured")
            prompt()
            notify_ready = getattr(self.driver, "notify_input_calibration_ready", None)
            if not callable(notify_ready):
                raise CalibrationError("input calibration readiness callback is not configured")
            # The capability-bound timer starts before readiness is published
            # externally.  A consumer cannot observe a full human window while
            # the delayed-action deadline has not yet been installed.
            calibration_ticket = queue_ticket(
                window,
                delay=human_timeout + 0.5,
                calibration_ticket=calibration_ticket,
            )
            notify_ready(self.readiness)
            self._readiness_published = True
            self.ready_for_human.set()
            if self.on_ready is not None:
                self.on_ready(self.readiness)
            deadline = self.monotonic() + human_timeout
            self._stage = "human"

            def current_events() -> list[XI2Event]:
                return observer.events()

            def human_ready(events: list[XI2Event]) -> bool:
                # An observer callback may have already recorded a foreign or
                # malformed event.  Wake bounded fake/real waiters immediately
                # so the workflow reports that concrete error instead of
                # waiting for a complete pair that must never be accepted.
                return bool(callback_error) or self._has_human(
                    events,
                    window,
                    synthetic_key_source,
                    synthetic_pointer_source,
                    hierarchy,
                    require_motion=isinstance(backend, X11Backend),
                )

            while not human_ready(current_events()):
                if callback_error:
                    raise CalibrationError(callback_error[0])
                remaining = deadline - self.monotonic()
                if remaining <= 0:
                    raise CalibrationError("human input sample deadline expired")
                try:
                    observer.wait_for_human(
                        human_ready,
                        timeout=min(0.25, remaining),
                    )
                except Exception as exc:
                    if "deadline expired" not in str(exc):
                        raise
            if callback_error:
                raise CalibrationError(callback_error[0])
            if not first_human.is_set() or not revoked:
                raise CalibrationError("human sample did not revoke calibration generation")
            observer.stop()
            self.driver.stop_input_observer(observer, already_stopped=True)
            poll = getattr(window.process, "poll", None)
            if callable(poll) and poll() is not None:
                raise CalibrationError("owned GTK calibration target exited before the human sample")
            # InputMonitor owns the streaming stdout reader.  Its stop method
            # intentionally does not call communicate(), so the final records
            # must come from the reader's retained parsed stream after the
            # reader has joined.
            records = list(observer.events())
            if not records:
                raise CalibrationError("input observer produced no complete event records")
            evidence = self._build_evidence(
                records,
                window,
                self.driver.driver_version,
                hierarchy=hierarchy,
                display=self.display,
                xauthority=self.driver.xauthority,
                synthetic_key_source=synthetic_key_source,
                synthetic_pointer_source=synthetic_pointer_source,
                game_identity_fingerprint=game_identity.fingerprint,
                game_geometry=game_identity.child_geometry,
                target_executable=window.executable,
                target_process_start=window.process_start,
                target_geometry=window.geometry,
                keyboard_operations=keyboard_operations,
                keyboard_keymap=keyboard_keymap,
                xtest_profile=xtest_profile,
                synthetic_motion_transitions=synthetic_motion_transitions,
                synthetic_motion_query=synthetic_motion_query,
                synthetic_motion_seed=synthetic_motion_seed,
                synthetic_motion_plan=synthetic_motion_plan,
                synthetic_motion_probe=self._synthetic_pointer_probe,
                synthetic_prefix_records=synthetic_records,
            )
            # A deliberate sample revokes the generation and must discard the
            # queued harmless action before this result can be considered live.
            if self.driver.queued_calibration_count:
                raise CalibrationError("queued calibration action survived human takeover")
            if self.driver.calibration_executions:
                raise CalibrationError("delayed calibration action fired before human takeover")
            due_deadline = getattr(self.driver, "calibration_due_deadline", None)
            if due_deadline is None:
                due_deadline = time.monotonic() + min(5.0, max(0.5, human_timeout))
            while getattr(self.driver, "calibration_due_count", 0) < 1:
                if time.monotonic() >= due_deadline:
                    break
                time.sleep(0.01)
            if getattr(self.driver, "calibration_due_count", 0) < 1:
                raise CalibrationError("delayed calibration action did not reach its due point")
            if (
                hasattr(self.driver, "calibration_rejected_count")
                and getattr(self.driver, "calibration_rejected_count", 0) < 1
            ):
                raise CalibrationError(
                    "delayed calibration action was not rejected by the stale generation fence"
                )
            if hasattr(self.driver, "retain_input_evidence"):
                self.driver.retain_input_evidence(evidence)
            return evidence
        except Exception as exc:
            # Keep the original exception authoritative.  The context is
            # assembled after the finally block has stopped the observer and
            # released the owned GTK target.
            self._failure_error = exc
            raise
        finally:
            self.ready_for_human.clear()
            clear_ready = getattr(self.driver, "clear_input_calibration_ready", None)
            if callable(clear_ready):
                clear_ready()
            if observer is not None:
                try:
                    self.driver.stop_input_observer(observer)
                except Exception:
                    pass
            cancel_operation = getattr(self.driver, "cancel_calibration_actions", None)
            try:
                target.close()
            finally:
                if callable(cancel_operation):
                    cancel_operation()
            if self._failure_error is not None and not self._readiness_published:
                try:
                    self.failure_context = self._build_failure_context(self._failure_error)
                except Exception:
                    # The daemon reports the original workflow exception even
                    # if local context assembly is unavailable.
                    self.failure_context = None

    @staticmethod
    def _records(trace: str) -> list[XI2Event]:
        try:
            return parse_xi2_records(trace, require_event=True)
        except InputEventError as exc:
            raise CalibrationError(str(exc)) from exc

    @staticmethod
    def _has_synthetic(
        events: list[XI2Event],
        window: OwnedWindow,
        key_source: int,
        pointer_source: int,
        hierarchy: tuple[tuple[int, int, str, str], ...] = (),
        *,
        require_motion: bool = False,
        keyboard_operations: tuple[KeyboardProbeOperation, ...] = (),
        keyboard_keymap: tuple[tuple[str, int], ...] = (),
        xtest_profile: XTestInputProfile | None = None,
        synthetic_motion_probe: SyntheticPointerProbe | None = None,
        failure_reasons: dict[str, Any] | None = None,
    ) -> bool:
        try:
            InputCalibrationWorkflow._validate_keyboard_probe(
                events,
                key_source,
                window,
                hierarchy,
                keyboard_operations,
                dict(keyboard_keymap),
            )
            InputCalibrationWorkflow._validate_sequence(
                events,
                pointer_source,
                window,
                hierarchy,
                pointer=True,
                require_motion=require_motion,
                expected_detail=1,
                xtest_profile=xtest_profile,
                synthetic_motion_probe=synthetic_motion_probe,
            )
        except CalibrationError as exc:
            if failure_reasons is not None:
                failure_reasons["count"] = int(failure_reasons.get("count", 0)) + 1
                reason = " ".join(str(exc).split())[:512]
                if failure_reasons.get("first") is None:
                    failure_reasons["first"] = reason
                failure_reasons["last"] = reason
            return False
        return True

    @staticmethod
    def _keymap_detail(keysym: str, keymap: dict[str, int]) -> int:
        detail = keymap.get(keysym)
        if detail is None:
            detail = keymap.get(keysym.rsplit("+", 1)[-1])
        if not isinstance(detail, int) or detail <= 0:
            raise CalibrationError(f"input calibration keymap has no detail for {keysym!r}")
        return detail

    @classmethod
    def _validate_keyboard_probe(
        cls,
        records: list[XI2Event],
        source: int,
        window: OwnedWindow,
        hierarchy: tuple[tuple[int, int, str, str], ...],
        operations: tuple[KeyboardProbeOperation, ...],
        keymap: dict[str, int],
    ) -> tuple[KeyboardProfileRow, ...]:
        """Validate F13 plus the exact ordered physical keyboard profile."""

        if not operations or not keymap:
            raise CalibrationError("input calibration keyboard probe specification is incomplete")
        parent = next((row[1] for row in hierarchy if row[0] == source), 0)
        if parent <= 0:
            raise CalibrationError("event proof has no measured keyboard master/source hierarchy")
        events = [event for event in records if event.source_device == source]
        expected: list[tuple[EventClass, int, str | None]] = [
            (EventClass.RAW_KEY_PRESS, 8, None),
            (EventClass.KEY_PRESS, 8, "press"),
            (EventClass.RAW_KEY_RELEASE, 8, None),
            (EventClass.KEY_RELEASE, 8, "release"),
        ]
        for operation in operations:
            for step in operation.steps:
                detail = cls._keymap_detail(step.keysym, keymap)
                raw_class = EventClass.RAW_KEY_PRESS if step.phase == "press" else EventClass.RAW_KEY_RELEASE
                delivered_class = EventClass.KEY_PRESS if step.phase == "press" else EventClass.KEY_RELEASE
                expected.extend(((raw_class, detail, None), (delivered_class, detail, step.phase)))
        if len(events) != len(expected):
            raise CalibrationError("input calibration keyboard probe has extra or missing key transitions")
        profile_rows: list[KeyboardProfileRow] = []
        index = 0
        last_time: int | None = None
        for expected_class, detail, phase in expected:
            event = events[index]
            index += 1
            if event.event_class is not expected_class or event.detail != detail:
                raise CalibrationError("input calibration keyboard probe key detail or order changed")
            if not xi2_timestamp_forward(last_time, event.timestamp_ms):
                raise CalibrationError("input calibration keyboard probe timestamps are out of order")
            last_time = event.timestamp_ms
            if event.is_raw:
                if event.master_device != parent or event.child_xid is not None:
                    raise CalibrationError("input calibration raw keyboard event relationship is malformed")
            else:
                if event.master_device != source or not window.contains_xid(event.child_xid):
                    raise CalibrationError(
                        "input calibration delivered keyboard target is outside the owned target"
                    )
                if event.modifiers is None or event.modifier_effective is None:
                    raise CalibrationError("input calibration keyboard modifier effects are incomplete")
                if event.modifier_effective != event.modifiers[0] | event.modifiers[1] | event.modifiers[2]:
                    raise CalibrationError("input calibration keyboard modifier effect changed")
                if phase is not None:
                    # Pair the delivered facts with their declared step below.
                    continue
        delivered = [event for event in events if not event.is_raw]
        if len(delivered) != 2 + sum(len(operation.steps) for operation in operations):
            raise CalibrationError("input calibration keyboard delivered transition count changed")
        delivered_index = 2
        for operation in operations:
            row_steps: list[tuple[str, str, int, tuple[int, int, int], int]] = []
            for step in operation.steps:
                event = delivered[delivered_index]
                delivered_index += 1
                detail = cls._keymap_detail(step.keysym, keymap)
                if event.detail != detail or event.event_class.value != (
                    "KeyPress" if step.phase == "press" else "KeyRelease"
                ):
                    raise CalibrationError("input calibration keyboard step does not match its live keymap")
                assert event.modifiers is not None and event.modifier_effective is not None
                row_steps.append((step.phase, step.keysym, detail, event.modifiers, event.modifier_effective))
            profile_rows.append((operation.operation, operation.payload, tuple(row_steps)))
        return tuple(profile_rows)

    @staticmethod
    def _has_human(
        events: list[XI2Event],
        window: OwnedWindow,
        key_source: int,
        pointer_source: int,
        hierarchy: tuple[tuple[int, int, str, str], ...] = (),
        *,
        require_motion: bool = False,
    ) -> bool:
        """Require complete deliberate key and pointer press/release pairs."""

        excluded = {key_source, pointer_source}
        key_sources = {
            event.source_device
            for event in events
            if event.event_class is EventClass.KEY_PRESS
            and event.source_device not in excluded
            and window.contains_xid(event.child_xid)
        }
        pointer_sources = {
            event.source_device
            for event in events
            if event.event_class is EventClass.BUTTON_PRESS
            and event.source_device not in excluded
            and window.contains_xid(event.child_xid)
        }
        if not key_sources or not pointer_sources:
            return False
        if not hierarchy:
            return True
        for source in key_sources:
            try:
                InputCalibrationWorkflow._validate_sequence(events, source, window, hierarchy, pointer=False)
            except CalibrationError:
                continue
            for pointer in pointer_sources:
                try:
                    InputCalibrationWorkflow._validate_sequence(
                        events,
                        pointer,
                        window,
                        hierarchy,
                        pointer=True,
                        require_motion=require_motion,
                    )
                except CalibrationError:
                    continue
                return True
        return False

    @classmethod
    def _validate_sequence(
        cls,
        records: list[XI2Event],
        source: int,
        window: OwnedWindow,
        hierarchy: tuple[tuple[int, int, str, str], ...],
        *,
        pointer: bool,
        require_motion: bool = False,
        expected_detail: int | None = None,
        expected_modifiers: tuple[int, int, int] | None = None,
        xtest_profile: XTestInputProfile | None = None,
        synthetic_motion_probe: SyntheticPointerProbe | None = None,
    ) -> None:
        parent = next((row[1] for row in hierarchy if row[0] == source), 0)
        if parent <= 0:
            raise CalibrationError("event proof has no measured master/source hierarchy")
        required = (
            (
                EventClass.RAW_BUTTON_PRESS,
                EventClass.BUTTON_PRESS,
                EventClass.RAW_BUTTON_RELEASE,
                EventClass.BUTTON_RELEASE,
            )
            if pointer
            else (
                EventClass.RAW_KEY_PRESS,
                EventClass.KEY_PRESS,
                EventClass.RAW_KEY_RELEASE,
                EventClass.KEY_RELEASE,
            )
        )
        optional = (
            {
                EventClass.DEVICE_CHANGED,
                EventClass.RAW_MOTION,
                EventClass.MOTION,
                EventClass.ENTER,
                EventClass.LEAVE,
            }
            if pointer
            else set()
        )
        events = [event for event in records if event.source_device == source]
        position = 0
        detail: int | None = None
        delivered_modifiers: tuple[int, int, int] | None = None
        motion_seen = False
        last_time: int | None = None
        raw_motion_pairing = RawMotionPairing()
        canonical = (
            pointer and require_motion and xtest_profile is not None and source == xtest_profile.source_device
        )
        if canonical:
            from .driver import SyntheticPointerProbe

            if not isinstance(synthetic_motion_probe, SyntheticPointerProbe):
                raise CalibrationError("event proof XTEST motion has no typed pointer plan")
        last_accepted_endpoint: tuple[int, int] | None = None
        canonical_transitions: list[tuple[int, int]] = []
        for event in events:
            if not xi2_timestamp_valid(event.timestamp_ms):
                raise CalibrationError("event proof timestamp is outside the XI2 CARD32 domain")
            if event.event_class is EventClass.RAW_MOTION:
                if event.master_device != parent or event.child_xid not in (None, 0):
                    raise CalibrationError("event proof has an incorrect raw motion device relationship")
                if event.detail != RAW_MOTION_DETAIL:
                    raise CalibrationError(f"event proof raw motion detail must be {RAW_MOTION_DETAIL}")
                if not xi2_timestamp_forward(last_time, event.timestamp_ms):
                    raise CalibrationError("event proof timestamps are out of order")
                if canonical:
                    rows = {axis: value for axis, value, _raw in event.valuators}
                    if set(rows) != {0, 1} or any(
                        not math.isfinite(value) or not value.is_integer() for value in rows.values()
                    ):
                        raise CalibrationError("event proof raw motion valuators are not exact XTEST axes")
                    endpoint = (int(rows[0]), int(rows[1]))
                else:
                    endpoint = None
                if not raw_motion_pairing.queue_raw(event.timestamp_ms, endpoint):
                    raise CalibrationError("event proof raw motion is not a forward one-to-one pair")
                last_time = event.timestamp_ms
                continue
            if event.event_class in optional:
                pending_head = (
                    raw_motion_pairing.pending_timestamps[0]
                    if raw_motion_pairing.pending_timestamps
                    else None
                )
                pending_primary = (
                    event.event_class is EventClass.MOTION and pending_head == event.timestamp_ms
                )
                if not pending_primary and not xi2_timestamp_forward(last_time, event.timestamp_ms):
                    raise CalibrationError("event proof timestamps are out of order")
                if last_time is None or xi2_timestamp_forward(last_time, event.timestamp_ms):
                    last_time = event.timestamp_ms
                expected_master = parent if event.event_class is EventClass.DEVICE_CHANGED else source
                if event.master_device != expected_master:
                    raise CalibrationError("event proof has an incorrect optional device relationship")
                if event.event_class is EventClass.MOTION:
                    if not window.contains_xid(event.child_xid):
                        raise CalibrationError("event proof motion left the owned ancestry")
                    if event.modifiers is None or event.modifier_effective is None:
                        raise CalibrationError("event proof motion modifier effects are missing")
                    if (
                        event.modifier_effective
                        != event.modifiers[0] | event.modifiers[1] | event.modifiers[2]
                    ):
                        raise CalibrationError("event proof motion modifier effects do not match")
                    if expected_modifiers is not None and event.modifiers != expected_modifiers:
                        raise CalibrationError(
                            "event proof motion modifier state changed from the sealed expectation"
                        )
                    if canonical:
                        assert xtest_profile is not None
                        pending_endpoint = (
                            raw_motion_pairing.pending_endpoints[0]
                            if raw_motion_pairing.pending_endpoints
                            else None
                        )
                        if pending_endpoint is None:
                            raise CalibrationError("event proof XTEST Motion has no preceding raw endpoint")
                        try:
                            transition = decode_xtest_motion_transition(
                                event,
                                expected_source=source,
                                expected_parent=parent,
                                expected_root_xid=xtest_profile.root_xid,
                                expected_child_xids=window.allowed_event_target_xids,
                                planned_endpoint=pending_endpoint,
                            )
                        except InputEventError as exc:
                            raise CalibrationError(str(exc)) from exc
                        if last_accepted_endpoint is not None:
                            # The source transition's origin is the endpoint
                            # delivered by the preceding owned XTEST motion;
                            # a changed origin is an outside/human transition.
                            if (
                                transition.origin_root != last_accepted_endpoint
                                or transition.origin_event != last_accepted_endpoint
                            ):
                                raise CalibrationError(
                                    "event proof XTEST Motion origin is not the prior endpoint"
                                )
                    if not raw_motion_pairing.observe_delivered(event.timestamp_ms):
                        raise CalibrationError(
                            "event proof raw motion is not paired one-to-one with the following "
                            "same-timestamp delivery"
                        )
                    if canonical:
                        last_accepted_endpoint = transition.endpoint_root
                        canonical_transitions.append(transition.endpoint_root)
                    motion_seen = True
                elif event.event_class in {EventClass.ENTER, EventClass.LEAVE}:
                    if (
                        require_motion
                        and event.child_xid not in (None, 0)
                        and not window.contains_xid(event.child_xid)
                    ):
                        raise CalibrationError("event proof crossing left the owned ancestry")
                continue
            if position >= len(required) or event.event_class is not required[position]:
                raise CalibrationError("event proof has an incomplete or out-of-order operation")
            if event.master_device != (parent if event.is_raw else source):
                raise CalibrationError("event proof has an incorrect raw/delivered device relationship")
            if not xi2_timestamp_forward(last_time, event.timestamp_ms):
                raise CalibrationError("event proof timestamps are out of order")
            last_time = event.timestamp_ms
            if detail is None:
                detail = event.detail
            if event.detail != detail:
                raise CalibrationError("event proof detail changed during one operation")
            if expected_detail is not None and event.detail != expected_detail:
                raise CalibrationError("event proof detail does not match the owned operation")
            if event.is_raw:
                if event.child_xid is not None:
                    raise CalibrationError("raw event unexpectedly carried a target child")
            elif not window.contains_xid(event.child_xid):
                raise CalibrationError("delivered event target left the owned ancestry")
            elif event.modifiers is None or event.modifier_effective is None:
                raise CalibrationError("event proof modifier state or effects are missing")
            elif event.modifier_effective != event.modifiers[0] | event.modifiers[1] | event.modifiers[2]:
                raise CalibrationError("event proof modifier effects do not match the measured state")
            elif expected_modifiers is not None and event.modifiers != expected_modifiers:
                raise CalibrationError("event proof modifier state changed from the sealed expectation")
            elif delivered_modifiers is None:
                delivered_modifiers = event.modifiers
            elif event.modifiers != delivered_modifiers:
                raise CalibrationError("event proof modifier state changed during one operation")
            position += 1
        if position != len(required):
            raise CalibrationError("event proof does not contain complete press/release records")
        if raw_motion_pairing.pending:
            raise CalibrationError("event proof raw motion has no following same-timestamp delivery")
        if require_motion and not motion_seen:
            raise CalibrationError("XTEST pointer proof has no measured motion event")
        if canonical and synthetic_motion_probe is not None:
            expected = synthetic_motion_probe.emitted_transitions
            if tuple(canonical_transitions) != expected:
                raise CalibrationError("event proof XTEST motion sequence differs from the pointer plan")
            if not canonical_transitions:
                raise CalibrationError("event proof XTEST motion sequence is empty")
            origin = (int(synthetic_motion_probe.origin[0]), int(synthetic_motion_probe.origin[1]))
            requested = synthetic_motion_probe.requested_endpoint
            if last_accepted_endpoint is None or last_accepted_endpoint != requested:
                raise CalibrationError("event proof XTEST motion sequence has the wrong final endpoint")
            first_transition = next(
                event
                for event in events
                if event.event_class is EventClass.MOTION and event.source_device == source
            )
            try:
                first_decoded = decode_xtest_motion_transition(
                    first_transition,
                    expected_source=source,
                    expected_parent=parent,
                    expected_root_xid=xtest_profile.root_xid if xtest_profile is not None else 0,
                    expected_child_xids=window.allowed_event_target_xids,
                )
            except (InputEventError, AttributeError) as exc:
                raise CalibrationError("event proof XTEST first motion is malformed") from exc
            if tuple(map(int, first_decoded.origin_root)) != origin:
                raise CalibrationError("event proof XTEST motion sequence has the wrong certified origin")
            left, top, width, height = window.geometry
            if any(
                not (left <= point[0] < left + width and top <= point[1] < top + height)
                for point in canonical_transitions
            ):
                raise CalibrationError("event proof XTEST motion sequence left the certified target")

    @staticmethod
    def _sequence_modifiers(records: list[XI2Event], source: int) -> tuple[int, int, int]:
        delivered = [
            event
            for event in records
            if event.source_device == source
            and not event.is_raw
            and event.event_class
            in {
                EventClass.KEY_PRESS,
                EventClass.KEY_RELEASE,
                EventClass.BUTTON_PRESS,
                EventClass.BUTTON_RELEASE,
            }
        ]
        if not delivered or any(
            event.modifiers is None or event.modifier_effective is None for event in delivered
        ):
            raise CalibrationError("input calibration lacks complete synthetic modifier effects")
        state = delivered[0].modifiers
        assert state is not None
        if any(event.modifiers != state for event in delivered):
            raise CalibrationError("input calibration synthetic modifier state was unstable")
        expected_effective = state[0] | state[1] | state[2]
        if any(event.modifier_effective != expected_effective for event in delivered):
            raise CalibrationError("input calibration synthetic modifier effect was unstable")
        return state

    @staticmethod
    def _controlled_echo_proof(
        records: list[XI2Event],
        window: OwnedWindow,
        synthetic_key_source: int,
        synthetic_pointer_source: int,
        hierarchy: tuple[tuple[int, int, str, str], ...],
        xtest_profile: XTestInputProfile | None = None,
    ) -> tuple[tuple[Any, ...], ...]:
        """Correlate only exact forwarded echoes of the owned pointer motion."""

        pointer_parent = next(
            (row[1] for row in hierarchy if row[0] == synthetic_pointer_source),
            0,
        )
        if pointer_parent <= 0:
            raise CalibrationError("input calibration has no synthetic pointer master")
        forwarded = [
            event
            for event in records
            if event.event_class is EventClass.MOTION
            and event.source_device != synthetic_pointer_source
            and event.source_device != synthetic_key_source
        ]
        if not forwarded:
            return ()
        proof: list[tuple[Any, ...]] = []
        latest_primary: XI2Event | None = None
        latest_primary_used = False
        for event in records:
            if event.event_class is EventClass.MOTION and event.source_device == synthetic_pointer_source:
                latest_primary = event
                latest_primary_used = False
                continue
            if event not in forwarded:
                continue
            if event.child_xid not in window.allowed_event_target_xids:
                raise CalibrationError("unproven forwarded motion targeted outside the owned GTK target")
            # Delivered XI2 Motion prints the source device in both positions
            # (raw records use master(source)).  The parent/master relation is
            # proven by the hierarchy for the synthetic source, while each
            # delivered record must carry its own exact source pair.
            if event.master_device != event.source_device:
                raise CalibrationError("unproven forwarded motion has an unexpected device pair")
            if xtest_profile is not None and (
                event.root_xid != xtest_profile.root_xid
                or event.event_xid != xtest_profile.root_xid
                or event.root_xy is None
                or event.event_xy != event.root_xy
            ):
                raise CalibrationError("forwarded motion is not rooted at the sealed XTEST root")
            if event.modifiers is None or event.modifier_effective is None:
                raise CalibrationError("unproven forwarded motion lacks modifier effects")
            candidate = latest_primary
            if candidate is None or latest_primary_used:
                raise CalibrationError("forwarded motion is not an exact controlled synthetic echo")
            if candidate.child_xid not in window.allowed_event_target_xids:
                raise CalibrationError("controlled synthetic primary left the owned GTK target")
            try:
                if xtest_profile is not None:
                    transition = decode_xtest_motion_transition(
                        candidate,
                        expected_source=synthetic_pointer_source,
                        expected_parent=pointer_parent,
                        expected_root_xid=xtest_profile.root_xid,
                        expected_child_xids=window.allowed_event_target_xids,
                    )
                else:
                    transition = None
            except InputEventError as exc:
                raise CalibrationError("forwarded motion is not an exact controlled synthetic echo") from exc
            endpoint = transition.endpoint_root if transition is not None else candidate.root_xy
            if (
                endpoint is None
                or event.root_xy != endpoint
                or event.event_xy != endpoint
                or event.child_xid != candidate.child_xid
                or candidate.modifiers != event.modifiers
                or candidate.modifier_effective != event.modifier_effective
                or xi2_timestamp_elapsed(candidate.timestamp_ms, event.timestamp_ms) is None
                or xi2_timestamp_elapsed(candidate.timestamp_ms, event.timestamp_ms) > 250
            ):
                raise CalibrationError("forwarded motion is not an exact controlled synthetic echo")
            latest_primary_used = True
            proof.append(
                (
                    "MotionEcho",
                    event.timestamp_ms,
                    event.master_device,
                    event.source_device,
                    int(event.child_xid),
                    event.root_xy,
                    event.event_xy,
                    candidate.timestamp_ms,
                    candidate.source_device,
                    int(candidate.child_xid),
                )
            )
        return tuple(proof)

    @staticmethod
    def _canonical_motion_transitions(
        records: list[XI2Event],
        window: OwnedWindow,
        profile: XTestInputProfile,
        *,
        synthetic_motion_probe: SyntheticPointerProbe | None = None,
    ) -> tuple[tuple[Any, ...], ...]:
        """Seal the measured raw-to-primary XTEST pointer transitions."""

        profile.verify()
        parent = profile.parent_device
        pairing = RawMotionPairing()
        transitions: list[tuple[Any, ...]] = []
        previous_endpoint: tuple[int, int] | None = None
        for event in records:
            if event.source_device != profile.source_device:
                continue
            if event.event_class is EventClass.RAW_MOTION:
                rows = {axis: value for axis, value, _raw in event.valuators}
                if set(rows) != {0, 1} or any(
                    not math.isfinite(value) or not value.is_integer() for value in rows.values()
                ):
                    raise CalibrationError("synthetic raw motion lacks exact finite XTEST axes")
                if not pairing.queue_raw(event.timestamp_ms, (int(rows[0]), int(rows[1]))):
                    raise CalibrationError("synthetic raw motion is not a forward FIFO")
                continue
            if event.event_class is not EventClass.MOTION:
                continue
            pending_endpoint = pairing.pending_endpoints[0] if pairing.pending_endpoints else None
            if pending_endpoint is None:
                raise CalibrationError("synthetic Motion lacks its following raw XTEST record")
            try:
                transition = decode_xtest_motion_transition(
                    event,
                    expected_source=profile.source_device,
                    expected_parent=parent,
                    expected_root_xid=profile.root_xid,
                    expected_child_xids=window.allowed_event_target_xids,
                    planned_endpoint=pending_endpoint,
                )
            except InputEventError as exc:
                raise CalibrationError(str(exc)) from exc
            if previous_endpoint is not None and (
                transition.origin_root != previous_endpoint or transition.origin_event != previous_endpoint
            ):
                raise CalibrationError("synthetic XTEST origin is not the previous delivered endpoint")
            if not pairing.observe_delivered(event.timestamp_ms):
                raise CalibrationError("synthetic raw/primary XTEST transition is not one-to-one")
            previous_endpoint = transition.endpoint_root
            transitions.append(
                (
                    "XTestMotionTransition",
                    transition.timestamp_ms,
                    transition.device_id,
                    transition.source_device,
                    transition.parent_device,
                    transition.target_xid,
                    transition.root_xid,
                    transition.event_xid,
                    transition.origin_root,
                    transition.origin_event,
                    transition.endpoint_root,
                    transition.endpoint_event,
                    transition.valuators,
                    transition.modifiers,
                    transition.modifier_effective,
                )
            )
        if pairing.pending:
            raise CalibrationError("synthetic raw motion has no following primary XTEST delivery")
        if not transitions:
            raise CalibrationError("synthetic XTEST proof has no canonical motion transition")
        if synthetic_motion_probe is not None:
            if (
                tuple(transition[10] for transition in transitions)
                != synthetic_motion_probe.emitted_transitions
            ):
                raise CalibrationError("synthetic XTEST motion sequence differs from the pointer plan")
            if tuple(transitions[0][8]) != synthetic_motion_probe.origin[:2]:
                raise CalibrationError("synthetic XTEST motion sequence has the wrong certified origin")
            if tuple(transitions[-1][10]) != synthetic_motion_probe.requested_endpoint:
                raise CalibrationError("synthetic XTEST motion sequence has the wrong final endpoint")
            left, top, width, height = window.geometry
            if any(
                not (left <= transition[10][0] < left + width and top <= transition[10][1] < top + height)
                for transition in transitions
            ):
                raise CalibrationError("synthetic XTEST motion sequence left the certified target")
        return tuple(transitions)

    @staticmethod
    def _event_proof(records: list[XI2Event]) -> tuple[tuple[Any, ...], ...]:
        return tuple(
            (
                event.event_class.value,
                event.master_device,
                event.source_device,
                event.detail,
                event.timestamp_ms,
                event.root_xid,
                event.event_xid,
                event.child_xid,
                event.root_xy,
                event.event_xy,
                event.modifiers,
                event.modifier_effective,
            )
            for event in records
        )

    @classmethod
    def _build_evidence(
        cls,
        records: list[XI2Event],
        window: OwnedWindow,
        driver_version: str,
        *,
        hierarchy: tuple[tuple[int, int, str, str], ...] = (),
        display: str = ":0",
        xauthority: str = "",
        synthetic_key_source: int | None = None,
        synthetic_pointer_source: int | None = None,
        game_identity_fingerprint: str = "",
        game_geometry: tuple[int, int, int, int] = (),
        target_executable: str = "",
        target_process_start: str = "",
        target_geometry: tuple[int, int, int, int] = (),
        keyboard_operations: tuple[KeyboardProbeOperation, ...] = (),
        keyboard_keymap: tuple[tuple[str, int], ...] = (),
        xtest_profile: XTestInputProfile | None = None,
        synthetic_motion_transitions: tuple[tuple[Any, ...], ...] = (),
        synthetic_motion_query: tuple[Any, ...] = (),
        synthetic_motion_seed: int = 0,
        synthetic_motion_plan: tuple[tuple[float, int, int], ...] = (),
        synthetic_motion_probe: SyntheticPointerProbe | None = None,
        synthetic_prefix_records: list[XI2Event] | tuple[XI2Event, ...] | None = None,
    ) -> InputCalibrationEvidence:
        if (
            not game_identity_fingerprint
            or len(game_geometry) != 4
            or not target_executable
            or not target_process_start
            or len(target_geometry) != 4
            or not display
            or not xauthority
        ):
            raise CalibrationError("input evidence identity, geometry, and game binding are incomplete")
        if xtest_profile is not None and not synthetic_motion_query:
            raise CalibrationError("input evidence has no query-bound synthetic XTEST producer result")
        if window.layout_kind == GTK_LAYOUT_ROOT_CHILD:
            if (
                window.root_xid <= 0
                or window.parent_xid != window.root_xid
                or window.root_xid in window.allowed_event_target_xids
                or window.managed_frame_xid is not None
                or window.managed_frame_geometry
                or window.xid not in window.descendant_xids
                or window.xid in window.excluded_input_only_xids
                or len(window.excluded_input_only_xids) != 1
                or not window.excluded_input_only_xids.issubset(window.descendant_xids)
                or window.root_xid in window.descendant_xids
                or window.descendant_xids != {window.xid, *window.excluded_input_only_xids}
                or window.allowed_event_target_xids
                != window.descendant_xids - window.excluded_input_only_xids
                or window.xid not in window.allowed_event_target_xids
            ):
                raise CalibrationError("input evidence has no certified root-child target")
        elif window.layout_kind == GTK_LAYOUT_MUTTER_FRAME:
            if (
                window.managed_frame_xid is None
                or window.managed_frame_xid <= 0
                or window.managed_frame_xid == window.xid
                or len(window.managed_frame_geometry) != 4
                or not window.allowed_event_target_xids
                or window.managed_frame_xid not in window.allowed_event_target_xids
                or window.xid not in window.allowed_event_target_xids
                or (window.root_xid > 0 and window.parent_xid != window.managed_frame_xid)
                or (window.root_xid > 0 and window.root_xid in window.allowed_event_target_xids)
            ):
                raise CalibrationError("input evidence has no certified managed-frame target")
        else:
            raise CalibrationError("input evidence target layout is unsupported")
        if synthetic_key_source is None:
            synthetic_key_source = next(
                (event.source_device for event in records if event.event_class is EventClass.KEY_PRESS),
                0,
            )
        if synthetic_pointer_source is None:
            synthetic_pointer_source = next(
                (event.source_device for event in records if event.event_class is EventClass.BUTTON_PRESS),
                0,
            )
        if synthetic_key_source <= 0 or synthetic_pointer_source <= 0:
            raise CalibrationError("synthetic XTEST source evidence is incomplete")
        if xtest_profile is None:
            raise CalibrationError("input evidence has no measured XTEST profile")
        xtest_profile.verify()
        from .driver import SyntheticPointerProbe

        if not isinstance(synthetic_motion_probe, SyntheticPointerProbe):
            raise CalibrationError("input evidence has no typed XTEST motion probe")
        if synthetic_motion_seed != synthetic_motion_probe.seed:
            raise CalibrationError("input evidence XTEST motion seed differs from its typed probe")
        if tuple(synthetic_motion_plan) != synthetic_motion_probe.plan:
            raise CalibrationError("input evidence XTEST motion plan differs from its typed probe")
        if tuple(synthetic_motion_query) != (
            *synthetic_motion_probe.origin,
            *synthetic_motion_probe.requested_endpoint,
            *synthetic_motion_probe.final_observed,
        ):
            raise CalibrationError("input evidence XTEST motion query differs from its typed probe")
        synthetic_key = [
            event
            for event in records
            if event.event_class is EventClass.KEY_PRESS and event.source_device == synthetic_key_source
        ]
        synthetic_pointer = [
            event
            for event in records
            if event.event_class is EventClass.BUTTON_PRESS
            and event.source_device == synthetic_pointer_source
        ]
        human_key = [
            event
            for event in records
            if event.event_class is EventClass.KEY_PRESS
            and event.source_device not in {synthetic_key_source, synthetic_pointer_source}
        ]
        human_pointer = [
            event
            for event in records
            if event.event_class is EventClass.BUTTON_PRESS
            and event.source_device not in {synthetic_key_source, synthetic_pointer_source}
        ]
        if not synthetic_key or not synthetic_pointer or not human_key or not human_pointer:
            raise CalibrationError("synthetic and deliberate human key/click records are incomplete")
        prefix_records = list(synthetic_prefix_records) if synthetic_prefix_records is not None else records
        if not prefix_records:
            raise CalibrationError("input evidence synthetic prefix is empty")
        derived_motion_transitions = cls._canonical_motion_transitions(
            prefix_records,
            window,
            xtest_profile,
            synthetic_motion_probe=synthetic_motion_probe,
        )
        if not derived_motion_transitions:
            raise CalibrationError("baseline calibration evidence must contain an XTEST transition sequence")
        baseline_transition = derived_motion_transitions[0]
        if tuple(baseline_transition[8]) == tuple(derived_motion_transitions[-1][10]):
            raise CalibrationError("baseline calibration evidence must prove nonzero displacement")
        if tuple(synthetic_motion_query[:2]) == tuple(synthetic_motion_query[3:5]):
            raise CalibrationError("baseline calibration query must prove nonzero displacement")
        if synthetic_motion_transitions and tuple(synthetic_motion_transitions) != derived_motion_transitions:
            raise CalibrationError("caller-supplied XTEST transitions differ from the recorded prefix")
        synthetic_motion_transitions = derived_motion_transitions
        synthetic_echo_proof = cls._controlled_echo_proof(
            prefix_records,
            window,
            synthetic_key_source,
            synthetic_pointer_source,
            hierarchy,
            xtest_profile,
        )
        if not keyboard_operations or not keyboard_keymap:
            raise CalibrationError("input evidence keyboard profile facts are incomplete")
        keyboard_profile = cls._validate_keyboard_probe(
            records,
            synthetic_key_source,
            window,
            hierarchy,
            keyboard_operations,
            dict(keyboard_keymap),
        )
        synthetic_key_baseline = [
            event
            for event in records
            if event.source_device == synthetic_key_source
            and event.event_class in {EventClass.KEY_PRESS, EventClass.KEY_RELEASE}
        ][:2]
        if len(synthetic_key_baseline) != 2:
            raise CalibrationError("input evidence synthetic keyboard baseline is incomplete")
        synthetic_key_modifiers = cls._sequence_modifiers(
            [
                event
                for event in records
                if event.source_device == synthetic_key_source
                and event.event_class
                in {
                    EventClass.RAW_KEY_PRESS,
                    EventClass.KEY_PRESS,
                    EventClass.RAW_KEY_RELEASE,
                    EventClass.KEY_RELEASE,
                }
            ][:4],
            synthetic_key_source,
        )
        synthetic_pointer_modifiers = cls._sequence_modifiers(records, synthetic_pointer_source)
        if synthetic_key_modifiers != synthetic_pointer_modifiers:
            raise CalibrationError("synthetic key and pointer modifier states do not match")
        synthetic_modifiers = synthetic_key_modifiers
        relevant = [*synthetic_key, *synthetic_pointer, *human_key, *human_pointer]
        if any(not window.contains_xid(event.child_xid) for event in relevant):
            raise CalibrationError("input sample targeted a window outside the owned GTK target")
        # Every forwarded source is retained as evidence.  A forwarded motion
        # is admitted only through the exact controlled echo proof above.
        forwarded = [
            event
            for event in records
            if event.source_device not in {synthetic_key_source, synthetic_pointer_source}
        ]
        if any(
            event.is_action_event and event.child_xid is not None and not window.contains_xid(event.child_xid)
            for event in forwarded
        ):
            raise CalibrationError("forwarded XI2 event targeted an unrelated window")
        cls._validate_sequence(
            records,
            synthetic_pointer_source,
            window,
            hierarchy,
            pointer=True,
            expected_detail=1,
            expected_modifiers=synthetic_modifiers,
        )
        cls._validate_sequence(
            records,
            human_key[0].source_device,
            window,
            hierarchy,
            pointer=False,
        )
        cls._validate_sequence(
            records,
            human_pointer[0].source_device,
            window,
            hierarchy,
            pointer=True,
        )
        return InputCalibrationEvidence._issue(
            fingerprint="",
            display=display,
            target_pid=window.pid,
            target_xid=window.xid,
            target_child_xid=window.xid,
            synthetic_key_source=synthetic_key_source,
            synthetic_pointer_source=synthetic_pointer_source,
            human_key_source=human_key[0].source_device,
            human_pointer_source=human_pointer[0].source_device,
            xinput_hierarchy=hierarchy,
            synthetic_events=tuple(
                (event.event_class.value, event.source_device, event.detail)
                for event in records
                if event.source_device in {synthetic_key_source, synthetic_pointer_source}
            ),
            human_events=tuple(
                (event.event_class.value, event.source_device, event.detail)
                for event in records
                if event.source_device not in {synthetic_key_source, synthetic_pointer_source}
                and event.is_action_event
            ),
            forwarded_events=tuple(
                (event.event_class.value, event.source_device, event.child_xid) for event in forwarded
            ),
            driver_version=driver_version,
            xauthority=xauthority,
            event_proof=tuple(
                (
                    event.event_class.value,
                    event.master_device,
                    event.source_device,
                    event.detail,
                    event.timestamp_ms,
                    event.root_xid,
                    event.event_xid,
                    event.child_xid,
                    event.root_xy,
                    event.event_xy,
                    event.modifiers,
                    event.modifier_effective,
                )
                for event in records
            ),
            game_identity_fingerprint=game_identity_fingerprint,
            game_geometry=game_geometry,
            target_executable=target_executable,
            target_process_start=target_process_start,
            target_geometry=target_geometry,
            managed_frame_xid=window.managed_frame_xid or 0,
            managed_frame_geometry=window.managed_frame_geometry,
            target_event_xids=tuple(sorted(window.allowed_event_target_xids)),
            target_layout_kind=window.layout_kind,
            target_root_xid=window.root_xid,
            target_parent_xid=window.parent_xid,
            target_descendant_xids=tuple(sorted(window.descendant_xids)),
            target_excluded_input_only_xids=tuple(sorted(window.excluded_input_only_xids)),
            synthetic_modifiers=synthetic_modifiers,
            synthetic_echo_proof=synthetic_echo_proof,
            xtest_profile=xtest_profile,
            synthetic_motion_transitions=tuple(synthetic_motion_transitions),
            synthetic_motion_query=tuple(synthetic_motion_query),
            synthetic_motion_seed=synthetic_motion_seed,
            synthetic_motion_plan=tuple(synthetic_motion_plan),
            keyboard_keymap=tuple(keyboard_keymap),
            keyboard_profile=keyboard_profile,
        )


class NativeCalibrationRunner:
    """Capture and validate native render evidence from the configured client."""

    def __init__(
        self,
        driver: Any,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        diagnostic: Any | None = None,
    ):
        self.driver = driver
        self.monotonic = monotonic
        self.diagnostic = diagnostic
        self.memory_meter: LinuxProcStatusVmHwmReset5 | None = getattr(driver, "native_memory_meter", None)

    def _publish_resume_required_boundary(
        self, identity: WindowIdentity, boundary: Any, path: Path
    ) -> Callable[[WindowIdentity, Any, Any], None]:
        """Validate and publish the held/100 boundary immediately before resume."""

        resume_required = getattr(self.driver, "notify_native_resume_required", None)
        if not callable(resume_required):
            raise CalibrationError("native resume-required callback is not configured")
        resume = getattr(self.driver, "notify_native_resume_ready", None)
        if not callable(resume):
            raise CalibrationError("native resume-ready callback is not configured")
        resume_required(identity, boundary)
        diagnostic = self.diagnostic
        if diagnostic is not None and callable(getattr(diagnostic, "mark_release", None)):
            diagnostic.mark_release(path)
        return resume

    @contextmanager
    def _diagnostic_span(
        self,
        name: str,
        *,
        requested_stage: str | None = None,
        requested_ordinal: int | None = None,
    ):
        diagnostic = self.diagnostic
        if diagnostic is None or not callable(getattr(diagnostic, "span", None)):
            yield
            return
        with diagnostic.span(
            name,
            requested_stage=requested_stage,
            requested_ordinal=requested_ordinal,
        ):
            yield

    def _native_update_budget_capability(
        self,
        provider: Any,
        identity: WindowIdentity,
        ordinal: int,
    ) -> Any | None:
        """Return a witness only from the exact daemon-owned provider path."""

        eligibility = getattr(self.driver, "_native_identity_budget_eligibility", None)
        verifier = getattr(self.driver, "_native_identity_budget_capability_valid", None)
        provider_owner = getattr(provider, "__self__", None)
        eligibility_owner = getattr(eligibility, "__self__", None)
        verifier_owner = getattr(verifier, "__self__", None)
        provider_function = getattr(provider, "__func__", None)
        eligibility_function = getattr(eligibility, "__func__", None)
        verifier_function = getattr(verifier, "__func__", None)
        # A caller can manufacture an object whose three private methods have
        # the same names and return a truthy object.  The witness is closed
        # only when these are the installed SessionDaemon implementations and
        # their owner is the exact driver being calibrated.
        from .daemon import SessionDaemon

        installed_provider = getattr(self.driver, "native_measurement_provider", None)
        installed_owner = getattr(installed_provider, "__self__", None)

        if (
            getattr(provider_owner, "driver", None) is not self.driver
            or provider_owner is not installed_owner
            or provider_function is not SessionDaemon._native_measurement_provider
            or eligibility_function is not SessionDaemon._native_update_identity_budget_capability
            or verifier_function is not SessionDaemon._native_update_identity_budget_capability_valid
        ):
            return None
        owner_type = type(provider_owner) if provider_owner is not None else None
        if not (
            callable(eligibility)
            and callable(verifier)
            and provider_owner is not None
            and eligibility_owner is provider_owner
            and verifier_owner is provider_owner
            and provider_function is getattr(owner_type, "_native_measurement_provider", None)
            and eligibility_function is getattr(owner_type, "_native_update_identity_budget_capability", None)
            and verifier_function
            is getattr(owner_type, "_native_update_identity_budget_capability_valid", None)
        ):
            return None
        with self._diagnostic_span("alignment_capability"):
            candidate = eligibility(provider, identity, "updates", ordinal)
        if candidate is None or not verifier(candidate, provider, identity, "updates", ordinal):
            return None
        return candidate

    @staticmethod
    def _validate_cells(path: Path) -> None:
        try:
            from PIL import Image

            with Image.open(path) as image:
                image.load()
                if image.size != (256, 256):
                    raise CalibrationError("native crop is not exactly 256x256")
                for y in range(128):
                    for x in range(128):
                        pixels = [
                            image.getpixel((x * 2 + dx, y * 2 + dy))[:3] for dy in (0, 1) for dx in (0, 1)
                        ]
                        if any(pixel not in {(0, 0, 0), (255, 255, 255)} for pixel in pixels):
                            raise CalibrationError("native status cells are not exact black/white 2x2 cells")
                        if len(set(pixels)) != 1:
                            raise CalibrationError("native status cell has inconsistent physical pixels")
        except ImportError as exc:
            raise CalibrationError("Pillow is required for native cell validation") from exc

    @staticmethod
    def _validate_metrics(metrics: dict[str, Any], *, minimum_updates: int) -> None:
        if not isinstance(metrics, dict):
            raise CalibrationError("native render metrics are not an object")
        required = {
            "constructionSeconds",
            "updateCount",
            "updateP99Ms",
            "textureCount",
            "effectiveScale",
            "cellSize",
            "luaHeap",
            "nativePhase",
            "nativeOrdinal",
        }
        if set(metrics) != required:
            raise CalibrationError("native render metrics have an unexpected schema")
        if (
            isinstance(metrics["updateCount"], bool)
            or not isinstance(metrics["updateCount"], int)
            or metrics["updateCount"] < minimum_updates
            or isinstance(metrics["textureCount"], bool)
            or not isinstance(metrics["textureCount"], int)
            or not 1 <= metrics["textureCount"] <= 16_384
        ):
            raise CalibrationError("native render metrics do not cover the required updates/textures")
        if (
            isinstance(metrics["effectiveScale"], bool)
            or not isinstance(metrics["effectiveScale"], (int, float))
            or not math.isfinite(float(metrics["effectiveScale"]))
            or metrics["effectiveScale"] <= 0
        ):
            raise CalibrationError("native effective scale is not numeric")
        if not isinstance(metrics["cellSize"], (list, tuple)) or tuple(metrics["cellSize"]) != (2, 2):
            raise CalibrationError("native physical 2x2 cell mapping is not proven")
        for key in ("constructionSeconds", "updateP99Ms", "effectiveScale"):
            if (
                isinstance(metrics[key], bool)
                or not isinstance(metrics[key], (int, float))
                or not math.isfinite(float(metrics[key]))
                or float(metrics[key]) < 0
            ):
                raise CalibrationError(f"native metric {key} is not numeric")
        heap = metrics["luaHeap"]
        if not isinstance(heap, dict) or set(heap) != {
            "method",
            "available",
            "beforeKiB",
            "afterKiB",
            "netKiB",
        }:
            raise CalibrationError("native Lua heap diagnostic schema is invalid")
        if heap["method"] != "collectgarbage_count" or not isinstance(heap["available"], bool):
            raise CalibrationError("native Lua heap diagnostic method is invalid")
        for key in ("beforeKiB", "afterKiB", "netKiB"):
            value = heap[key]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
            ):
                raise CalibrationError("native Lua heap diagnostic is not finite")
            if key != "netKiB" and float(value) < 0:
                raise CalibrationError("native Lua heap diagnostic has negative memory")
        if float(heap["netKiB"]) != float(heap["afterKiB"]) - float(heap["beforeKiB"]):
            raise CalibrationError("native Lua heap diagnostic net is not derived from its samples")
        if not heap["available"] and any(heap[key] != 0 for key in ("beforeKiB", "afterKiB", "netKiB")):
            raise CalibrationError("unavailable native Lua heap diagnostic must use zero sentinels")
        phase = metrics["nativePhase"]
        ordinal = metrics["nativeOrdinal"]
        if (
            not isinstance(phase, str)
            or phase not in {"normal", "held", "updates"}
            or isinstance(ordinal, bool)
            or not isinstance(ordinal, int)
        ):
            raise CalibrationError("native phase/ordinal telemetry is invalid")
        if ordinal < 0 or ordinal > 100:
            raise CalibrationError("native telemetry ordinal is outside 0..100")
        if (phase == "normal" and ordinal != 0) or (phase == "updates" and ordinal not in range(1, 100)):
            raise CalibrationError("native phase/ordinal telemetry does not match its phase")
        if phase == "held" and ordinal not in {0, 100}:
            raise CalibrationError("native held telemetry ordinal is invalid")

    def run(self) -> NativeCalibrationEvidence:
        from .driver import X11Backend

        if not isinstance(self.driver.backend, X11Backend):
            raise CalibrationError("native calibration requires the configured X11 backend")
        if self.driver.profile.canvas_crop != (80, 48, 256, 256) or self.driver.profile.canvas_origin != (
            80,
            48,
        ):
            raise CalibrationError("native canvas must use the pinned 80,48 256x256 crop")
        provider = getattr(self.driver, "native_measurement_provider", None)
        if not callable(provider):
            raise CalibrationError("native measurement provider is not configured")
        if not isinstance(self.memory_meter, LinuxProcStatusVmHwmReset5):
            raise CalibrationError("native reset5 memory meter is not configured")
        identity = self.driver.resolve_identity()
        if (
            self.memory_meter.pid != identity.pid
            or self.memory_meter.process_start != identity.process_start
            or self.memory_meter.identity_fingerprint != identity.fingerprint
        ):
            raise CalibrationError("native reset5 memory meter is not bound to the verified client")
        hierarchy = self.driver.input_hierarchy()
        if not hierarchy:
            raise CalibrationError("native calibration has no XInput hierarchy")
        failure_status = getattr(self.driver, "notify_native_measurement_failed", None)
        if not callable(failure_status):
            raise CalibrationError("native measurement failure-status callback is not configured")
        expected_boot: list[str | None] = [None]
        last_sequence = 0
        last_update_count = -1

        def report_failure(error: Exception) -> None:
            nonlocal last_sequence, last_update_count
            restoration = failure_status(identity, str(error))
            if not isinstance(restoration, NativeRestorationReceipt):
                raise CalibrationError("native measurement failure returned no restoration receipt")
            restoration.verify()

        # The lead-owned bootstrap hook may verify that the addon/render
        # process is idle and ready to construct its native surface.  It runs
        # before the reset5 baseline and must not allocate addon textures or
        # return caller metrics; the constructor provider below is the first
        # allocation/update observation.
        preconstruction = getattr(self.driver, "native_preconstruction_provider", None)
        if not callable(preconstruction):
            raise CalibrationError("native preconstruction provider is not configured")
        baseline_values: list[int] = []
        first_window: list[_PendingNativeMemoryWindow] = []
        active_window = False

        def baseline_reader(pid: int) -> int:
            nonlocal active_window
            if pid != identity.pid:
                raise CalibrationError("native preconstruction baseline PID changed")
            if first_window:
                raise CalibrationError("native construction memory window was reset more than once")
            reset_ready = getattr(self.driver, "notify_native_memory_reset_ready", None)
            if not callable(reset_ready):
                raise CalibrationError("native construction memory archive callback is not configured")
            # Mark ownership before calling into reset5.  The meter may have
            # written clear_refs and then reject a post-reset identity check;
            # the outer runner must still abort that pending window.
            active_window = True
            pending = self.memory_meter.begin_window(
                "construction",
                0,
                limit_bytes=NATIVE_CONSTRUCTION_LIMIT_BYTES,
                before_reset=lambda record: reset_ready(identity, record),
            )
            first_window.append(pending)
            if pending.pid != pid:
                raise CalibrationError("native construction memory PID changed")
            baseline_values.append(pending.historical_hwm_bytes)
            return pending.historical_hwm_bytes

        def baseline_ready(pid: int, value: int) -> None:
            if pid != identity.pid or not baseline_values or value != baseline_values[-1]:
                raise CalibrationError("native baseline-ready signal is not bound to the measured HWM")
            signal = getattr(self.driver, "notify_native_baseline_ready", None)
            if not callable(signal):
                raise CalibrationError("native baseline-ready callback is not configured")
            signal(identity, value)

        def abort_memory_window() -> None:
            nonlocal active_window
            if active_window:
                try:
                    self.memory_meter.abort_window()
                finally:
                    active_window = False

        try:
            # The provider owns the absence capture.  It must call
            # baseline_reader only after that proof, then call baseline_ready
            # before returning the sealed token.  This is the externally
            # observable boundary at which the operator may load the client.
            preconstruction_evidence = preconstruction(
                identity,
                baseline_reader=baseline_reader,
                baseline_ready=baseline_ready,
            )
        except Exception as exc:
            failure = CalibrationError(f"native preconstruction bootstrap failed: {exc}")
            abort_memory_window()
            report_failure(failure)
            raise failure from exc
        if not isinstance(preconstruction_evidence, NativePreconstructionEvidence):
            failure = CalibrationError("native preconstruction did not return sealed absence evidence")
            abort_memory_window()
            report_failure(failure)
            raise failure
        try:
            preconstruction_evidence.verify_seal()
        except CalibrationError as failure:
            abort_memory_window()
            report_failure(failure)
            raise
        except Exception as exc:
            failure = CalibrationError("native preconstruction seal is invalid")
            abort_memory_window()
            report_failure(failure)
            raise failure from exc
        # The absence capture is consumed as soon as its seal is checked; do
        # not retain an unused temporary artifact while later identity/age
        # admission checks fail.
        try:
            self._release_capture(Path(preconstruction_evidence.capture_path))
        except Exception as exc:
            failure = CalibrationError(f"native preconstruction capture could not be released: {exc}")
            abort_memory_window()
            report_failure(failure)
            raise failure from exc
        if (
            preconstruction_evidence.identity_fingerprint != identity.fingerprint
            or preconstruction_evidence.process_start != identity.process_start
            or preconstruction_evidence.window_xid != identity.xid
            or preconstruction_evidence.child_xid != identity.child_xid
            or preconstruction_evidence.geometry != identity.child_geometry
            or preconstruction_evidence.process_executable != identity.executable
            or preconstruction_evidence.display != identity.display
            or preconstruction_evidence.baseline_hwm_bytes != (baseline_values[-1] if baseline_values else -1)
            or len(baseline_values) != 1
        ):
            failure = CalibrationError("native preconstruction proof is not bound to the measured client")
            abort_memory_window()
            report_failure(failure)
            raise failure
        try:
            current_time = self.monotonic()
            stale = current_time - float(preconstruction_evidence.observed_monotonic) < 0 or (
                current_time - float(preconstruction_evidence.observed_monotonic) > 5.0
            )
            release_stale = current_time - float(
                preconstruction_evidence.bootstrap_release_monotonic
            ) < 0 or (current_time - float(preconstruction_evidence.bootstrap_release_monotonic) > 5.0)
        except Exception as exc:
            failure = CalibrationError(f"native preconstruction clock validation failed: {exc}")
            abort_memory_window()
            report_failure(failure)
            raise failure from exc
        if stale:
            failure = CalibrationError("native preconstruction capture is stale")
            abort_memory_window()
            report_failure(failure)
            raise failure
        if release_stale:
            failure = CalibrationError("native bootstrap release is stale")
            abort_memory_window()
            report_failure(failure)
            raise failure
        pending_captures: set[Path] = set()
        success = False
        alignment_owner = getattr(provider, "__self__", None)
        alignment_enter = getattr(alignment_owner, "_native_update_alignment_enter", None)
        alignment_exit = getattr(alignment_owner, "_native_update_alignment_exit", None)
        alignment_token: object | None = None
        last_capture_monotonic = -1.0
        last_publisher_uptime_ms: int | None = None
        try:
            if callable(alignment_enter):
                alignment_token = alignment_enter(provider, identity)
            try:
                construction = provider(identity, "construct", 0)
            except Exception as exc:
                raise CalibrationError(f"native constructor measurement failed: {exc}") from exc
            if not isinstance(construction, NativeRenderObservation):
                raise CalibrationError("native provider returned an unbound construction observation")
            if construction.capture_path is not None:
                try:
                    pending_captures.add(Path(construction.capture_path))
                except Exception as exc:
                    raise CalibrationError("native construction capture path is malformed") from exc
            if construction.identity_fingerprint != identity.fingerprint:
                raise CalibrationError("native construction observation identity changed")
            self._validate_capture_time(construction, -1.0, now=self.monotonic())
            last_capture_monotonic = float(construction.capture_monotonic)

            def capture_observation(
                observation: NativeRenderObservation,
                *,
                previous_sequence: int | None,
                label: str,
                minimum_updates: int,
                expected_phase: str,
                expected_ordinal: int,
                expected_update_count: int,
                require_contiguous_sequence: bool,
            ) -> tuple[Path, Any]:
                nonlocal last_capture_monotonic, last_publisher_uptime_ms, last_sequence, last_update_count
                returned_path = getattr(observation, "capture_path", None)
                if returned_path is not None:
                    pending_captures.add(Path(returned_path))
                if observation.identity_fingerprint != identity.fingerprint:
                    raise CalibrationError(f"native {label} observation identity changed")
                if (
                    observation.native_phase != expected_phase
                    or observation.native_ordinal != expected_ordinal
                    or observation.metrics.get("nativePhase") != expected_phase
                    or observation.metrics.get("nativeOrdinal") != expected_ordinal
                    or observation.metrics.get("updateCount") != expected_update_count
                ):
                    raise CalibrationError(
                        f"native {label} phase/ordinal/update count does not match the requested observation"
                    )
                if (
                    previous_sequence is not None
                    and expected_phase in {"normal", "held"}
                    and expected_update_count <= last_update_count
                ):
                    raise CalibrationError(f"native {label} publication count did not advance")
                self._validate_capture_time(
                    observation, last_capture_monotonic if previous_sequence else -1.0, now=self.monotonic()
                )
                if observation.capture_path is None:
                    raise CalibrationError(f"native {label} observation has no capture artifact")
                capture_path = Path(observation.capture_path)
                with self._diagnostic_span("runner_hash"):
                    self._capture_observation(identity, observation)
                with self._diagnostic_span("identity_post_capture"):
                    if self.driver.resolve_identity().fingerprint != identity.fingerprint:
                        raise CalibrationError(f"native {label} capture identity changed")
                with self._diagnostic_span("cells"):
                    self._validate_cells(capture_path)
                with self._diagnostic_span("runner_decode"):
                    decoded = self.driver.decode_native_capture(
                        capture_path, identity=identity, decoder=decoder
                    )
                if decoded.frame is None:
                    raise CalibrationError(f"native {label} capture did not decode a frame")
                if previous_sequence is not None:
                    if require_contiguous_sequence and decoded.frame.sequence != previous_sequence + 1:
                        raise CalibrationError(f"native {label} capture sequence is not contiguous")
                    if not require_contiguous_sequence and decoded.frame.sequence <= previous_sequence:
                        raise CalibrationError(f"native {label} capture sequence did not advance")
                if expected_boot[0] is not None and decoded.frame.boot != expected_boot[0]:
                    raise CalibrationError(f"native {label} frame boot changed during measurement")
                with self._diagnostic_span("validation"):
                    self._bind_observation(observation, decoded.frame, capture_path)
                    self._validate_metrics(observation.metrics, minimum_updates=minimum_updates)
                if last_publisher_uptime_ms is not None and (
                    (expected_phase == "updates" and expected_ordinal >= 2) or expected_ordinal == 100
                ):
                    try:
                        publisher_delta_ms = unsigned_delta(
                            decoded.frame.uptime_ms,
                            last_publisher_uptime_ms,
                        )
                    except ValueError as exc:
                        raise CalibrationError(f"native {label} publication cadence is not 1Hz") from exc
                    if not 750 <= publisher_delta_ms <= 1250:
                        raise CalibrationError(f"native {label} publication cadence is not 1Hz")
                last_sequence = decoded.frame.sequence
                last_update_count = expected_update_count
                last_capture_monotonic = float(observation.capture_monotonic)
                last_publisher_uptime_ms = decoded.frame.uptime_ms
                return capture_path, decoded

            decoder = self.driver.new_native_decoder()
            construction_path, sample = capture_observation(
                construction,
                previous_sequence=None,
                label="constructor",
                minimum_updates=0,
                expected_phase="normal",
                expected_ordinal=0,
                expected_update_count=int(construction.metrics["updateCount"]),
                require_contiguous_sequence=False,
            )
            expected_boot[0] = sample.frame.boot
            if (
                float(preconstruction_evidence.bootstrap_release_monotonic)
                < first_window[0].reset_completed_monotonic
            ):
                raise CalibrationError("native construction work started before bootstrap release")
            construction_memory = self.memory_meter.finish_window(
                first_window[0],
                workload_start_monotonic=float(preconstruction_evidence.bootstrap_release_monotonic),
                workload_end_monotonic=self.monotonic(),
                workload_start_ordinal=0,
                workload_end_ordinal=0,
            )
            active_window = False
            self._release_capture(construction_path)
            pending_captures.discard(construction_path)

            hold_required = getattr(self.driver, "notify_native_hold_required", None)
            if not callable(hold_required):
                raise CalibrationError("native hold-required callback is not configured")
            hold_required(identity)
            try:
                held_start = provider(identity, "held", 0)
            except Exception as exc:
                raise CalibrationError(f"native held/0 measurement failed: {exc}") from exc
            held_start_path, held_sample = capture_observation(
                held_start,
                previous_sequence=last_sequence,
                label="held/0",
                minimum_updates=0,
                expected_phase="held",
                expected_ordinal=0,
                expected_update_count=int(held_start.metrics["updateCount"]),
                require_contiguous_sequence=False,
            )
            self._release_capture(held_start_path)
            pending_captures.discard(held_start_path)
            reset_ready = getattr(self.driver, "notify_native_memory_reset_ready", None)
            if not callable(reset_ready):
                raise CalibrationError("native update memory archive callback is not configured")
            # Register ownership before reset5 performs its post-reset
            # identity/status reads.  A successful clear_refs write followed
            # by a PID identity failure must still be abortable by the
            # runner's final cleanup.
            active_window = True
            updates_window = self.memory_meter.begin_window(
                "updates",
                0,
                limit_bytes=NATIVE_UPDATES_LIMIT_BYTES,
                before_reset=lambda record: reset_ready(identity, record),
            )
            ready = getattr(self.driver, "notify_native_memory_ready", None)
            if not callable(ready):
                raise CalibrationError("native update memory-ready callback is not configured")
            ready(identity, updates_window)

            update_window_start = updates_window.reset_completed_monotonic
            for update in range(1, 100):
                try:
                    identity_budget_capability = self._native_update_budget_capability(
                        provider, identity, update
                    )
                except Exception as exc:
                    raise CalibrationError(
                        f"native update {update} identity-budget eligibility failed"
                    ) from exc
                if identity_budget_capability is None:
                    with self._diagnostic_span(
                        "identity_pre_provider",
                        requested_stage="updates",
                        requested_ordinal=update,
                    ):
                        current = self.driver.resolve_identity()
                    if current.fingerprint != identity.fingerprint:
                        raise CalibrationError("native client identity changed during updates")
                try:
                    observation = provider(identity, "updates", update)
                except Exception as exc:
                    raise CalibrationError(f"native update {update} measurement failed: {exc}") from exc
                update_path, decoded = capture_observation(
                    observation,
                    previous_sequence=last_sequence,
                    label=f"update {update}",
                    minimum_updates=update,
                    expected_phase="updates",
                    expected_ordinal=update,
                    expected_update_count=int(held_start.metrics["updateCount"]) + update,
                    require_contiguous_sequence=True,
                )
                self._release_capture(update_path)
                pending_captures.discard(update_path)

            try:
                final = provider(identity, "held", 100)
            except Exception as exc:
                raise CalibrationError(f"native held/100 measurement failed: {exc}") from exc
            final_path, final_sample = capture_observation(
                final,
                previous_sequence=last_sequence,
                label="held/100",
                minimum_updates=100,
                expected_phase="held",
                expected_ordinal=100,
                expected_update_count=int(held_start.metrics["updateCount"]) + 100,
                require_contiguous_sequence=True,
            )
            updates_memory = self.memory_meter.finish_window(
                updates_window,
                workload_start_monotonic=update_window_start,
                workload_end_monotonic=float(final.capture_monotonic),
                workload_start_ordinal=1,
                workload_end_ordinal=100,
            )
            active_window = False
            metrics = dict(final.metrics)
            self._validate_metrics(metrics, minimum_updates=100)
            if float(metrics["constructionSeconds"]) > 5.0:
                raise CalibrationError("native construction exceeds 5 seconds")
            fingerprint = CalibrationFingerprint(
                process_pid=identity.pid,
                windows_xid=identity.xid,
                child_xid=identity.child_xid,
                executable=identity.executable,
                display=identity.display,
                geometry=identity.child_geometry,
                xinput_hierarchy=hierarchy,
                driver_version=self.driver.driver_version,
                xauthority=self.driver.xauthority,
                wine_prefix=self.driver.profile.wine_prefix,
                process_start=identity.process_start,
            ).value
            capture_sha256 = hashlib.sha256(final_path.read_bytes()).hexdigest()
            boundary = NativeResumeBoundary._issue(
                identity_fingerprint=identity.fingerprint,
                process_start=identity.process_start,
                process_executable=identity.executable,
                boot=final_sample.frame.boot,
                sequence=final_sample.frame.sequence,
                crc32=final_sample.frame.crc32,
                update_count=int(final.metrics["updateCount"]),
                capture_monotonic=float(final.capture_monotonic),
                capture_sha256=capture_sha256,
                render=dict(metrics),
            )
            boundary.verify()
            # The held/100 crop is the durable terminal evidence.  It must be
            # delivered to the real diagnostic observer before the subsequent
            # resume provider begins a new attempt, while remaining retained
            # for the sealed resume boundary and final receipt.
            resume = self._publish_resume_required_boundary(identity, boundary, final_path)
            try:
                resumed = provider(identity, "resume", 0)
            except Exception as exc:
                raise CalibrationError(f"native normal resume measurement failed: {exc}") from exc
            resume_path, resume_sample = capture_observation(
                resumed,
                previous_sequence=last_sequence,
                label="resume normal/0",
                minimum_updates=0,
                expected_phase="normal",
                expected_ordinal=0,
                expected_update_count=int(final.metrics["updateCount"]) + 1,
                require_contiguous_sequence=True,
            )
            self._release_capture(resume_path)
            pending_captures.discard(resume_path)
            if float(resumed.metrics["updateP99Ms"]) > 250.0:
                raise CalibrationError("native completed update p99 exceeds 250 ms")

            # The resume-required callback already published the sealed
            # held/100 boundary before this provider call.  The completed
            # evidence below adds the fresh normal/0 frame and is delivered
            # only to the final resume-ready callback.
            result = NativeCalibrationEvidence._issue(
                fingerprint=fingerprint,
                process_pid=identity.pid,
                process_executable=identity.executable,
                window_xid=identity.xid,
                child_xid=identity.child_xid,
                geometry=identity.child_geometry,
                xinput_hierarchy=hierarchy,
                crop=self.driver.profile.canvas_crop,
                boot=final_sample.frame.boot,
                sequence=final_sample.frame.sequence,
                crc32=final_sample.frame.crc32,
                render=dict(metrics),
                construction_update_count=int(construction.metrics["updateCount"]),
                held_update_count=int(held_start.metrics["updateCount"]),
                resume_update_count=int(resumed.metrics["updateCount"]),
                memory_measurements=(construction_memory, updates_memory),
                native_identity_fingerprint=identity.fingerprint,
                resume_sequence=resume_sample.frame.sequence,
                resume_crc32=resume_sample.frame.crc32,
                resume_render=dict(resumed.metrics),
                resume_capture_monotonic=float(resumed.capture_monotonic),
                capture_path=str(final_path),
                xauthority=self.driver.xauthority,
                wine_prefix=self.driver.profile.wine_prefix,
                process_start=identity.process_start,
                capture_sha256=capture_sha256,
                capture_monotonic=float(final.capture_monotonic),
                preconstruction_fingerprint=preconstruction_evidence.fingerprint,
            )
            result.verify_seal()
            resume(identity, result, resumed)
            success = True
            return result
        except Exception as exc:
            try:
                report_failure(exc)
            except Exception as restore_exc:
                raise CalibrationError(
                    "native measurement failure restoration was not confirmed"
                ) from restore_exc
            raise
        finally:
            if callable(alignment_exit):
                alignment_exit(alignment_token)
            if not success:
                if active_window:
                    try:
                        self.memory_meter.abort_window()
                    except Exception:
                        pass
                for path in tuple(pending_captures):
                    try:
                        self._release_capture(path)
                    except Exception:
                        pass

    @staticmethod
    def _bind_observation(observation: NativeRenderObservation, frame: Any, path: Path) -> None:
        """Require addon telemetry to describe the exact decoded capture."""

        if (
            observation.boot is None
            or observation.sequence is None
            or isinstance(observation.sequence, bool)
            or observation.sequence <= 0
            or observation.crc32 is None
            or isinstance(observation.crc32, bool)
            or not isinstance(observation.crc32, int)
            or observation.capture_id is None
            or not re.fullmatch(r"[0-9a-f]{64}", observation.capture_id)
            or observation.capture_path is None
            or observation.capture_sha256 is None
            or not re.fullmatch(r"[0-9a-f]{64}", observation.capture_sha256)
            or isinstance(observation.capture_monotonic, bool)
            or not isinstance(observation.capture_monotonic, (int, float))
            or not math.isfinite(float(observation.capture_monotonic))
            or observation.capture_monotonic < 0
        ):
            raise CalibrationError("native render observation lacks complete capture binding")
        capture_id = hashlib.sha256(path.read_bytes()).hexdigest()
        if Path(observation.capture_path) != path:
            raise CalibrationError("native render metadata was not decoded from its retained capture")
        if observation.capture_sha256 != capture_id or observation.capture_id != capture_id:
            raise CalibrationError("native render capture hash does not match decoded pixels")
        if (observation.native_phase is not None or observation.native_ordinal is not None) and (
            observation.native_phase != observation.metrics.get("nativePhase")
            or observation.native_ordinal != observation.metrics.get("nativeOrdinal")
        ):
            raise CalibrationError("native phase metadata does not match sealed render telemetry")
        expected = {
            "boot": (observation.boot, getattr(frame, "boot", None)),
            "sequence": (observation.sequence, getattr(frame, "sequence", None)),
            "crc32": (observation.crc32, getattr(frame, "crc32", None)),
            "capture_id": (observation.capture_id, capture_id),
        }
        for field, (value, actual) in expected.items():
            if value != actual:
                raise CalibrationError(f"native render {field} does not match the decoded frame")
        decoded_metrics = getattr(getattr(frame, "snapshot", None), "value", {}).get("render")
        if not isinstance(decoded_metrics, dict) or decoded_metrics != observation.metrics:
            raise CalibrationError("native render metrics do not match the retained decoded frame")

    @staticmethod
    def _validate_capture_time(
        observation: NativeRenderObservation,
        prior: float,
        *,
        now: float,
        max_age: float = 5.0,
    ) -> None:
        value = observation.capture_monotonic
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or isinstance(now, bool)
            or not isinstance(now, (int, float))
            or not math.isfinite(float(now))
        ):
            raise CalibrationError("native capture timestamp is malformed")
        age = float(now) - float(value)
        if value < 0 or age < 0 or age > max_age or (prior >= 0 and value <= prior):
            raise CalibrationError("native capture timestamp did not advance")

    def _release_capture(self, path: Path) -> None:
        """Release one consumed provider artifact through the lead-owned hook."""

        release = getattr(self.driver, "release_native_capture", None)
        if not callable(release):
            raise CalibrationError("native provider release hook is not configured")
        try:
            release(path)
        except Exception as exc:
            raise CalibrationError(f"native provider could not release consumed capture: {exc}") from exc

    def _capture_observation(self, identity: Any, observation: NativeRenderObservation) -> Path:
        """Use exactly the private artifact named by the provider metadata."""

        del identity
        if observation.capture_path is None or observation.capture_sha256 is None:
            raise CalibrationError("native provider must return a bound capture artifact")
        path = Path(observation.capture_path)
        if not path.is_file():
            raise CalibrationError("native provider retained capture is unavailable")
        observed = hashlib.sha256(path.read_bytes()).hexdigest()
        if observed != observation.capture_sha256:
            raise CalibrationError("native provider retained capture hash changed")
        return path


__all__ = [
    "CalibrationError",
    "CalibrationFingerprint",
    "CalibrationReadiness",
    "InputCalibrationEvidence",
    "InputCalibrationWorkflow",
    "KeyboardProbeOperation",
    "KeyboardProbeStep",
    "KeyboardProfileRow",
    "LinuxProcStatusVmHwmReset5",
    "NativeMemoryArchiveReceipt",
    "NativeMemoryAssessmentArchiveReceipt",
    "NativeCalibrationEvidence",
    "NativeMemoryMeasurement",
    "NativeMemoryResetRecord",
    "NativeResumeBoundary",
    "NativeRestorationReceipt",
    "NativeMemoryWindowToken",
    "NativePreconstructionEvidence",
    "NativeRenderObservation",
    "NativeCalibrationRunner",
    "OwnedGtkCalibrationWindow",
    "OwnedWindow",
    "xtest_sources",
    "xinput_hierarchy",
]
