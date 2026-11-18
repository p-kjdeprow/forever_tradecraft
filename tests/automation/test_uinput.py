from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
from crowley_automation.desktop_input import GuiLease
from crowley_automation.gui import uinput_worker as worker
from crowley_automation.gui.uinput import DeviceError, receive_message, send_message


class Monitor:
    def __init__(self):
        self.takeover = False
        self.fault = False

    def check(self):
        if self.fault:
            raise DeviceError("coverage lost")

    def active(self):
        return self.takeover


class Device:
    def __init__(self):
        self.events = []

    def write(self, *event):
        self.events.append(event)

    def syn(self):
        pass


def state():
    clock = [10.0]
    device, monitor = Device(), Monitor()
    value = worker.DeviceState(device, monitor, clock=lambda: clock[0], wall=lambda: 1000)
    return value, clock, device, monitor


def test_unchanged_movement_key_emits_no_repeated_down_and_release_balances():
    value, clock, device, _ = state()
    value.authorize(1010)
    value.feedback = value.travel = 11
    value.key(103, True)
    clock[0] += 0.1
    value.key(103, True)
    value.release()
    assert device.events == [(1, 103, 1), (1, 103, 0)]
    assert value.held == []


@pytest.mark.parametrize(
    "failure", ["watchdog", "feedback", "travel", "authorization", "takeover", "coverage"]
)
def test_independent_worker_checks_stop_without_parent_callback(failure):
    value, clock, device, monitor = state()
    value.authorize(1010)
    value.key(103, True)
    if failure == "watchdog":
        clock[0] += 1.51
    elif failure == "takeover":
        monitor.takeover = True
    elif failure == "coverage":
        monitor.fault = True
    else:
        setattr(value, failure, 10.1)
        clock[0] = 10.1
    with pytest.raises(DeviceError):
        value.check()
    value.release()
    assert device.events[-1] == (1, 103, 0)


def test_held_authorization_cannot_be_extended_by_heartbeats_or_renewal():
    value, clock, _, _ = state()
    value.authorize(1001)
    value.key(103, True)
    first = value.authorization
    clock[0] += 0.1
    value.heartbeat = clock[0]
    value.authorize(2000)
    assert value.authorization == first


def test_physical_history_excludes_exact_owned_object_and_invalidates_hotplug(monkeypatch):
    identities = {"/fake/keyboard": (1, 2, 3)}
    devices = []

    class Reader:
        def __init__(self, path):
            self.path, self.events = path, []
            self.closed = False
            devices.append(self)

        def active_keys(self):
            return []

        def read(self):
            values, self.events = self.events, []
            return iter(values)

        def close(self):
            self.closed = True

    monkeypatch.setattr(worker, "inventory", lambda: dict(identities))
    monkeypatch.setattr(worker, "device_identity", lambda path: identities[path])
    monitor = worker.PhysicalMonitor(SimpleNamespace(InputDevice=Reader))
    assert monitor.active()  # startup has no historical coverage
    monitor.last_activity -= 301
    assert not monitor.active()
    identities["/fake/owned"] = (4, 5, 6)
    monitor.add_owned("/fake/owned")
    assert [d.path for d in devices] == ["/fake/keyboard"]
    monitor.check()
    devices[0].events = [SimpleNamespace(type=1, code=30, value=1)]
    monitor.check()
    assert monitor.active()
    identities["/fake/new"] = (7, 8, 9)
    with pytest.raises(DeviceError, match="coverage lost"):
        monitor.check()
    monitor.close()
    assert devices[0].closed


def test_missing_permissions_are_not_hidden_as_empty_inventory(monkeypatch):
    monkeypatch.setattr(worker, "inventory", lambda: {"/inaccessible": (1, 2, 3)})

    def denied(_path):
        raise PermissionError("denied")

    with pytest.raises(PermissionError):
        worker.PhysicalMonitor(SimpleNamespace(InputDevice=denied))


