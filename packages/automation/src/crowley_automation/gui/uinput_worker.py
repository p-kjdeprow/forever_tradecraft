"""Private virtual-device helper. No game API, images, policy, ledger or model calls."""

from __future__ import annotations

import ctypes
import hashlib
import json
import math
import os
import select
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from crowley_automation.desktop_input import GuiLease
from crowley_automation.gui.control_child import _classify
from crowley_automation.gui.uinput import WATCHDOG_SECONDS, DeviceError, receive_message, send_message
from crowley_automation.presence import PRESENCE_WINDOW_S, Presence


def device_identity(path: str) -> tuple[int, int, int, int, int, int, str]:
    info = os.stat(path)
    return (
        info.st_rdev,
        info.st_ino,
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_ctime_ns,
        str((Path("/sys/class/input") / Path(path).name).resolve(strict=True)),
    )


def inventory() -> dict[str, tuple[int, int, int, int, int, int, str]]:
    # evdev.list_devices intentionally hides inaccessible devices. Never use it
    # to establish complete physical-input coverage.
    return {str(path): device_identity(str(path)) for path in Path("/dev/input").glob("event*")}


class PhysicalMonitor:
    def __init__(self, evdev: Any, coverage: dict[str, Any] | None = None) -> None:
        self._inventory = inventory()
        if not self._inventory:
            raise DeviceError("no physical input coverage")
        self.devices: list[Any] = []
        self.last_activity = time.monotonic()
        try:
            for path in self._inventory:
                device = evdev.InputDevice(path)
                self.devices.append(device)
                if device_identity(path) != self._inventory[path]:
                    raise DeviceError("input device changed while opening")
            self.last_activity = time.monotonic()
            # A fresh reader has no prior history. Mutter is not qualified for
            # every controller and will count our uinput events later. Establish
            # a complete idle window rather than treating an empty queue as idle.
            if any(device.active_keys() for device in self.devices):
                self.last_activity = time.monotonic()
            if coverage:
                self._bootstrap(coverage)
            self.check()
        except BaseException:
            self.close()
            raise

    def _bootstrap(self, coverage: dict[str, Any]) -> None:
        """Only empirical, exact-topology keyboard/mouse Mutter qualification.

        This profile is evidence from an explicit per-device idle-monitor test
        in the bound desktop session, not an automatically asserted capability.
        Unknown devices/classes never degrade to an assumed-idle path.
        """
        if set(coverage) != set(self._inventory):
            raise DeviceError("qualified idle coverage does not match the exact input topology")
        for device in self.devices:
            record = coverage[device.path]
            capabilities = device.capabilities(absinfo=False)
            if set(capabilities) - {0, 1, 2, 4, 17, 20}:
                raise DeviceError("controller/touch/unknown history requires full idle observation")
            keys = capabilities.get(1, ())
            if any(not (1 <= code <= 255 or 272 <= code <= 279) for code in keys):
                raise DeviceError("controller/digitizer keys are outside qualified idle coverage")
            if set(capabilities.get(2, ())) - {0, 1, 6, 8, 11, 12}:
                raise DeviceError("unknown relative controls are outside qualified idle coverage")
            identity = hashlib.sha256(json.dumps(self._inventory[device.path]).encode()).hexdigest()
            caps = hashlib.sha256(json.dumps(capabilities, sort_keys=True).encode()).hexdigest()
            if (identity, caps) != (record["identity_sha256"], record["capabilities_sha256"]):
                raise DeviceError("qualified idle device identity or capabilities changed")
            result = subprocess.run(
                ["udevadm", "info", "--query=property", "--name", device.path],
                capture_output=True,
                text=True,
                check=True,
                timeout=2,
            )
            properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
            # systemd defines absent ID_SEAT as seat0. No other seat is qualified.
            if (
                record["seat"] != "seat0"
                or properties.get("ID_SEAT", "seat0") != "seat0"
                or "seat" not in properties.get("CURRENT_TAGS", "").split(":")
            ):
                raise DeviceError("input device is outside the qualified GNOME seat")
        idle = Presence().idle_ms() / 1000
        self.last_activity = time.monotonic() - min(idle, PRESENCE_WINDOW_S)
        # check() follows immediately: held state or events queued during all
        # qualification queries override this baseline, never the reverse.

    def add_owned(self, path: str) -> None:
        owned = device_identity(path)
        current = inventory()
        if path in self._inventory or current != {**self._inventory, path: owned}:
            raise DeviceError("input topology changed while creating the owned device")
        # Exclusion is the exact kernel object returned by UInput, never a name.
        self._inventory = current

    def check(self) -> None:
        if inventory() != self._inventory:
            raise DeviceError("input coverage lost: hotplug or device replacement")
        for device in self.devices:
            if device.active_keys():
                self.last_activity = time.monotonic()
            try:
                for event in device.read():
                    if event.type == 0 and event.code == 3:  # SYN_DROPPED
                        raise DeviceError("physical event history was dropped")
                    if event.type not in {0, 4, 17, 20}:
                        self.last_activity = time.monotonic()
            except BlockingIOError:
                pass

    def active(self) -> bool:
        return time.monotonic() - self.last_activity < PRESENCE_WINDOW_S

    def close(self) -> None:
        for device in self.devices:
            device.close()
        self.devices.clear()


