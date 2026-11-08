from __future__ import annotations

import signal
import socket
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from crowley.session import cli
from crowley.session.model import CLI_VERSION, LunaAdapter, ModelError
from crowley.session.motion import DEFAULT_MOVEMENT_CALIBRATION_PULSE_SECONDS
from crowley.session.startup import (
    TAKEOVER_DRILL_OPERATION_TIMEOUT_SECONDS,
    TAKEOVER_DRILL_TRANSPORT_MARGIN_SECONDS,
)


def _stub_cli_boundary(monkeypatch, tmp_path: Path, response: dict[str, Any]):
    config = SimpleNamespace(socket=tmp_path / "session.sock", journal=tmp_path / "session.jsonl")
    requests: list[tuple[Path, dict[str, Any], float]] = []

    monkeypatch.setattr(cli, "load_config", lambda _path: config)

    def socket_request(path: Path, request: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        requests.append((path, request, timeout))
        return response

    monkeypatch.setattr(cli, "_socket_request", socket_request)
    return requests


def test_motion_calibration_cli_serializes_shared_default(monkeypatch, tmp_path: Path, capsys) -> None:
    requests = _stub_cli_boundary(monkeypatch, tmp_path, {"ok": True, "status": {}})

    assert cli.main(["motion-movement-calibrate", "--generation", "11"]) == 0

    path, request, timeout = requests[0]
    assert path == tmp_path / "session.sock"
    assert request == {
        "command": "motion_movement_calibrate",
        "generation": 11,
        "pulseSeconds": DEFAULT_MOVEMENT_CALIBRATION_PULSE_SECONDS,
    }
    assert request["pulseSeconds"] == DEFAULT_MOVEMENT_CALIBRATION_PULSE_SECONDS
    assert timeout == 95.0
    assert '"ok": true' in capsys.readouterr().out


def test_motion_calibration_cli_preserves_explicit_server_rejection(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    requests = _stub_cli_boundary(
        monkeypatch,
        tmp_path,
        {"ok": False, "error": "movement basis calibration pulse is outside bounds"},
    )

    assert cli.main(["motion-movement-calibrate", "--generation", "11", "--pulse-seconds", "0.9"]) == 1

    _path, request, timeout = requests[0]
    assert request["pulseSeconds"] == 0.9
    assert timeout == 95.0
    assert "outside bounds" in capsys.readouterr().out


def test_takeover_drill_cli_budget_covers_server_lifecycle_and_transport(
    monkeypatch, tmp_path, capsys
) -> None:
    requests = _stub_cli_boundary(monkeypatch, tmp_path, {"ok": True, "status": {"state": "passed"}})

    assert cli.main(["takeover-drill", "--generation", "70"]) == 0

    _path, request, timeout = requests[0]
    assert request == {"command": "takeover_drill", "generation": 70}
    assert timeout == TAKEOVER_DRILL_OPERATION_TIMEOUT_SECONDS + TAKEOVER_DRILL_TRANSPORT_MARGIN_SECONDS
    assert timeout > 125.0
    assert '"state": "passed"' in capsys.readouterr().out


def test_motion_camera_calibration_cli_preserves_generation_and_boot(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    requests = _stub_cli_boundary(
        monkeypatch,
        tmp_path,
        {
            "ok": True,
            "status": {},
            "camera": {
                "identity": "client-motion",
                "generation": 11,
                "captureId": "camera:boot:1:4",
                "yawPerPixel": 0.1,
                "convention": "xy-facing-radians-v1",
                "boot": "boot",
            },
        },
    )

    assert cli.main(["motion-camera-calibrate", "--generation", "11", "--delta-pixels", "8"]) == 0

    _path, request, timeout = requests[0]
    assert request == {"command": "motion_camera_calibrate", "generation": 11, "deltaPixels": 8}
    assert timeout == 35.0
    output = capsys.readouterr().out
    assert '"generation": 11' in output
    assert '"boot": "boot"' in output


def test_paused_region_ipc_requires_exact_fields_and_forwards_original_timing() -> None:
    class Owner:
        def __init__(self) -> None:
            self.requests: list[dict[str, object]] = []

        def capture_paused_region_sample(self, **request: object) -> dict[str, object]:
            self.requests.append(request)
            return {"ok": True, "sample": {"generation": request["expected_generation"]}}

    controller = SimpleNamespace()
    owner = Owner()
    ipc = cli.SessionIPC(controller, owner)  # type: ignore[arg-type]
    request = {
        "command": "paused_region_sample",
        "generation": 9,
        "parent_started": 101.25,
        "deadline": 102.25,
        "config_file_sha256": "a" * 64,
        "config_semantic_sha256": "b" * 64,
    }
    assert ipc.dispatch(request) == {"ok": True, "sample": {"generation": 9}}
    assert owner.requests == [
        {
            "expected_generation": 9,
            "parent_started": 101.25,
            "deadline": 102.25,
            "config_file_sha256": "a" * 64,
            "config_semantic_sha256": "b" * 64,
        }
    ]
    assert ipc.dispatch({**request, "extra": True}) == {
        "ok": False,
        "error": "paused region request fields are malformed",
    }
    assert len(owner.requests) == 1
    assert cli.SessionIPC(controller).dispatch(request)["ok"] is False  # type: ignore[arg-type]


def test_bounded_ipc_reader_and_json_reject_malformed_records() -> None:
    left, right = socket.socketpair()
    try:
        right.sendall(b'{"ok":true}\n')
        assert cli._BoundedIPCReader(left).read_record(32) == b'{"ok":true}'
    finally:
        left.close()
        right.close()

    left, right = socket.socketpair()
    try:
        right.sendall(b"12345678")
        with pytest.raises(ValueError, match="unterminated|byte limit"):
            cli._BoundedIPCReader(left).read_record(8)
    finally:
        left.close()
        right.close()

    for invalid in (
        b'{"ok":true,"ok":false}',
        b'{"deadline":NaN}',
        b'{"deadline":Infinity}',
        b'{"ok":true} trailing',
    ):
        with pytest.raises(ValueError, match="strict JSON"):
            cli._parse_bounded_json(invalid)


def test_cli_server_rejects_oversized_unterminated_request_before_dispatch(tmp_path: Path) -> None:
    socket_path = tmp_path / "bounded-server.sock"
    owner_release = threading.Event()
    owner_started = threading.Event()
    owner_failure = RuntimeError("fixture owner stopped")
    server_errors: list[BaseException] = []

    class Owner:
        def run_forever(self) -> None:
            owner_started.set()
            assert owner_release.wait(2.0)
            raise owner_failure

    def run_server() -> None:
        try:
            cli.serve(socket_path, object(), Owner(), supervise_owner=True)  # type: ignore[arg-type]
        except BaseException as exc:
            server_errors.append(exc)

    server = threading.Thread(target=run_server, daemon=True)
    server.start()
    try:
        assert owner_started.wait(2.0)
        deadline = time.monotonic() + 2.0
        while not socket_path.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert socket_path.exists()
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(2.0)
            connection.connect(str(socket_path))
            connection.sendall(b"x" * (cli._IPC_REQUEST_MAX_BYTES + 1))
            response = connection.makefile("rb").readline(cli._IPC_RESPONSE_MAX_BYTES + 1)
        assert len(response) <= cli._IPC_RESPONSE_MAX_BYTES
        record = cli._parse_bounded_json(response[:-1])
        assert record["ok"] is False
        assert "unterminated" in record["error"] or "byte limit" in record["error"]
        assert server.is_alive()
    finally:
        owner_release.set()
        server.join(timeout=3.0)
    assert not server.is_alive()
    assert server_errors and isinstance(server_errors[0], RuntimeError)
    assert server_errors[0].__cause__ is owner_failure


@pytest.mark.parametrize(
    "payload",
    [
        b'{"ok":true,"ok":false}\n',
        b'{"ok":true,"value":NaN}\n',
        b'{"ok":true}',
        b'{"ok":true} {}\n',
        b"x" * (cli._IPC_RESPONSE_MAX_BYTES + 1),
    ],
)
def test_socket_request_rejects_malformed_truncated_trailing_and_oversized_responses(
    tmp_path: Path, payload: bytes
) -> None:
    socket_path = tmp_path / "bounded.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)
    received: list[bytes] = []

    def responder() -> None:
        with listener.accept()[0] as connection:
            reader = cli._BoundedIPCReader(connection)
            request = reader.read_record(cli._IPC_REQUEST_MAX_BYTES)
            received.append(request or b"")
            connection.sendall(payload)

    worker = threading.Thread(target=responder, daemon=True)
    worker.start()
    try:
        with pytest.raises((RuntimeError, ValueError)):
            cli._socket_request(socket_path, {"command": "status"}, timeout=2.0)
    finally:
        listener.close()
        worker.join(timeout=2.0)
    assert received == [b'{"command":"status"}']


def test_socket_request_uses_one_absolute_deadline_for_slow_drip_response(tmp_path: Path) -> None:
    socket_path = tmp_path / "slow-drip.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)

    def responder() -> None:
        try:
            with listener.accept()[0] as connection:
                cli._BoundedIPCReader(connection).read_record(cli._IPC_REQUEST_MAX_BYTES)
                for byte in b'{"ok":true}\n':
                    connection.sendall(bytes((byte,)))
                    time.sleep(0.04)
        except OSError:
            pass

    worker = threading.Thread(target=responder, daemon=True)
    worker.start()
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="absolute response deadline"):
            cli._socket_request(socket_path, {"command": "status"}, timeout=0.13)
    finally:
        listener.close()
        worker.join(timeout=1.0)
    elapsed = time.monotonic() - started
    assert elapsed < 0.3
    assert not worker.is_alive()


def test_socket_request_allows_only_bounded_capture_failure_evidence_grace(tmp_path: Path) -> None:
    socket_path = tmp_path / "capture-grace.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(socket_path))
    listener.listen(1)
    request_started = time.monotonic()
    sample_deadline = request_started + 0.12
    request = {"command": "paused_region_sample", "deadline": sample_deadline}

    def responder() -> None:
        with listener.accept()[0] as connection:
            cli._BoundedIPCReader(connection).read_record(cli._IPC_REQUEST_MAX_BYTES)
            time.sleep(max(0.0, sample_deadline - time.monotonic()) + 0.02)
            connection.sendall(b'{"ok":false,"error":"late owner failure"}\n')

    worker = threading.Thread(target=responder, daemon=True)
    worker.start()
    try:
        response = cli._socket_request(
            socket_path,
            request,
            timeout=1.0,
            response_deadline=sample_deadline,
            failure_evidence_grace=0.25,
        )
        assert time.monotonic() > sample_deadline
        assert response == {"ok": False, "error": "late owner failure"}
        with pytest.raises(ValueError, match="limited to paused region"):
            cli._socket_request(
                socket_path,
                {"command": "status"},
                timeout=1.0,
                failure_evidence_grace=0.01,
            )
        with pytest.raises(ValueError, match="between zero and 250ms"):
            cli._socket_request(
                socket_path,
                request,
                timeout=1.0,
                response_deadline=sample_deadline,
                failure_evidence_grace=0.251,
            )
    finally:
        listener.close()
        worker.join(timeout=1.0)
    assert not worker.is_alive()


def _model_for_process_test(neutral_dir: Path) -> LunaAdapter:
    adapter = LunaAdapter.__new__(LunaAdapter)
    adapter.neutral_dir = neutral_dir
    adapter.timeout = 1.0
    adapter.model = "gpt-6-luna"
    adapter.cli_version = CLI_VERSION
    adapter.codex_path = "/bin/true"
    adapter._cancelled = threading.Event()
    adapter._process_lock = threading.RLock()
    adapter._model_call_lock = threading.RLock()
    adapter._process = None
    adapter._cancel_epoch = 0
    return adapter


def test_model_cancel_before_process_registration_is_not_cleared(monkeypatch, tmp_path: Path) -> None:
    adapter = _model_for_process_test(tmp_path / "model-neutral")

    class Process:
        pid = 4242

        def __init__(self) -> None:
            self.returncode: int | None = None
            self.exited = threading.Event()

        def poll(self):
            return self.returncode

        def communicate(self, timeout=None):
            if not self.exited.wait(timeout):
                raise subprocess.TimeoutExpired(["model"], timeout)
            return "", ""

    process = Process()

    monkeypatch.setattr(
        "crowley.session.model.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["codex", "--version"], 0, stdout=f"{CLI_VERSION}\n", stderr=""
        ),
    )

    def popen(*_args, **_kwargs):
        adapter.cancel()
        return process

    monkeypatch.setattr("crowley.session.model.subprocess.Popen", popen)

    def killpg(_pid: int, signum: int) -> None:
        if signum == signal.SIGKILL:
            process.returncode = -signum
            process.exited.set()

    monkeypatch.setattr("crowley.session.model.os.killpg", killpg)
    with pytest.raises(ModelError, match="cancelled"):
        adapter._run_cli_unlocked(tmp_path / "schema", tmp_path / "image", prompt="test", timeout=0.5)
    assert process.returncode == -signal.SIGKILL
    assert adapter._process is None


def test_model_near_deadline_cleanup_is_bounded_and_retains_live_owner(monkeypatch, tmp_path: Path) -> None:
    adapter = _model_for_process_test(tmp_path / "model-neutral")

    class Process:
        pid = 4343
        returncode = None

        def poll(self):
            return self.returncode

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired(["model"], timeout)

    process = Process()
    monkeypatch.setattr(
        "crowley.session.model.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["codex", "--version"], 0, stdout=f"{CLI_VERSION}\n", stderr=""
        ),
    )
    monkeypatch.setattr("crowley.session.model.subprocess.Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr("crowley.session.model.os.killpg", lambda *_args: None)
    started = time.monotonic()
    with pytest.raises(ModelError, match="cleanup exceeded"):
        adapter._run_cli_unlocked(tmp_path / "schema", tmp_path / "image", prompt="test", timeout=0.01)
    assert time.monotonic() - started < 0.5
    assert adapter._process is process


def test_model_retained_live_owner_blocks_next_call_and_shutdown_reaps_it(
    monkeypatch, tmp_path: Path
) -> None:
    adapter = _model_for_process_test(tmp_path / "model-neutral")

    class Process:
        pid = 4444

        def __init__(self) -> None:
            self.returncode: int | None = None
            self.kill_count = 0

        def poll(self):
            return self.returncode

        def communicate(self, timeout=None):
            if self.returncode is None:
                raise subprocess.TimeoutExpired(["model"], timeout)
            return "", ""

    process = Process()
    popen_calls = 0
    signals: list[int] = []
    monkeypatch.setattr(
        "crowley.session.model.subprocess.run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            ["codex", "--version"], 0, stdout=f"{CLI_VERSION}\n", stderr=""
        ),
    )

    def popen(*_args, **_kwargs):
        nonlocal popen_calls
        popen_calls += 1
        return process

    monkeypatch.setattr("crowley.session.model.subprocess.Popen", popen)

    def killpg(_pid: int, signum: int) -> None:
        signals.append(signum)
        process.kill_count += 1
        # Leave the first bounded call live so it is retained as an owner;
        # shutdown must perform the follow-up escalation and reap it.
        if process.kill_count >= 3 and signum == signal.SIGKILL:
            process.returncode = -signum

    monkeypatch.setattr("crowley.session.model.os.killpg", killpg)
    with pytest.raises(ModelError, match="cleanup exceeded"):
        adapter._run_cli_unlocked(tmp_path / "schema", tmp_path / "image", prompt="test", timeout=0.01)
    assert adapter._process is process

    with pytest.raises(ModelError):
        adapter._run_cli_unlocked(tmp_path / "schema", tmp_path / "image", prompt="again", timeout=0.5)
    assert popen_calls == 1

    adapter.shutdown()
    assert process.returncode == -signal.SIGKILL
    assert adapter._process is None
    assert signals.count(signal.SIGKILL) >= 2