# Run the real supervisor/scheduler/socket/lease code with fake device adapters.
# No /dev/input or /dev/uinput paths are opened by these subprocesses.
HARNESS = r"""
import json, socket, sys, types
from pathlib import Path
from crowley_automation.gui import uinput_worker as w
from crowley_automation.desktop_input import GuiLease
log = Path(sys.argv[4])
class Monitor:
    def __init__(self, *_): pass
    def add_owned(self, _): pass
    def check(self): pass
    def active(self): return False
    def close(self): pass
class Device:
    device = types.SimpleNamespace(path='/fake/owned')
    def __init__(self, *args, **kwargs): pass
    def write(self, *event):
        with log.open('a') as f: f.write(json.dumps(event)+'\n')
    def syn(self): pass
    def close(self):
        with log.open('a') as f: f.write('"destroy"\n')
class Pointer:
    def __init__(self): pass
    def size(self): return (1920,1080)
    def close(self): pass
w.PhysicalMonitor, w.XPointer = Monitor, Pointer
sys.modules['evdev'] = types.SimpleNamespace(UInput=Device)
sock = socket.socket(fileno=int(sys.argv[1]))
lease = GuiLease.from_inherited_fd(Path(sys.argv[2]), int(sys.argv[3]))
try:
    with lease.hold(): w.serve(sock, lease)
except Exception:
    pass
finally:
    sock.close()
"""


@pytest.mark.parametrize("disconnect", [True, False])
def test_helper_releases_and_destroys_on_parent_loss_or_missing_heartbeat(tmp_path, disconnect):
    log = tmp_path / "events"
    lease = GuiLease(tmp_path / "private" / "gui.lock")
    with lease.hold():
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        proc = subprocess.Popen(
            [sys.executable, "-c", HARNESS, str(child.fileno()), str(lease.path), str(lease.fd), str(log)],
            pass_fds=(child.fileno(), lease.fd),
        )
        child.close()
        try:
            send_message(
                parent,
                {
                    "op": "init",
                    "profile": {
                        "x_to_evdev": {"111": 103},
                        "forward_code": 103,
                        "display_size": [1920, 1080],
                        "idle_coverage": {},
                    },
                },
            )
            assert receive_message(parent)["ok"]
            send_message(
                parent,
                {
                    "op": "forward",
                    "code": 103,
                    "deadline": time.time() + 60,
                    "feedback_deadline": time.monotonic() + 1.9,
                    "travel_deadline": time.monotonic() + 1.9,
                },
            )
            assert receive_message(parent)["ok"]
            if disconnect:
                parent.close()
            proc.wait(timeout=4)
            events = [json.loads(line) for line in log.read_text().splitlines()]
            assert events == [[1, 103, 1], [1, 103, 0], "destroy"]
            assert lease.held  # helper is reaped and controls are gone before unlock
        finally:
            parent.close()
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)


def test_selected_transport_never_uses_xtest_when_device_activation_fails():
    from crowley_automation.backend import DesktopIO

    io = DesktopIO.__new__(DesktopIO)
    io.config = SimpleNamespace(input_backend="uinput")
    calls = []

    def unavailable():
        raise DeviceError("missing permissions")

    io._device_session = unavailable
    io._ensure_input = lambda: calls.append("xtest")
    with pytest.raises(DeviceError, match="missing permissions"):
        io.emit(["keydown", "038", "keyup", "038"], 5)
    assert calls == []


def test_transport_selection_roundtrips_and_requires_calibration(tmp_path):
    from crowley_automation.config import AutomationConfig, load_config, render_automation_toml
    from pydantic import ValidationError

    from tests.support.config import fixture_automation

    base = fixture_automation(tmp_path)
    assert base.input_backend == "xtest"
    raw = base.model_dump()
    raw["input_backend"] = "uinput"
    with pytest.raises(ValidationError, match="input_calibration"):
        AutomationConfig.model_validate(raw)
    raw["input_calibration"] = str(tmp_path / "measured.json")
    config = AutomationConfig.model_validate(raw)
    file = tmp_path / "automation.toml"
    file.write_text(render_automation_toml(config))
    assert load_config(file) == config
    raw["input_backend"] = "unknown"
    with pytest.raises(ValidationError):
        AutomationConfig.model_validate(raw)


