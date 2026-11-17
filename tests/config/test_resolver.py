from __future__ import annotations

from pathlib import Path

import pytest
from crowley_host.config import HostConfig, HostConfigError, load_host_config, resolve_ops

from tests.support.config import fixture_host, fixture_target, fixture_xdg, write_host_toml


def test_ops_resolver_derives_install_paths_from_one_host_identity(tmp_path: Path) -> None:
    target = fixture_target()
    host = fixture_host(tmp_path)
    xdg = fixture_xdg(tmp_path)
    ops = resolve_ops(target, host, xdg=xdg)

    state = xdg.crowley_state / target.house / target.target_id / host.wtf_account / host.character
    flavor = Path(host.game_root) / target.product_dir
    assert ops.flavor_dir == flavor
    assert ops.account_dir == flavor / "WTF/Account/account#1"
    assert ops.saved_variables == flavor / "WTF/Account/account#1/SavedVariables/Crowley.lua"
    assert ops.data_lua == flavor / "Interface/AddOns/Crowley_Data/Data.lua"
    assert ops.state_dir == state


def test_host_loader_reports_missing_keys_and_rejects_unknown(tmp_path: Path) -> None:
    path = tmp_path / "host.toml"
    path.write_text("unknown = true\n", encoding="utf-8")
    with pytest.raises(HostConfigError) as error:
        load_host_config(path)
    message = str(error.value)
    assert "unknown" in message
    assert "wine_prefix" in message
    assert "game_root" in message
    assert "automation_canvas_crop" in message
    assert "character_select_name" in message
    assert "model_crop" not in message


def test_host_loader_rejects_removed_calibration_fields(tmp_path: Path) -> None:
    host = fixture_host(tmp_path)
    values = host.model_dump()
    values["model_crop"] = [0, 0, 1920, 1080]
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        HostConfig.model_validate(values)


def test_host_crops_must_be_256_square_and_nonoverlapping(tmp_path: Path) -> None:
    values = fixture_host(tmp_path).model_dump()
    values["canvas_crop"] = (80, 48, 255, 256)
    with pytest.raises(ValueError, match="canvas_crop must be a 256x256 rectangle"):
        HostConfig.model_validate(values)

    values = fixture_host(tmp_path).model_dump()
    values["automation_canvas_crop"] = values["canvas_crop"]
    with pytest.raises(ValueError, match="automation_canvas_crop must not overlap canvas_crop"):
        HostConfig.model_validate(values)


def test_character_select_name_is_a_required_visual_literal(tmp_path: Path) -> None:
    values = fixture_host(tmp_path).model_dump()
    values["character_select_name"] = ""
    with pytest.raises(ValueError, match="value must be a non-empty string"):
        HostConfig.model_validate(values)
    values["character_select_name"] = "   "
    with pytest.raises(ValueError, match="value must be a non-empty string"):
        HostConfig.model_validate(values)


def test_host_toml_round_trip(tmp_path: Path) -> None:
    host = fixture_host(tmp_path)
    path = tmp_path / "host.toml"
    write_host_toml(path, host)
    assert load_host_config(path) == host
