"""Explicit local IPC and command line controls for the session daemon."""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import socket
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import (
    MAX_RELOAD_DEADLINE_SECONDS,
    load_client_config,
)
from .config import load_config as load_daemon_config
from .controller import Controller, ControllerState, SessionMode
from .daemon import SessionDaemon
from .driver import DriverError
from .motion import (
    DEFAULT_MOVEMENT_CALIBRATION_PULSE_SECONDS,
    MAX_WAYPOINT_SECONDS,
    MOTION_CALIBRATION_OPERATION_SECONDS,
    MOTION_CAMERA_OPERATION_SECONDS,
    MOTION_KEY_OPERATION_SECONDS,
    MOTION_TRANSPORT_MARGIN_SECONDS,
)
from .startup import (
    TAKEOVER_DRILL_OPERATION_TIMEOUT_SECONDS,
    TAKEOVER_DRILL_TRANSPORT_MARGIN_SECONDS,
)
from .supervisor import (
    DEFAULT_FAILURE_BUDGET,
    DEFAULT_MIN_RESUME_INTERVAL_SECONDS,
    DEFAULT_POLL_SECONDS,
    Supervisor,
    SupervisorError,
)
from .supervisor import run as run_supervisor

_IPC_REQUEST_MAX_BYTES = 4096
_IPC_RESPONSE_MAX_BYTES = 524288

# Keep this table next to dispatch so adding a command that reads host state
# requires an explicit review.  IPC commands not listed here are socket-only
# clients and must use load_client_config; motion-profile exits before IPC.
_FULL_CONFIG_COMMANDS: dict[str, str] = {
    "daemon": "constructs the input-capable daemon and validates its local files",
}

CLIENT_NATIVE_CONSTRUCTION_CLEANUP_MARGIN_SECONDS = MOTION_TRANSPORT_MARGIN_SECONDS
CLIENT_NATIVE_CONSTRUCTION_TIMEOUT_SECONDS = (
    float(MAX_RELOAD_DEADLINE_SECONDS) + CLIENT_NATIVE_CONSTRUCTION_CLEANUP_MARGIN_SECONDS
)

# Existing tests and downstream callers patch this module-level seam.  It now
# deliberately names the minimal loader; daemon startup uses load_daemon_config.
load_config = load_client_config


def _parse_hint(value: str) -> tuple[float, float]:
    parts = value.split(",")
    if len(parts) != 2:
        raise argparse.ArgumentTypeError("expected X,Y")
    try:
        result = tuple(float(part) for part in parts)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("hint coordinates must be finite numbers") from exc
    if any(not math.isfinite(item) for item in result):
        raise argparse.ArgumentTypeError("hint coordinates must be finite numbers")
    return result  # type: ignore[return-value]


