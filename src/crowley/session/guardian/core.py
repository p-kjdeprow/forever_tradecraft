"""Core input guardian lifecycle and ordered event observation.

Lock hierarchy:

1. InputLease._lock protects the lease descriptor and is acquired only by
   lease acquire/release. It is never acquired while InputGuardian._lock is
   held.
2. InputGuardian._lock protects generation, action state, physical holds,
   process registries, calibration tickets, and watchdog publication. The
   watchdog state write and acknowledgement run under this lock. Blocking
   ancestry lookups, physical release, revocation callbacks, process waits,
   and other external I/O run outside it. The bounded ``start`` and
   ``release_admission_gate`` admission callbacks execute under it and must be
   nonblocking so registration remains linearized.
3. The independent watchdog owns its pipe-state and release state in its
   process; it never acquires the guardian or lease locks.

Observation takes a snapshot under InputGuardian._lock, performs the ordered
correlation phases without that lock, then commits only if the same generation
and action are still current under the lock. Revocation releases physical input
before invoking callbacks.
"""

from __future__ import annotations

import atexit
import json
import math
import os
import select
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..input_events import (
    RAW_MOTION_DETAIL,
    decode_xtest_motion_transition,
    xi2_timestamp_elapsed,
    xi2_timestamp_forward,
    xi2_timestamp_valid,
)
from ..input_tool import NativeReleaseSpec
from .correlation import CORRELATION_WINDOW_MS, ActionCorrelation, EchoGraceExpectation, _Observation
from .lease import GuardianError, InputLease
from .watchdog import _watchdog_entry as _watchdog_entry_impl


