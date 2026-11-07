"""Bounded, failure-only input calibration diagnostics."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import crowley.session.calibration as calibration_module
from crowley.session.calibration import InputCalibrationWorkflow
from crowley.session.controller import ControllerState
from crowley.session.daemon import SessionDaemon
from crowley.session.driver import CalibrationTicket, DesktopDriver, DriverError, DriverProfile, InputMonitor
from crowley.session.input_events import EventClass, parse_xi2_records
from tests.test_session_native_regressions import (
    FOREIGN,
    FULL_XI2_TRACE,
    TARGET,
    _event,
    _render_events,
    _WorkflowDriver,
    _WorkflowTarget,
)


def test_failure_snapshot_replays_complete_finite_input_profile(tmp_path: Path) -> None:
    """The full current keyboard vocabulary is validated before bounded retention."""

    driver = _WorkflowDriver(outside=False)
    window = _WorkflowTarget().window
    hierarchy = driver.input_hierarchy()
    operations = object.__new__(DesktopDriver)._calibration_keyboard_operations()
    keymap = {"F13": 8}
    for operation in operations:
        for step in operation.steps:
            keymap.setdefault(step.keysym, len(keymap) + 8)
    keyboard_events = [
        _event(EventClass.RAW_KEY_PRESS, master=3, source=5, timestamp=240000000, detail=8, child=None),
        _event(EventClass.KEY_PRESS, master=5, source=5, timestamp=240000001, detail=8),
        _event(EventClass.RAW_KEY_RELEASE, master=3, source=5, timestamp=240000002, detail=8, child=None),
        _event(EventClass.KEY_RELEASE, master=5, source=5, timestamp=240000003, detail=8),
    ]
    timestamp = 240000004
    for operation in operations:
        for step in operation.steps:
            detail = keymap[step.keysym]
            raw_class = EventClass.RAW_KEY_PRESS if step.phase == "press" else EventClass.RAW_KEY_RELEASE
            delivered_class = EventClass.KEY_PRESS if step.phase == "press" else EventClass.KEY_RELEASE
            keyboard_events.extend(
                (
                    _event(raw_class, master=3, source=5, timestamp=timestamp, detail=detail, child=None),
                    _event(delivered_class, master=5, source=5, timestamp=timestamp + 1, detail=detail),
                )
            )
            timestamp += 2
    assert len(operations) == 4 + len(DesktopDriver.COMMANDS)
    total_steps = sum(len(operation.steps) for operation in operations)
    assert total_steps > 0
    assert len(keyboard_events) == 4 + 2 * total_steps
    pointer_events = [event for event in driver._records if event.source_device != 5]
    events = pointer_events + keyboard_events
    assert InputCalibrationWorkflow._has_synthetic(
        events,
        window,
        5,
        4,
        hierarchy,
        keyboard_operations=operations,
        keyboard_keymap=tuple(sorted(keymap.items())),
        require_motion=True,
    )
    raw = _render_events(events).replace("\nEVENT type ", "\n\nEVENT type ")
    assert raw.count("EVENT type ") == len(events)
    assert len(parse_xi2_records(raw, require_event=True)) == len(events)
    monitor = InputMonitor(":test", lambda _event: None)

    def start_observer() -> None:
        raw_path = tmp_path / "finite-input-trace.txt"
        raw_path.write_text(raw, encoding="utf-8")
        monitor.observer.process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import pathlib,sys,time;sys.stdout.write(pathlib.Path(sys.argv[1]).read_text());"
                "sys.stdout.flush();time.sleep(5)",
                str(raw_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    monitor.observer.start = start_observer  # type: ignore[method-assign]
    monitor.start()
    try:
        monitor.wait_for_event(lambda seen: len(seen) == len(events), timeout=3)
        monitor.stop()
    finally:
        monitor.stop()

    snapshot = monitor.diagnostic_snapshot()

    assert snapshot["rawTruncated"] is True
    assert snapshot["rawBytes"] <= 1024 * 1024
    assert snapshot["rawBytesSeen"] == len(raw.encode())
    assert snapshot["rawBlocksSeen"] == len(events)
    retained = "".join(snapshot["rawBlocks"]) + str(snapshot["partialRaw"])
    assert retained != raw
    assert len(retained.encode()) <= 1024 * 1024


def test_failure_snapshot_retains_partial_parser_reason_and_caps() -> None:
    monitor = InputMonitor(":test", lambda _event: None)
    monitor._text = [FULL_XI2_TRACE + "EVENT type 17 (RawMotion)\n    device: "]
    monitor._failure_diagnostic = {
        "text": "secret-looking-but-bounded-observer-output",
        "reason": "parser rejected the final partial block",
    }
    monitor._parser = SimpleNamespace(
        failure_diagnostic={"reason": "missing required field", "text": "partial"}
    )

    snapshot = monitor.diagnostic_snapshot()

    assert snapshot["partialRaw"].startswith("EVENT type 17")
    assert snapshot["failureDiagnostic"]["reason"] == "parser rejected the final partial block"
    assert snapshot["parserFailure"]["reason"] == "missing required field"

    monitor._text = ["EVENT type 17 (RawMotion)\n" + ("x" * (1024 * 1024 + 128))]
    capped = monitor.diagnostic_snapshot()
    assert capped["rawTruncated"] is True
    assert capped["rawBytesSeen"] > 1024 * 1024
    assert capped["rawBytes"] <= 1024 * 1024
    assert len(str(capped["partialRaw"]).encode()) <= 1024 * 1024


class _FailureObserver:
    def __init__(self, callback) -> None:
        self.callback = callback
        self.stopped = False

    def collect_until(self, predicate, *, timeout: float) -> str:
        del timeout
        assert predicate([]) is False
        raise DriverError("XI2 calibration event deadline expired")

    def stop(self) -> str:
        self.stopped = True
        return FULL_XI2_TRACE

    def diagnostic_snapshot(self) -> dict[str, object]:
        return {
            "rawBlocks": [FULL_XI2_TRACE],
            "partialRaw": "",
            "rawBytes": len(FULL_XI2_TRACE.encode()),
            "rawBytesSeen": len(FULL_XI2_TRACE.encode()),
            "rawBlocksSeen": 12,
            "rawBlocksRetained": 12,
            "rawTruncated": False,
            "parsedEventCount": 12,
            "parserFailure": None,
            "failureDiagnostic": {"reason": "synthetic profile did not complete"},
            "observerAlive": False,
        }


class _ResettingFailureTarget(_WorkflowTarget):
    def close(self) -> None:
        self.window = None


class _FailureDriver(_WorkflowDriver):
    def __init__(self) -> None:
        super().__init__(outside=False)
        self.observer = None

    def start_input_observer(self, *, calibration: bool, callback):
        assert calibration
        self.observer = _FailureObserver(callback)
        return self.observer

    def stop_input_observer(self, observer, *, already_stopped: bool = False) -> None:
        del already_stopped
        observer.stop()


class _BoundedInputMonitor(InputMonitor):
    """Use the production reader/parser with a short fixture timeout."""

    def collect_until(self, predicate, *, timeout: float) -> str:
        return super().collect_until(predicate, timeout=min(timeout, 0.1))


class _RealMonitorFailureDriver(_FailureDriver):
    def __init__(self, *, invalid_suffix: str = "") -> None:
        super().__init__()
        self.real_monitor: InputMonitor | None = None
        self.invalid_suffix = invalid_suffix

    def start_input_observer(self, *, calibration: bool, callback):
        assert calibration
        monitor = _BoundedInputMonitor(":test", callback)
        raw = (
            FULL_XI2_TRACE
            + f"\nEVENT type 17 (RawMotion)\n    device: {self.invalid_suffix}\n"
            + "EVENT type 2 (KeyPress)\n"
        )

        def start() -> None:
            monitor.observer.process = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    f"import sys,time;sys.stdout.write({raw!r});sys.stdout.flush();time.sleep(5)",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )

        monitor.observer.start = start  # type: ignore[method-assign]
        monitor.start()
        self.real_monitor = monitor
        self.observer = monitor
        return monitor

    def stop_input_observer(self, observer, *, already_stopped: bool = False) -> None:
        del already_stopped
        observer.stop()


def test_workflow_failure_context_records_real_predicate_reasons_and_cleanup() -> None:
    driver = _FailureDriver()
    target = _ResettingFailureTarget()
    workflow = InputCalibrationWorkflow(driver, display=":test", window_factory=lambda: target)

    with pytest.raises(DriverError, match="XI2 calibration event deadline expired"):
        workflow.run(human_timeout=1)

    context = workflow.failure_context
    assert context is not None
    assert context["phase"] == "pre_human"
    assert context["stage"] == "synthetic"
    assert context["readinessPublished"] is False
    assert context["syntheticPredicate"]["count"] >= 1
    assert context["syntheticPredicate"]["first"]
    assert context["syntheticPredicate"]["last"]
    assert context["observer"]["failureDiagnostic"]["reason"] == "synthetic profile did not complete"
    assert context["replayable"] is False
    assert driver.queued_calibration_count == 0
    assert driver.observer.stopped is True
    assert target.window is None


def test_workflow_failure_context_uses_real_monitor_parser_and_cleanup() -> None:
    """The composed failure path snapshots the production reader/parser."""

    driver = _RealMonitorFailureDriver()
    target = _ResettingFailureTarget()
    workflow = InputCalibrationWorkflow(driver, display=":test", window_factory=lambda: target)

    with pytest.raises(DriverError, match="XI2 calibration event deadline expired"):
        workflow.run(human_timeout=1)

    context = workflow.failure_context
    assert context is not None
    observer = context["observer"]
    assert isinstance(observer, dict)
    assert observer["rawBlocks"]
    assert observer["partialRaw"]
    assert observer["parserFailure"]["reason"] == "RawMotion: missing blank-line delimiter"
    assert observer["failureDiagnostic"]["reason"] == "RawMotion: missing blank-line delimiter"
    assert observer["observerAlive"] is False
    assert context["error"] == "XI2 calibration event deadline expired"
    assert context["syntheticPredicate"]["count"] >= 1
    assert context["syntheticPredicate"]["first"] == context["syntheticPredicate"]["last"]
    assert context["replayable"] is False
    assert target.window is None
    assert driver.real_monitor is not None
    assert driver.real_monitor.thread is not None
    assert not driver.real_monitor.thread.is_alive()


def test_daemon_persists_actual_synthetic_origin_query(tmp_path: Path, monkeypatch) -> None:
    """The real producer query survives workflow cleanup and daemon persistence."""

    class OriginFailureDriver(_WorkflowDriver):
        def __init__(self) -> None:
            super().__init__(outside=False)
            backend = self.backend
            backend.profile = DriverProfile(display=":test", xauthority="Xauthority")
            backend.xauthority = "Xauthority"
            backend._calibration_focus_ticket = None
            backend._last_synthetic_motion_query = None
            backend._last_synthetic_pointer_diagnostic = None

            def run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                if args[:2] == ["xdotool", "getwindowgeometry"]:
                    return subprocess.CompletedProcess(
                        args,
                        0,
                        "WINDOW=0x600119\nX=0\nY=0\nWIDTH=480\nHEIGHT=240\nSCREEN=0\n",
                        "",
                    )
                if args == ["xdotool", "getwindowfocus"]:
                    return subprocess.CompletedProcess(args, 0, str(TARGET), "")
                return subprocess.CompletedProcess(args, 0, "", "")

            backend._run = run  # type: ignore[method-assign]
            pointer_reads = iter(((100, 100, TARGET), (100, 100, FOREIGN)))
            backend.pointer_position = lambda **_kwargs: next(pointer_reads)  # type: ignore[method-assign]

        def issue_calibration_ticket(self, window):
            self.calibration_ticket = CalibrationTicket(
                epoch=self.calibration_epoch,
                target_pid=window.pid,
                target_xid=window.xid,
                target_xids=window.allowed_event_target_xids,
                title=window.title,
                geometry=window.geometry,
                managed_frame_xid=window.managed_frame_xid or 0,
                managed_frame_geometry=window.managed_frame_geometry,
                target_executable=window.executable,
                target_process_start=window.process_start,
            )
            return self.calibration_ticket

        def synthetic_target_keyboard_profile(self, window, *, operations, calibration_ticket):
            assert tuple(operations) == self._keyboard_operations
            left, top, width, height = window.geometry
            self.backend.synthetic_calibration(
                hex(window.xid),
                left + width // 2,
                top + height // 2,
                calibration_ticket=calibration_ticket,
            )

    driver = OriginFailureDriver()
    target = _ResettingFailureTarget()
    workflows: list[InputCalibrationWorkflow] = []

    class FixtureWorkflow(InputCalibrationWorkflow):
        def __init__(self, fixture_driver, *, display, on_ready):
            super().__init__(
                fixture_driver,
                display=display,
                window_factory=lambda: target,
                on_ready=on_ready,
            )
            workflows.append(self)

    monkeypatch.setattr(calibration_module, "InputCalibrationWorkflow", FixtureWorkflow)

    class Lock:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    daemon = SessionDaemon.__new__(SessionDaemon)
    daemon.driver = driver
    daemon.config = SimpleNamespace(
        profile=driver.profile,
        journal=tmp_path / "state" / "journal.json",
    )
    daemon.controller = SimpleNamespace(
        lock=Lock(),
        status=lambda: SimpleNamespace(state=ControllerState.INPUT_CALIBRATION, generation=1),
    )
    daemon._begin_calibration = lambda *_args, **_kwargs: None
    daemon._validate_runtime_paths = lambda: None
    daemon._set_calibration_readiness = lambda _readiness: None
    finished: dict[str, object] = {}
    daemon._finish_calibration = lambda **kwargs: finished.update(kwargs)

    with pytest.raises(DriverError, match="origin is outside the owned target"):
        daemon.run_input_calibration()

    artifact = tmp_path / "state" / "input-calibration-diagnostics" / "latest-pre-human-failure.json"
    assert artifact.is_file()
    assert stat_mode(artifact) == 0o600
    persisted = json.loads(artifact.read_text())
    assert persisted["phase"] == "pre_human"
    assert persisted["stage"] == "synthetic"
    assert persisted["readinessPublished"] is False
    assert len(persisted["sourceBindings"]["crowley.session.driver"]) == 64
    assert persisted["targetPlacement"] == {
        "producer": "InputCalibrationWorkflow.target_pointer_preflight",
        "targetXid": TARGET,
        "targetGeometry": [0, 0, 480, 240],
        "origin": {"x": 100, "y": 100, "windowXid": TARGET},
        "predicates": {"certifiedHitWindow": True, "insideTargetGeometry": True},
    }
    assert persisted["syntheticPointer"] == {
        "producer": "X11Backend.synthetic_calibration",
        "targetXid": TARGET,
        "targetGeometry": [0, 0, 480, 240],
        "requested": [240, 120],
        "origin": {"x": 100, "y": 100, "windowXid": FOREIGN},
        "originPredicates": {
            "certifiedHitWindow": False,
            "insideTargetGeometry": True,
            "nonzeroDisplacement": True,
        },
    }
    assert len(workflows) == 1
    context = workflows[0].failure_context
    assert context is not None
    assert context["readinessPublished"] is False
    assert context["syntheticPointer"] == persisted["syntheticPointer"]
    assert driver.observer is not None
    assert target.window is None
    assert finished["state"] == "error"


def test_daemon_failure_path_persists_actual_workflow_context(tmp_path: Path, monkeypatch) -> None:
    """The daemon exception boundary retains the actual workflow context."""

    driver = _RealMonitorFailureDriver()

    class FixtureWorkflow(InputCalibrationWorkflow):
        def __init__(self, fixture_driver, *, display, on_ready):
            super().__init__(
                fixture_driver,
                display=display,
                window_factory=_ResettingFailureTarget,
                on_ready=on_ready,
            )

    monkeypatch.setattr(calibration_module, "InputCalibrationWorkflow", FixtureWorkflow)

    class Lock:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    daemon = SessionDaemon.__new__(SessionDaemon)
    daemon.driver = driver
    daemon.config = SimpleNamespace(
        profile=driver.profile,
        journal=tmp_path / "state" / "journal.json",
    )
    daemon.controller = SimpleNamespace(
        lock=Lock(),
        status=lambda: SimpleNamespace(state=ControllerState.INPUT_CALIBRATION, generation=1),
    )
    daemon._begin_calibration = lambda *_args, **_kwargs: None
    daemon._validate_runtime_paths = lambda: None
    daemon._set_calibration_readiness = lambda _readiness: None
    finished: dict[str, object] = {}
    daemon._finish_calibration = lambda **kwargs: finished.update(kwargs)

    with pytest.raises(DriverError, match="XI2 calibration event deadline expired"):
        daemon.run_input_calibration()

    artifact = tmp_path / "state" / "input-calibration-diagnostics" / "latest-pre-human-failure.json"
    assert artifact.is_file()
    persisted = json.loads(artifact.read_text())
    assert persisted["stage"] == "synthetic"
    assert persisted["readinessPublished"] is False
    assert len(persisted["sourceBindings"]["crowley.session.driver"]) == 64
    assert persisted["observer"]["rawBlocks"]
    assert persisted["observer"]["partialRaw"]
    assert persisted["observer"]["parserFailure"]["reason"] == "RawMotion: missing blank-line delimiter"
    assert persisted["observer"]["failureDiagnostic"]["reason"] == "RawMotion: missing blank-line delimiter"
    assert driver.real_monitor is not None
    assert driver.real_monitor.observer.process is None
    assert driver.real_monitor.thread is not None
    assert not driver.real_monitor.thread.is_alive()

    original_replace = os.replace
    failing_driver = _RealMonitorFailureDriver()
    daemon.driver = failing_driver
    daemon.config = SimpleNamespace(
        profile=failing_driver.profile,
        journal=tmp_path / "write-failure" / "state" / "journal.json",
    )
    monkeypatch.setattr(os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("replace failed")))
    with pytest.raises(DriverError, match="XI2 calibration event deadline expired"):
        daemon.run_input_calibration()
    assert failing_driver.real_monitor is not None
    assert failing_driver.real_monitor.observer.process is None
    assert failing_driver.real_monitor.thread is not None
    assert not failing_driver.real_monitor.thread.is_alive()
    diagnostics = tmp_path / "write-failure" / "state" / "input-calibration-diagnostics"
    assert not (diagnostics / ".latest-pre-human-failure.json.tmp").exists()

    monkeypatch.setattr(os, "replace", original_replace)
    restored_driver = _RealMonitorFailureDriver(invalid_suffix="second-context")
    daemon.driver = restored_driver
    with pytest.raises(DriverError, match="XI2 calibration event deadline expired"):
        daemon.run_input_calibration()
    restored_artifact = json.loads((diagnostics / "latest-pre-human-failure.json").read_text())
    assert any("second-context" in block for block in restored_artifact["observer"]["rawBlocks"])
    assert not (diagnostics / ".latest-pre-human-failure.json.tmp").exists()
    assert restored_driver.real_monitor is not None
    assert restored_driver.real_monitor.observer.process is None
    assert restored_driver.real_monitor.thread is not None
    assert not restored_driver.real_monitor.thread.is_alive()
    assert finished["state"] == "error"


def _failure_context() -> dict[str, object]:
    return {
        "schema": "input-calibration-failure-v1",
        "phase": "pre_human",
        "stage": "synthetic",
        "errorType": "CalibrationError",
        "error": "XI2 calibration event deadline expired",
        "readinessPublished": False,
        "observer": {
            "rawBlocks": [FULL_XI2_TRACE],
            "partialRaw": "",
            "rawBytes": len(FULL_XI2_TRACE.encode()),
            "rawBlocksSeen": 12,
            "rawTruncated": False,
            "parsedEventCount": 12,
        },
        "replayable": False,
        "input": {"inputToolDigest": "tool-digest"},
    }


def test_daemon_persists_only_latest_private_failure_artifact(tmp_path: Path) -> None:
    daemon = SessionDaemon.__new__(SessionDaemon)
    daemon.config = SimpleNamespace(journal=tmp_path / "state" / "journal.json")

    daemon._persist_input_calibration_failure(_failure_context())
    artifact_dir = tmp_path / "state" / "input-calibration-diagnostics"
    artifact = artifact_dir / "latest-pre-human-failure.json"
    assert artifact.exists()
    assert stat_mode(artifact) == 0o600
    assert stat_mode(artifact_dir) == 0o700
    payload = json.loads(artifact.read_text())
    assert payload["error"] == "XI2 calibration event deadline expired"
    assert payload["observer"]["rawBlocks"]
    assert "Xauthority" not in artifact.read_text()

    daemon._persist_input_calibration_failure(_failure_context() | {"error": "newest"})
    assert list(artifact_dir.glob("*.json")) == [artifact]
    assert json.loads(artifact.read_text())["error"] == "newest"


def test_diagnostic_write_failure_does_not_escape_or_create_success_artifact(
    tmp_path: Path, monkeypatch
) -> None:
    daemon = SessionDaemon.__new__(SessionDaemon)
    daemon.config = SimpleNamespace(journal=tmp_path / "state" / "journal.json")

    original_replace = os.replace
    monkeypatch.setattr(os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("replace failed")))
    daemon._persist_input_calibration_failure(_failure_context())
    assert not (
        tmp_path / "state" / "input-calibration-diagnostics" / "latest-pre-human-failure.json"
    ).exists()
    monkeypatch.setattr(os, "replace", original_replace)

    daemon._persist_input_calibration_failure(_failure_context() | {"readinessPublished": True})
    assert not (
        tmp_path / "state" / "input-calibration-diagnostics" / "latest-pre-human-failure.json"
    ).exists()


def test_diagnostic_cleanup_failure_keeps_one_bounded_owned_slot(tmp_path: Path, monkeypatch) -> None:
    daemon = SessionDaemon.__new__(SessionDaemon)
    daemon.config = SimpleNamespace(journal=tmp_path / "state" / "journal.json")
    diagnostics = tmp_path / "state" / "input-calibration-diagnostics"
    original_replace = os.replace
    original_unlink = Path.unlink

    monkeypatch.setattr(os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("replace failed")))

    def fail_owned_temp_unlink(path: Path, *, missing_ok: bool = False) -> None:
        if path.name == ".latest-pre-human-failure.json.tmp":
            raise OSError("owned temp cleanup failed")
        original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fail_owned_temp_unlink)
    daemon._persist_input_calibration_failure(_failure_context() | {"error": "first"})
    daemon._persist_input_calibration_failure(_failure_context() | {"error": "second"})
    assert list(diagnostics.iterdir()) == [diagnostics / ".latest-pre-human-failure.json.tmp"]

    monkeypatch.setattr(Path, "unlink", original_unlink)
    monkeypatch.setattr(os, "replace", original_replace)
    assert not (diagnostics / "latest-pre-human-failure.json").exists()


def stat_mode(path: Path) -> int:
    return path.stat().st_mode & 0o777
