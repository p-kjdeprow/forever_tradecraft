from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path

import pytest

from crowley.session import cli
from crowley.session import config as config_module
from crowley.session.config import ClientConfig, load_client_config


def _start_socket_server(
    path: Path, response: dict[str, object]
) -> tuple[threading.Thread, list[dict[str, object]], list[BaseException]]:
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
    except PermissionError:
        listener.close()
        pytest.skip("sandbox denies AF_UNIX listener operations")
    listener.listen(1)
    requests: list[dict[str, object]] = []
    errors: list[BaseException] = []

    def serve() -> None:
        try:
            with listener:
                connection, _ = listener.accept()
                with connection:
                    record = bytearray()
                    while not record.endswith(b"\n"):
                        chunk = connection.recv(4096)
                        if not chunk:
                            raise RuntimeError("fake server received a truncated request")
                        record.extend(chunk)
                    request = json.loads(bytes(record))
                    if not isinstance(request, dict):
                        raise TypeError("fake server received a non-object request")
                    requests.append(request)
                    connection.sendall(json.dumps(response).encode("utf-8") + b"\n")
        except BaseException as exc:
            errors.append(exc)

    worker = threading.Thread(target=serve)
    worker.start()
    return worker, requests, errors


def test_client_socket_override_does_not_load_host_or_tunables(tmp_path: Path, capsys) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    socket_path = runtime / "session.sock"
    config_path = tmp_path / "missing-session.toml"
    assert load_client_config(config_path, socket_override=socket_path) == ClientConfig(socket=socket_path)
    worker, requests, errors = _start_socket_server(socket_path, {"ok": True, "status": {"state": "PAUSED"}})
    try:
        assert (
            cli.main(
                [
                    "--config",
                    str(config_path),
                    "--socket",
                    str(socket_path),
                    "pause",
                    "--generation",
                    "10",
                ]
            )
            == 0
        )
    finally:
        worker.join(timeout=2.0)
    assert not worker.is_alive()
    assert not errors
    assert requests == [{"command": "pause", "generation": 10}]
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_client_config_ignores_unreadable_host_and_tunables(tmp_path: Path, monkeypatch) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    (runtime / "crowley").mkdir(mode=0o700)
    config_dir = tmp_path / "config" / "crowley"
    config_dir.mkdir(parents=True)
    host = config_dir / "host.toml"
    host.write_text("not valid host configuration\n", encoding="utf-8")
    host.chmod(0)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    assert load_client_config(tmp_path / "missing-session.toml") == ClientConfig(
        socket=runtime / "crowley/session.sock"
    )


def test_client_config_rejects_socket_parent_owned_by_another_user(tmp_path: Path, monkeypatch) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    socket_path = runtime / "session.sock"
    current_uid = os.getuid()
    monkeypatch.setattr(config_module.os, "getuid", lambda: current_uid + 1)
    with pytest.raises(ValueError, match="owned by the current user"):
        load_client_config(tmp_path / "session.toml", socket_override=socket_path)


def test_client_config_rejects_relative_socket_override(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="session.socket must be an absolute path"):
        load_client_config(tmp_path / "session.toml", socket_override="runtime/session.sock")


def test_client_config_rejects_socket_symlink(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    socket_path = runtime / "session.sock"
    socket_path.symlink_to(runtime / "real.sock")
    with pytest.raises(ValueError, match="session.socket must not be a symlink"):
        load_client_config(tmp_path / "session.toml", socket_override=socket_path)