class XPointer:
    """Persistent read-only X11 pointer feedback; never warps the cursor."""

    def __init__(self) -> None:
        self.lib = ctypes.CDLL("libX11.so.6")
        self.lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.lib.XOpenDisplay.restype = ctypes.c_void_p
        self.lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        self.lib.XDefaultRootWindow.restype = ctypes.c_ulong
        self.lib.XQueryPointer.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_uint),
        ]
        self.lib.XQueryPointer.restype = ctypes.c_int
        self.lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
        for name in ("XDisplayWidth", "XDisplayHeight"):
            function = getattr(self.lib, name)
            function.argtypes = [ctypes.c_void_p, ctypes.c_int]
            function.restype = ctypes.c_int
        self.display = self.lib.XOpenDisplay(None)
        if not self.display:
            raise DeviceError("pointer feedback display unavailable")
        self.root = self.lib.XDefaultRootWindow(self.display)

    def size(self) -> tuple[int, int]:
        return self.lib.XDisplayWidth(self.display, 0), self.lib.XDisplayHeight(self.display, 0)

    def position(self) -> tuple[int, int]:
        root, child = ctypes.c_ulong(), ctypes.c_ulong()
        x, y, wx, wy, mask = ctypes.c_int(), ctypes.c_int(), ctypes.c_int(), ctypes.c_int(), ctypes.c_uint()
        ok = self.lib.XQueryPointer(
            self.display,
            self.root,
            ctypes.byref(root),
            ctypes.byref(child),
            ctypes.byref(x),
            ctypes.byref(y),
            ctypes.byref(wx),
            ctypes.byref(wy),
            ctypes.byref(mask),
        )
        if not ok:
            raise DeviceError("pointer feedback left its display")
        return x.value, y.value

    def close(self) -> None:
        self.lib.XCloseDisplay(self.display)