class InputGuardian:
    """Generation fence, held-input release, deadline and parent watchdog."""

    def __init__(
        self,
        lease_path: str | os.PathLike[str],
        *,
        release_input: Callable[[], None] | None = None,
        parent_pid: int | None = None,
        owner_pid: int | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        watch_interval: float = 0.1,
        watchdog_command: tuple[str, ...] = (),
        watchdog_env: dict[str, str] | None = None,
        watchdog_release_spec: NativeReleaseSpec | None = None,
    ) -> None:
        # A release spec is transferred to the guardian at call entry. This
        # makes every constructor exit responsible for closing the duplicate,
        # including a lease-acquire failure before the watchdog can start.
        self._watchdog_release_spec = watchdog_release_spec
        try:
            self.lease = InputLease(lease_path)
            self.lease.acquire()
        except Exception:
            self._close_release_spec()
            raise
        # The lease is deliberately acquired at construction.  A caller must
        # have ownership before creating the journal/socket that uses it.
        self.generation = 0
        self.armed = False
        self.revoked_reason = "startup"
        self._release_input = release_input or (lambda: None)
        self._callbacks: list[Callable[[str, int], None]] = []
        self._lock = threading.RLock()
        self._clock = monotonic
        self._watch_interval = watch_interval
        self._parent_pid = os.getppid() if parent_pid is None else parent_pid
        self._parent_start = self._proc_start(self._parent_pid)
        self._watch_stop = threading.Event()
        self._watcher: threading.Thread | None = None
        self._watchdog_read = self._watchdog_write = -1
        self._watchdog_ack_read = self._watchdog_ack_write = -1
        self._watchdog_process = None
        self._owner_pid = os.getpid() if owner_pid is None else owner_pid
        self._owner_start = self._proc_start(self._owner_pid)
        self._watchdog_interval = watch_interval
        self._watchdog_command = tuple(watchdog_command)
        self._watchdog_env = dict(watchdog_env or os.environ)
        if watchdog_release_spec is not None:
            try:
                watchdog_release_spec.verify_image()
            except Exception as exc:
                self._close_release_spec()
                self.lease.release()
                raise GuardianError("native input watchdog release image is invalid") from exc
        try:
            self._watchdog_read, self._watchdog_write = os.pipe()
            self._watchdog_ack_read, self._watchdog_ack_write = os.pipe()
            ready_read, ready_write = os.pipe()
        except Exception:
            for descriptor in (
                getattr(self, "_watchdog_read", -1),
                getattr(self, "_watchdog_write", -1),
                getattr(self, "_watchdog_ack_read", -1),
                getattr(self, "_watchdog_ack_write", -1),
            ):
                if descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            if self._watchdog_release_spec is not None:
                self._close_release_spec()
            self.lease.release()
            raise
        try:
            import multiprocessing
            from multiprocessing.reduction import DupFd

            # ``DupFd`` makes the two command descriptors explicit in the
            # spawn child; unrelated controller descriptors are not inherited.
            context = multiprocessing.get_context("spawn")
            self._watchdog_process = context.Process(
                target=_watchdog_entry,
                args=(
                    self._owner_pid,
                    self._owner_start,
                    DupFd(self._watchdog_read),
                    watch_interval,
                    self._watchdog_command,
                    self._watchdog_env,
                    DupFd(self._watchdog_write),
                    None,
                    DupFd(ready_write),
                    NativeReleaseSpec(
                        DupFd(self._watchdog_release_spec.fd),
                        self._watchdog_release_spec.digest,
                        self._watchdog_release_spec.version,
                    )
                    if self._watchdog_release_spec is not None
                    else None,
                    DupFd(self._watchdog_ack_write),
                ),
                name="crowley-input-watchdog",
                daemon=False,
            )
            self._watchdog_process.start()
            os.close(ready_write)
            ready_write = -1
            os.close(self._watchdog_ack_write)
            self._watchdog_ack_write = -1
            ready, _, _ = select.select([ready_read], [], [], max(2.0, watch_interval * 20.0))
            if not ready or os.read(ready_read, 64) != b"READY\n":
                raise GuardianError("input watchdog did not acquire its pinned release image")
        except Exception:
            process = self._watchdog_process
            if process is not None and process.is_alive():
                process.terminate()
                process.join(timeout=1.0)
            if self._watchdog_write >= 0:
                try:
                    os.close(self._watchdog_write)
                except OSError:
                    pass
                self._watchdog_write = -1
            for descriptor_name in ("_watchdog_ack_read", "_watchdog_ack_write"):
                descriptor = getattr(self, descriptor_name, -1)
                if descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                    setattr(self, descriptor_name, -1)
            if self._watchdog_release_spec is not None:
                self._close_release_spec()
            self.lease.release()
            raise
        finally:
            # The child inherited the read end; the owner only writes state.
            os.close(self._watchdog_read)
            self._watchdog_read = -1
            if ready_write >= 0:
                os.close(ready_write)
            os.close(ready_read)
        self._action: ActionCorrelation | None = None
        self._echo_grace_expectations: list[EchoGraceExpectation] = []
        # Epochs are one-way.  A release callback or a later arm must never
        # make an already revoked ticket admissible again.
        self._canceled_epochs: set[int] = set()
        # Calibration and game authority share this lock.  A calibration
        # ticket is invalidated in the same transition that closes a game
        # generation, so a delayed harmless action cannot race revocation.
        self._calibration_epoch = 0
        self._calibration_ticket: Any | None = None
        self._last_event_time_ms: int | None = None
        self._held = False
        self._held_keys: set[str] = set()
        self._held_buttons: set[int] = set()
        self._active_process_groups: set[int] = set()
        self._calibration_bootstrap_process_groups: set[int] = set()
        self._observer_process_groups: set[int] = set()
        self._release_failed = False
        self._watchdog_failed = False
        # Normal interpreter shutdown runs multiprocessing's own child join
        # hook before inherited pipe descriptors are necessarily closed.  A
        # guardian-owned exit hook closes the lease/watchdog explicitly first,
        # while SIGKILL/owner loss still leaves the independent watchdog alive
        # to observe EOF and release its last tracked input set.
        self._atexit_registered = True
        atexit.register(self._close_at_exit)

    def _close_at_exit(self) -> None:
        try:
            self.close()
        except Exception:
            # If native release is unavailable during interpreter teardown,
            # leave the watchdog and lease as the safety fallback.  The child
            # receives EOF once this process exits and retries its exact set.
            pass

    def _close_release_spec(self) -> None:
        spec, self._watchdog_release_spec = self._watchdog_release_spec, None
        if spec is None:
            return
        try:
            os.close(spec.fd)
        except OSError:
            pass

    @staticmethod
    def _proc_start(pid: int) -> str | None:
        try:
            return Path(f"/proc/{pid}/stat").read_text(encoding="ascii").split()[21]
        except (OSError, IndexError):
            return None

    @property
    def action(self) -> ActionCorrelation | None:
        with self._lock:
            return self._action

    @property
    def held(self) -> bool:
        with self._lock:
            return self._held

    @property
    def active_process_groups(self) -> frozenset[int]:
        with self._lock:
            return frozenset(self._active_process_groups)

    @property
    def observer_process_groups(self) -> frozenset[int]:
        with self._lock:
            return frozenset(self._observer_process_groups)

    def on_revoke(self, callback: Callable[[str, int], None]) -> None:
        with self._lock:
            self._callbacks.append(callback)

    def register_process_group(self, pgid: int, generation: int | None = None) -> None:
        """Track an already-exec'd wrapper group under the admission fence."""

        if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 0:
            raise GuardianError("process group identity is malformed")
        if generation is None:
            raise GuardianError("input process groups require a current game generation")
        with self._lock:
            if not self.armed or generation != self.generation or generation in self._canceled_epochs:
                raise GuardianError("input lease is revoked or generation is stale")
            if self._watchdog_failed:
                raise GuardianError("input watchdog is unhealthy")
            self._active_process_groups.add(pgid)
            if not self._watchdog_state():
                self._active_process_groups.discard(pgid)
                raise GuardianError("input watchdog state was not acknowledged")

    def register_calibration_process_group(self, pgid: int, ticket: Any) -> None:
        """Track a mutating calibration helper under the exact ticket fence."""

        if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 0:
            raise GuardianError("process group identity is malformed")
        with self._lock:
            if self.armed or self._calibration_ticket is not ticket or self._held or self._release_failed:
                raise GuardianError("calibration ticket is stale or revoked")
            if self._watchdog_failed:
                raise GuardianError("input watchdog is unhealthy")
            self._active_process_groups.add(pgid)
            if not self._watchdog_state():
                self._active_process_groups.discard(pgid)
                raise GuardianError("input watchdog state was not acknowledged")

    def register_calibration_bootstrap_process(self, pgid: int, epoch: int) -> None:
        """Register a gated GTK helper before its XID/ticket is published."""

        if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 0:
            raise GuardianError("process group identity is malformed")
        with self._lock:
            if (
                self.armed
                or not isinstance(epoch, int)
                or epoch != self._calibration_epoch
                or epoch <= 0
                or self._held
                or self._release_failed
            ):
                raise GuardianError("calibration bootstrap process is stale or revoked")
            if self._watchdog_failed:
                raise GuardianError("input watchdog is unhealthy")
            self._calibration_bootstrap_process_groups.add(pgid)
            self._active_process_groups.add(pgid)
            if not self._watchdog_state():
                self._calibration_bootstrap_process_groups.discard(pgid)
                self._active_process_groups.discard(pgid)
                raise GuardianError("input watchdog state was not acknowledged")

    def adopt_calibration_bootstrap_process(self, pgid: int, ticket: Any) -> None:
        """Bind a registered GTK bootstrap group to its exact ticket."""

        with self._lock:
            if self.armed or self._calibration_ticket is not ticket:
                raise GuardianError("calibration ticket is stale or revoked")
            if pgid not in self._calibration_bootstrap_process_groups:
                raise GuardianError("calibration bootstrap process is not registered")
            self._calibration_bootstrap_process_groups.discard(pgid)
            self._watchdog_state()

    def observer_dispatch_admission(self, start: Callable[[], Any], *, calibration: bool = False) -> Any:
        """Start and register an observer in one owner-death fence."""

        with self._lock:
            calibration_target_only = (
                calibration
                and self._calibration_epoch > 0
                and len(self._calibration_bootstrap_process_groups) == 1
                and self._active_process_groups <= self._calibration_bootstrap_process_groups
            )
            if (
                self._release_failed
                or self._held
                or (self._active_process_groups and not calibration_target_only)
                or (calibration and not calibration_target_only)
            ):
                raise GuardianError("observer cannot start while physical ownership is unresolved")
            if self._watchdog_failed:
                raise GuardianError("input watchdog is unhealthy")
            result = start()
            process = getattr(getattr(result, "observer", result), "process", None)
            pid = getattr(process, "pid", None)
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                raise GuardianError("observer did not publish a process group")
            self._observer_process_groups.add(pid)
            if not self._watchdog_state():
                self._observer_process_groups.discard(pid)
                raise GuardianError("input watchdog state was not acknowledged")
            # A gated helper is registered before its post-exec pipe is
            # released.  The readiness wait belongs outside this authority
            # lock; only the nonblocking spawn/registration boundary is fenced.
            release_gate = getattr(result, "release_admission_gate", None)
            if callable(release_gate):
                try:
                    release_gate()
                except Exception as exc:
                    self._observer_process_groups.discard(pid)
                    self._watchdog_state()
                    raise GuardianError("observer admission gate could not be released") from exc
            return result

    def unregister_observer_process(self, pgid: int) -> None:
        with self._lock:
            self._observer_process_groups.discard(int(pgid))
            self._watchdog_state()

    def unregister_process_group(self, pgid: int) -> None:
        with self._lock:
            # A calibration bootstrap has the same process-group identity in
            # both ledgers until it is either adopted by its ticket or reaped.
            # Removing only the active entry leaves a dead bootstrap looking
            # like the certified target and can admit a later observer.
            group = int(pgid)
            self._active_process_groups.discard(group)
            self._calibration_bootstrap_process_groups.discard(group)
            self._watchdog_state()

    def arm(self, evidence: Any | None = None) -> int:
        """Arm only with an owned live calibration record.

        ``evidence`` is intentionally duck typed here to avoid importing the
        calibration module (which imports the driver).  It must expose
        ``is_live`` and ``fingerprint``; booleans and assertion tokens are
        rejected.
        """

        if isinstance(evidence, (str, bytes, bool)) or not getattr(evidence, "is_live", False):
            raise GuardianError("input lease requires owned live calibration evidence")
        if not getattr(evidence, "fingerprint", ""):
            raise GuardianError("input calibration fingerprint is missing")
        with self._lock:
            if not self.lease.held:
                raise GuardianError("input lease is not held")
            if self._release_failed or self._held or self._active_process_groups:
                raise GuardianError("input guardian has unresolved physical ownership")
            if self.armed and self.generation:
                self._canceled_epochs.add(self.generation)
            self.generation += 1
            self.armed = True
            self.revoked_reason = ""
            self._action = None
            self._echo_grace_expectations.clear()
            self._watch_stop.clear()
            if self._watcher is None or not self._watcher.is_alive():
                self._watcher = threading.Thread(
                    target=self._watch, name="crowley-input-guardian", daemon=True
                )
                self._watcher.start()
            return self.generation

    @property
    def calibration_epoch(self) -> int:
        with self._lock:
            return self._calibration_epoch

    def begin_calibration_operation(self) -> int:
        """Start one calibration epoch under the same authority as game input."""

        with self._lock:
            if (
                self.armed
                or self._active_process_groups
                or self._observer_process_groups
                or self._held
                or self._release_failed
            ):
                raise GuardianError("calibration cannot start while game input is armed")
            self._calibration_epoch += 1
            self._calibration_ticket = None
            return self._calibration_epoch

    def issue_calibration_ticket(self, ticket: Any) -> None:
        """Publish one exact ticket before any calibration mutation."""

        with self._lock:
            epoch = getattr(ticket, "epoch", 0)
            if (
                self.armed
                or not isinstance(epoch, int)
                or epoch != self._calibration_epoch
                or epoch <= 0
                or (
                    self._calibration_ticket is not ticket
                    and (
                        (self._active_process_groups - self._calibration_bootstrap_process_groups)
                        or (self._observer_process_groups and not self._calibration_bootstrap_process_groups)
                        or self._held
                        or self._release_failed
                    )
                )
            ):
                raise GuardianError("calibration epoch is stale or game input is armed")
            self._calibration_ticket = ticket

    def calibration_ticket_current(self, ticket: Any) -> bool:
        with self._lock:
            return (
                not self.armed
                and self._calibration_ticket is ticket
                and getattr(ticket, "epoch", 0) == self._calibration_epoch
            )

    def calibration_dispatch_admission(self, ticket: Any, dispatch: Callable[[], Any]) -> Any:
        """Run calibration spawn/registration/gate release under the one fence."""

        with self._lock:
            if (
                self.armed
                or self._calibration_ticket is not ticket
                or getattr(ticket, "epoch", 0) != self._calibration_epoch
            ):
                raise GuardianError("calibration ticket is stale or revoked")
            if self._watchdog_failed:
                raise GuardianError("input watchdog is unhealthy")
            return dispatch()

    def cancel_calibration_operation(self, ticket: Any | None = None) -> None:
        with self._lock:
            if ticket is not None and self._calibration_ticket is not ticket:
                return
            self._calibration_epoch += 1
            self._calibration_ticket = None

    def publish_current_holds(
        self,
        generation: int,
        keys: set[str] | frozenset[str] = frozenset(),
        buttons: set[int] | frozenset[int] = frozenset(),
    ) -> None:
        """Publish only the physical items currently being emitted."""

        with self._lock:
            if not self.armed or generation != self.generation:
                raise GuardianError("input lease is revoked or generation is stale")
            if self._watchdog_failed:
                raise GuardianError("input watchdog is unhealthy")
            action = self._action
            normalized_keys = {str(key) for key in keys}
            normalized_buttons = {int(button) for button in buttons}
            if (normalized_keys or normalized_buttons) and action is None:
                raise GuardianError("physical holds have no active input action")
            if action is not None and (
                not normalized_keys.issubset(action.planned_held_keys)
                or not normalized_buttons.issubset(action.planned_held_buttons)
            ):
                raise GuardianError("physical holds do not match the admitted action plan")
            self._held_keys = normalized_keys
            self._held_buttons = normalized_buttons
            self._held = bool(self._held_keys or self._held_buttons)
            if not self._watchdog_state() or self._watchdog_failed:
                self._held_keys.clear()
                self._held_buttons.clear()
                self._held = False
                raise GuardianError("input watchdog state was not acknowledged")

    def publish_calibration_holds(
        self,
        ticket: Any,
        keys: set[str] | frozenset[str] = frozenset(),
        buttons: set[int] | frozenset[int] = frozenset(),
    ) -> None:
        """Publish current harmless-target holds under its exact ticket."""

        with self._lock:
            if self.armed or self._calibration_ticket is not ticket:
                raise GuardianError("calibration ticket is stale or revoked")
            if self._watchdog_failed:
                raise GuardianError("input watchdog is unhealthy")
            self._held_keys = {str(key) for key in keys}
            self._held_buttons = {int(button) for button in buttons}
            self._held = bool(self._held_keys or self._held_buttons)
            if not self._watchdog_state() or self._watchdog_failed:
                self._held_keys.clear()
                self._held_buttons.clear()
                self._held = False
                raise GuardianError("input watchdog state was not acknowledged")

    def admission_gate_ready(self) -> None:
        """Fail closed if the watchdog died after hold publication."""

        with self._lock:
            process = self._watchdog_process
            if self._watchdog_failed or self._watchdog_write < 0 or process is None or not process.is_alive():
                self._watchdog_failed = True
                raise GuardianError("input watchdog is unhealthy")

    def dispatch_admission(self, generation: int, dispatch: Callable[[], Any]) -> Any:
        """Run a nonblocking spawn/registration under the epoch fence.

        ``dispatch`` must only create and register the bounded child process;
        all waiting and command-output handling belongs outside this lock.
        Revocation therefore cannot observe a generation with an unregistered
        process or allow a stale ticket to start later.
        """

        with self._lock:
            if not self.armed or generation != self.generation or generation in self._canceled_epochs:
                raise GuardianError("input lease is revoked or generation is stale")
            if self._watchdog_failed:
                raise GuardianError("input watchdog is unhealthy")
            return dispatch()

    def activate_generation(self, generation: int) -> None:
        """Clear cancellation only for a freshly armed, current epoch."""

        with self._lock:
            if not self.armed or generation != self.generation or generation in self._canceled_epochs:
                raise GuardianError("input lease is revoked or generation is stale")
            return

    def begin_action(
        self,
        generation: int,
        *,
        target_xid: int,
        source_device: int,
        master_device: int,
        event_classes: set[str] | frozenset[str],
        detail: int | None = None,
        timeout: float = 10.0,
        device_by_class: dict[str, int] | None = None,
        target_ancestor: Callable[[int], bool] | None = None,
        required_sequence: tuple[str, ...] = (),
        optional_classes: set[str] | frozenset[str] = frozenset(),
        held_keys: set[str] | frozenset[str] = frozenset(),
        held_buttons: set[int] | frozenset[int] = frozenset(),
        expected_modifiers: tuple[int, int, int] | None = None,
        expected_details: tuple[int, ...] = (),
        expected_modifier_sequence: tuple[tuple[int, int, int], ...] = (),
        require_motion_before_button: bool = True,
        expected_coordinates: tuple[float, float] | None = None,
        expected_event_coordinates: tuple[float, float] | None = None,
        expected_motion_trace: tuple[tuple[int, int, int, int], ...] = (),
        expected_positioning_trace: tuple[tuple[int, int, int, int], ...] = (),
        initial_origin: tuple[int, int, int, int] | None = None,
        expected_root_xid: int | None = None,
        expected_event_xid: int | None = None,
        expected_child_xids: frozenset[int] | set[int] = frozenset(),
        canonical_motion: bool = False,
        expected_press_coordinates: tuple[float, float] | None = None,
        expected_press_event_coordinates: tuple[float, float] | None = None,
        expected_release_coordinates: tuple[float, float] | None = None,
        expected_release_event_coordinates: tuple[float, float] | None = None,
        require_motion_before_release: bool = False,
        modifier_group_size: int = 4,
        echo_proof: tuple[tuple[Any, ...], ...] = (),
        echo_sources: set[int] | frozenset[int] = frozenset(),
        require_delivered_modifiers: bool = True,
    ) -> ActionCorrelation:
        with self._lock:
            if not self.armed or generation != self.generation:
                raise GuardianError("input lease is revoked or generation is stale")
            if self._watchdog_failed:
                raise GuardianError("input watchdog is unhealthy")
            if self._action is not None:
                raise GuardianError("another input action is already held")
            if isinstance(modifier_group_size, bool) or modifier_group_size <= 0:
                raise GuardianError("modifier group size is malformed")
            for modifiers in expected_modifier_sequence:
                if (
                    not isinstance(modifiers, (tuple, list))
                    or len(modifiers) != 3
                    or any(
                        isinstance(value, bool) or not isinstance(value, int) or value < 0
                        for value in modifiers
                    )
                ):
                    raise GuardianError("expected modifier sequence is malformed")
            if expected_modifiers is not None and (
                not isinstance(expected_modifiers, (tuple, list))
                or len(expected_modifiers) != 3
                or any(
                    isinstance(value, bool) or not isinstance(value, int) or value < 0
                    for value in expected_modifiers
                )
            ):
                raise GuardianError("expected modifier state is malformed")
            if expected_coordinates is not None and (
                not isinstance(expected_coordinates, (tuple, list))
                or len(expected_coordinates) != 2
                or any(
                    isinstance(value, bool) or not isinstance(value, (int, float))
                    for value in expected_coordinates
                )
                or any(not math.isfinite(float(value)) for value in expected_coordinates)
            ):
                raise GuardianError("expected pointer coordinates are malformed")
            if expected_event_coordinates is not None and (
                not isinstance(expected_event_coordinates, (tuple, list))
                or len(expected_event_coordinates) != 2
                or any(
                    isinstance(value, bool) or not isinstance(value, (int, float))
                    for value in expected_event_coordinates
                )
                or any(not math.isfinite(float(value)) for value in expected_event_coordinates)
            ):
                raise GuardianError("expected delivered pointer coordinates are malformed")
            for motion in expected_motion_trace:
                if (
                    not isinstance(motion, tuple)
                    or len(motion) != 4
                    or any(isinstance(value, bool) or not isinstance(value, int) for value in motion)
                ):
                    raise GuardianError("expected motion trace is malformed")
            for motion in expected_positioning_trace:
                if (
                    not isinstance(motion, tuple)
                    or len(motion) != 4
                    or any(isinstance(value, bool) or not isinstance(value, int) for value in motion)
                ):
                    raise GuardianError("expected positioning trace is malformed")
            for coordinates, label in (
                (expected_press_coordinates, "press"),
                (expected_press_event_coordinates, "press event"),
                (expected_release_coordinates, "release"),
                (expected_release_event_coordinates, "release event"),
            ):
                if coordinates is not None and (
                    not isinstance(coordinates, (tuple, list))
                    or len(coordinates) != 2
                    or any(
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(float(value))
                        for value in coordinates
                    )
                ):
                    raise GuardianError(f"expected {label} coordinates are malformed")
            for echo in echo_proof:
                if (
                    not isinstance(echo, tuple)
                    or len(echo) != 10
                    or echo[0] != "MotionEcho"
                    or any(
                        isinstance(echo[index], bool) or not isinstance(echo[index], int) or echo[index] <= 0
                        for index in (2, 3, 4, 8, 9)
                    )
                    or any(not xi2_timestamp_valid(echo[index]) for index in (1, 7))
                    or any(
                        not isinstance(point, (tuple, list))
                        or len(point) != 2
                        or any(
                            isinstance(value, bool) or not isinstance(value, (int, float)) for value in point
                        )
                        for point in (echo[5], echo[6])
                    )
                ):
                    raise GuardianError("controlled echo proof is malformed")
            if any(
                isinstance(source, bool) or not isinstance(source, int) or source <= 0
                for source in echo_sources
            ):
                raise GuardianError("forwarded echo source set is malformed")
            if any(not isinstance(key, str) or not key for key in held_keys):
                raise GuardianError("planned held key is malformed")
            if any(
                isinstance(button, bool) or not isinstance(button, int) or button <= 0
                for button in held_buttons
            ):
                raise GuardianError("planned held button is malformed")
            if require_motion_before_release and not expected_motion_trace:
                raise GuardianError("release-gated action requires an expected motion trace")
            if "RawMotion" in required_sequence:
                raise GuardianError("RawMotion cannot be an action endpoint")
            now = self._clock()
            action = ActionCorrelation(
                generation,
                target_xid,
                source_device,
                master_device,
                frozenset(event_classes),
                detail,
                now,
                now + timeout,
                tuple(sorted((device_by_class or {}).items())),
                target_ancestor,
                tuple(required_sequence),
                frozenset(optional_classes),
                expected_modifiers=expected_modifiers,
                expected_details=tuple(expected_details),
                expected_modifier_sequence=tuple(expected_modifier_sequence),
                expected_coordinates=(
                    tuple(expected_coordinates) if expected_coordinates is not None else None
                ),
                expected_event_coordinates=(
                    tuple(expected_event_coordinates) if expected_event_coordinates is not None else None
                ),
                expected_press_coordinates=(
                    tuple(expected_press_coordinates) if expected_press_coordinates is not None else None
                ),
                expected_press_event_coordinates=(
                    tuple(expected_press_event_coordinates)
                    if expected_press_event_coordinates is not None
                    else None
                ),
                expected_release_coordinates=(
                    tuple(expected_release_coordinates) if expected_release_coordinates is not None else None
                ),
                expected_release_event_coordinates=(
                    tuple(expected_release_event_coordinates)
                    if expected_release_event_coordinates is not None
                    else None
                ),
                expected_motion_trace=tuple(expected_motion_trace),
                expected_positioning_trace=tuple(expected_positioning_trace),
                initial_origin=tuple(initial_origin) if initial_origin is not None else None,
                expected_root_xid=expected_root_xid,
                expected_event_xid=expected_event_xid,
                expected_child_xids=frozenset(expected_child_xids),
                canonical_motion=canonical_motion,
                planned_held_keys=frozenset(held_keys),
                planned_held_buttons=frozenset(held_buttons),
                require_motion_before_release=require_motion_before_release,
                modifier_group_size=modifier_group_size,
                echo_proof=tuple(echo_proof),
                require_delivered_modifiers=require_delivered_modifiers,
                require_motion_before_button=require_motion_before_button,
                echo_sources=frozenset(echo_sources)
                | frozenset(
                    int(echo[3])
                    for echo in echo_proof
                    if isinstance(echo[3], int) and not isinstance(echo[3], bool) and echo[3] > 0
                ),
            )
            if canonical_motion:
                if (
                    initial_origin is None
                    or len(initial_origin) != 4
                    or any(isinstance(value, bool) or not isinstance(value, int) for value in initial_origin)
                    or expected_root_xid is None
                    or expected_event_xid is None
                ):
                    raise GuardianError("canonical XTEST motion origin/trace contract is malformed")
                if (
                    expected_root_xid != expected_event_xid
                    or isinstance(expected_root_xid, bool)
                    or not isinstance(expected_root_xid, int)
                    or expected_root_xid <= 0
                    or not expected_child_xids
                    or any(
                        isinstance(xid, bool) or not isinstance(xid, int) or xid <= 0
                        for xid in expected_child_xids
                    )
                    or tuple(initial_origin[:2]) != tuple(initial_origin[2:])
                ):
                    raise GuardianError("canonical XTEST root/child identity is malformed")
                action.last_accepted_endpoint = tuple(initial_origin)
            self._action = action
            self._last_event_time_ms = None
            # ``held_keys``/``held_buttons`` describe the plan, not physical
            # ownership.  The backend publishes the exact set only after its
            # process group is registered and the admission gate is about to
            # emit input.  A death in this pre-emission window must not cause
            # keyup/mouseup for an input that was never pressed.
            self._held_keys.clear()
            self._held_buttons.clear()
            self._held = False
            self._watchdog_state()
        return action

    def finish_action(self, generation: int, action: ActionCorrelation | None = None) -> None:
        incomplete = False
        with self._lock:
            # Check and clear are one transition.  A revoke can otherwise
            # increment the generation between ``check`` and this block and a
            # stale worker would clear the next action's held state.
            if not self.armed or generation != self.generation:
                raise GuardianError("input lease is revoked or generation is stale")
            if action is not None and self._action is not action:
                raise GuardianError("input action is no longer current")
            current_action = self._action if action is None else action
            if current_action is not None and not current_action.complete:
                incomplete = True
            if incomplete:
                # Clearing an incomplete correlation would turn late native
                # events into unattributed input and could strand a hold.  A
                # failed completion is therefore a revocation boundary.
                pass
            else:
                self._action = None
                if current_action is not None and current_action.pending_echoes:
                    deadline = self._clock() + CORRELATION_WINDOW_MS / 1000.0
                    self._echo_grace_expectations.extend(
                        EchoGraceExpectation(
                            generation=current_action.generation,
                            deadline_monotonic=deadline,
                            timestamp=timestamp,
                            target_xid=target,
                            root_xy=root_xy,
                            event_xy=event_xy,
                            modifiers=modifiers,
                            echo_sources=current_action.echo_sources,
                            canonical_motion=current_action.canonical_motion,
                            expected_root_xid=current_action.expected_root_xid,
                            expected_event_xid=current_action.expected_event_xid,
                            require_delivered_modifiers=current_action.require_delivered_modifiers,
                        )
                        for timestamp, target, root_xy, event_xy, modifiers in current_action.pending_echoes
                    )
                    current_action.pending_echoes.clear()
                self._last_event_time_ms = None
                self._held = False
                self._held_keys.clear()
                self._held_buttons.clear()
                # Keep the state transition and its watchdog publication in
                # the same ownership lock.  A new action must never publish
                # before this clean state, nor can a stale finish publish
                # after a later transition.
                self._watchdog_state()
        if incomplete:
            self.revoke("input action completed before its sealed event trace")
            raise GuardianError("input action event trace is incomplete")

    def check(self, generation: int) -> None:
        reason: str | None = None
        with self._lock:
            if not self.armed or generation != self.generation:
                raise GuardianError("input lease is revoked or generation is stale")
            action = self._action
            now = self._clock()
            if action is not None and now >= action.deadline_monotonic:
                reason = "bounded action deadline expired"
            if reason is None:
                return
        self.revoke(reason)
        raise GuardianError(reason)

    def observe(self, event: Any) -> bool:
        """Correlate one parsed XI2 event; unknown input revokes immediately."""

        with self._lock:
            action = self._action
            now = self._clock()
            self._echo_grace_expectations[:] = [
                expectation
                for expectation in self._echo_grace_expectations
                if expectation.generation == self.generation and now < expectation.deadline_monotonic
            ]
            grace_expectations = tuple(self._echo_grace_expectations)
            prior_timestamp = self._last_event_time_ms
            generation = self.generation
            action_pending_echoes = tuple(action.pending_echoes) if action is not None else ()
        observation = _Observation(
            event=event,
            action=action,
            prior_timestamp=prior_timestamp,
            generation=generation,
            event_class=getattr(event.event_class, "value", event.event_class),
            target=getattr(event, "target_xid", None),
            grace_expectations=grace_expectations,
            action_pending_echoes=action_pending_echoes,
        )
        self._observe_target_phase(observation)
        self._observe_admission_phase(observation)
        self._observe_constraint_phase(observation)
        self._observe_record_phase(observation)
        self._observe_commit_phase(observation)
        if observation.allowed:
            return True
        self.revoke(
            "unattributed XI2 event "
            f"class={getattr(event.event_class, 'value', event.event_class)} "
            f"source={getattr(event, 'source_device', '?')} "
            f"target={getattr(event, 'target_xid', '?')}"
        )
        return False

    @staticmethod
    def _echo_expectation_matches(event: Any, expectation: EchoGraceExpectation) -> bool:
        """Apply the exact forwarded-echo contract to one expectation."""

        if (
            getattr(event.event_class, "value", event.event_class) != "Motion"
            or event.source_device not in expectation.echo_sources
            or event.master_device != event.source_device
        ):
            return False
        elapsed = xi2_timestamp_elapsed(expectation.timestamp, event.timestamp_ms)
        return bool(
            elapsed is not None
            and elapsed <= CORRELATION_WINDOW_MS
            and event.target_xid == expectation.target_xid
            and (
                not expectation.canonical_motion
                or (
                    getattr(event, "root_xid", None) == expectation.expected_root_xid
                    and getattr(event, "event_xid", None) == expectation.expected_event_xid
                )
            )
            and expectation.root_xy is not None
            and expectation.event_xy is not None
            and getattr(event, "root_xy", None) == expectation.root_xy
            and getattr(event, "event_xy", None) == expectation.event_xy
            and getattr(event, "modifiers", None) == expectation.modifiers
        )

    @staticmethod
    def _action_echo_expectation(
        action: ActionCorrelation,
        pending: tuple[
            int,
            int | None,
            tuple[float, float] | None,
            tuple[float, float] | None,
            tuple[int, int, int] | None,
        ],
    ) -> EchoGraceExpectation:
        timestamp, target_xid, root_xy, event_xy, modifiers = pending
        return EchoGraceExpectation(
            generation=action.generation,
            deadline_monotonic=action.deadline_monotonic,
            timestamp=timestamp,
            target_xid=target_xid,
            root_xy=root_xy,
            event_xy=event_xy,
            modifiers=modifiers,
            echo_sources=action.echo_sources,
            canonical_motion=action.canonical_motion,
            expected_root_xid=action.expected_root_xid,
            expected_event_xid=action.expected_event_xid,
            require_delivered_modifiers=action.require_delivered_modifiers,
        )

    @staticmethod
    def _delivered_motion_modifiers_valid(event: Any) -> bool:
        modifiers = getattr(event, "modifiers", None)
        effective = getattr(event, "modifier_effective", None)
        return bool(
            isinstance(modifiers, (tuple, list))
            and len(modifiers) == 3
            and all(
                isinstance(value, int) and not isinstance(value, bool) and value >= 0 for value in modifiers
            )
            and isinstance(effective, int)
            and not isinstance(effective, bool)
            and effective >= 0
            and effective == (modifiers[0] | modifiers[1] | modifiers[2])
        )

    def _observe_target_phase(self, observation: _Observation) -> None:
        """Validate target ancestry and correlate canonical/forwarded motion."""

        action = observation.action
        event = observation.event
        event_class = observation.event_class
        target = observation.target
        target_ok = False
        echo_match = False
        motion_match_index: int | None = None
        positioning_match_index: int | None = None
        echo_pending_index: int | None = None
        canonical_motion_checked = False
        canonical_raw_queued = False
        canonical_endpoint: tuple[int, int] | None = None
        if action is None:
            for index, expectation in enumerate(observation.grace_expectations):
                if self._echo_expectation_matches(event, expectation):
                    echo_match = True
                    target_ok = True
                    observation.grace_expectation = expectation
                    observation.grace_index = index
                    break
            observation.target_ok = target_ok
            observation.echo_match = echo_match
            return
        # Raw and device-change records describe device transitions and
        # deliberately have no delivered target.  A forged/raw block that
        # carries a child XID must not inherit the delivered target proof.
        if (getattr(event, "is_raw", False) or event_class == "DeviceChanged") and target not in (
            None,
            0,
        ):
            target_ok = False
        else:
            target_ok = target == action.target_xid
        if (
            not target_ok
            and target not in (None, 0)
            and not getattr(event, "is_raw", False)
            and event_class != "DeviceChanged"
            and action.target_ancestor is not None
        ):
            try:
                # X11 ancestry lookup may execute xwininfo; never hold the
                # guardian lock while doing that blocking operation.
                target_ok = bool(action.target_ancestor(target))
            except Exception:
                target_ok = False
        if (
            not target_ok
            and target in (None, 0)
            and event_class in action.optional_classes
            and event_class in {"Enter", "Leave"}
        ):
            # XI2 crossing records legitimately report child=0 while
            # transitioning through a managed frame.  They remain bounded optional
            # records; raw/device transitions do not receive this exception.
            target_ok = True
        if (
            not target_ok
            and target in (None, 0)
            and (getattr(event, "is_raw", False) or event_class == "DeviceChanged")
        ):
            target_ok = True
        if action.canonical_motion and (
            event_class == "RawMotion"
            or (event_class == "Motion" and event.source_device == action.source_device)
        ):
            if action.positioning_index < len(action.expected_positioning_trace):
                expected_transition = action.expected_positioning_trace[action.positioning_index]
                positioning_phase = True
            elif action.motion_index < len(action.expected_motion_trace):
                expected_transition = action.expected_motion_trace[action.motion_index]
                positioning_phase = False
            else:
                expected_transition = None
                positioning_phase = False
            if expected_transition is None:
                target_ok = False
            elif event_class == "RawMotion":
                raw_endpoint_rows = {axis: value for axis, value, _raw in getattr(event, "valuators", ())}
                if (
                    event.source_device != action.source_device
                    or event.master_device != action.master_device
                    or event.detail != RAW_MOTION_DETAIL
                    or set(raw_endpoint_rows) != {0, 1}
                    or any(
                        isinstance(value, bool)
                        or not isinstance(value, float)
                        or not math.isfinite(value)
                        or not value.is_integer()
                        for value in raw_endpoint_rows.values()
                    )
                    or (int(raw_endpoint_rows[0]), int(raw_endpoint_rows[1])) != expected_transition[:2]
                    or not action.raw_motion_pairing.queue_raw(
                        event.timestamp_ms,
                        expected_transition[:2],
                    )
                ):
                    target_ok = False
                else:
                    canonical_raw_queued = True
            elif event.source_device == action.source_device:
                canonical_motion_checked = True
                try:
                    transition = decode_xtest_motion_transition(
                        event,
                        expected_source=action.source_device,
                        expected_parent=action.master_device,
                        expected_root_xid=action.expected_root_xid,
                        expected_child_xids=action.expected_child_xids,
                        planned_endpoint=expected_transition[:2],
                    )
                except Exception:
                    target_ok = False
                else:
                    origin = action.last_accepted_endpoint
                    if (
                        not target_ok
                        or origin is None
                        or transition.origin_root != origin[:2]
                        or transition.origin_event != origin[2:]
                    ):
                        target_ok = False
                    elif (
                        not action.raw_motion_pairing.pending_endpoints
                        or action.raw_motion_pairing.pending_endpoints[0] != transition.endpoint_root
                    ):
                        target_ok = False
                    else:
                        canonical_endpoint = transition.endpoint_root
                        if positioning_phase:
                            positioning_match_index = action.positioning_index
                        else:
                            motion_match_index = action.motion_index
                        action.emitted_motion_count += 1
                        if action.echo_sources:
                            # The forwarded record may arrive after a later
                            # planned primary. Preserve every exact pair so
                            # the source event can consume it in any order.
                            action.pending_echoes.append(
                                (
                                    event.timestamp_ms,
                                    target,
                                    transition.endpoint_root,
                                    transition.endpoint_event,
                                    event.modifiers,
                                )
                            )
        if action.release_started and event_class in {"Motion", "RawMotion"}:
            target_ok = False
        if (
            target_ok
            and event_class == "Motion"
            and event.source_device in action.echo_sources
            and observation.action_pending_echoes
        ):
            # The calibration proof establishes only the source
            # relationship. Runtime admission must pair each forwarded
            # record with an outstanding source-4 operation; old
            # calibration coordinates/timestamps are never replayed.
            pending_items = tuple(enumerate(observation.action_pending_echoes))
            for index, pending in pending_items:
                expectation = self._action_echo_expectation(action, pending)
                if self._echo_expectation_matches(event, expectation):
                    echo_match = True
                    echo_pending_index = index
                    observation.matched_echo_expectation = expectation
                    break
        if not echo_match and event_class == "Motion":
            for index, expectation in enumerate(observation.grace_expectations):
                if self._echo_expectation_matches(event, expectation):
                    echo_match = True
                    target_ok = True
                    observation.grace_expectation = expectation
                    observation.grace_index = index
                    break
        if (
            target_ok
            and action is not None
            and event_class == "Motion"
            and event.source_device in action.echo_sources
            and not echo_match
        ):
            # A forwarded source is meaningful only as the exact echo of an
            # outstanding primary. Letting a mismatched source-7
            # Motion continue through the ordinary optional-Motion path
            # would silently accept wrong root/event identities or an
            # unpaired human record.
            target_ok = False
        if target_ok and event_class == "Motion" and not echo_match and not canonical_motion_checked:
            root_xy = getattr(event, "root_xy", None)
            event_xy = getattr(event, "event_xy", None)
            if action.sequence_index < 2:
                pre_trace = action.expected_positioning_trace or action.expected_motion_trace
                pre_index = (
                    action.positioning_index if action.expected_positioning_trace else action.motion_index
                )
                for index in range(pre_index, len(pre_trace)):
                    expected = pre_trace[index]
                    if root_xy == expected[:2] and event_xy == expected[2:]:
                        if action.expected_positioning_trace:
                            positioning_match_index = index
                        else:
                            motion_match_index = index
                        break
            elif action.sequence_index >= 2 and action.expected_motion_trace:
                for index in range(action.motion_index, len(action.expected_motion_trace)):
                    expected = action.expected_motion_trace[index]
                    if root_xy == expected[:2] and event_xy == expected[2:]:
                        motion_match_index = index
                        break
        observation.target_ok = target_ok
        observation.echo_match = echo_match
        observation.motion_match_index = motion_match_index
        observation.positioning_match_index = positioning_match_index
        observation.echo_pending_index = echo_pending_index
        observation.canonical_motion_checked = canonical_motion_checked
        observation.canonical_raw_queued = canonical_raw_queued
        observation.canonical_endpoint = canonical_endpoint

    def _observe_admission_phase(self, observation: _Observation) -> None:
        """Apply the sequence, timestamp, generation, and deadline gates."""

        action = observation.action
        event = observation.event
        event_class = observation.event_class
        target_ok = observation.target_ok
        echo_match = observation.echo_match
        sequence_ok = True
        now = self._clock()
        timestamp = getattr(event, "timestamp_ms", None)
        if (
            observation.grace_expectation is None
            and action is not None
            and observation.echo_match
            and observation.matched_echo_expectation is not None
            and now > action.deadline_monotonic
        ):
            # An observation may have captured the current action before it
            # completed. Reconcile a now-published grace expectation before
            # applying the completed action's deadline.
            observation.grace_expectation = self._reconcile_echo_grace_transition(observation, now)
        if observation.grace_expectation is not None:
            timestamp_ok = xi2_timestamp_valid(timestamp)
            observation.sequence_ok = True
            observation.now = now
            observation.timestamp = timestamp
            observation.timestamp_ok = timestamp_ok
            observation.allowed = bool(
                self.armed
                and event_class == "Motion"
                and echo_match
                and target_ok
                and timestamp_ok
                and now < observation.grace_expectation.deadline_monotonic
            )
            return
        if action is not None and action.required_sequence:
            if event_class in action.optional_classes:
                sequence_ok = True
            elif action.sequence_index < len(action.required_sequence):
                sequence_ok = event_class == action.required_sequence[action.sequence_index]
            else:
                sequence_ok = False
        action_deadline = None
        if action is not None:
            action_deadline = action.deadline_monotonic
        deadline_ok = action_deadline is not None and now <= action_deadline
        if not deadline_ok and observation.echo_match and observation.matched_echo_expectation is not None:
            # Let commit reconcile a finish that races this admission phase.
            # If the action is still current, commit rechecks the deadline and
            # rejects the stale echo without changing its correlation state.
            deadline_ok = True
        pending_raw_primary = bool(
            action is not None
            and event_class == "Motion"
            and not echo_match
            and action.raw_motion_pairing.pending_timestamps
            and timestamp == action.raw_motion_pairing.pending_timestamps[0]
        )
        timestamp_ok = bool(
            xi2_timestamp_valid(timestamp)
            and (
                echo_match
                or pending_raw_primary
                or xi2_timestamp_forward(observation.prior_timestamp, timestamp)
            )
        )
        allowed = bool(
            self.armed
            and action is not None
            and event_class in action.event_classes
            and (event.source_device == action.source_device or echo_match)
            and (echo_match or event.master_device == action.expected_device(event_class))
            and target_ok
            and (
                action.detail is None
                or event.detail == action.detail
                or event_class in {"DeviceChanged", "Motion", "RawMotion", "Enter", "Leave"}
            )
            and (event_class != "RawMotion" or event.detail == RAW_MOTION_DETAIL)
            and sequence_ok
            and timestamp_ok
            and deadline_ok
            and action.started_monotonic <= now
            and (
                event_class != "Motion"
                or echo_match
                or (
                    action.sequence_index < 2
                    and (
                        not (action.expected_positioning_trace or action.expected_motion_trace)
                        or observation.positioning_match_index is not None
                        or observation.motion_match_index is not None
                    )
                )
                or (
                    action.sequence_index >= 2
                    and (not action.expected_motion_trace or observation.motion_match_index is not None)
                )
            )
            and not (
                event_class in {"RawButtonPress", "ButtonPress"}
                and (action.expected_positioning_trace or action.expected_motion_trace)
                and action.require_motion_before_button
                and (
                    action.positioning_index < len(action.expected_positioning_trace)
                    if action.expected_positioning_trace
                    else action.motion_index < len(action.expected_motion_trace)
                )
            )
        )
        observation.sequence_ok = sequence_ok
        observation.now = now
        observation.timestamp = timestamp
        observation.pending_raw_primary = pending_raw_primary
        observation.timestamp_ok = timestamp_ok
        observation.allowed = allowed

    def _reconcile_echo_grace_transition(
        self, observation: _Observation, now: float
    ) -> EchoGraceExpectation | None:
        """Claim a moved echo expectation after the active action deadline."""

        with self._lock:
            if (
                self.generation != observation.generation
                or not self.armed
                or self._action is observation.action
            ):
                return None
            for expectation in self._echo_grace_expectations:
                if (
                    expectation.generation == observation.generation
                    and now < expectation.deadline_monotonic
                    and self._echo_expectation_matches(observation.event, expectation)
                ):
                    return expectation
        return None

    def _observe_constraint_phase(self, observation: _Observation) -> None:
        """Apply detail, modifier, coordinate, and release constraints."""

        action = observation.action
        event = observation.event
        event_class = observation.event_class
        allowed = observation.allowed
        if observation.grace_expectation is not None:
            if (
                allowed
                and observation.grace_expectation.require_delivered_modifiers
                and event_class == "Motion"
                and not getattr(event, "is_raw", False)
                and not self._delivered_motion_modifiers_valid(event)
            ):
                allowed = False
            observation.allowed = allowed
            return
        detail_events = {
            "RawKeyPress",
            "KeyPress",
            "RawKeyRelease",
            "KeyRelease",
            "RawButtonPress",
            "ButtonPress",
            "RawButtonRelease",
            "ButtonRelease",
        }
        step_index = action.sequence_index if action is not None else 0
        detail_group = step_index // max(1, action.modifier_group_size if action is not None else 4)
        if allowed and action is not None and event_class in detail_events:
            expected_detail = None
            if action.detail is not None:
                expected_detail = action.detail
            elif action.expected_details and step_index < len(action.expected_details):
                expected_detail = action.expected_details[step_index]
            elif action.detail is None:
                # A runtime key/button action must carry the measured native
                # detail sequence.  Learning the first detail from the event
                # would let an arbitrary key stream through the guard.
                allowed = False
            if allowed and expected_detail is not None and event.detail != expected_detail:
                allowed = False
            if allowed and not getattr(event, "is_raw", False):
                delivered_modifiers = getattr(event, "modifiers", None)
                expected_modifiers = action.expected_modifiers
                if action.expected_modifier_sequence and detail_group < len(
                    action.expected_modifier_sequence
                ):
                    expected_modifiers = action.expected_modifier_sequence[detail_group]
                if action.require_delivered_modifiers and delivered_modifiers is None:
                    allowed = False
                effective = getattr(event, "modifier_effective", None)
                if (
                    allowed
                    and action.require_delivered_modifiers
                    and (
                        effective is None
                        or not isinstance(effective, int)
                        or not isinstance(delivered_modifiers, (tuple, list))
                        or len(delivered_modifiers) != 3
                        or any(
                            isinstance(value, bool) or not isinstance(value, int) or value < 0
                            for value in delivered_modifiers
                        )
                    )
                ):
                    allowed = False
                if (
                    allowed
                    and action.require_delivered_modifiers
                    and effective
                    != (delivered_modifiers[0] | delivered_modifiers[1] | delivered_modifiers[2])
                ):
                    allowed = False
                if allowed and expected_modifiers is not None and delivered_modifiers != expected_modifiers:
                    allowed = False
                if (
                    allowed
                    and action.expected_modifier_sequence
                    and detail_group < len(action.expected_modifier_sequence)
                    and delivered_modifiers != action.expected_modifier_sequence[detail_group]
                ):
                    allowed = False
        if (
            allowed
            and action is not None
            and event_class == "Motion"
            and not observation.echo_match
            and action.raw_motion_pairing.pending_timestamps
            and (
                observation.timestamp is None
                or action.raw_motion_pairing.pending_timestamps[0] != observation.timestamp
            )
        ):
            # A raw trace is useful only as the same-timestamp ancestry of the
            # delivered endpoint.  A different or missing timestamp is an
            # unattributed physical stream and revokes the action.
            allowed = False
        if (
            allowed
            and action is not None
            and event_class == "Motion"
            and not getattr(event, "is_raw", False)
            and action.require_delivered_modifiers
            and action.expected_modifiers is not None
            and getattr(event, "modifiers", None) != action.expected_modifiers
        ):
            allowed = False
        if (
            allowed
            and action is not None
            and event_class in {"RawButtonRelease", "ButtonRelease"}
            and action.expected_motion_trace
            and action.require_motion_before_release
            and action.motion_index < len(action.expected_motion_trace)
        ):
            allowed = False
        if (
            allowed
            and action is not None
            and event_class in {"RawButtonRelease", "ButtonRelease"}
            and action.require_motion_before_release
            and action.motion_index < len(action.expected_motion_trace)
        ):
            allowed = False
        if (
            allowed
            and action is not None
            and event_class in {"ButtonPress", "ButtonRelease", "Motion"}
            and not observation.echo_match
            and not (event_class == "Motion" and action.expected_motion_trace)
            and action.expected_coordinates is not None
            and (
                getattr(event, "root_xy", None) != action.expected_coordinates
                or (
                    action.expected_event_coordinates is not None
                    and getattr(event, "event_xy", None) != action.expected_event_coordinates
                )
                or (
                    action.expected_event_coordinates is None
                    and getattr(event, "event_xy", None) != action.expected_coordinates
                )
            )
        ):
            allowed = False
        if (
            allowed
            and action is not None
            and event_class == "Motion"
            and not observation.echo_match
            and event.source_device == action.source_device
            and not action.canonical_motion
        ):
            if action.sequence_index < 2:
                pre_trace = action.expected_positioning_trace or action.expected_motion_trace
                pre_match = (
                    observation.positioning_match_index
                    if action.expected_positioning_trace
                    else observation.motion_match_index
                )
                if pre_trace and pre_match is None:
                    allowed = False
            elif action.expected_motion_trace and observation.motion_match_index is None:
                allowed = False
        if allowed and action is not None and event_class == "RawMotion" and action.release_started:
            allowed = False
        if allowed and action is not None and event_class in {"ButtonPress", "ButtonRelease"}:
            expected_root = (
                action.expected_press_coordinates
                if event_class == "ButtonPress"
                else action.expected_release_coordinates
            )
            expected_event = (
                action.expected_press_event_coordinates
                if event_class == "ButtonPress"
                else action.expected_release_event_coordinates
            )
            if expected_root is not None and getattr(event, "root_xy", None) != expected_root:
                allowed = False
            if expected_event is not None and getattr(event, "event_xy", None) != expected_event:
                allowed = False
        observation.allowed = allowed

    def _observe_record_phase(self, observation: _Observation) -> None:
        """Record accepted motion ancestry and validate delivered modifiers."""

        action = observation.action
        event = observation.event
        event_class = observation.event_class
        allowed = observation.allowed
        if observation.grace_expectation is not None:
            return
        if (
            allowed
            and action is not None
            and event_class == "Motion"
            and not observation.echo_match
            and event.source_device == action.source_device
            and not action.canonical_motion
        ):
            # Save only the current operation's complete source-4 facts.  A
            # later source-7 record must match this bounded pair exactly.
            action.current_motion_timestamp = observation.timestamp
            action.current_motion_target = observation.target
            action.current_motion_root_xy = getattr(event, "root_xy", None)
            action.current_motion_event_xy = getattr(event, "event_xy", None)
            modifiers = getattr(event, "modifiers", None)
            action.current_motion_modifiers = tuple(modifiers) if modifiers is not None else None
            action.emitted_motion_count += 1
            if action.echo_sources:
                action.pending_echoes.append(
                    (
                        observation.timestamp,
                        observation.target,
                        action.current_motion_root_xy,
                        action.current_motion_event_xy,
                        action.current_motion_modifiers,
                    )
                )
            if not action.raw_motion_pairing.observe_delivered(observation.timestamp):
                allowed = False
        if (
            allowed
            and action is not None
            and event_class == "RawMotion"
            and not observation.canonical_raw_queued
        ):
            if not action.raw_motion_pairing.queue_raw(observation.timestamp):
                allowed = False
        if (
            allowed
            and action is not None
            and event_class in {"Enter", "Leave"}
            and action.require_delivered_modifiers
            and action.expected_modifiers is not None
            and getattr(event, "modifiers", None) != action.expected_modifiers
        ):
            allowed = False
        if (
            allowed
            and action is not None
            and event_class == "Motion"
            and not getattr(event, "is_raw", False)
            and action.require_delivered_modifiers
            and (
                not isinstance(getattr(event, "modifiers", None), (tuple, list))
                or len(getattr(event, "modifiers", ())) != 3
                or any(
                    isinstance(value, bool) or not isinstance(value, int) or value < 0
                    for value in getattr(event, "modifiers", ())
                )
                or isinstance(getattr(event, "modifier_effective", None), bool)
                or not isinstance(getattr(event, "modifier_effective", None), int)
                or getattr(event, "modifier_effective", 0) < 0
                or event.modifier_effective != (event.modifiers[0] | event.modifiers[1] | event.modifiers[2])
            )
        ):
            allowed = False
        observation.allowed = allowed

    def _observe_commit_phase(self, observation: _Observation) -> None:
        """Commit correlation counters only for the same action generation."""

        if not observation.allowed:
            return
        with self._lock:
            action = observation.action
            if self.generation != observation.generation or not self.armed:
                observation.allowed = False
            elif observation.grace_expectation is not None:
                grace_index = next(
                    (
                        index
                        for index, expectation in enumerate(self._echo_grace_expectations)
                        if expectation is observation.grace_expectation
                    ),
                    None,
                )
                if grace_index is None:
                    observation.allowed = False
                else:
                    self._echo_grace_expectations.pop(grace_index)
            else:
                if self._action is not action:
                    retry_index = None
                    if observation.echo_match and observation.matched_echo_expectation is not None:
                        retry_index = next(
                            (
                                index
                                for index, expectation in enumerate(self._echo_grace_expectations)
                                if expectation.generation == observation.generation
                                and self._clock() < expectation.deadline_monotonic
                                and self._echo_expectation_matches(
                                    observation.event,
                                    expectation,
                                )
                            ),
                            None,
                        )
                    if retry_index is None:
                        observation.allowed = False
                    else:
                        self._echo_grace_expectations.pop(retry_index)
                    return
                if (
                    observation.echo_match
                    and observation.matched_echo_expectation is not None
                    # A matched echo always belongs to a correlated action;
                    # fail closed if that invariant is ever broken.
                    and (action is None or self._clock() > action.deadline_monotonic)
                ):
                    observation.allowed = False
                    return
                if not observation.echo_match:
                    if observation.pending_raw_primary and observation.prior_timestamp is not None:
                        # The FIFO-head primary may be older in scalar
                        # form than a later raw arrival. Retain the
                        # arrival-domain clock instead of moving it back.
                        self._last_event_time_ms = (
                            observation.prior_timestamp
                            if not xi2_timestamp_forward(observation.prior_timestamp, observation.timestamp)
                            else observation.timestamp
                        )
                    else:
                        self._last_event_time_ms = observation.timestamp
                if observation.echo_match and observation.echo_pending_index is not None:
                    action.pending_echoes.pop(observation.echo_pending_index)
                    action.echo_index += 1
                if observation.motion_match_index is not None:
                    action.motion_index = observation.motion_match_index + 1
                if observation.positioning_match_index is not None:
                    action.positioning_index = observation.positioning_match_index + 1
                if (
                    action.canonical_motion
                    and observation.canonical_endpoint is not None
                    and observation.event_class == "Motion"
                ):
                    if not action.raw_motion_pairing.observe_delivered(observation.timestamp):
                        observation.allowed = False
                    else:
                        action.last_accepted_endpoint = (
                            observation.canonical_endpoint[0],
                            observation.canonical_endpoint[1],
                            observation.canonical_endpoint[0],
                            observation.canonical_endpoint[1],
                        )
                if action.required_sequence and observation.event_class not in action.optional_classes:
                    action.sequence_index += 1
                if observation.event_class == "RawButtonRelease":
                    action.release_started = True

    def revoke(self, reason: str) -> int:
        callbacks: list[Callable[[str, int], None]]
        with self._lock:
            old_generation = self.generation
            if old_generation:
                self._canceled_epochs.add(old_generation)
            self.generation += 1
            generation = self.generation
            self.armed = False
            self.revoked_reason = reason
            self._calibration_epoch += 1
            self._calibration_ticket = None
            self._echo_grace_expectations.clear()
            active = (
                self._action is not None
                or bool(self._active_process_groups)
                or bool(self._observer_process_groups)
            )
            self._action = None
            held = self._held
            callbacks = list(self._callbacks)
        # Physical release precedes callbacks so slow journal/model/observer
        # cleanup cannot extend the interval in which native input is held.
        callback_reason = reason
        if held or active:
            try:
                self._release_input()
            except Exception:
                # A failed release is reported to callbacks as a guardian
                # failure; retain the held state so the independent watchdog
                # can retry release rather than losing the only fallback.
                callback_reason = f"{reason}; physical input release failed"
                self._release_failed = True
                if held:
                    with self._lock:
                        self._watchdog_state()
            else:
                with self._lock:
                    self._held = False
                    self._held_keys.clear()
                    self._held_buttons.clear()
                    self._release_failed = False
                    self._watchdog_state()
        for callback in callbacks:
            try:
                callback(callback_reason, generation)
            except Exception:
                continue
        return generation

    def _watchdog_state(self) -> bool:
        with self._lock:
            if self._watchdog_failed or self._watchdog_write < 0:
                return False
            state = json.dumps(
                {
                    "groups": sorted(self._active_process_groups | self._observer_process_groups),
                    "keys": sorted(self._held_keys),
                    "buttons": sorted(self._held_buttons),
                },
                separators=(",", ":"),
            ).encode()
            frame = b"T" + state + b"\n"
            try:
                view = memoryview(frame)
                while view:
                    written = os.write(self._watchdog_write, view)
                    if written <= 0:
                        raise OSError("watchdog state write made no progress")
                    view = view[written:]
                if self._watchdog_ack_read >= 0:
                    ready, _, _ = select.select(
                        [self._watchdog_ack_read], [], [], max(1.0, self._watchdog_interval * 20.0)
                    )
                    if not ready or os.read(self._watchdog_ack_read, 4) != b"ACK\n":
                        raise OSError("watchdog state acknowledgement was not received")
                return True
            except (OSError, ValueError):
                # Fail closed exactly once. Cleanup must not recurse into another
                # state publication after the command channel has failed.
                self._watchdog_failed = True
                if self._watchdog_write >= 0:
                    try:
                        os.close(self._watchdog_write)
                    except OSError:
                        pass
                    self._watchdog_write = -1
                return False

    def parent_alive(self) -> bool:
        if self._parent_pid <= 0:
            return False
        current = self._proc_start(self._parent_pid)
        return current is not None and (self._parent_start is None or current == self._parent_start)

    def _watch(self) -> None:
        while not self._watch_stop.wait(self._watch_interval):
            try:
                with self._lock:
                    action = self._action
                    armed = self.armed
                if armed and not self.parent_alive():
                    self.revoke("guardian parent process exited")
                    return
                if action is not None and self._clock() >= action.deadline_monotonic:
                    self.revoke("bounded action deadline expired")
            except Exception:
                self.revoke("guardian watchdog failure")
                return

    def close(self) -> None:
        self._watch_stop.set()
        self.revoke("guardian closed")
        # A native release can fail transiently while the action process is
        # being terminated.  Retry while the independent watchdog still has
        # the exact held set; never send its clean-close marker or unlock the
        # lease while physical input remains owned.
        for _attempt in range(3):
            if not self.held and not self.active_process_groups:
                break
            try:
                self._release_input()
            except Exception as exc:
                self._release_failed = True
                reason = f"guardian close physical input release failed: {exc}"
                for callback in tuple(self._callbacks):
                    try:
                        callback(reason, self.generation)
                    except Exception:
                        continue
                time.sleep(min(self._watch_interval, 0.25))
                continue
            with self._lock:
                self._held = False
                self._held_keys.clear()
                self._held_buttons.clear()
                self._release_failed = False
            self._watchdog_state()
            if self.active_process_groups:
                time.sleep(min(self._watch_interval, 0.25))
                continue
            break
        if self.held or self.active_process_groups or self.observer_process_groups or self._release_failed:
            # Leave the non-daemon process and lease alive as the final owner
            # death fallback.  The caller can retry close after the release
            # command becomes available and every observer has been stopped;
            # safety is preferable to a false clean shutdown that loses a
            # tracked process group or observer.
            raise GuardianError("input guardian close could not prove process/input release")
        if self._watcher is not None and self._watcher is not threading.current_thread():
            self._watcher.join(timeout=1)
        if self._watchdog_write >= 0:
            try:
                os.write(self._watchdog_write, b"S\n")
            except OSError:
                pass
            try:
                os.close(self._watchdog_write)
            except OSError:
                pass
            self._watchdog_write = -1
        if self._watchdog_process is not None:
            self._watchdog_process.join(timeout=2)
            if self._watchdog_process.is_alive():
                self._watchdog_process.terminate()
                self._watchdog_process.join(timeout=1)
        if self._watchdog_ack_read >= 0:
            try:
                os.close(self._watchdog_ack_read)
            except OSError:
                pass
            self._watchdog_ack_read = -1
        self._close_release_spec()
        self.lease.release()
        if self._atexit_registered:
            atexit.unregister(self._close_at_exit)
            self._atexit_registered = False


def _watchdog_entry(
    owner_pid: int,
    owner_start: str | None,
    read_fd: int,
    interval: float,
    release_command: tuple[str, ...],
    release_env: dict[str, str],
    write_fd: int | None = None,
    release_fd: int | None = None,
    ready_fd: int | None = None,
    release_spec: NativeReleaseSpec | None = None,
    ack_fd: int | None = None,
) -> None:
    """Preserve the historical watchdog entrypoint and process-start seam."""

    _watchdog_entry_impl(
        owner_pid,
        owner_start,
        read_fd,
        interval,
        release_command,
        release_env,
        write_fd,
        release_fd,
        ready_fd,
        release_spec,
        ack_fd,
        InputGuardian._proc_start,
    )


__all__ = ["ActionCorrelation", "GuardianError", "InputGuardian", "InputLease"]