@pytest.mark.parametrize(
    "change", [None, "keymap", "settings", "authority", "display", "gain", "mapping", "left_handed"]
)
def test_calibration_is_explicit_private_and_bound_to_desktop(tmp_path, change):
    import hashlib

    from crowley_automation.gui.uinput import calibration

    authority = tmp_path / "authority"
    authority.write_bytes(b"fake session identity")
    keymap = "keycode 38 = a A\nkeycode 111 = Up\n"
    settings = "0.0\n0.0\nfalse\n"
    value = {
        "schema": 1,
        "display": ":99",
        "authority_sha256": hashlib.sha256(authority.read_bytes()).hexdigest(),
        "x_keymap_sha256": hashlib.sha256(keymap.encode()).hexdigest(),
        "mouse_settings_sha256": hashlib.sha256(settings.encode()).hexdigest(),
        "x_to_evdev": {"38": 30, "111": 103},
        "gain_x": 1.5,
        "gain_y": 1.2,
        "display_size": [1920, 1080],
        "idle_coverage": {},
    }
    if change == "gain":
        value["gain_x"] = float("nan")
    if change == "mapping":
        value["x_to_evdev"]["111"] = 30
    if change == "display":
        value["display"] = ":100"
    if change in {"keymap", "settings", "authority"}:
        field = {
            "keymap": "x_keymap_sha256",
            "settings": "mouse_settings_sha256",
            "authority": "authority_sha256",
        }[change]
        value[field] = "0" * 64
    file = tmp_path / "calibration.json"
    file.write_text(json.dumps(value))
    file.chmod(0o600)

    def run(argv, **_kwargs):
        return SimpleNamespace(
            stdout=keymap
            if argv[0] == "xmodmap"
            else (
                ("true\n" if change == "left_handed" else "false\n") if argv[-1] == "left-handed" else "0.0\n"
            )
        )

    env = {"DISPLAY": ":99", "XAUTHORITY": str(authority)}
    if change is None:
        measured = calibration(str(file), environment=env, run=run)
        assert measured["forward_code"] == 103
    else:
        with pytest.raises(DeviceError):
            calibration(str(file), environment=env, run=run)


@pytest.mark.parametrize(
    "case", ["qualified", "queued_input", "held", "controller", "seat", "identity", "topology"]
)
def test_mutter_bootstrap_requires_positive_exact_coverage_and_never_overwrites_activity(monkeypatch, case):
    import hashlib

    identity = (1, 2, 3)
    caps = {0: [0], 1: [30, 42], 4: [4]}
    if case == "controller":
        caps[3] = [0, 1]
    readers = []

    class Reader:
        def __init__(self, path):
            self.path, self.closed = path, False
            readers.append(self)
            self.events = [SimpleNamespace(type=1, code=30)] if case == "queued_input" else []

        def active_keys(self):
            return [30] if case == "held" else []

        def capabilities(self, **_kwargs):
            return caps

        def read(self):
            events, self.events = self.events, []
            return iter(events)

        def close(self):
            self.closed = True

    record = {
        "identity_sha256": hashlib.sha256(json.dumps(identity).encode()).hexdigest(),
        "capabilities_sha256": hashlib.sha256(json.dumps(caps, sort_keys=True).encode()).hexdigest(),
        "seat": "seat0",
    }
    if case == "identity":
        record["identity_sha256"] = "0" * 64
    coverage = {"/fake/keyboard": record}
    if case == "topology":
        coverage = {"/fake/other": record}
    monkeypatch.setattr(worker, "inventory", lambda: {"/fake/keyboard": identity})
    monkeypatch.setattr(worker, "device_identity", lambda _path: identity)
    monkeypatch.setattr(worker, "Presence", lambda: SimpleNamespace(idle_ms=lambda: 400000))
    output = "CURRENT_TAGS=:seat:uaccess:\nID_SEAT=" + ("seat1" if case == "seat" else "seat0") + "\n"
    monkeypatch.setattr(worker.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(stdout=output))
    if case in {"controller", "seat", "identity", "topology"}:
        with pytest.raises(DeviceError):
            worker.PhysicalMonitor(SimpleNamespace(InputDevice=Reader), coverage)
        assert all(reader.closed for reader in readers)
    else:
        monitor = worker.PhysicalMonitor(SimpleNamespace(InputDevice=Reader), coverage)
        assert monitor.active() == (case in {"queued_input", "held"})
        monitor.close()


