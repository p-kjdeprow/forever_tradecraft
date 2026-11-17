from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from crowley_automation.desktop_session import DesktopSessionProvider, DesktopSessionUnavailable


def _provider(tmp_path: Path, *, extra: tuple[str, ...] = (), x_status: int = 0):
    authority = tmp_path / "Xauthority"
    authority.write_bytes(b"test-cookie")
    authority.chmod(0o600)
    entries = (
        "DISPLAY=:0",
        f"XAUTHORITY={authority}",
        "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus",
        *extra,
    )

    def runner(argv, **kwargs):
        if argv[0] == "busctl":
            payload = json.dumps({"type": "as", "data": entries})
            return subprocess.CompletedProcess(argv, 0, payload, "")
        assert argv[0] == "xdpyinfo"
        assert kwargs["env"]["DISPLAY"] == ":0"
        assert kwargs["env"]["XAUTHORITY"] == str(authority)
        return subprocess.CompletedProcess(argv, x_status, "", "")

    return DesktopSessionProvider(":0", runner=runner), authority


def test_session_uses_structured_manager_environment_and_stable_fingerprint(tmp_path: Path) -> None:
    provider, authority = _provider(tmp_path, extra=("LANG=en_US.UTF-8", "UNSAFE=value"))
    session = provider.current()
    assert session.environment["XAUTHORITY"] == str(authority)
    assert "UNSAFE" not in session.environment
    assert session.fingerprint


def test_missing_or_duplicate_display_facts_are_a_typed_hold(tmp_path: Path) -> None:
    provider, _authority = _provider(tmp_path, extra=("DISPLAY=:0",))
    with pytest.raises(DesktopSessionUnavailable) as error:
        provider.current()
    assert error.value.reason == "desktop_session_unavailable"


def test_session_refuses_an_x_query_failure(tmp_path: Path) -> None:
    provider, _authority = _provider(tmp_path, x_status=1)
    with pytest.raises(DesktopSessionUnavailable) as error:
        provider.current()
    assert error.value.reason == "desktop_x_query_failed"


def test_session_refuses_a_symlinked_authority(tmp_path: Path) -> None:
    provider, authority = _provider(tmp_path)
    link = tmp_path / "authority-link"
    link.symlink_to(authority)
    original_current = provider.current
    assert original_current().fingerprint

    # A separate structured manager response is needed to test the link path.
    def runner(argv, **kwargs):
        if argv[0] == "busctl":
            payload = json.dumps(
                {
                    "type": "as",
                    "data": ["DISPLAY=:0", f"XAUTHORITY={link}"],
                }
            )
            return subprocess.CompletedProcess(argv, 0, payload, "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    symlink_provider = DesktopSessionProvider(":0", runner=runner)
    with pytest.raises(DesktopSessionUnavailable) as error:
        symlink_provider.current()
    assert error.value.reason == "desktop_authority_unavailable"
