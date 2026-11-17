"""Resolve the authenticated X11 session from the user manager, on demand."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

_MANAGER = "org.freedesktop.systemd1"
_OBJECT = "/org/freedesktop/systemd1"
_INTERFACE = "org.freedesktop.systemd1.Manager"
_ENVIRONMENT_PROPERTY = "Environment"
_ENV_ALLOWLIST = frozenset(
    {
        "DBUS_SESSION_BUS_ADDRESS",
        "DISPLAY",
        "HOME",
        "LANG",
        "LC_ALL",
        "PATH",
        "XAUTHORITY",
        "XDG_RUNTIME_DIR",
        "XDG_SESSION_TYPE",
        "WAYLAND_DISPLAY",
    }
)


class DesktopSessionUnavailable(RuntimeError):
    """No authenticated, currently usable graphical session can be verified."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class DesktopSession:
    """One validated session environment and its change-detection fingerprint."""

    environment: Mapping[str, str]
    fingerprint: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))


class DesktopSessionProvider:
    """Read structured user-manager environment and validate one X11 connection."""

    def __init__(
        self,
        display: str,
        *,
        runner=subprocess.run,
        environ: Mapping[str, str] | None = None,
        authority_stat=None,
    ) -> None:
        self._display = display
        self._runner = runner
        self._environ = dict(os.environ if environ is None else environ)
        self._authority_stat = authority_stat or Path.stat

    def current(self) -> DesktopSession:
        """Return the current manager session, or a sanitized stable hold reason."""

        try:
            done = self._runner(
                [
                    "busctl",
                    "--user",
                    "--json=short",
                    "get-property",
                    _MANAGER,
                    _OBJECT,
                    _INTERFACE,
                    _ENVIRONMENT_PROPERTY,
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=3.0,
                env=self._environ,
            )
        except (OSError, subprocess.SubprocessError):
            raise DesktopSessionUnavailable("desktop_session_unavailable") from None
        if done.returncode != 0:
            raise DesktopSessionUnavailable("desktop_session_unavailable")
        try:
            values = _manager_environment(done.stdout)
        except (TypeError, ValueError, json.JSONDecodeError):
            raise DesktopSessionUnavailable("desktop_session_unavailable") from None

        if values.get("DISPLAY") != self._display:
            raise DesktopSessionUnavailable("desktop_display_mismatch")
        authority = values.get("XAUTHORITY")
        if authority is None or not Path(authority).is_absolute():
            raise DesktopSessionUnavailable("desktop_authority_unavailable")
        auth_path = Path(authority)
        try:
            auth_stat = self._authority_stat(auth_path, follow_symlinks=False)
        except (OSError, TypeError):
            raise DesktopSessionUnavailable("desktop_authority_unavailable") from None
        if (
            not stat.S_ISREG(auth_stat.st_mode)
            or auth_stat.st_uid != os.getuid()
            or not os.access(auth_path, os.R_OK)
        ):
            raise DesktopSessionUnavailable("desktop_authority_unavailable")

        environment = {key: value for key, value in values.items() if key in _ENV_ALLOWLIST}
        environment["DISPLAY"] = self._display
        environment["XAUTHORITY"] = str(auth_path)
        try:
            query = self._runner(
                ["xdpyinfo", "-display", self._display],
                capture_output=True,
                text=True,
                check=False,
                timeout=3.0,
                env=environment,
            )
        except (OSError, subprocess.SubprocessError):
            raise DesktopSessionUnavailable("desktop_x_query_failed") from None
        if query.returncode != 0:
            raise DesktopSessionUnavailable("desktop_x_query_failed")

        digest = hashlib.sha256()
        for key, value in sorted(environment.items()):
            digest.update(key.encode("utf-8"))
            digest.update(b"\0")
            digest.update(value.encode("utf-8"))
            digest.update(b"\0")
        digest.update(
            f"{auth_stat.st_dev}:{auth_stat.st_ino}:{auth_stat.st_uid}:{auth_stat.st_mode}:"
            f"{auth_stat.st_mtime_ns}:{auth_stat.st_ctime_ns}".encode("ascii")
        )
        return DesktopSession(environment, digest.hexdigest())


def _manager_environment(raw: str) -> dict[str, str]:
    """Decode busctl's JSON property, rejecting malformed or duplicate entries."""

    value = json.loads(raw, object_pairs_hook=_unique_object)
    if not isinstance(value, dict) or set(value) != {"type", "data"} or value["type"] != "as":
        raise ValueError("manager Environment property is malformed")
    entries = value["data"]
    if not isinstance(entries, list) or len(entries) > 512:
        raise ValueError("manager Environment property is malformed")
    result: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, str) or len(entry) > 8192 or "\0" in entry:
            raise ValueError("manager Environment entry is malformed")
        key, separator, content = entry.partition("=")
        if not separator or not key.isidentifier() or key in result:
            raise ValueError("manager Environment entry is malformed")
        result[key] = content
    return result


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON property")
        result[key] = value
    return result


__all__ = ["DesktopSession", "DesktopSessionProvider", "DesktopSessionUnavailable"]