def test_short_arm_rejects_unqualified_history_before_launching_helper(monkeypatch):
    from crowley_automation import backend

    io = backend.DesktopIO.__new__(backend.DesktopIO)
    io._devices = None
    io._lease = object()
    io.config = SimpleNamespace(input_calibration="/fake/calibration")
    io._env = {}
    io._deadline = lambda: time.time() + 120
    monkeypatch.setattr(backend, "calibration", lambda *_args, **_kwargs: {"idle_coverage": {}})
    with pytest.raises(backend.DesktopBackendError, match="short arm"):
        io._device_session()


@pytest.mark.parametrize("mode", [0o644, 0o660])
def test_calibration_rejects_nonprivate_file_before_parsing(tmp_path, mode):
    from crowley_automation.gui.uinput import calibration

    file = tmp_path / "calibration.json"
    file.write_text("{}")
    file.chmod(mode)
    with pytest.raises(DeviceError, match="private user-owned"):
        calibration(str(file), environment={})


def test_calibration_rejects_another_owner_before_parsing(tmp_path, monkeypatch):
    import os

    from crowley_automation.gui.uinput import calibration

    file = tmp_path / "calibration.json"
    file.write_text("{}")
    file.chmod(0o600)
    actual_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: actual_uid + 1)
    with pytest.raises(DeviceError, match="private user-owned"):
        calibration(str(file), environment={})


def test_scheduler_handles_check_latency_crossing_requested_wait(monkeypatch):
    now, timeouts = [0.0], []

    def check():
        now[0] += 0.1

    def select(_read, _write, _error, timeout):
        assert timeout >= 0
        timeouts.append(timeout)
        return ([], [], [])

    monkeypatch.setattr(worker.select, "select", select)
    worker._wait_until(object(), SimpleNamespace(check=check), 0.05, lambda: None, clock=lambda: now[0])
    assert timeouts == [0, 0]


@pytest.mark.parametrize("gain", [2, 0])
def test_relative_pointer_job_checks_feedback_and_rejects_unreachable_endpoint(tmp_path, gain):
    import os

    harness = HARNESS.replace("import json, socket, sys, types", "import json, socket, sys, types, os")
    harness = harness.replace("class Monitor:", "position = [0,0]\nclass Monitor:", 1)
    harness = harness.replace(
        "    def write(self, *event):\n",
        '    def write(self, *event):\n        if event[0] == 2: position[event[1]] += event[2]*int(os.environ["FAKE_GAIN"])\n',
    )
    harness = harness.replace(
        "    def size(self): return (1920,1080)",
        "    def position(self): return tuple(position)\n    def size(self): return (1920,1080)",
    )
    log = tmp_path / "events"
    lease = GuiLease(tmp_path / "private" / "gui.lock")
    with lease.hold():
        parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        proc = subprocess.Popen(
            [sys.executable, "-c", harness, str(child.fileno()), str(lease.path), str(lease.fd), str(log)],
            pass_fds=(child.fileno(), lease.fd),
            env={**os.environ, "FAKE_GAIN": str(gain)},
        )
        child.close()
        try:
            send_message(
                parent,
                {
                    "op": "init",
                    "profile": {
                        "x_to_evdev": {"111": 103},
                        "forward_code": 103,
                        "display_size": [1920, 1080],
                        "gain_x": 1.5,
                        "gain_y": 1.5,
                    },
                },
            )
            assert receive_message(parent)["ok"]
            send_message(
                parent,
                {
                    "op": "execute",
                    "args": ["sleep", ".03", "mousemove", "4", "0", "sleep", ".03", "mousemove", "8", "0"],
                    "deadline": time.time() + 10,
                },
            )
            if gain:
                assert receive_message(parent)["ok"]
            else:
                with pytest.raises(DeviceError, match="disconnected"):
                    receive_message(parent)
            parent.close()
            proc.wait(timeout=3)
            events = [json.loads(line) for line in log.read_text().splitlines()]
            assert events[-1] == "destroy"
            relative = [event for event in events[:-1] if event[0] == 2]
            assert relative
            assert all(event[0] != 1 for event in events[:-1])  # no click on either outcome
            if gain:
                assert sum(event[2] for event in relative) * gain == 8
                assert len(relative) > 2  # endpoint correction, not an assumed pixel/count identity
        finally:
            parent.close()
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=2)
