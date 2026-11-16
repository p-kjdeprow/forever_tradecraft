from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from crowley_common.game_target import selected_target
from crowley_scanner.companion.config import ScannerConfig, load_settings, render_scanner_toml
from pydantic import ValidationError


def test_scanner_toml_has_only_scanner_owned_runtime_settings() -> None:
    assert set(ScannerConfig.model_fields) == {
        "game_root",
        "wtf_account",
        "character",
        "server_url",
        "poll_seconds",
        "refresh_minutes",
    }
    settings = ScannerConfig.model_validate(
        {
            "game_root": "/opt/wow",
            "wtf_account": "account",
            "character": "character",
            "server_url": "http://127.0.0.1:8080",
        }
    )
    assert settings.poll_seconds == 3.0
    assert settings.refresh_minutes == 5.0
    assert tomllib.loads(render_scanner_toml(settings))["scanner"] == settings.model_dump()


@pytest.mark.parametrize(
    "extra",
    (
        {"window_title": "World of Warcraft"},
        {"database_url": "postgresql://localhost/db"},
        {"canvas_crop": [80, 48, 256, 256]},
    ),
)
def test_scanner_config_rejects_host_geometry_and_market_settings(extra: dict[str, object]) -> None:
    values: dict[str, object] = {
        "game_root": "/opt/wow",
        "wtf_account": "account",
        "character": "character",
        "server_url": "http://127.0.0.1:8080",
        **extra,
    }
    with pytest.raises(ValidationError):
        ScannerConfig.model_validate(values)


def test_scanner_settings_derive_paths_from_scanner_toml_and_preserve_state_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    runtime.chmod(0o700)
    for name, value in (
        ("HOME", str(tmp_path)),
        ("XDG_CONFIG_HOME", str(tmp_path / "config")),
        ("XDG_DATA_HOME", str(tmp_path / "data")),
        ("XDG_STATE_HOME", str(tmp_path / "state")),
        ("XDG_CACHE_HOME", str(tmp_path / "cache")),
        ("XDG_RUNTIME_DIR", str(runtime)),
        ("CROWLEY_API_TOKEN", "test-token"),
    ):
        monkeypatch.setenv(name, value)
    game_root = str(tmp_path / "game")
    source = tmp_path / "scanner.toml"
    source.write_text(
        render_scanner_toml(
            ScannerConfig.model_validate(
                {
                    "game_root": game_root,
                    "wtf_account": "account",
                    "character": "character",
                    "server_url": "http://127.0.0.1:8080",
                }
            )
        ),
        encoding="utf-8",
    )

    settings = load_settings(source)
    target = selected_target()
    assert settings.api_token.get_secret_value() == "test-token"
    assert settings.wow_path == Path(game_root) / target.product_dir
    assert settings.saved_variables == (
        Path(game_root)
        / target.product_dir
        / "WTF"
        / "Account"
        / "account"
        / "SavedVariables"
        / "Crowley.lua"
    )
    assert settings.state_file == (
        tmp_path
        / "state"
        / "crowley"
        / target.house
        / target.target_id
        / "account"
        / "character"
        / "companion.json"
    )
    monkeypatch.delenv("CROWLEY_API_TOKEN")
    source.with_name("secrets.env").write_text("CROWLEY_API_TOKEN=file-token\n", encoding="utf-8")
    assert load_settings(source).api_token.get_secret_value() == "file-token"