class DeviceState:
    """Held-state and absolute budget checks, independent of parent observation latency."""

    def __init__(self, device: Any, monitor: Any, *, clock=time.monotonic, wall=time.time) -> None:
        self.device, self.monitor, self.clock, self.wall = device, monitor, clock, wall
        self.held: list[int] = []
        self.executing = False
        self.heartbeat = clock()
        self.authorization = math.inf
        self.feedback = math.inf
        self.travel = math.inf

    def check(self, *, require_idle: bool = True) -> None:
        self.monitor.check()
        if require_idle and self.monitor.active():
            raise DeviceError("physical takeover blocks device input")
        now = self.clock()
        if (self.held or self.executing) and now - self.heartbeat >= WATCHDOG_SECONDS:
            raise DeviceError("parent watchdog expired")
        if now >= min(self.authorization, self.feedback, self.travel):
            raise DeviceError("input authorization, feedback or travel budget expired")

    def authorize(self, wall_deadline: object) -> None:
        if (
            isinstance(wall_deadline, bool)
            or not isinstance(wall_deadline, (float, int))
            or not math.isfinite(wall_deadline)
        ):
            raise DeviceError("absolute input authorization is missing")
        remaining = wall_deadline - self.wall()
        if remaining <= 0:
            raise DeviceError("input authorization expired")
        deadline = self.clock() + remaining
        self.authorization = min(self.authorization, deadline) if self.held else deadline

    def key(self, code: int, down: bool) -> None:
        if down:
            self.check()
            if code in self.held:
                return
            self.held.append(code)
        elif code in self.held:
            self.held.remove(code)
        else:
            return
        self.device.write(1, code, int(down))
        self.device.syn()

    def release(self) -> None:
        try:
            for code in reversed(self.held):
                self.device.write(1, code, 0)
            self.device.syn()
        finally:
            self.held.clear()
            self.executing = False
            self.authorization = self.feedback = self.travel = math.inf


def parse_events(
    args: tuple[str, ...], mapping: dict[str, int]
) -> tuple[list[tuple[float, str, Any]], float]:
    """Validate the complete native allowlist and mapping before the first write."""
    operation = _classify("xdotool", args)
    if operation not in {"key", "click", "release", "motion"}:
        raise DeviceError("device input is outside the emission allowlist")
    events: list[tuple[float, str, Any]] = []
    at = 0.0
    index = 0
    while index < len(args):
        command = args[index]
        if command == "sleep":
            at += float(args[index + 1])
            index += 2
        elif command == "mousemove":
            events.append((at, "move", (int(args[index + 1]), int(args[index + 2]))))
            index += 3
        elif command in {"keydown", "keyup"}:
            code = mapping.get(str(int(args[index + 1])))
            if code is None:
                raise DeviceError("key has no qualified evdev mapping")
            events.append((at, command, code))
            index += 2
        elif command in {"mousedown", "mouseup"}:
            events.append((at, command, 272))  # BTN_LEFT, explicitly not a hardware identity
            index += 2
        else:
            raise DeviceError("unknown device operation")
    return events, at


def _wait_until(
    channel: socket.socket, state: DeviceState, until: float, heartbeat: Any, *, clock=time.monotonic
) -> None:
    while clock() < until:
        state.check()
        if select.select([channel], [], [], max(0.0, min(0.01, until - clock())))[0]:
            heartbeat()
    if select.select([channel], [], [], 0)[0]:
        heartbeat()
    state.check()


