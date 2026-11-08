from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from crowley.config import (
    BinaryConfigError,
    CompanionTunables,
    HostConfig,
    HostConfigError,
    OpsTunables,
    PinnedObserver,
    SessionTunables,
    TunablesError,
    load_host_config,
    load_pinned_binaries,
    load_session_tunables,
    resolve_companion,
    resolve_ops,
    resolve_session,
)
from crowley.config import resolved as resolved_module
from crowley.session.config import load_config
from crowley.session.driver import X11Backend
from tests.support.config import (
    fixture_companion_tunables,
    fixture_host,
    fixture_session_tunables,
    fixture_target,
    fixture_xdg,
    write_host_toml,
    write_session_toml,
)


def test_resolvers_derive_every_service_path_from_one_identity(tmp_path: Path) -> None:
    target = fixture_target()
    host = fixture_host(tmp_path)
    xdg = fixture_xdg(tmp_path)
    session = resolve_session(target, host, fixture_session_tunables(), xdg=xdg)
    companion = resolve_companion(
        target,
        host,
        fixture_companion_tunables(),
        xdg=xdg,
    )
    ops = resolve_ops(target, host, OpsTunables(), xdg=xdg)

    state = xdg.crowley_state / target.house / target.target_id / host.wtf_account / host.character
    flavor = Path(host.game_root) / target.product_dir
    assert session.flavor_dir == flavor
    assert session.saved_variables == flavor / "WTF/Account/account#1/SavedVariables/Crowley.lua"
    assert session.data_lua == flavor / "Interface/AddOns/Crowley_Data/Data.lua"
    assert session.queue_lua == flavor / "Interface/AddOns/Crowley_Data/Queue.lua"
    assert session.journal == state / "journal.json"
    assert session.companion_state == state / "companion.json"
    assert session.socket == xdg.runtime_dir / "crowley/session.sock"
    assert session.capture_dir == xdg.runtime_dir / "crowley/captures"
    assert companion.state_file == state / "companion.json"
    assert companion.saved_variables == session.saved_variables
    assert companion.data_lua == session.data_lua
    assert ops.flavor_dir == flavor
    assert ops.state_dir == state
    assert session.route_file.is_file()
    assert session.client_build == target.native_build
    assert session.interface == target.interface
    assert session.player_faction == target.faction
    assert session.locale == target.locale
    assert session.client_version == target.client_version
    assert session.cadence_seconds == target.cadence_seconds
    assert session.sweep_cap_seconds == target.sweep_cap_seconds
    route = json.loads(session.route_file.read_text(encoding="utf-8"))
    assert all(not str(source).startswith("/") for source in route["sources"])


def test_host_loader_reports_all_missing_keys_and_rejects_unknown(tmp_path: Path) -> None:
    path = tmp_path / "host.toml"
    path.write_text("unknown = true\n", encoding="utf-8")
    with pytest.raises(HostConfigError) as error:
        load_host_config(path)
    message = str(error.value)
    assert "unknown" in message
    assert "wine_prefix" in message
    assert "game_root" in message
    assert "model_crop" in message


def test_tunables_reject_unknown_sections_and_keys(tmp_path: Path) -> None:
    path = tmp_path / "session.toml"
    path.write_text('[session]\nmode = "repeat"\nunknown = 1\n\n[profile]\n', encoding="utf-8")
    with pytest.raises(TunablesError, match="unknown or missing tables"):
        load_session_tunables(path)


def test_pinned_codex_configuration_requires_an_explicit_location() -> None:
    with pytest.raises(BinaryConfigError, match="CROWLEY_CODEX_PATH is required"):
        load_pinned_binaries({})


def test_session_loader_consumes_only_host_and_tunables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    (runtime / "crowley").mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("CROWLEY_CODEX_PATH", str(tmp_path / "codex"))
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_dir))
    host = fixture_host(tmp_path)
    host_path = config_dir / "crowley" / "host.toml"
    host_path.parent.mkdir()
    write_host_toml(host_path, host)
    session_path = config_dir / "crowley" / "session.toml"
    write_session_toml(session_path)

    loaded = load_config(session_path)

    assert loaded.profile.wine_prefix == host.wine_prefix
    assert (
        loaded.saved_variables
        == Path(host.game_root) / "_classic_beta_" / "WTF/Account/account#1/SavedVariables/Crowley.lua"
    )
    assert loaded.socket == runtime / "crowley/session.sock"
    assert loaded.routes_file is not None and loaded.routes_file.is_file()


