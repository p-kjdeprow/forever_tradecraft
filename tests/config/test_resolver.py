from __future__ import annotations

from pathlib import Path

import pytest

from crowley.config import (
    CompanionTunables,
    HostConfig,
    HostConfigError,
    OpsTunables,
    TunablesError,
    load_companion_tunables,
    load_host_config,
    resolve_companion,
    resolve_ops,
)
from tests.support.config import (
    fixture_companion_tunables,
    fixture_host,
    fixture_target,
    fixture_xdg,
    write_companion_toml,
)


def test_resolvers_derive_every_service_path_from_one_identity(tmp_path: Path) -> None:
    target = fixture_target()
    host = fixture_host(tmp_path)
    xdg = fixture_xdg(tmp_path)
    companion = resolve_companion(
        target,
        host,
        fixture_companion_tunables(),
        xdg=xdg,
    )
    ops = resolve_ops(target, host, OpsTunables(), xdg=xdg)

    state = xdg.crowley_state / target.house / target.target_id / host.wtf_account / host.character
    flavor = Path(host.game_root) / target.product_dir
    assert companion.flavor_dir == flavor
    assert companion.saved_variables == flavor / "WTF/Account/account#1/SavedVariables/Crowley.lua"
    assert companion.data_lua == flavor / "Interface/AddOns/Crowley_Data/Data.lua"
    assert companion.queue_lua == flavor / "Interface/AddOns/Crowley_Data/Queue.lua"
    assert companion.state_file == state / "companion.json"
    assert ops.flavor_dir == flavor
    assert ops.state_dir == state


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
    path = tmp_path / "companion.toml"
    path.write_text("[companion]\nunknown = 1\n\n[profile]\n", encoding="utf-8")
    with pytest.raises(TunablesError, match="unknown or missing tables"):
        load_companion_tunables(path)


def test_companion_tunables_round_trip_the_fixture(tmp_path: Path) -> None:
    path = tmp_path / "companion.toml"
    write_companion_toml(path)
    assert load_companion_tunables(path) == fixture_companion_tunables()


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
