from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from crowley.server.config import ServerSettings


def test_server_settings_require_database_url_and_host() -> None:
    with pytest.raises(ValidationError) as error:
        ServerSettings(api_token="token")

    message = str(error.value)
    assert "database_url" in message
    assert "host" in message


def test_server_settings_does_not_load_a_cwd_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(
        "CROWLEY_DATABASE_URL=postgresql://from-file\nCROWLEY_HOST=from-file\nCROWLEY_API_TOKEN=from-file\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValidationError, match="database_url"):
        ServerSettings()


def test_server_settings_accept_explicit_required_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CROWLEY_DATABASE_URL", "postgresql://db.example.invalid/crowley")
    monkeypatch.setenv("CROWLEY_HOST", "server-test")
    monkeypatch.setenv("CROWLEY_API_TOKEN", "token")
    monkeypatch.setenv("CROWLEY_DATA_DIR", str(tmp_path / "data"))

    settings = ServerSettings()

    assert settings.database_url == "postgresql://db.example.invalid/crowley"
    assert settings.host == "server-test"


def test_warm_advice_defaults_on_and_accepts_environment_override(monkeypatch: pytest.MonkeyPatch) -> None:
    values = {
        "database_url": "postgresql://db.example.invalid/crowley",
        "host": "test",
        "api_token": "token",
    }
    assert ServerSettings(**values).warm_advice is True

    monkeypatch.setenv("CROWLEY_WARM_ADVICE", "false")
    assert ServerSettings(**values).warm_advice is False
    monkeypatch.setenv("CROWLEY_WARM_ADVICE", "true")
    assert ServerSettings(**values).warm_advice is True