def test_production_x11_uses_observer_from_resolved_fixture_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = fixture_target()
    xdg = fixture_xdg(tmp_path)
    monkeypatch.setenv("HOME", str(xdg.config_home.parent))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg.config_home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg.cache_home))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg.runtime_dir))
    codex = tmp_path / "codex"
    codex.write_text("#!/bin/sh\n", encoding="ascii")
    codex.chmod(0o700)
    monkeypatch.setenv("CROWLEY_CODEX_PATH", str(codex))
    host_path = xdg.crowley_config / "host.toml"
    host_path.parent.mkdir(parents=True)
    write_host_toml(host_path, fixture_host(tmp_path))
    session_path = xdg.crowley_config / "session.toml"
    write_session_toml(session_path)
    observer = tmp_path / "observer"
    observer.write_bytes(b"fixture identity observer")
    observer.chmod(0o700)
    observer_pin = PinnedObserver(
        observer,
        hashlib.sha256(observer.read_bytes()).hexdigest(),
        "1",
    )
    monkeypatch.setattr(resolved_module, "resolve_pinned_observer", lambda _xdg: observer_pin)

    loaded = load_config(session_path, expected_target=target)

    class InputTool:
        def close(self) -> None:
            return None

    backend = X11Backend.production(loaded.profile, input_tool=InputTool())  # type: ignore[arg-type]
    try:
        assert backend.resolver_kind == "pinned-rust-v1"
        assert backend._production_observer_required is True
    finally:
        backend.close()


@pytest.mark.parametrize(
    "values",
    [
        {
            "quiet_margin_seconds": 0,
            "upload_deadline_seconds": 60,
            "reload_deadline_seconds": 45,
            "ack_deadline_seconds": 60,
        },
        {
            "quiet_margin_seconds": 900,
            "upload_deadline_seconds": 60,
            "reload_deadline_seconds": 900,
            "ack_deadline_seconds": 60,
        },
        {
            "quiet_margin_seconds": 15,
            "upload_deadline_seconds": 0,
            "reload_deadline_seconds": 45,
            "ack_deadline_seconds": 60,
        },
        {
            "quiet_margin_seconds": 15,
            "upload_deadline_seconds": 61,
            "reload_deadline_seconds": 45,
            "ack_deadline_seconds": 60,
        },
        {
            "quiet_margin_seconds": 15,
            "upload_deadline_seconds": 60,
            "reload_deadline_seconds": 0,
            "ack_deadline_seconds": 60,
        },
        {
            "quiet_margin_seconds": 15,
            "upload_deadline_seconds": 60,
            "reload_deadline_seconds": 601,
            "ack_deadline_seconds": 60,
        },
        {
            "quiet_margin_seconds": 15,
            "upload_deadline_seconds": 60,
            "reload_deadline_seconds": 45,
            "ack_deadline_seconds": 0,
        },
        {
            "quiet_margin_seconds": 15,
            "upload_deadline_seconds": 60,
            "reload_deadline_seconds": 45,
            "ack_deadline_seconds": 601,
        },
        {
            "quiet_margin_seconds": 45,
            "upload_deadline_seconds": 60,
            "reload_deadline_seconds": 45,
            "ack_deadline_seconds": 60,
        },
    ],
)
def test_session_tunables_reject_invalid_operational_constraints(values: dict[str, int]) -> None:
    with pytest.raises(ValueError):
        SessionTunables.model_validate(values)


@pytest.mark.parametrize(
    "values",
    [
        {"poll_seconds": float("nan"), "refresh_minutes": 5.0},
        {"poll_seconds": 3.0, "refresh_minutes": -1.0},
        {"poll_seconds": 400.0, "refresh_minutes": 5.0},
    ],
)
def test_companion_tunables_reject_nonfinite_or_inconsistent_intervals(values: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        CompanionTunables.model_validate(values)


def test_host_geometry_rejects_crops_outside_the_client(tmp_path: Path) -> None:
    values = fixture_host(tmp_path).model_dump()
    values["model_crop"] = (0, 0, values["client_width"], values["client_height"] + 1)
    with pytest.raises(ValueError, match="model_crop exceeds client geometry"):
        HostConfig.model_validate(values)


@pytest.mark.parametrize(
    ("width", "height", "message"),
    [
        (639, 480, "model_crop must be at least 640x480"),
        (640, 479, "model_crop must be at least 640x480"),
    ],
)
def test_host_geometry_rejects_model_crop_below_each_minimum(
    tmp_path: Path, width: int, height: int, message: str
) -> None:
    values = fixture_host(tmp_path).model_dump()
    values["model_crop"] = (0, 0, width, height)
    with pytest.raises(ValueError, match=message):
        HostConfig.model_validate(values)


def test_host_geometry_rejects_equal_canvas_and_model_crops(tmp_path: Path) -> None:
    values = fixture_host(tmp_path).model_dump()
    values["canvas_crop"] = (0, 0, 640, 480)
    values["model_crop"] = (0, 0, 640, 480)
    with pytest.raises(ValueError, match="model_crop must be separate from canvas_crop"):
        HostConfig.model_validate(values)


def test_pinned_binary_loader_does_not_expand_tilde() -> None:
    with pytest.raises(BinaryConfigError, match="absolute path"):
        load_pinned_binaries({"CROWLEY_CODEX_PATH": "~/codex"})
