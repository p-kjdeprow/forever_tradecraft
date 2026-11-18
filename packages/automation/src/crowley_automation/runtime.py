"""Public standalone operation runtime and host maintenance guard."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .backend import BackendIO, DesktopBackend
from .config import AutomationConfig, load_config
from .desktop_input import GuiLease, gui_lock_path
from .ledger import Ledger
from .model import Batch
from .runner import AutomationRunner, BatchAuthorization, RunnerConfig, SessionArm
from .targets import load_profiles


def default_ledger_path(environ: dict[str, str] | None = None) -> Path:
    source = os.environ if environ is None else environ
    home = source.get("HOME")
    if not isinstance(home, str) or not Path(home).is_absolute():
        raise ValueError("HOME must be set to an absolute path")
    state = source.get("XDG_STATE_HOME") or str(Path(home) / ".local" / "state")
    if not Path(state).is_absolute():
        raise ValueError("XDG_STATE_HOME must be an absolute path")
    return Path(state) / "crowley" / "automation" / "ledger.sqlite3"


def _operation_ledger(ledger_path: str | Path | None, io: BackendIO | None) -> Ledger:
    default = default_ledger_path()
    selected = default if ledger_path is None else Path(ledger_path).expanduser().absolute()
    if io is None and selected.resolve(strict=False) != default.resolve(strict=False):
        raise ValueError("production run and dry-run require the shared default automation ledger")
    return Ledger(selected)


def _runtime(
    batch: Batch,
    *,
    config: AutomationConfig,
    ledger: Ledger,
    lease: GuiLease,
    io: BackendIO | None,
) -> tuple[AutomationRunner, DesktopBackend]:
    return _runtime_components(config=config, ledger=ledger, lease=lease, io=io)


def _runtime_components(
    *,
    config: AutomationConfig,
    ledger: Ledger,
    lease: GuiLease,
    io: BackendIO | None,
) -> tuple[AutomationRunner, DesktopBackend]:
    targets, routes = load_profiles(config)
    limits = tuple(sorted((route_id, len(route.waypoints)) for route_id, route in routes.items()))
    runner_config = RunnerConfig(frozenset(targets), limits)
    backend = DesktopBackend(
        config,
        targets,
        routes,
        lease,
        io=io,
        stop_requested=ledger.stop_requested,
    )
    return AutomationRunner(ledger, runner_config), backend


@contextmanager
def seller_runtime(
    *,
    ledger_path: str | Path | None = None,
    gui_lease: GuiLease | None = None,
    config: AutomationConfig | None = None,
    io: BackendIO | None = None,
) -> Iterator[tuple[Ledger, AutomationRunner, DesktopBackend, GuiLease]]:
    """Hold the shared GUI lease for one complete foreground seller session."""
    ledger = _operation_ledger(ledger_path, io)
    selected_config = load_config() if config is None else config
    lease = gui_lease or GuiLease(gui_lock_path())
    runner, backend = _runtime_components(config=selected_config, ledger=ledger, lease=lease, io=io)
    with _lease_scope(lease):
        try:
            yield ledger, runner, backend, lease
        finally:
            backend.close()


def run_seller_child(
    batch: Batch,
    *,
    runner: AutomationRunner,
    seller_store: Any,
    session_id: str,
    decision_id: str,
    phase: str,
    backend: DesktopBackend,
) -> dict[str, object]:
    """Dispatch one policy-derived child through the runner's session gate."""
    result = runner.run_seller_child(
        batch,
        seller_store=seller_store,
        session_id=session_id,
        decision_id=decision_id,
        phase=phase,
        backend=backend,
    )
    return dict(result)


def arm_seller_backend(
    backend: DesktopBackend,
    *,
    ledger: Ledger,
    seller_store: Any,
    session_id: str,
    policy_expires_at: int,
    ah_target_id: str,
) -> None:
    """Bind all preparatory and protected backend input to one active seller session."""

    def stopped() -> bool:
        try:
            return ledger.stop_requested() or seller_store.status(session_id).state not in {
                "active",
                "completed",
            }
        except Exception:
            # A broken ledger or session lookup must fail closed at every input gate.
            return True

    backend.stop_requested = stopped
    backend.bind_seller_ah_target(ah_target_id)
    backend.set_deadline(policy_expires_at)


@contextmanager
def _lease_scope(lease: GuiLease) -> Iterator[None]:
    if lease.held:
        yield
    else:
        with lease.hold():
            yield


def run_batch(
    batch: Batch,
    authorization: BatchAuthorization,
    session_arm: SessionArm,
    confirmed_batch_id: str,
    *,
    ledger_path: str | Path | None = None,
    gui_lease: GuiLease | None = None,
    config: AutomationConfig | None = None,
    io: BackendIO | None = None,
) -> dict[str, object]:
    """Run one confirmed batch under the shared GUI lease and durable ledger."""

    ledger = _operation_ledger(ledger_path, io)
    selected_config = load_config() if config is None else config
    lease = gui_lease or GuiLease(gui_lock_path())
    runner, backend = _runtime(batch, config=selected_config, ledger=ledger, lease=lease, io=io)
    backend.set_deadline(min(batch.expires_at, authorization.expires_at, session_arm.expires_at))
    with _lease_scope(lease):
        try:
            status = runner.run(
                batch,
                authorization=authorization,
                session_arm=session_arm,
                confirmed_batch_id=confirmed_batch_id,
                backend=backend,
            )
            result = dict(status)
            result["cleanup"] = backend.cleanup_state
            return result
        finally:
            backend.close()


def dry_run_batch(
    batch: Batch,
    authorization: BatchAuthorization,
    session_arm: SessionArm,
    confirmed_batch_id: str,
    *,
    ledger_path: str | Path | None = None,
    gui_lease: GuiLease | None = None,
    config: AutomationConfig | None = None,
    io: BackendIO | None = None,
) -> dict[str, object]:
    """Perform native argument/cost preflight without a click or movement hold."""

    ledger = _operation_ledger(ledger_path, io)
    selected_config = load_config() if config is None else config
    lease = gui_lease or GuiLease(gui_lock_path())
    runner, backend = _runtime(batch, config=selected_config, ledger=ledger, lease=lease, io=io)
    backend.set_deadline(min(batch.expires_at, authorization.expires_at, session_arm.expires_at))
    with _lease_scope(lease):
        try:
            result = runner.preflight(
                batch,
                authorization=authorization,
                session_arm=session_arm,
                confirmed_batch_id=confirmed_batch_id,
                backend=backend,
            )
            result["cleanup"] = backend.cleanup_state
            return result
        finally:
            backend.close()


def maintenance_blocker(ledger_path: str | Path | None = None) -> str | None:
    """Return the stable host hold reason without creating missing local state."""

    path = default_ledger_path() if ledger_path is None else Path(ledger_path).expanduser().absolute()
    if not os.path.lexists(path):
        return None
    try:
        ledger = Ledger(path)
        recovered = ledger.recover_if_idle()
        if recovered is None:
            return "automation_running"
        if ledger.has_uncertain_effect():
            return "automation_uncertain"
        return None
    except (OSError, RuntimeError, ValueError, sqlite3.Error):
        return "automation_state_invalid"


__all__ = ["default_ledger_path", "dry_run_batch", "maintenance_blocker", "run_batch"]