class SessionIPC:
    def __init__(self, controller: Controller, owner: SessionDaemon | None = None):
        self.controller = controller
        self.owner = owner
        self._interaction_lock = threading.Lock()

    @contextmanager
    def _interaction_slot(self):
        """Reject a concurrent physical intent instead of queueing it."""

        if not self._interaction_lock.acquire(blocking=False):
            raise DriverError("another interaction request is already in flight")
        try:
            yield
        finally:
            self._interaction_lock.release()

    def dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        command = request.get("command")
        if not isinstance(command, str):
            return {"ok": False, "error": "IPC command must be a string"}
        if command == "paused_region_sample":
            if self.owner is None:
                return {"ok": False, "error": "paused region sample requires the daemon"}
            required = {
                "command",
                "generation",
                "parent_started",
                "deadline",
                "config_file_sha256",
                "config_semantic_sha256",
            }
            if set(request) != required:
                return {"ok": False, "error": "paused region request fields are malformed"}
            try:
                return self.owner.capture_paused_region_sample(
                    expected_generation=request["generation"],
                    parent_started=request["parent_started"],
                    deadline=request["deadline"],
                    config_file_sha256=request["config_file_sha256"],
                    config_semantic_sha256=request["config_semantic_sha256"],
                )
            except (RuntimeError, ValueError, KeyError, TypeError) as exc:
                return {"ok": False, "error": str(exc)}
        generation = request.get("generation")
        mutating = {
            "start",
            "pause",
            "stop",
            "closeout",
            "resume",
            "native_bootstrap",
            "native_calibrate",
            "input_calibrate",
            "takeover_drill",
            "takeover_drill_cancel",
        }
        if command == "calibration_status":
            if self.owner is None:
                return {"ok": False, "error": "calibration status requires the daemon"}
            return {"ok": True, "status": self.owner.calibration_status()}

        if (
            command in {"start", "resume", "takeover_drill", "takeover_drill_cancel"}
            and self.owner is not None
        ):
            allowed = {
                "start": {"command", "generation", "mode"},
                "resume": {"command", "generation"},
                "takeover_drill": {"command", "generation"},
                "takeover_drill_cancel": {"command", "generation"},
            }[command]
            if set(request) != allowed:
                return {"ok": False, "error": f"{command} request fields are malformed"}
            if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
                return {"ok": False, "error": "a nonnegative generation is required"}
            with self.controller.lock:
                current = self.controller.status()
                if current.generation != generation:
                    return {"ok": False, "error": "stale generation"}
            try:
                if command == "takeover_drill_cancel":
                    result = self.owner.cancel_takeover_drill()
                else:
                    with self._interaction_slot():
                        if command in {"start", "resume"}:
                            result = self.owner.runtime_start(
                                expected_generation=generation,
                                mode=str(request.get("mode", "repeat")),
                                resume=command == "resume",
                            )
                        else:
                            result = self.owner.run_takeover_drill(expected_generation=generation)
                return {"ok": True, "status": result}
            except (DriverError, RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:
                return {"ok": False, "error": str(exc)}

        construction_commands = {
            "native_construction_prepare",
            "native_construction_control_preflight",
            "native_construction_bootstrap",
            "native_construction_recheck",
            "native_construction_finalize_admit",
            "native_construction_recovery_begin",
            "native_construction_recovery_complete",
            "native_construction_recovery_cancel",
        }
        if command in construction_commands:
            if self.owner is None:
                return {"ok": False, "error": "native construction requires the daemon"}
            allowed_fields = {
                "native_construction_prepare": {"command", "generation"},
                "native_construction_control_preflight": {"command", "generation"},
                "native_construction_bootstrap": {
                    "command",
                    "generation",
                    "reservationId",
                    "expectedConstructorBoot",
                },
                "native_construction_recheck": {"command", "generation", "reservationId"},
                "native_construction_finalize_admit": {"command", "generation", "reservationId"},
                "native_construction_recovery_begin": {"command", "generation", "reservationId"},
                "native_construction_recovery_complete": {"command", "generation", "reservationId"},
                "native_construction_recovery_cancel": {"command", "generation", "reservationId"},
            }[command]
            if set(request) != allowed_fields:
                return {"ok": False, "error": "native construction request fields are malformed"}
            if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
                return {"ok": False, "error": "a positive generation is required"}
            reservation_id = request.get("reservationId")
            if command == "native_construction_recovery_begin":
                if not isinstance(reservation_id, str) or len(reservation_id) > 128:
                    return {"ok": False, "error": "recovery reservationId is malformed"}
            elif command not in {"native_construction_prepare", "native_construction_control_preflight"}:
                if not isinstance(reservation_id, str) or not reservation_id or len(reservation_id) > 128:
                    return {"ok": False, "error": "reservationId is malformed"}
            expected_boot = request.get("expectedConstructorBoot")
            if command == "native_construction_bootstrap" and (
                not isinstance(expected_boot, str) or not expected_boot or len(expected_boot.encode()) > 96
            ):
                return {"ok": False, "error": "expectedConstructorBoot is malformed"}
            try:
                if command == "native_construction_prepare":
                    result = self.owner.native_construction_prepare(expected_generation=generation)
                elif command == "native_construction_control_preflight":
                    result = self.owner.native_construction_control_preflight(expected_generation=generation)
                elif command == "native_construction_bootstrap":
                    result = self.owner.native_construction_bootstrap(
                        expected_generation=generation,
                        reservation_id=reservation_id,
                        expected_constructor_boot=expected_boot,
                    )
                elif command == "native_construction_recheck":
                    result = self.owner.native_construction_recheck(
                        expected_generation=generation, reservation_id=reservation_id
                    )
                elif command == "native_construction_finalize_admit":
                    result = self.owner.native_construction_finalize_admit(
                        expected_generation=generation, reservation_id=reservation_id
                    )
                elif command == "native_construction_recovery_begin":
                    result = self.owner.native_construction_recovery_begin(
                        expected_generation=generation, reservation_id=reservation_id
                    )
                elif command == "native_construction_recovery_complete":
                    result = self.owner.native_construction_recovery_complete(
                        expected_generation=generation, reservation_id=reservation_id
                    )
                else:
                    result = self.owner.native_construction_recovery_cancel(
                        expected_generation=generation, reservation_id=reservation_id
                    )
            except (DriverError, RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True, **result} if isinstance(result, dict) else {"ok": True, "status": result}
        # Calibration includes a live capture or an operator takeover sample.
        # It must run outside the controller mutex so another socket can pause
        # and revoke it while the workflow is waiting.
        if command in {"native_calibrate", "input_calibrate"}:
            if self.owner is None:
                return {"ok": False, "error": "calibration requires the daemon"}
            with self.controller.lock:
                status = self.controller.status()
                if (
                    isinstance(generation, bool)
                    or not isinstance(generation, int)
                    or generation != status.generation
                ):
                    return {"ok": False, "error": "stale generation"}
            try:
                result = self.owner.start_calibration(
                    "native" if command == "native_calibrate" else "input",
                    expected_generation=generation,
                )
            except (RuntimeError, ValueError, KeyError, TypeError) as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "status": result}

        if command == "native_bootstrap":
            if self.owner is None:
                return {"ok": False, "error": "native bootstrap requires the daemon"}
            with self.controller.lock:
                status = self.controller.status()
                if (
                    isinstance(generation, bool)
                    or not isinstance(generation, int)
                    or generation != status.generation
                ):
                    return {"ok": False, "error": "stale generation"}
            try:
                result = self.owner.native_bootstrap(expected_generation=generation)
            except (RuntimeError, ValueError, KeyError, TypeError) as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "status": result}

        if command == "executor_click":
            if self.owner is None:
                return {"ok": False, "error": "executor click requires the daemon"}
            native_flush = getattr(self.owner, "_native_construction_flush_expiry", None)
            if callable(native_flush):
                native_flush()
            native_blocks = getattr(self.owner, "_native_construction_blocks_competing_entry", None)
            if callable(native_blocks) and native_blocks():
                return {"ok": False, "error": "native construction admission is active"}
            required = {"command", "generation", "queueId", "seq", "confirmQueueId", "cycleId"}
            if set(request) != required:
                return {"ok": False, "error": "executor click request fields are malformed"}
            if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
                return {"ok": False, "error": "a positive generation is required"}
            queue_id = request.get("queueId")
            confirm_queue_id = request.get("confirmQueueId")
            cycle_id = request.get("cycleId")
            seq = request.get("seq")
            if (
                not isinstance(queue_id, str)
                or not queue_id
                or len(queue_id.encode("utf-8")) > 192
                or not isinstance(confirm_queue_id, str)
                or len(confirm_queue_id.encode("utf-8")) > 192
                or not isinstance(cycle_id, str)
                or len(cycle_id.encode("utf-8")) > 128
                or isinstance(seq, bool)
                or not isinstance(seq, int)
                or seq <= 0
            ):
                return {"ok": False, "error": "executor click request values are malformed"}
            with self.controller.lock:
                if self.controller.status().generation != generation:
                    return {"ok": False, "error": "stale generation"}
            try:
                with self._interaction_slot():
                    result = self.owner.execute_executor_click(
                        queue_id=queue_id,
                        seq=seq,
                        confirm_queue_id=confirm_queue_id,
                        cycle_id=cycle_id,
                        generation=generation,
                    )
            except (DriverError, RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True, **result}

        if command in {"mail_loop_start", "mail_loop_status", "mail_loop_stop"}:
            if self.owner is None:
                return {"ok": False, "error": "mail loop requires the daemon"}
            allowed = {
                "mail_loop_start": {"command", "generation", "queueId", "seq", "confirmQueueId"},
                "mail_loop_status": {"command"},
                "mail_loop_stop": {"command", "generation"},
            }[command]
            if set(request) != allowed:
                return {"ok": False, "error": "mail-loop request fields are malformed"}
            if command == "mail_loop_status":
                return {"ok": True, "status": self.owner.mail_loop_status()}
            generation = request.get("generation")
            if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
                return {"ok": False, "error": "a positive generation is required"}
            if command == "mail_loop_start":
                queue_id = request.get("queueId")
                seq = request.get("seq")
                confirmation = request.get("confirmQueueId")
                if (
                    not isinstance(queue_id, str)
                    or not queue_id
                    or isinstance(seq, bool)
                    or not isinstance(seq, int)
                    or seq <= 0
                    or confirmation != ""
                ):
                    return {"ok": False, "error": "mail-loop start values are malformed"}
                try:
                    if len(queue_id.encode("utf-8")) > 192:
                        return {"ok": False, "error": "mail-loop start values are malformed"}
                except UnicodeError:
                    return {"ok": False, "error": "mail-loop start values are malformed"}
            try:
                if command == "mail_loop_start":
                    status = self.owner.mail_loop_start(
                        generation=generation,
                        queue_id=queue_id,
                        seq=seq,
                        confirm_queue_id=confirmation,
                    )
                else:
                    status = self.owner.mail_loop_stop(generation=generation)
            except (DriverError, RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:
                return {"ok": False, "error": str(exc)}
            return {"ok": True, "status": status}

        # Re-arming validates the retained native evidence and starts the live
        # observer.  Those operations can block on X11 and must run outside
        # the controller mutex; Controller.resume repeats the generation check
        # at its own admission boundary.
        if command == "resume":
            with self.controller.lock:
                status = self.controller.status()
                if (
                    isinstance(generation, bool)
                    or not isinstance(generation, int)
                    or generation != status.generation
                ):
                    return {"ok": False, "error": "stale generation"}
            try:
                result = self.controller.resume(expected_generation=generation)
            except (RuntimeError, ValueError, KeyError, TypeError) as exc:
                return {"ok": False, "error": str(exc)}
            return {
                "ok": True,
                "status": {
                    "state": result.state.value,
                    "mode": result.mode.value,
                    "generation": result.generation,
                    "leaseGeneration": result.lease_generation,
                    "stopRequested": result.stop_requested,
                    "closeoutRequested": result.closeout_requested,
                    "streak": result.streak,
                    "blocker": result.blocker,
                    "lastUid": result.last_uid,
                    "startedAt": result.started_at,
                    "expectedBoot": result.expected_boot,
                    "actionIntent": result.action_intent,
                },
            }

        # Interaction intents are an explicit operator-owned mode.  They are
        # admitted outside the controller mutex because native capture and a
        # bounded xdotool chain may block; every owner method repeats the
        # generation/current-mode fence before and after the physical action.
        interaction_commands = {
            "interaction_start",
            "interaction_stop",
            "motion_key",
            "motion_camera",
            "motion_camera_calibrate",
            "motion_movement_calibrate",
            "motion_waypoint",
            "route_walk",
            "world_click",
        }
        if command in interaction_commands:
            if self.owner is None:
                return {"ok": False, "error": "interaction requires the daemon"}
            native_flush = getattr(self.owner, "_native_construction_flush_expiry", None)
            if callable(native_flush):
                native_flush()
            native_blocks = getattr(self.owner, "_native_construction_blocks_competing_entry", None)
            if callable(native_blocks) and native_blocks():
                return {"ok": False, "error": "native construction admission is active"}
            if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
                return {"ok": False, "error": "a positive generation is required"}
            allowed_fields = {
                "interaction_start": {"command", "generation"},
                "interaction_stop": {"command", "generation"},
                "motion_key": {"command", "generation", "role", "duration"},
                "motion_camera": {"command", "generation", "deltaPixels"},
                "motion_camera_calibrate": {"command", "generation", "deltaPixels"},
                "motion_movement_calibrate": {"command", "generation", "pulseSeconds"},
                "motion_waypoint": {"command", "generation", "x", "y"},
                "route_walk": {"command", "generation", "route"},
                "world_click": {
                    "command",
                    "generation",
                    "target",
                    "hintX",
                    "hintY",
                    "radius",
                    "expect",
                },
            }[command]
            if set(request) != allowed_fields:
                return {"ok": False, "error": "interaction request fields are malformed"}
            try:
                if command == "interaction_start":
                    with self._interaction_slot():
                        motion_fence = self.owner._claim_motion_fence()
                        if motion_fence is None:
                            raise DriverError("another motion intent is already in flight")
                        try:
                            self.controller.enter_interaction(expected_generation=generation)
                        finally:
                            motion_fence.release()
                    return {"ok": True, "status": self.owner.status_dict()}
                if command == "interaction_stop":
                    # Stop is the cancellation boundary.  It must revoke the
                    # active lease immediately rather than queue behind a
                    # long native motion holding the intent slot.
                    self.controller.exit_interaction(expected_generation=generation)
                    return {"ok": True, "status": self.owner.status_dict()}
                if command == "motion_key":
                    from .executor import KeyRole

                    try:
                        role = KeyRole(request["role"])
                    except (TypeError, ValueError) as exc:
                        raise DriverError("motion key role is not allowlisted") from exc
                    duration = request["duration"]
                    if (
                        isinstance(duration, bool)
                        or not isinstance(duration, (int, float))
                        or not math.isfinite(float(duration))
                    ):
                        raise DriverError("motion key duration is malformed")
                    with self._interaction_slot():
                        frame = self.owner.execute_motion_key(role, float(duration), generation=generation)
                elif command == "motion_camera":
                    delta = request["deltaPixels"]
                    with self._interaction_slot():
                        frame = self.owner.execute_camera_delta(delta, generation=generation)
                elif command == "motion_camera_calibrate":
                    delta = request["deltaPixels"]
                    with self._interaction_slot():
                        mapping = self.owner.calibrate_camera_mapping(delta, generation=generation)
                    return {
                        "ok": True,
                        "status": self.owner.status_dict(),
                        "camera": {
                            "identity": mapping.identity_fingerprint,
                            "generation": mapping.generation,
                            "captureId": mapping.capture_id,
                            "yawPerPixel": mapping.yaw_per_pixel,
                            "convention": mapping.convention,
                            "boot": mapping.boot,
                        },
                    }
                elif command == "motion_movement_calibrate":
                    pulse = request["pulseSeconds"]
                    if (
                        isinstance(pulse, bool)
                        or not isinstance(pulse, (int, float))
                        or not math.isfinite(float(pulse))
                    ):
                        raise DriverError("movement basis calibration pulse is malformed")
                    with self._interaction_slot():
                        basis = self.owner.calibrate_movement_basis(float(pulse), generation=generation)
                    return {
                        "ok": True,
                        "status": self.owner.status_dict(),
                        "movement": {
                            "identity": basis.identity_fingerprint,
                            "boot": basis.boot,
                            "captureId": basis.capture_id,
                            "beforeSequence": basis.before_sequence,
                            "afterSequence": basis.after_sequence,
                            "forwardHeadingOffset": basis.forward_heading_offset,
                            "facingSign": basis.facing_sign,
                            "forwardDisplacement": basis.forward_displacement,
                            "forwardPulseSeconds": basis.pulse_seconds,
                            "strafeHeadingOffset": basis.strafe_heading_offset,
                            "strafeDisplacement": basis.strafe_displacement,
                            "strafePulseSeconds": basis.strafe_pulse_seconds,
                            "backwardHeadingOffset": basis.backward_heading_offset,
                            "backwardDisplacement": basis.backward_displacement,
                            "backwardPulseSeconds": basis.backward_pulse_seconds,
                            "strafeRightHeadingOffset": basis.strafe_right_heading_offset,
                            "strafeRightDisplacement": basis.strafe_right_displacement,
                            "strafeRightPulseSeconds": basis.strafe_right_pulse_seconds,
                            "handedness": basis.handedness,
                            "convention": basis.convention,
                        },
                    }
                elif command == "route_walk":
                    route = request["route"]
                    if not isinstance(route, str) or not route or len(route) > 128:
                        raise DriverError("route name is malformed")
                    with self._interaction_slot():
                        frame = self.owner.execute_route_walk(route, generation=generation)
                    result = self.owner._motion_result(frame)
                    return {
                        "ok": True,
                        "status": self.owner.status_dict(),
                        "motion": result,
                        "hover": result["hover"],
                    }
                elif command == "world_click":
                    target = request["target"]
                    hint_x = request["hintX"]
                    hint_y = request["hintY"]
                    radius = request["radius"]
                    expect = request["expect"]
                    if not isinstance(target, str) or not target or len(target.encode("utf-8")) > 48:
                        raise DriverError("world click target name is malformed")
                    if (
                        any(
                            isinstance(value, bool)
                            or not isinstance(value, (int, float))
                            or not math.isfinite(float(value))
                            for value in (hint_x, hint_y, radius)
                        )
                        or not 0.0 <= float(radius) <= 300.0
                    ):
                        raise DriverError("world click probe geometry is malformed")
                    if expect != "mailOpen":
                        raise DriverError("world click expected state is not allowlisted")
                    with self._interaction_slot():
                        frame = self.owner.execute_world_click(
                            target,
                            float(hint_x),
                            float(hint_y),
                            float(radius),
                            expect=expect,
                            generation=generation,
                        )
                    result = self.owner._motion_result(frame)
                    return {
                        "ok": True,
                        "status": self.owner.status_dict(),
                        "motion": result,
                        "hover": result["hover"],
                    }
                else:
                    target_x = request["x"]
                    target_y = request["y"]
                    if (
                        isinstance(target_x, bool)
                        or not isinstance(target_x, (int, float))
                        or not math.isfinite(float(target_x))
                        or isinstance(target_y, bool)
                        or not isinstance(target_y, (int, float))
                        or not math.isfinite(float(target_y))
                    ):
                        raise DriverError("waypoint coordinates must be finite numbers")
                    with self._interaction_slot():
                        frame = self.owner.execute_waypoint(
                            float(target_x), float(target_y), generation=generation
                        )
                return {
                    "ok": True,
                    "status": self.owner.status_dict(),
                    "motion": {
                        "boot": frame.boot,
                        "sequence": frame.sequence,
                        "identity": frame.identity_fingerprint,
                        "zone": frame.zone,
                        "instance": frame.instance,
                        "x": frame.x,
                        "y": frame.y,
                        "z": frame.z,
                        "facing": frame.facing,
                        "speed": frame.speed,
                    },
                }
            except (DriverError, RuntimeError, ValueError, TypeError, KeyError, OSError) as exc:
                return {"ok": False, "error": str(exc)}

        with self.controller.lock:
            try:
                status = self.controller.status()
                if command in mutating and (
                    isinstance(generation, bool)
                    or not isinstance(generation, int)
                    or generation != status.generation
                ):
                    return {"ok": False, "error": "stale generation"}
                # Re-read while still holding the controller lock.  This
                # closes the check/mutate window when an operator takeover
                # revokes the generation between request admission and the
                # requested transition.
                if command in mutating:
                    admitted = self.controller.status()
                    if admitted.generation != status.generation or (
                        admitted.state in {ControllerState.PAUSED, ControllerState.BLOCKED}
                        and command == "start"
                    ):
                        return {"ok": False, "error": "stale generation"}
                if command == "status":
                    result = self.controller.status()
                elif command == "start":
                    result = self.controller.start(SessionMode(request.get("mode", "repeat")))
                elif command == "pause":
                    result = self.controller.pause(request.get("reason", "IPC pause"))
                elif command == "stop":
                    result = self.controller.stop()
                elif command == "closeout":
                    result = self.controller.closeout()
                else:
                    return {"ok": False, "error": "unknown command"}
            except (RuntimeError, ValueError, KeyError, TypeError) as exc:
                return {"ok": False, "error": str(exc)}
        if command == "status" and self.owner is not None:
            # ``SessionDaemon.status_dict`` takes the calibration-state lock
            # before it reads controller/journal state.  Never hold the
            # controller lock while asking the owner for that aggregate
            # snapshot: calibration completion takes the locks in the
            # opposite direction and a status poll must not deadlock it.
            return {"ok": True, "status": self.owner.status_dict()}
        return {
            "ok": True,
            "status": {
                "state": result.state.value,
                "mode": result.mode.value,
                "generation": result.generation,
                "leaseGeneration": result.lease_generation,
                "stopRequested": result.stop_requested,
                "closeoutRequested": result.closeout_requested,
                "streak": result.streak,
                "blocker": result.blocker,
                "lastUid": result.last_uid,
                "startedAt": result.started_at,
                "expectedBoot": result.expected_boot,
                "actionIntent": result.action_intent,
            },
        }


def serve(
    socket_path: str | os.PathLike[str],
    controller: Controller,
    owner: SessionDaemon | None = None,
    *,
    supervise_owner: bool = False,
    _lease_fd: int | None = None,
) -> None:
    path = Path(socket_path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lease_path = path.with_name(path.name + ".lock")
    lease_fd = _lease_fd
    if lease_fd is None:
        lease_fd = os.open(lease_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(lease_fd)
            raise RuntimeError("another Crowley session daemon owns the process lease") from exc
    assert lease_fd is not None
    bound = False
    owner_thread = None
    handler_threads: set[threading.Thread] = set()
    handler_connections: set[socket.socket] = set()
    handler_lock = threading.RLock()
    shutdown_incomplete = False
    try:
        # The process lease is held before touching the socket.  Once it is
        # exclusive, an existing socket is necessarily stale and can be
        # removed without disrupting another daemon.
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
            # A live socket is never unlinked. A stale socket is an operator
            # cleanup issue because removing it could disrupt another daemon.
            server.bind(str(path))
            bound = True
            os.chmod(path, 0o600)
            server.listen(4)
            ipc = SessionIPC(controller, owner)

            owner_failure: list[BaseException] = []
            if supervise_owner and owner is not None:

                def run_owner() -> None:
                    try:
                        owner.run_forever()
                    except BaseException as exc:  # propagate service failure to the socket loop
                        owner_failure.append(exc)

                owner_thread = threading.Thread(target=run_owner, name="crowley-session-loop")
                owner_thread.start()

            def handle(connection: socket.socket) -> None:
                try:
                    with connection:
                        reader = _BoundedIPCReader(connection)
                        while True:
                            try:
                                raw = reader.read_record(_IPC_REQUEST_MAX_BYTES)
                                if raw is None:
                                    break
                                request = _parse_bounded_json(raw)
                                if not isinstance(request, dict):
                                    raise TypeError("IPC request must be a JSON object")
                                response = ipc.dispatch(request)
                            except (
                                json.JSONDecodeError,
                                TypeError,
                                AttributeError,
                                OSError,
                                ValueError,
                            ) as exc:
                                response = {"ok": False, "error": str(exc)[:256]}
                            try:
                                encoded = _encode_bounded_record(response, _IPC_RESPONSE_MAX_BYTES)
                            except ValueError:
                                encoded = b'{"error":"IPC response exceeds its byte limit","ok":false}\n'
                            connection.sendall(encoded)
                            if reader.failed:
                                break
                except (OSError, TimeoutError):
                    pass
                finally:
                    with handler_lock:
                        handler_connections.discard(connection)
                        handler_threads.discard(threading.current_thread())

            server.settimeout(0.5 if supervise_owner else None)
            while True:
                if owner_failure:
                    raise RuntimeError("session capture/action loop failed") from owner_failure[0]
                try:
                    connection, _ = server.accept()
                except TimeoutError:
                    continue
                with handler_lock:
                    handler_connections.add(connection)
                    worker = threading.Thread(target=handle, args=(connection,), daemon=True)
                    handler_threads.add(worker)
                worker.start()
    finally:
        # Closing the listening socket is handled by the context manager.  Any
        # accepted connection may still be blocked in its line reader, so
        # revoke those descriptors and confirm every handler has exited before
        # releasing the process lease.
        with handler_lock:
            connections = tuple(handler_connections)
            handlers = tuple(handler_threads)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
        for handler in handlers:
            if handler is not threading.current_thread():
                handler.join(timeout=5)
        with handler_lock:
            shutdown_incomplete = any(handler.is_alive() for handler in handler_threads)
        if supervise_owner and owner is not None:
            request_shutdown = getattr(owner, "request_shutdown", None)
            shutdown_error: BaseException | None = None
            if callable(request_shutdown):
                try:
                    request_shutdown()
                except BaseException as exc:
                    shutdown_error = exc
            if owner_thread is not None and owner_thread is not threading.current_thread():
                owner_thread.join(timeout=5)
                shutdown_incomplete = shutdown_incomplete or owner_thread.is_alive()
            if shutdown_error is not None:
                shutdown_incomplete = True
                if not owner_thread or not owner_thread.is_alive():
                    raise RuntimeError("session owner shutdown failed") from shutdown_error
        if bound and not shutdown_incomplete:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        if shutdown_incomplete:
            raise RuntimeError("IPC shutdown did not confirm owner and handler termination")
        fcntl.flock(lease_fd, fcntl.LOCK_UN)
        os.close(lease_fd)


def _open_process_lease(socket_path: str | os.PathLike[str]) -> tuple[int, Path]:
    """Acquire the service lease before constructing the session owner."""

    path = Path(socket_path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lease_path = path.with_name(path.name + ".lock")
    lease_fd = os.open(lease_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lease_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(lease_fd)
        raise RuntimeError("another Crowley session daemon owns the process lease") from exc
    os.fchmod(lease_fd, 0o600)
    return lease_fd, lease_path


def _socket_request(
    path: Path,
    request: dict[str, Any],
    *,
    timeout: float = 5.0,
    response_deadline: float | None = None,
    failure_evidence_grace: float = 0.0,
) -> dict[str, Any]:
    started = time.monotonic()
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(float(timeout))
    ):
        raise ValueError("IPC timeout must be a finite number")
    if timeout <= 0:
        raise ValueError("IPC timeout must be positive")
    if (
        isinstance(failure_evidence_grace, bool)
        or not isinstance(failure_evidence_grace, (int, float))
        or not math.isfinite(float(failure_evidence_grace))
    ):
        raise ValueError("IPC evidence grace must be a finite number")
    if failure_evidence_grace < 0 or failure_evidence_grace > 0.25:
        raise ValueError("IPC failure evidence grace must be between zero and 250ms")
    if failure_evidence_grace and request.get("command") != "paused_region_sample":
        raise ValueError("IPC failure evidence grace is limited to paused region capture")
    request_deadline = started + float(timeout) if response_deadline is None else response_deadline
    if (
        isinstance(request_deadline, bool)
        or not isinstance(request_deadline, (int, float))
        or not math.isfinite(float(request_deadline))
    ):
        raise ValueError("IPC response deadline must be a finite monotonic timestamp")
    request_deadline = float(request_deadline)
    if request_deadline <= started:
        raise TimeoutError("IPC absolute response deadline has already expired")
    if response_deadline is not None and failure_evidence_grace:
        request_sample_deadline = request.get("deadline")
        if (
            isinstance(request_sample_deadline, bool)
            or not isinstance(request_sample_deadline, (int, float))
            or float(request_sample_deadline) != request_deadline
        ):
            raise ValueError("IPC evidence grace must match the paused sample deadline")
    response_read_deadline = request_deadline + float(failure_evidence_grace)

    def set_remaining_timeout(connection: socket.socket, deadline: float) -> None:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("IPC absolute response deadline expired")
        connection.settimeout(remaining)

    encoded_request = _encode_bounded_record(request, _IPC_REQUEST_MAX_BYTES)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        set_remaining_timeout(connection, request_deadline)
        connection.connect(str(path))
        set_remaining_timeout(connection, request_deadline)
        connection.sendall(encoded_request)
        connection.shutdown(socket.SHUT_WR)
        reader = _BoundedIPCReader(connection)
        raw = reader.read_record(_IPC_RESPONSE_MAX_BYTES, deadline_monotonic=response_read_deadline)
        if raw is None:
            raise RuntimeError("IPC response is truncated")
        if reader.buffer:
            raise RuntimeError("IPC response has trailing data after its record")
        while True:
            set_remaining_timeout(connection, response_read_deadline)
            try:
                trailing = connection.recv(1)
            except TimeoutError as exc:
                raise TimeoutError(
                    "IPC absolute response deadline expired while awaiting record EOF"
                ) from exc
            if not trailing:
                break
            raise RuntimeError("IPC response has trailing data after its record")
    response = _parse_bounded_json(raw)
    if not isinstance(response, dict):
        raise RuntimeError("IPC response must be a JSON object")
    return response


class _BoundedIPCReader:
    """Read complete UTF-8 newline records without unbounded buffering."""

    def __init__(self, connection: socket.socket):
        self.connection = connection
        self.buffer = bytearray()
        self.failed = False

    def read_record(self, limit: int, *, deadline_monotonic: float | None = None) -> bytes | None:
        while True:
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                if newline + 1 > limit:
                    self.failed = True
                    raise ValueError("IPC record exceeds its byte limit")
                record = bytes(self.buffer[:newline])
                del self.buffer[: newline + 1]
                return record
            if len(self.buffer) >= limit:
                self.failed = True
                raise ValueError("IPC record is unterminated or exceeds its byte limit")
            if deadline_monotonic is not None:
                remaining = deadline_monotonic - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("IPC absolute response deadline expired while reading a record")
                self.connection.settimeout(remaining)
            try:
                chunk = self.connection.recv(min(8192, limit - len(self.buffer) + 1))
            except TimeoutError as exc:
                if deadline_monotonic is None:
                    raise
                raise TimeoutError("IPC absolute response deadline expired while reading a record") from exc
            if not chunk:
                if not self.buffer:
                    return None
                self.failed = True
                raise ValueError("IPC record is truncated before newline")
            self.buffer.extend(chunk)


def _parse_bounded_json(record: bytes) -> Any:
    try:
        source = record.decode("utf-8", errors="strict")
        return json.loads(
            source,
            object_pairs_hook=_json_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"IPC record is not strict JSON: {exc}") from exc


def _encode_bounded_record(value: Any, limit: int) -> bytes:
    try:
        encoded = (json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode(
            "utf-8"
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(f"IPC value is not bounded JSON: {exc}") from exc
    if len(encoded) > limit:
        raise ValueError(f"IPC record exceeds its {limit}-byte limit")
    return encoded


def _json_object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _motion_demonstrations(source: str) -> tuple[Any, ...]:
    """Parse explicit numeric demonstration summaries for profile building."""

    from .executor import DemonstrationRole, MotionDemonstration
    from .preferences import MAX_INLINE_JSON_BYTES

    if len(source.encode("utf-8")) > MAX_INLINE_JSON_BYTES:
        raise ValueError(f"demonstrations exceed the maximum JSON size of {MAX_INLINE_JSON_BYTES} bytes")

    try:
        decoded = json.loads(
            source,
            object_pairs_hook=_json_object_without_duplicates,
            parse_constant=_reject_json_constant,
        )
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"demonstrations must be valid JSON: {exc}") from exc
    if not isinstance(decoded, list) or not decoded:
        raise ValueError("demonstrations must be a non-empty JSON array")
    if len(decoded) > 256:
        raise ValueError("demonstrations exceed the maximum count of 256")
    expected = frozenset({"role", "distance", "duration", "target_width", "dwell", "curvature"})
    result = []
    for index, item in enumerate(decoded):
        if not isinstance(item, dict):
            raise ValueError(f"demonstration {index} must be an object")
        actual = frozenset(item)
        missing = expected - actual
        unknown = actual - expected
        if missing:
            raise ValueError(f"demonstration {index} is missing fields: {', '.join(sorted(missing))}")
        if unknown:
            raise ValueError(f"demonstration {index} has unknown fields: {', '.join(sorted(unknown))}")
        try:
            role = DemonstrationRole(item["role"])
            result.append(
                MotionDemonstration(
                    role=role,
                    distance=item["distance"],
                    duration=item["duration"],
                    target_width=item["target_width"],
                    dwell=item["dwell"],
                    curvature=item["curvature"],
                )
            )
        except (OverflowError, TypeError, ValueError) as exc:
            raise ValueError(f"demonstration {index} is outside its bounds: {exc}") from exc
    return tuple(result)


def _motion_profile_command(args: argparse.Namespace) -> int:
    """Run profile storage commands without loading daemon configuration."""

    from .executor import aggregate_motion_profile
    from .preferences import (
        PreferenceError,
        load_motion_profile,
        motion_profile_document,
        save_motion_profile,
    )

    command = args.motion_profile_command
    if command == "build":
        demonstrations = _motion_demonstrations(args.demonstrations)
        profile = aggregate_motion_profile(demonstrations)
        save_motion_profile(args.output, profile, demonstration_count=len(demonstrations))
        document = motion_profile_document(args.output)
    elif command == "show":
        # Loading validates the selected file and never falls back to a
        # baseline profile.  Keep the explicit call visible in this command.
        load_motion_profile(args.profile)
        document = motion_profile_document(args.profile)
    elif command == "reset":
        profile_path = Path(args.profile)
        if profile_path.exists():
            if not profile_path.is_file():
                raise PreferenceError("existing profile target is not a regular file")
            load_motion_profile(profile_path)
            profile_path.unlink()
        document = {"ok": True, "reset": True}
    else:  # pragma: no cover - argparse enforces the closed set
        raise PreferenceError("unknown motion profile command")
    print(json.dumps(document, sort_keys=True, separators=(",", ":")))
    return 0


def _supervise_command(args: argparse.Namespace, socket_path: Path) -> int:
    """Resume documented transient blockers; see ``supervisor.py``."""

    def request(payload: dict[str, Any]) -> dict[str, Any]:
        # Same per-command transport bounds as the status/resume subcommands.
        timeout = 20.0 if payload["command"] == "resume" else 5.0
        try:
            return _socket_request(socket_path, payload, timeout=timeout)
        except TimeoutError:
            return {"ok": False, "error": "IPC operation timed out"}
        except (ConnectionRefusedError, FileNotFoundError, json.JSONDecodeError):
            return {"ok": False, "error": "session daemon is not running"}
        except OSError as exc:
            return {"ok": False, "error": f"IPC transport failed: {exc}"}

    try:
        supervisor = Supervisor(
            request,
            min_resume_interval=args.min_resume_interval,
            failure_budget=args.failure_budget,
            out=sys.stdout,
        )
        run_supervisor(supervisor, poll_seconds=args.poll_seconds)
    except (SupervisorError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="crowley-session")
    parser.add_argument("--config", default="~/.config/crowley/session.toml")
    parser.add_argument("--journal")
    parser.add_argument("--socket")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("status", "start", "resume", "pause", "stop", "closeout"):
        sub = subparsers.add_parser(command)
        if command == "start":
            sub.add_argument("--mode", choices=[mode.value for mode in SessionMode], default="repeat")
        if command in {"start", "resume", "pause", "stop", "closeout"}:
            sub.add_argument("--generation", type=int, required=True)
    for command in ("takeover-drill", "takeover-drill-cancel"):
        subparsers.add_parser(command).add_argument("--generation", type=int, required=True)
    subparsers.add_parser("daemon")
    supervise = subparsers.add_parser("supervise")
    supervise.add_argument("--poll-seconds", type=float, default=DEFAULT_POLL_SECONDS)
    supervise.add_argument("--min-resume-interval", type=float, default=DEFAULT_MIN_RESUME_INTERVAL_SECONDS)
    supervise.add_argument("--failure-budget", type=int, default=DEFAULT_FAILURE_BUDGET)
    subparsers.add_parser("native-bootstrap").add_argument("--generation", type=int, required=True)
    subparsers.add_parser("native-construction-prepare").add_argument("--generation", type=int, required=True)
    subparsers.add_parser("native-construction-control-preflight").add_argument(
        "--generation", type=int, required=True
    )
    native_construction_bootstrap = subparsers.add_parser("native-construction-bootstrap")
    native_construction_bootstrap.add_argument("--generation", type=int, required=True)
    native_construction_bootstrap.add_argument("--reservation-id", required=True)
    native_construction_bootstrap.add_argument("--expected-constructor-boot", required=True)
    for command in ("native-construction-recheck", "native-construction-finalize-admit"):
        native_admit = subparsers.add_parser(command)
        native_admit.add_argument("--generation", type=int, required=True)
        native_admit.add_argument("--reservation-id", required=True)
    native_recovery_begin = subparsers.add_parser("native-construction-recovery-begin")
    native_recovery_begin.add_argument("--generation", type=int, required=True)
    native_recovery_begin.add_argument("--reservation-id", default="")
    for command in ("native-construction-recovery-complete", "native-construction-recovery-cancel"):
        native_recovery = subparsers.add_parser(command)
        native_recovery.add_argument("--generation", type=int, required=True)
        native_recovery.add_argument("--reservation-id", required=True)
    for command in ("native-calibrate", "input-calibrate"):
        gate = subparsers.add_parser(command)
        gate.add_argument("--generation", type=int, required=True)
    subparsers.add_parser("interaction-start").add_argument("--generation", type=int, required=True)
    subparsers.add_parser("interaction-stop").add_argument("--generation", type=int, required=True)
    motion_key = subparsers.add_parser("motion-key")
    motion_key.add_argument("--generation", type=int, required=True)
    motion_key.add_argument("--role", required=True)
    motion_key.add_argument("--duration", type=float, required=True)
    motion_camera = subparsers.add_parser("motion-camera")
    motion_camera.add_argument("--generation", type=int, required=True)
    motion_camera.add_argument("--delta-pixels", type=int, required=True)
    camera_calibrate = subparsers.add_parser("motion-camera-calibrate")
    camera_calibrate.add_argument("--generation", type=int, required=True)
    camera_calibrate.add_argument("--delta-pixels", type=int, required=True)
    movement_calibrate = subparsers.add_parser("motion-movement-calibrate")
    movement_calibrate.add_argument("--generation", type=int, required=True)
    movement_calibrate.add_argument(
        "--pulse-seconds", type=float, default=DEFAULT_MOVEMENT_CALIBRATION_PULSE_SECONDS
    )
    motion_waypoint = subparsers.add_parser("motion-waypoint")
    motion_waypoint.add_argument("--generation", type=int, required=True)
    motion_waypoint.add_argument("--x", type=float, required=True)
    motion_waypoint.add_argument("--y", type=float, required=True)
    route_walk = subparsers.add_parser("route-walk")
    route_walk.add_argument("--generation", type=int, required=True)
    route_walk.add_argument("--route", required=True)
    world_click = subparsers.add_parser("world-click")
    world_click.add_argument("--generation", type=int, required=True)
    world_click.add_argument("--target", required=True)
    world_click.add_argument("--hint", type=_parse_hint, required=True)
    world_click.add_argument("--radius", type=float, required=True)
    world_click.add_argument("--expect", choices=("mailOpen",), required=True)
    executor_click = subparsers.add_parser("executor-click")
    executor_click.add_argument("--generation", type=int, required=True)
    executor_click.add_argument("--queue-id", required=True)
    executor_click.add_argument("--seq", type=int, required=True)
    executor_click.add_argument("--confirm-queue-id", required=True)
    executor_click.add_argument("--cycle-id", required=True)
    mail_loop_start = subparsers.add_parser("mail-loop-start")
    mail_loop_start.add_argument("--generation", type=int, required=True)
    mail_loop_start.add_argument("--queue-id", required=True)
    mail_loop_start.add_argument("--seq", type=int, required=True)
    mail_loop_status = subparsers.add_parser("mail-loop-status")
    del mail_loop_status
    mail_loop_stop = subparsers.add_parser("mail-loop-stop")
    mail_loop_stop.add_argument("--generation", type=int, required=True)
    subparsers.add_parser("calibration-status")
    motion_profile = subparsers.add_parser("motion-profile")
    motion_profile_subparsers = motion_profile.add_subparsers(dest="motion_profile_command", required=True)
    build = motion_profile_subparsers.add_parser("build")
    build.add_argument("--demonstrations", required=True, help="explicit numeric demonstration JSON")
    build.add_argument("--output", required=True, type=Path)
    show = motion_profile_subparsers.add_parser("show")
    show.add_argument("--profile", required=True, type=Path)
    reset = motion_profile_subparsers.add_parser("reset")
    reset.add_argument("--profile", required=True, type=Path)
    args = parser.parse_args(argv)
    if args.command == "motion-profile":
        try:
            return _motion_profile_command(args)
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    if args.command in _FULL_CONFIG_COMMANDS:
        config = load_daemon_config(
            Path(args.config).expanduser(),
            socket_override=args.socket if args.socket else None,
        )
        if args.journal:
            config = replace(
                config,
                journal=Path(args.journal).expanduser(),
            )
        # Take ownership before constructing the controller, journal, or
        # input-capable driver.  A competing service therefore cannot read or
        # mutate durable state while this process is still deciding whether it
        # owns the desktop.
        lease_fd, _lease_path = _open_process_lease(config.socket)
        daemon: SessionDaemon | None = None
        try:
            daemon = SessionDaemon(config)
            # Keep socket binding and its process lease on the service's main
            # thread. A stale/live socket or a second lease must terminate the
            # service so systemd can apply its restart policy; a daemon-threaded
            # socket failure must never leave an input-capable loop stranded.
            serve(
                config.socket,
                daemon.controller,
                daemon,
                supervise_owner=True,
                _lease_fd=lease_fd,
            )
        finally:
            if daemon is None:
                fcntl.flock(lease_fd, fcntl.LOCK_UN)
                os.close(lease_fd)
        return 0
    # IPC clients intentionally do not inspect daemon profile, route, game,
    # executable, or SavedVariables settings.
    client_config = (
        load_config(Path(args.config).expanduser(), socket_override=args.socket)
        if args.socket
        else load_config(Path(args.config).expanduser())
    )
    if args.command == "supervise":
        return _supervise_command(args, client_config.socket)
    request = {"command": args.command.replace("-", "_")}
    if hasattr(args, "mode"):
        request["mode"] = args.mode
    if args.command == "motion-key":
        request.update(role=args.role, duration=args.duration)
    elif args.command == "motion-camera":
        request["deltaPixels"] = args.delta_pixels
    elif args.command == "motion-camera-calibrate":
        request["deltaPixels"] = args.delta_pixels
    elif args.command == "motion-movement-calibrate":
        request["pulseSeconds"] = args.pulse_seconds
    elif args.command == "motion-waypoint":
        request.update(x=args.x, y=args.y)
    elif args.command == "route-walk":
        request["route"] = args.route
    elif args.command == "world-click":
        request.update(
            target=args.target,
            hintX=args.hint[0],
            hintY=args.hint[1],
            radius=args.radius,
            expect=args.expect,
        )
    elif args.command == "executor-click":
        request.update(
            queueId=args.queue_id,
            seq=args.seq,
            confirmQueueId=args.confirm_queue_id,
            cycleId=args.cycle_id,
        )
    elif args.command == "mail-loop-start":
        request.update(
            queueId=args.queue_id,
            seq=args.seq,
            confirmQueueId="",
        )
    elif args.command == "mail-loop-stop":
        pass
    if args.command == "native-construction-bootstrap":
        request.update(
            reservationId=args.reservation_id,
            expectedConstructorBoot=args.expected_constructor_boot,
        )
    elif args.command in {
        "native-construction-recheck",
        "native-construction-finalize-admit",
        "native-construction-recovery-complete",
        "native-construction-recovery-cancel",
    }:
        request["reservationId"] = args.reservation_id
    elif args.command == "native-construction-recovery-begin":
        request["reservationId"] = args.reservation_id
    if getattr(args, "generation", None) is not None:
        request["generation"] = args.generation
    try:
        # Native motion owns one bounded server operation.  The transport
        # timeout must leave room for its final observation/release cleanup;
        # a client timeout must never outlive the daemon's ownership decision.
        if args.command.startswith("native-construction-"):
            # This bound covers the daemon's maximum accepted reload deadline
            # and the cleanup margin without loading daemon configuration.
            operation_timeout = CLIENT_NATIVE_CONSTRUCTION_TIMEOUT_SECONDS
        elif args.command == "takeover-drill":
            operation_timeout = TAKEOVER_DRILL_OPERATION_TIMEOUT_SECONDS
        elif args.command in {"start", "resume"}:
            operation_timeout = 20.0
        elif args.command == "motion-movement-calibrate":
            operation_timeout = MOTION_CALIBRATION_OPERATION_SECONDS
        elif args.command == "motion-waypoint":
            operation_timeout = MAX_WAYPOINT_SECONDS
        elif args.command in {"motion-camera", "motion-camera-calibrate"}:
            operation_timeout = MOTION_CAMERA_OPERATION_SECONDS
        elif args.command == "motion-key":
            operation_timeout = MOTION_KEY_OPERATION_SECONDS
        elif args.command in {"route-walk", "world-click", "executor-click"}:
            operation_timeout = 60.0
        else:
            operation_timeout = 5.0
        if args.command.startswith("native-construction-"):
            request_timeout = CLIENT_NATIVE_CONSTRUCTION_TIMEOUT_SECONDS
        else:
            request_timeout = (
                operation_timeout + MOTION_TRANSPORT_MARGIN_SECONDS
                if args.command.startswith("motion-")
                or args.command in {"route-walk", "world-click", "executor-click"}
                else operation_timeout
            )
        if args.command == "takeover-drill":
            request_timeout += TAKEOVER_DRILL_TRANSPORT_MARGIN_SECONDS
        response = _socket_request(client_config.socket, request, timeout=request_timeout)
    except TimeoutError:
        response = {
            "ok": False,
            "error": "IPC operation timed out; its completion and input-release state are indeterminate",
        }
    except (ConnectionRefusedError, FileNotFoundError, json.JSONDecodeError):
        response = {"ok": False, "error": "session daemon is not running"}
    except OSError as exc:
        response = {"ok": False, "error": f"IPC transport failed; operation state is indeterminate: {exc}"}
    print(json.dumps(response, sort_keys=True))
    return 0 if response.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["SessionIPC", "main", "serve"]
