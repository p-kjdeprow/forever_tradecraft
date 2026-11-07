from __future__ import annotations

import json
import os
import socket
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from crowley.core.game_target import GameTarget, packaged_game_target, target_scan_plan
from crowley.core.watchlist import load_catalog
from crowley.session import cli
from crowley.session import config as config_module
from crowley.session.config import (
    MAX_RELOAD_DEADLINE_SECONDS,
    ClientConfig,
    SessionConfig,
    load_client_config,
    load_config,
)


def _write_client_config(path: Path, socket_path: Path, *, invalid_full_config: bool = False) -> None:
    routes_path = path.parent / "missing-routes.json"
    observer_path = path.parent / "missing-observer"
    lines = ["[session]", f"socket = {json.dumps(str(socket_path))}"]
    if invalid_full_config:
        lines.extend(
            (
                f"routes_file = {json.dumps(str(routes_path))}",
                f"identity_observer_executable = {json.dumps(str(observer_path))}",
                f"identity_observer_executable_sha256 = {json.dumps('0' * 64)}",
            )
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_valid_full_config(tmp_path: Path, socket_path: Path) -> tuple[Path, GameTarget]:
    target = replace(
        packaged_game_target(),
        game_root=str(tmp_path / "game"),
        wine_prefix=str(tmp_path / "wine"),
    )
    wow_root = Path(target.game_root) / target.product_dir
    saved_variables = wow_root / "WTF/Account/Test/SavedVariables/Crowley.lua"
    data_lua = wow_root / "Interface/AddOns/Crowley_Data/Data.lua"
    companion_state = tmp_path / "companion.json"
    saved_variables.parent.mkdir(parents=True)
    data_lua.parent.mkdir(parents=True)
    saved_variables.write_text("", encoding="utf-8")
    data_lua.write_text("", encoding="utf-8")
    companion_state.write_text("{}", encoding="utf-8")
    plan = target_scan_plan(load_catalog(), target)
    config_path = tmp_path / "session.toml"
    missing_routes = tmp_path / "missing-routes.json"
    config_path.write_text(
        "\n".join(
            (
                "[profile]",
                f"wine_prefix = {json.dumps(target.wine_prefix)}",
                f"window_class = {json.dumps(target.window_class)}",
                f"executable = {json.dumps(target.executable)}",
                f"realm = {json.dumps(target.realm)}",
                f"house = {json.dumps(target.house)}",
                f"client_build = {json.dumps(target.native_build)}",
                f"interface = {target.interface}",
                f"client_version = {json.dumps(target.client_version)}",
                f"player_faction = {json.dumps(target.faction)}",
                f"locale = {json.dumps(target.locale)}",
                f"plan_count = {len(plan['itemIDs'])}",
                "canvas_crop = [80, 48, 256, 256]",
                "model_crop = [80, 48, 1760, 984]",
                f"plan_hash = {json.dumps(plan['planHash'])}",
                f"plan_json = {json.dumps(plan['planJSON'])}",
                "",
                "[session]",
                f"socket = {json.dumps(str(socket_path))}",
                f"saved_variables = {json.dumps(str(saved_variables))}",
                f"companion_state = {json.dumps(str(companion_state))}",
                f"data_lua = {json.dumps(str(data_lua))}",
                f"routes_file = {json.dumps(str(missing_routes))}",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    return config_path, target


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


class _RecordingIPC:
    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.path: Path | None = None
        self.request: dict[str, object] | None = None
        self.timeout: float | None = None

    def send(self, path: Path, request: dict[str, object], *, timeout: float) -> dict[str, object]:
        self.path = path
        self.request = request
        self.timeout = timeout
        return self.response


def test_client_command_uses_configured_socket_with_invalid_full_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    socket_path = runtime / "session.sock"
    config_path = tmp_path / "session.toml"
    _write_client_config(config_path, socket_path, invalid_full_config=True)
    worker, requests, errors = _start_socket_server(socket_path, {"ok": True, "status": {"state": "PAUSED"}})
    monkeypatch.setattr(
        cli,
        "load_daemon_config",
        lambda *_args, **_kwargs: pytest.fail("client command loaded the full daemon configuration"),
    )

    try:
        assert cli.main(["--config", str(config_path), "pause", "--generation", "9"]) == 0
    finally:
        worker.join(timeout=2.0)
    assert not worker.is_alive()
    assert not errors
    assert requests == [{"command": "pause", "generation": 9}]
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_client_socket_override_does_not_read_unavailable_config(tmp_path: Path, capsys) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    socket_path = runtime / "session.sock"
    assert load_client_config(tmp_path / "missing-session.toml", socket_override=socket_path) == ClientConfig(
        socket=socket_path
    )
    worker, requests, errors = _start_socket_server(socket_path, {"ok": True, "status": {"state": "PAUSED"}})

    try:
        assert (
            cli.main(
                [
                    "--config",
                    str(tmp_path / "missing-session.toml"),
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


def test_native_construction_client_bound_covers_daemon_maximum(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    socket_path = runtime / "session.sock"
    config_path = tmp_path / "session.toml"
    _write_client_config(config_path, socket_path)
    server = _RecordingIPC({"ok": True, "status": {}})
    monkeypatch.setattr(cli, "_socket_request", server.send)
    monkeypatch.setattr(
        cli,
        "load_daemon_config",
        lambda *_args, **_kwargs: pytest.fail("client command loaded the full daemon configuration"),
    )

    assert cli.main(["--config", str(config_path), "native-construction-prepare", "--generation", "1"]) == 0

    assert server.path == socket_path
    assert server.request == {"command": "native_construction_prepare", "generation": 1}
    assert server.timeout == cli.CLIENT_NATIVE_CONSTRUCTION_TIMEOUT_SECONDS
    assert server.timeout > MAX_RELOAD_DEADLINE_SECONDS


def test_daemon_command_rejects_broken_routes_in_otherwise_valid_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    config_path, target = _write_valid_full_config(tmp_path, runtime / "session.sock")

    def load_full(path: Path, *, socket_override: str | Path | None = None) -> SessionConfig:
        return load_config(path, expected_target=target, socket_override=socket_override)

    monkeypatch.setattr(cli, "load_daemon_config", load_full)
    with pytest.raises(ValueError, match="routes_file is unavailable"):
        cli.main(["--config", str(config_path), "daemon"])


def test_client_config_rejects_relative_socket_path(tmp_path: Path) -> None:
    config_path = tmp_path / "session.toml"
    config_path.write_text('[session]\nsocket = "runtime/session.sock"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="session.socket must be an absolute path"):
        load_client_config(config_path)


def test_client_config_rejects_socket_symlink(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    socket_path = runtime / "session.sock"
    socket_path.symlink_to(runtime / "real.sock")
    config_path = tmp_path / "session.toml"
    _write_client_config(config_path, socket_path)

    with pytest.raises(ValueError, match="session.socket must not be a symlink"):
        load_client_config(config_path)


def test_client_config_rejects_non_private_socket_parent(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o755)
    runtime.chmod(0o755)
    config_path = tmp_path / "session.toml"
    _write_client_config(config_path, runtime / "session.sock")

    with pytest.raises(ValueError, match="session.socket parent directory must be private"):
        load_client_config(config_path)


def test_client_config_rejects_foreign_private_socket_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    config_path = tmp_path / "session.toml"
    _write_client_config(config_path, runtime / "session.sock")
    real_lstat = config_module.os.lstat

    def foreign_lstat(path: str | os.PathLike[str]) -> os.stat_result:
        metadata = real_lstat(path)
        if Path(path) == runtime:
            fields = list(metadata)
            fields[4] = metadata.st_uid + 1
            return os.stat_result(fields)
        return metadata

    monkeypatch.setattr(config_module.os, "lstat", foreign_lstat)
    with pytest.raises(ValueError, match="session.socket parent directory must be owned"):
        load_client_config(config_path)


def test_client_config_returns_only_socket_path(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    socket_path = runtime / "session.sock"
    config_path = tmp_path / "session.toml"
    _write_client_config(config_path, socket_path, invalid_full_config=True)

    config = load_client_config(config_path)

    assert config == ClientConfig(socket=socket_path)