def serve(channel: socket.socket, lease: GuiLease) -> None:
    import evdev

    profile = receive_message(channel)
    if profile.get("op") != "init" or not isinstance(profile.get("profile"), dict):
        raise DeviceError("missing device initialization")
    profile = profile["profile"]
    monitor = PhysicalMonitor(evdev, profile.get("idle_coverage"))
    device = pointer = None
    state = None
    try:
        while monitor.active():
            monitor.check()
            if select.select([channel], [], [], 0.05)[0]:
                # Parent EOF/cancellation closes the preparation session too.
                raise DeviceError("device preparation interrupted")
        pointer = XPointer()
        if tuple(profile["display_size"]) != pointer.size():
            raise DeviceError("calibrated display dimensions changed")
        device = evdev.UInput(
            {1: sorted({*profile["x_to_evdev"].values(), 272}), 2: [0, 1]},
            name="Crowley virtual input",
            phys="crowley/session",
        )
        if device.device is None:
            raise DeviceError("owned virtual event device is unavailable")
        monitor.add_owned(device.device.path)
        state = DeviceState(device, monitor)
        send_message(channel, {"ok": True})

        def heartbeat() -> None:
            message = receive_message(channel)
            if message != {"op": "heartbeat"}:
                raise DeviceError("unexpected request during device execution")
            state.heartbeat = time.monotonic()

        def wait(until: float) -> None:
            _wait_until(channel, state, until, heartbeat)

        while True:
            monitor.check()
            if state.held:
                # Budget expiry releases independently while the parent may be
                # blocked in capture. A subsequent hold needs fresh admission.
                if time.monotonic() >= min(state.authorization, state.feedback, state.travel):
                    state.release()
                else:
                    state.check()
            if not select.select([channel], [], [], 0.01)[0]:
                continue
            request = receive_message(channel)
            op = request.get("op")
            state.heartbeat = time.monotonic()
            if op == "heartbeat":
                continue
            if op == "status":
                send_message(channel, {"ok": True, "active": monitor.active()})
                continue
            if op == "release":
                state.release()
            elif op == "forward":
                code = request["code"]
                if type(code) is not int or code != profile.get("forward_code"):
                    raise DeviceError("forward key is not mapped")
                feedback, travel = request["feedback_deadline"], request["travel_deadline"]
                now = time.monotonic()
                if any(type(n) not in (float, int) or not math.isfinite(n) for n in (feedback, travel)):
                    raise DeviceError("forward deadlines are invalid")
                if not all(now < value <= now + 2.0 for value in (feedback, travel)):
                    raise DeviceError("forward budget exceeds position freshness")
                state.authorize(request.get("deadline"))
                state.feedback, state.travel = feedback, travel
                if state.held and state.held != [code]:
                    raise DeviceError("movement control changed without release")
                state.key(code, True)
            elif op == "execute":
                state.release()
                args = tuple(request.get("args", ()))
                if any(type(arg) is not str for arg in args):
                    raise DeviceError("device argv is malformed")
                events, duration = parse_events(args, profile["x_to_evdev"])
                release_only = _classify("xdotool", args) == "release"
                if not release_only:
                    state.authorize(request.get("deadline"))
                    state.executing = True
                    if time.monotonic() + duration >= state.authorization:
                        raise DeviceError("input timeline exceeds authorization")
                    state.check()
                started = time.monotonic()
                for at, command, value in events:
                    if not release_only:
                        wait(started + at)
                        if time.monotonic() - started - at > 0.1:
                            raise DeviceError("input timeline missed its scheduling bound")
                    if command == "move":
                        tx, ty = value
                        width, height = profile["display_size"]
                        if pointer.size() != (width, height):
                            raise DeviceError("pointer display changed during motion")
                        if not 0 <= tx < width or not 0 <= ty < height:
                            raise DeviceError("pointer target exceeds calibrated display")
                        # Closed-loop correction handles acceleration/rounding explicitly.
                        for _ in range(4):
                            state.check()
                            x, y = pointer.position()
                            if (x, y) == (tx, ty):
                                break
                            dx = round((tx - x) / profile["gain_x"])
                            dy = round((ty - y) / profile["gain_y"])
                            if tx != x and dx == 0:
                                dx = 1 if tx > x else -1
                            if ty != y and dy == 0:
                                dy = 1 if ty > y else -1
                            if dx:
                                device.write(2, 0, dx)
                            if dy:
                                device.write(2, 1, dy)
                            device.syn()
                            wait(time.monotonic() + 0.004)
                        if pointer.position() != (tx, ty):
                            raise DeviceError("relative pointer failed its measured endpoint")
                    else:
                        state.key(value, command in {"keydown", "mousedown"})
                if not release_only:
                    wait(started + duration)
                state.release()
            else:
                raise DeviceError("unknown device request")
            send_message(channel, {"ok": True})
    finally:
        try:
            if state is not None:
                state.release()
        finally:
            # Device destruction releases the kernel state even if release writes fail.
            if device is not None:
                device.close()
            if pointer is not None:
                pointer.close()
            monitor.close()


def main() -> int:
    channel = socket.socket(fileno=int(sys.argv[1]))
    lease = GuiLease.from_inherited_fd(Path(sys.argv[2]), int(sys.argv[3]))
    try:
        with lease.hold():
            try:
                serve(channel, lease)
            except Exception as exc:
                try:
                    send_message(channel, {"ok": False, "error": str(exc)})
                except (OSError, DeviceError):
                    pass
                return 1
    finally:
        channel.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
