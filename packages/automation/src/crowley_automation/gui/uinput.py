"""Explicit virtual-device transport with one supervised helper per GUI session.

No hardware identity spoofing. Device creation happens only after explicit selection,
calibration checks and GUI lease ownership. The helper owns device destruction and
its inherited lease; it does not run trading logic or receive secrets.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import select
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from crowley_common.platform.privfs import read_bounded

from crowley_automation.desktop_input import GuiLease

MAX_MESSAGE = 65536
WATCHDOG_SECONDS = 1.5


class DeviceError(RuntimeError):
    """An explicitly selected device transport cannot satisfy its contract."""


def send_message(channel: socket.socket, value: object) -> None:
    raw = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
    if len(raw) > MAX_MESSAGE or channel.send(raw) != len(raw):
        raise DeviceError("device message exceeds its atomic bound")


def receive_message(channel: socket.socket) -> dict[str, Any]:
    raw, _ancillary, flags, _address = channel.recvmsg(MAX_MESSAGE)
    if not raw or flags & socket.MSG_TRUNC:
        raise DeviceError("device helper disconnected or sent a truncated message")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise DeviceError("device message is malformed")
    return value


def calibration(path: str, *, environment: Mapping[str, str], run=subprocess.run) -> dict[str, Any]:
    """Require measured X/evdev mapping and a session-bound pointer estimate.

    Gains are initial estimates for closed-loop relative delivery, not an assertion
    that accelerated device counts equal pixels. Every target is checked in X11.
    """
    source = Path(path)
    before = source.lstat()
    if before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) != 0o600:
        raise DeviceError("input calibration must be a private user-owned 0600 file")
    raw = read_bounded(source, 32768)
    after = source.lstat()
    if before != after or before.st_ctime_ns != after.st_ctime_ns or before.st_mtime_ns != after.st_mtime_ns:
        raise DeviceError("input calibration permissions or identity changed during read")
    value = json.loads(raw)
    expected = {
        "schema",
        "display",
        "authority_sha256",
        "x_keymap_sha256",
        "mouse_settings_sha256",
        "x_to_evdev",
        "gain_x",
        "gain_y",
        "display_size",
        "idle_coverage",
    }
    if not isinstance(value, dict) or set(value) != expected or value["schema"] != 1:
        raise DeviceError("input calibration schema is invalid")
    coverage = value["idle_coverage"]
    if not isinstance(coverage, dict) or len(coverage) > 128:
        raise DeviceError("qualified idle coverage is invalid")
    for record in coverage.values():
        if not isinstance(record, dict) or set(record) != {"identity_sha256", "capabilities_sha256", "seat"}:
            raise DeviceError("qualified idle device record is invalid")
        if record["seat"] != "seat0" or any(
            not isinstance(record[key], str)
            or len(record[key]) != 64
            or any(char not in "0123456789abcdef" for char in record[key])
            for key in ("identity_sha256", "capabilities_sha256")
        ):
            raise DeviceError("qualified idle device identity is invalid")
    if value["display"] != environment["DISPLAY"]:
        raise DeviceError("input calibration belongs to another display")
    if hashlib.sha256(Path(environment["XAUTHORITY"]).read_bytes()).hexdigest() != value["authority_sha256"]:
        raise DeviceError("input calibration belongs to another graphical session")

    def query(argv: list[str]) -> str:
        result = run(argv, env=dict(environment), capture_output=True, text=True, check=True, timeout=3)
        return result.stdout

    keymap_text = query(["xmodmap", "-pke"])
    if hashlib.sha256(keymap_text.encode()).hexdigest() != value["x_keymap_sha256"]:
        raise DeviceError("measured keyboard layout changed")
    settings_rows = [
        query(["gsettings", "get", "org.gnome.desktop.peripherals.mouse", key])
        for key in ("speed", "accel-profile", "left-handed")
    ]
    if settings_rows[2].strip() != "false":
        raise DeviceError("uinput logical left click requires qualified right-handed pointer settings")
    settings = "".join(settings_rows)
    if hashlib.sha256(settings.encode()).hexdigest() != value["mouse_settings_sha256"]:
        raise DeviceError("measured pointer settings changed")
    mapping = value["x_to_evdev"]
    if not isinstance(mapping, dict) or not mapping or len(mapping) > 248:
        raise DeviceError("measured X-to-evdev mapping is missing")
    for key, code in mapping.items():
        if (
            not isinstance(key, str)
            or not key.isdecimal()
            or not 8 <= int(key) <= 255
            or key != str(int(key))
        ):
            raise DeviceError("measured X keycode is invalid")
        if type(code) is not int or not 1 <= code <= 255:
            raise DeviceError("measured evdev code is invalid")
    if len(set(mapping.values())) != len(mapping):
        raise DeviceError("measured physical mapping contains aliases")
    for axis in ("gain_x", "gain_y"):
        if (
            type(value[axis]) not in (int, float)
            or not math.isfinite(value[axis])
            or not 0.01 <= value[axis] <= 100
        ):
            raise DeviceError("measured pointer gain is invalid")
    size = value["display_size"]
    if (
        type(size) is not list
        or len(size) != 2
        or any(type(n) is not int or not 1 <= n <= 16384 for n in size)
    ):
        raise DeviceError("calibrated display size is invalid")
    from crowley_automation.desktop_input import parse_keymap

    up = parse_keymap(keymap_text).get("Up")
    value["forward_code"] = mapping.get(str(up)) if up is not None else None
    return value


class UinputSession:
    """One helper, no per-character processes, independent hold and parent-loss cleanup."""

    def __init__(
        self,
        lease: GuiLease,
        profile: Mapping[str, Any],
        *,
        environment: Mapping[str, str],
        guard: Callable[[], None],
        deadline: Callable[[], float],
        preparing: Callable[[], None],
    ) -> None:
        if not lease.held or lease.fd is None:
            raise DeviceError("device session requires the held GUI lease")
        self._guard, self._deadline, self._lease = guard, deadline, lease
        self._preparing = preparing
        self._closed = False
        self._channel, child_channel = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        try:
            self._child = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "crowley_automation.gui.uinput_worker",
                    str(child_channel.fileno()),
                    str(lease.path),
                    str(lease.fd),
                ],
                env=dict(environment),
                pass_fds=(child_channel.fileno(), lease.fd),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except BaseException:
            self._channel.close()
            raise
        finally:
            child_channel.close()
        try:
            # New raw readers must establish the existing five-minute idle
            # history once per foreground session. No devices emit during it.
            self._request(
                {"op": "init", "profile": dict(profile)},
                monitor=False,
                timeout=8 if profile.get("idle_coverage") else 310,
            )
        except BaseException:
            self.close()
            raise

    def _request(
        self, request: dict[str, Any], *, monitor: bool = True, timeout: float = 25
    ) -> dict[str, Any]:
        if self._closed or not self._lease.held:
            raise DeviceError("device session is closed or its GUI lease is absent")
        expires = time.monotonic() + timeout
        try:
            if monitor:
                self._guard()
            send_message(self._channel, request)
            while True:
                if request.get("op") == "init":
                    self._preparing()
                if monitor:
                    self._guard()
                    send_message(self._channel, {"op": "heartbeat"})
                remaining = expires - time.monotonic()
                if remaining <= 0:
                    raise DeviceError("device helper response timed out")
                if select.select([self._channel], [], [], min(0.05, remaining))[0]:
                    result = receive_message(self._channel)
                    if result.get("ok") is not True:
                        raise DeviceError(str(result.get("error", "device helper failed")))
                    return result
        except BaseException:
            # EOF makes the helper release/destroy before releasing its inherited lease.
            self.close()
            raise

    def human_active(self) -> tuple[bool, str]:
        result = self._request({"op": "status"}, monitor=False, timeout=2)
        return bool(result["active"]), "physical device activity monitor"

    @property
    def released(self) -> bool:
        return self._closed and self._child.poll() is not None

    def execute(self, args: Sequence[str], *, timeout: float) -> None:
        self._request({"op": "execute", "args": list(args), "deadline": self._deadline()}, timeout=timeout)

    def forward(self, code: int, *, feedback_deadline: float, travel_deadline: float) -> None:
        self._request(
            {
                "op": "forward",
                "code": code,
                "deadline": self._deadline(),
                "feedback_deadline": feedback_deadline,
                "travel_deadline": travel_deadline,
            }
        )

    def release_all(self) -> None:
        if not self._closed:
            self._request({"op": "release"}, monitor=False, timeout=2)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._channel.close()
        # The kernel destroys uinput devices on helper death, including SIGKILL.
        try:
            self._child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._child.kill()
            self._child.wait(timeout=2)
