from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from crowley.native import build as native_build
from crowley.native import pins
from crowley.session.input_tool import (
    ARCHIVE_BASENAME,
    MANIFEST_BASENAME,
    PINNED_ARCHIVE_SHA256,
    PINNED_COMMIT,
    PRIVATE_TOOL_BASENAME,
    InputToolError,
    PinnedInputTool,
    verify_artifact,
)

VENDORED_SOURCE_ARCHIVE = native_build.VENDORED_SOURCE_ARCHIVE
_TEST_BUILD_INPUTS = {
    "compiler": {"name": "cc", "sha256": "0" * 64, "version": "test compiler"},
    "linker": {"name": "ld", "sha256": "1" * 64, "version": "test linker"},
}


def _artifact_copy(tmp_path: Path, executable_source: str = "") -> Path:
    root = tmp_path / "xdotool"
    root.mkdir(mode=0o700, parents=True)
    shutil.copyfile(executable_source or "/bin/true", root / PRIVATE_TOOL_BASENAME)
    shutil.copyfile(VENDORED_SOURCE_ARCHIVE, root / ARCHIVE_BASENAME)
    digest = hashlib.sha256((root / PRIVATE_TOOL_BASENAME).read_bytes()).hexdigest()
    (root / MANIFEST_BASENAME).write_text(
        json.dumps(
            {
                "commit": PINNED_COMMIT,
                "archive_sha256": PINNED_ARCHIVE_SHA256,
                "executable": PRIVATE_TOOL_BASENAME,
                "executable_sha256": digest,
                pins.XDOTOOL_BUILD_INPUTS_KEY: _TEST_BUILD_INPUTS,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    for child in root.iterdir():
        child.chmod(
            stat.S_IRUSR | stat.S_IWUSR | (stat.S_IXUSR if child.name == PRIVATE_TOOL_BASENAME else 0)
        )
    return root


def fixture_input_tool(tmp_path: Path, executable_source: str = "/bin/true") -> PinnedInputTool:
    """Build a test-only pinned image around a harmless no-input executable."""

    return PinnedInputTool.from_root(_artifact_copy(tmp_path, executable_source))


def test_synthetic_keyboard_profile_preserves_physical_control_ownership_through_admission(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Exercise the real keyboard profile, Guardian gate, and pinned producer."""

    from crowley.session import driver as driver_module
    from crowley.session.calibration import KeyboardProbeOperation, KeyboardProbeStep

    source = tmp_path / "argv-recorder.c"
    executable = tmp_path / "argv-recorder"
    source.write_text(
        """
#define _GNU_SOURCE
#include <fcntl.h>
#include <stdlib.h>
#include <stdio.h>
#include <unistd.h>
int main(int argc, char **argv) {
    const char *path = getenv("CROWLEY_KEY_ARGV");
    if (path == NULL) return 2;
    int fd = open(path, O_WRONLY | O_CREAT | O_APPEND, 0600);
    if (fd < 0) return 3;
    for (int i = 0; i < argc; ++i) {
        dprintf(fd, "%s%s", i == 0 ? "" : "|", argv[i]);
    }
    dprintf(fd, "\\n");
    close(fd);
    return 0;
}
""",
        encoding="utf-8",
    )
    subprocess.run(["cc", "-O2", str(source), "-o", str(executable)], check=True)
    argv_log = tmp_path / "argv.log"
    monkeypatch.setenv("CROWLEY_KEY_ARGV", str(argv_log))

    profile = driver_module.DriverProfile(
        display=":input-key-ownership",
        xauthority=str(tmp_path / "Xauthority"),
    )
    tool = fixture_input_tool(tmp_path / "tool", str(executable))
    backend = driver_module.X11Backend(profile, input_tool=tool)
    driver = driver_module.DesktopDriver(
        profile=profile,
        backend=backend,
        lease_path=tmp_path / "desktop.lock",
    )
    try:
        epoch = driver.begin_calibration_operation()
        ticket = driver_module.CalibrationTicket(
            epoch=epoch,
            target_pid=os.getpid(),
            target_xid=0x101,
            target_xids=frozenset({0x101}),
            title="Crowley GTK calibration",
            geometry=(0, 0, 100, 100),
            managed_frame_xid=0x100,
            managed_frame_geometry=(0, 0, 100, 100),
            target_executable=sys.executable,
            target_process_start=driver_module._proc_start(os.getpid()),
        )
        driver.guardian.issue_calibration_ticket(ticket)
        backend._calibration_focus_ticket = ticket

        published_holds: list[tuple[frozenset[str], frozenset[int], frozenset[int]]] = []
        publish_holds = backend._publish_holds
        assert publish_holds is not None

        def observe_holds(authority: object, keys: set[str], buttons: set[int]) -> None:
            # This is the real admission path: the child is registered before
            # the gate opens, so the recorder cannot run before ownership is
            # visible to the Guardian.
            assert backend.active_process_groups
            published_holds.append((frozenset(keys), frozenset(buttons), backend.active_process_groups))
            publish_holds(authority, keys, buttons)

        backend._publish_holds = observe_holds
        operations = driver.calibration_keyboard_operations()
        right_control = KeyboardProbeOperation(
            "test_only_select_chat_all_right",
            "",
            (
                KeyboardProbeStep("press", "Control_R"),
                KeyboardProbeStep("press", "a"),
                KeyboardProbeStep("release", "a"),
                KeyboardProbeStep("release", "Control_R"),
            ),
        )
        left_control = KeyboardProbeOperation(
            "test_only_select_chat_all_left",
            "",
            (
                KeyboardProbeStep("press", "Control_L"),
                KeyboardProbeStep("press", "a"),
                KeyboardProbeStep("release", "a"),
                KeyboardProbeStep("release", "Control_L"),
            ),
        )
        # These explicit test-only operations exercise the generic chord
        # emitter and held-state admission path. Production calibration no
        # longer advertises the chat-selection chord as a runtime action.
        shifted_text = KeyboardProbeOperation(
            "type_allowlisted_command",
            "Ab",
            (
                KeyboardProbeStep("press", "Shift_L"),
                KeyboardProbeStep("press", "a"),
                KeyboardProbeStep("release", "a"),
                KeyboardProbeStep("release", "Shift_L"),
                KeyboardProbeStep("press", "b"),
                KeyboardProbeStep("release", "b"),
            ),
        )
        profile_operations = operations + (left_control, right_control, shifted_text)
        profile_keysyms = sorted({step.keysym for item in profile_operations for step in item.steps})
        profile_keymap = {keysym: index + 10 for index, keysym in enumerate(profile_keysyms)}
        backend.synthetic_keyboard_profile(
            profile_operations,
            keymap=profile_keymap,
            calibration_ticket=ticket,
        )

        rows = argv_log.read_text(encoding="utf-8").splitlines()
        assert rows
        control_l = f"{profile_keymap['Control_L']:03d}"
        control_r = f"{profile_keymap['Control_R']:03d}"
        shift_l = f"{profile_keymap['Shift_L']:03d}"
        letter_a = f"{profile_keymap['a']:03d}"
        letter_b = f"{profile_keymap['b']:03d}"
        return_key = f"{profile_keymap['Return']:03d}"
        assert any(
            row.endswith(f"keydown|{control_l}|keydown|{letter_a}|keyup|{letter_a}|keyup|{control_l}")
            for row in rows
        )
        assert any(
            row.endswith(f"keydown|{control_r}|keydown|{letter_a}|keyup|{letter_a}|keyup|{control_r}")
            for row in rows
        )
        assert any(
            row.endswith(f"keydown|{shift_l}|keydown|{letter_a}|keyup|{letter_a}|keyup|{shift_l}")
            for row in rows
        )
        assert any(row.endswith(f"keydown|{letter_b}|keyup|{letter_b}") for row in rows)
        assert any(row.endswith(f"keydown|{return_key}|keyup|{return_key}") for row in rows)
        assert not any("ctrl+a" in row for row in rows)
        assert published_holds
        assert not backend.active_process_groups
        assert not backend._process_calibration_tickets

        before_refusal = len(rows)
        with pytest.raises(driver_module.DriverError, match="holds do not match"):
            backend._run(
                [
                    "xdotool",
                    "keydown",
                    control_l,
                    "keydown",
                    letter_a,
                    "keyup",
                    letter_a,
                    "keyup",
                    control_l,
                ],
                calibration_ticket=ticket,
                held_keys={control_r, letter_a},
                input_schema="keyboard-steps",
                expected_argv=(
                    "xdotool",
                    "keydown",
                    control_l,
                    "keydown",
                    letter_a,
                    "keyup",
                    letter_a,
                    "keyup",
                    control_l,
                ),
            )
        assert len(argv_log.read_text(encoding="utf-8").splitlines()) == before_refusal
        assert not backend.active_process_groups

        driver.guardian.cancel_calibration_operation(ticket)
        with pytest.raises(driver_module.DriverError, match="cancelled|revoked"):
            backend._run(
                [
                    "xdotool",
                    "keydown",
                    control_l,
                    "keydown",
                    letter_a,
                    "keyup",
                    letter_a,
                    "keyup",
                    control_l,
                ],
                calibration_ticket=ticket,
                held_keys={control_l, letter_a},
                input_schema="keyboard-steps",
                expected_argv=(
                    "xdotool",
                    "keydown",
                    control_l,
                    "keydown",
                    letter_a,
                    "keyup",
                    letter_a,
                    "keyup",
                    control_l,
                ),
            )
        assert len(argv_log.read_text(encoding="utf-8").splitlines()) == before_refusal
        assert not backend.active_process_groups
        assert not backend._process_calibration_tickets
    finally:
        driver.close()


def test_vendored_source_archive_is_the_pinned_upstream_source() -> None:
    archive = VENDORED_SOURCE_ARCHIVE
    assert archive.stat().st_size == 115654
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == PINNED_ARCHIVE_SHA256


def test_nonzero_x11_screen_is_rejected_before_pin_or_lease(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from crowley.session import driver as driver_module

    def unexpected_pin() -> PinnedInputTool:
        raise AssertionError("nonzero screen was checked after tool pinning")

    monkeypatch.setattr(driver_module.PinnedInputTool, "load", unexpected_pin)
    lease_path = tmp_path / "desktop.lock"
    with pytest.raises(driver_module.DriverError, match="screen must be 0"):
        driver_module.DesktopDriver(
            profile=driver_module.DriverProfile(display=":0.1"),
            lease_path=lease_path,
        )
    assert not lease_path.exists()


@pytest.mark.parametrize("mismatch", ["display", "xauthority"])
def test_injected_backend_identity_must_match_selected_profile(tmp_path: Path, mismatch: str) -> None:
    from crowley.session.driver import DesktopDriver, DriverError, DriverProfile, X11Backend

    selected_xauthority = tmp_path / "selected.Xauthority"
    backend_xauthority = tmp_path / "backend.Xauthority"
    selected = DriverProfile(
        display=":selected" if mismatch == "display" else ":same",
        xauthority=str(selected_xauthority),
    )
    backend_profile = DriverProfile(
        display=":backend" if mismatch == "display" else ":same",
        xauthority=str(backend_xauthority if mismatch == "xauthority" else selected_xauthority),
    )
    tool = fixture_input_tool(tmp_path / "tool")
    backend = X11Backend(backend_profile, input_tool=tool)
    descriptor = tool.image.fd
    lease_path = tmp_path / "identity.lock"
    with pytest.raises(DriverError, match="does not match the selected profile"):
        DesktopDriver(profile=selected, backend=backend, lease_path=lease_path)
    assert not lease_path.exists()
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_private_artifact_and_sealed_memfd_identity(tmp_path: Path) -> None:
    root = _artifact_copy(tmp_path)
    identity = verify_artifact(root)
    tool = PinnedInputTool.from_root(root)
    try:
        assert identity.executable_sha256 == tool.digest
        assert os.readlink(f"/proc/{os.getpid()}/fd/{tool.image.fd}") == "/memfd:crowley-xdotool (deleted)"
        assert tool.proc_path.startswith("/proc/self/fd/")
    finally:
        tool.close()


def test_native_release_spec_rejects_matching_unsealed_memfd(tmp_path: Path) -> None:
    from crowley.session.guardian import GuardianError, InputGuardian
    from crowley.session.input_tool import NativeReleaseSpec, _memfd_create

    fd = _memfd_create()
    data = Path("/bin/true").read_bytes()
    os.write(fd, data)
    spec = NativeReleaseSpec(fd, hashlib.sha256(data).hexdigest(), "fixture")
    try:
        with pytest.raises(InputToolError, match="fully sealed"):
            spec.verify_image()
        with pytest.raises(GuardianError, match="release image is invalid"):
            InputGuardian(tmp_path / "unsealed.lock", watchdog_release_spec=spec)
        with pytest.raises(OSError):
            os.fstat(fd)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def test_driver_constructor_closes_pinned_image_on_lease_path_failure(tmp_path: Path) -> None:
    from crowley.session.driver import DesktopDriver, DriverError, DriverProfile, X11Backend

    profile = DriverProfile(display=":constructor", xauthority=str(tmp_path / "Xauthority"))
    tool = fixture_input_tool(tmp_path / "tool")
    backend = X11Backend(profile, input_tool=tool)
    descriptor = tool.image.fd
    with pytest.raises((TypeError, DriverError)):
        DesktopDriver(profile=profile, backend=backend, lease_path=object())
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_driver_constructor_closes_pinned_image_when_release_duplication_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from crowley.session.driver import DesktopDriver, DriverProfile, X11Backend

    profile = DriverProfile(display=":duplicate", xauthority=str(tmp_path / "Xauthority"))
    tool = fixture_input_tool(tmp_path / "tool")
    backend = X11Backend(profile, input_tool=tool)
    descriptor = tool.image.fd
    monkeypatch.setattr(
        backend.input_tool, "duplicate_release_spec", lambda: (_ for _ in ()).throw(OSError("dup"))
    )
    with pytest.raises(OSError, match="dup"):
        DesktopDriver(profile=profile, backend=backend, lease_path=tmp_path / "duplicate.lock")
    with pytest.raises(OSError):
        os.fstat(descriptor)


def test_guardian_closes_release_spec_when_lease_is_already_held(tmp_path: Path) -> None:
    from crowley.session.guardian import GuardianError, InputGuardian, InputLease

    tool = fixture_input_tool(tmp_path / "tool")
    spec = tool.duplicate_release_spec()
    lease_path = tmp_path / "held.lock"
    owner = InputLease(lease_path)
    owner.acquire()
    try:
        with pytest.raises(GuardianError, match="another process"):
            InputGuardian(lease_path, watchdog_release_spec=spec)
        with pytest.raises(OSError):
            os.fstat(spec.fd)
    finally:
        owner.release()
        tool.close()


def test_sealed_image_executes_after_published_path_is_removed(tmp_path: Path) -> None:
    root = _artifact_copy(tmp_path, "/bin/true")
    tool = PinnedInputTool.from_root(root)
    try:
        (root / PRIVATE_TOOL_BASENAME).unlink()
        result = subprocess.run(
            [tool.proc_path, "ignored"],
            check=False,
            pass_fds=(tool.image.fd,),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        assert result.returncode == 0
        with pytest.raises(InputToolError):
            verify_artifact(root)
    finally:
        tool.close()


def test_admission_eof_cannot_exec_harmless_process(tmp_path: Path) -> None:
    from crowley.session.process_admission import ADMISSION_ABORT_EXIT, ADMISSION_WRAPPER

    marker = tmp_path / "executed"
    read_fd, write_fd = os.pipe()
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            ADMISSION_WRAPPER,
            str(read_fd),
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).touch()",
        ],
        pass_fds=(read_fd,),
    )
    os.close(read_fd)
    os.close(write_fd)
    assert process.wait(timeout=2) == ADMISSION_ABORT_EXIT
    assert not marker.exists()


def test_publication_drift_revokes_actual_driver_and_clears_evidence(tmp_path: Path) -> None:
    from crowley.session.driver import DesktopDriver, DriverError, DriverProfile, X11Backend

    profile = DriverProfile(display=":drift", xauthority=str(tmp_path / "Xauthority"))
    root = _artifact_copy(tmp_path / "tool")
    backend = X11Backend(profile, input_tool=PinnedInputTool.from_root(root))
    driver = DesktopDriver(profile=profile, backend=backend, lease_path=tmp_path / "drift.lock")
    evidence = SimpleNamespace(is_live=True, fingerprint="drift-proof")
    driver._input_evidence = evidence
    driver._retained_input_evidence = evidence
    driver._input_calibration_readiness = evidence
    try:
        driver.guardian.arm(evidence)
        executable = root / PRIVATE_TOOL_BASENAME
        executable.write_bytes(executable.read_bytes() + b"drift")
        with pytest.raises(DriverError, match="publication drifted|digest changed"):
            backend._check_published_tool()
        assert not driver.guardian.armed
        assert driver._input_evidence is None
        assert driver._retained_input_evidence is None
        assert driver._input_calibration_readiness is None
    finally:
        driver.close()


def test_broken_watchdog_blocks_production_admission_without_exec(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from crowley.session.driver import DesktopDriver, DriverProfile, X11Backend
    from crowley.session.guardian import GuardianError

    marker = tmp_path / "executed"
    source = tmp_path / "marker.c"
    executable = tmp_path / "marker"
    source.write_text(
        """
#include <fcntl.h>
#include <stdlib.h>
#include <unistd.h>
int main(void) {
    const char *path = getenv("CROWLEY_MARKER");
    if (path == NULL) return 2;
    int fd = open(path, O_WRONLY | O_CREAT | O_APPEND, 0600);
    if (fd < 0) return 3;
    (void)write(fd, "executed\\n", 9);
    close(fd);
    return 0;
}
""",
        encoding="utf-8",
    )
    subprocess.run(["cc", "-O2", str(source), "-o", str(executable)], check=True)
    root = _artifact_copy(tmp_path / "tool", str(executable))
    tool = PinnedInputTool.from_root(root)
    selected_profile = DriverProfile(display=":watchdog", xauthority=str(tmp_path / "Xauthority"))
    backend = X11Backend(selected_profile, input_tool=tool)
    driver = DesktopDriver(profile=selected_profile, backend=backend, lease_path=tmp_path / "watchdog.lock")
    monkeypatch.setenv("CROWLEY_MARKER", str(marker))
    try:
        generation = driver.guardian.arm(SimpleNamespace(is_live=True, fingerprint="watchdog-proof"))
        watchdog = driver.guardian._watchdog_process
        assert watchdog is not None
        watchdog.terminate()
        watchdog.join(timeout=2)
        assert not driver.guardian._watchdog_state()
        assert driver.guardian._watchdog_failed
        with pytest.raises(GuardianError, match="watchdog is unhealthy"):
            backend._run(
                ["xdotool", "key", "--", "w"],
                generation=generation,
                held_keys={"w"},
            )
        assert not marker.exists()
        assert not backend.active_process_groups
    finally:
        driver.close()


def test_hold_watchdog_ack_failure_aborts_positive_gate_without_exec(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from crowley.session.driver import DesktopDriver, DriverProfile, X11Backend
    from crowley.session.guardian import GuardianError

    marker = tmp_path / "executed"
    source = tmp_path / "marker.c"
    executable = tmp_path / "marker"
    source.write_text(
        """
#include <fcntl.h>
#include <stdlib.h>
#include <unistd.h>
int main(void) {
    const char *path = getenv("CROWLEY_MARKER");
    if (path == NULL) return 2;
    int fd = open(path, O_WRONLY | O_CREAT | O_APPEND, 0600);
    if (fd < 0) return 3;
    (void)write(fd, "executed\\n", 9);
    close(fd);
    return 0;
}
""",
        encoding="utf-8",
    )
    subprocess.run(["cc", "-O2", str(source), "-o", str(executable)], check=True)
    tool = fixture_input_tool(tmp_path / "tool", str(executable))
    profile = DriverProfile(display=":hold-ack", xauthority=str(tmp_path / "Xauthority"))
    backend = X11Backend(profile, input_tool=tool)
    driver = DesktopDriver(profile=profile, backend=backend, lease_path=tmp_path / "hold-ack.lock")
    monkeypatch.setenv("CROWLEY_MARKER", str(marker))

    original_register = backend._register_process_group
    assert original_register is not None

    def reap_watchdog_after_registration(authority: int | None, pgid: int) -> None:
        # The real registration call waits for the watchdog ACK.  Kill and
        # reap that real child only after the ACK returns, before hold
        # publication and the positive admission token.
        original_register(authority, pgid)
        watchdog = driver.guardian._watchdog_process
        assert watchdog is not None
        watchdog.terminate()
        watchdog.join(timeout=2)
        assert not watchdog.is_alive()

    backend._register_process_group = reap_watchdog_after_registration
    try:
        generation = driver.guardian.arm(SimpleNamespace(is_live=True, fingerprint="hold-ack"))
        driver.guardian.begin_action(
            generation,
            target_xid=0x101,
            source_device=5,
            master_device=5,
            event_classes={"KeyPress"},
            held_keys={"w"},
        )
        with pytest.raises(GuardianError, match="watchdog"):
            backend._run(
                ["xdotool", "key", "--", "w"],
                generation=generation,
                held_keys={"w"},
            )
        assert not marker.exists()
        assert not backend.active_process_groups
    finally:
        driver.close()


def test_cancelled_after_group_registration_never_releases_admission_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A real wrapper killed after registration must never reach its target."""

    from crowley.session.driver import DesktopDriver, DriverError, DriverProfile, X11Backend

    marker = tmp_path / "executed"
    source = tmp_path / "marker.c"
    executable = tmp_path / "marker"
    source.write_text(
        """
#include <fcntl.h>
#include <stdlib.h>
#include <unistd.h>
int main(void) {
    const char *path = getenv("CROWLEY_MARKER");
    if (path == NULL) return 2;
    int fd = open(path, O_WRONLY | O_CREAT | O_APPEND, 0600);
    if (fd < 0) return 3;
    (void)write(fd, "executed\\n", 9);
    close(fd);
    return 0;
}
""",
        encoding="utf-8",
    )
    subprocess.run(["cc", "-O2", str(source), "-o", str(executable)], check=True)
    tool = fixture_input_tool(tmp_path / "tool", str(executable))
    profile = DriverProfile(display=":cancel-before-gate", xauthority=str(tmp_path / "Xauthority"))
    backend = X11Backend(profile, input_tool=tool)
    driver = DesktopDriver(profile=profile, backend=backend, lease_path=tmp_path / "cancel.lock")
    monkeypatch.setenv("CROWLEY_MARKER", str(marker))
    calls = 0

    def cancel_after_registration(generation: int | None, calibration_ticket: object | None = None) -> bool:
        del generation, calibration_ticket
        nonlocal calls
        calls += 1
        return calls >= 2

    monkeypatch.setattr(backend, "_is_cancelled", cancel_after_registration)
    try:
        generation = driver.guardian.arm(SimpleNamespace(is_live=True, fingerprint="cancel-before-gate"))
        driver.guardian.begin_action(
            generation,
            target_xid=0x101,
            source_device=5,
            master_device=5,
            event_classes={"KeyPress"},
            held_keys={"w"},
        )
        with pytest.raises(DriverError, match="cancelled"):
            backend._run(
                ["xdotool", "key", "--", "w"],
                generation=generation,
                held_keys={"w"},
            )
        assert calls >= 2
        assert not marker.exists()
        assert not backend.active_process_groups
    finally:
        driver.close()


def test_unknown_explicit_input_schema_rejects_before_spawn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from crowley.session.driver import DriverError, DriverProfile, X11Backend

    tool = fixture_input_tool(tmp_path / "tool")
    backend = X11Backend(
        DriverProfile(display=":unknown-schema", xauthority=str(tmp_path / "Xauthority")),
        input_tool=tool,
    )
    spawned = False

    def unexpected_spawn(*_args: object, **_kwargs: object) -> None:
        nonlocal spawned
        spawned = True
        raise AssertionError("unknown input schema reached subprocess admission")

    monkeypatch.setattr(subprocess, "Popen", unexpected_spawn)
    try:
        with pytest.raises(DriverError, match="unknown input schema"):
            backend._run(
                ["xdotool", "getwindowfocus"],
                input_schema="future-schema",
                expected_argv=("xdotool", "getwindowfocus"),
            )
        assert not spawned
        assert not backend.active_process_groups
    finally:
        tool.close()


def test_desktop_close_retains_pinned_image_when_guardian_close_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from crowley.session.driver import DesktopDriver, DriverProfile, X11Backend
    from crowley.session.guardian import GuardianError

    profile = DriverProfile(display=":close-retain", xauthority=str(tmp_path / "Xauthority"))
    tool = fixture_input_tool(tmp_path / "tool")
    backend = X11Backend(profile, input_tool=tool)
    driver = DesktopDriver(profile=profile, backend=backend, lease_path=tmp_path / "close-retain.lock")
    descriptor = tool.image.fd
    original_close = driver.guardian.close

    def failed_close() -> None:
        raise GuardianError("release still pending")

    monkeypatch.setattr(driver.guardian, "close", failed_close)
    try:
        with pytest.raises(GuardianError, match="release still pending"):
            driver.close()
        os.fstat(descriptor)
    finally:
        monkeypatch.setattr(driver.guardian, "close", original_close)
        driver.close()


def test_builder_rechecks_existing_artifact_linkage(tmp_path: Path) -> None:
    root = _artifact_copy(tmp_path)
    with pytest.raises(RuntimeError, match="static XTEST linkage"):
        native_build.build_xdotool(VENDORED_SOURCE_ARCHIVE, root)


def test_builder_idempotent_existing_artifact_rechecks_real_static_call_path(tmp_path: Path) -> None:
    destination = tmp_path / "built"
    first = native_build.build_xdotool(VENDORED_SOURCE_ARCHIVE, destination)
    digest = hashlib.sha256((first / PRIVATE_TOOL_BASENAME).read_bytes()).hexdigest()
    second = native_build.build_xdotool(VENDORED_SOURCE_ARCHIVE, destination)
    assert second == destination
    assert hashlib.sha256((second / PRIVATE_TOOL_BASENAME).read_bytes()).hexdigest() == digest


def test_builder_rejects_existing_dynamic_libxdo_dependency(tmp_path: Path) -> None:
    library_source = tmp_path / "xdo.c"
    library = tmp_path / "libxdo.so"
    executable_source = tmp_path / "dynamic.c"
    executable = tmp_path / "dynamic"
    library_source.write_text("void xdo_move_mouse(void) {}\n", encoding="utf-8")
    executable_source.write_text(
        "extern void xdo_move_mouse(void); int main(void) { xdo_move_mouse(); return 0; }\n",
        encoding="utf-8",
    )
    subprocess.run(["cc", "-shared", "-fPIC", str(library_source), "-o", str(library)], check=True)
    subprocess.run(
        [
            "cc",
            str(executable_source),
            "-L",
            str(tmp_path),
            "-Wl,-rpath," + str(tmp_path),
            "-lxdo",
            "-o",
            str(executable),
        ],
        check=True,
    )
    root = _artifact_copy(tmp_path / "artifact", str(executable))
    with pytest.raises(RuntimeError, match="static XTEST linkage"):
        native_build.build_xdotool(VENDORED_SOURCE_ARCHIVE, root)


def test_builder_rejects_existing_non_xtest_move_call_path(tmp_path: Path) -> None:
    source = tmp_path / "callpath.c"
    executable = tmp_path / "callpath"
    source.write_text(
        "void xdo_move_mouse(void) {} int main(void) { xdo_move_mouse(); return 0; }\n",
        encoding="utf-8",
    )
    subprocess.run(["cc", "-O2", str(source), "-o", str(executable)], check=True)
    root = _artifact_copy(tmp_path / "artifact", str(executable))
    with pytest.raises(RuntimeError, match="static XTEST linkage"):
        native_build.build_xdotool(VENDORED_SOURCE_ARCHIVE, root)


def test_pinned_watchdog_releases_exact_holds_after_publication_replacement(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    recorder_source = tmp_path / "recorder.c"
    recorder = tmp_path / "recorder"
    recorder_source.write_text(
        """
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

int main(int argc, char **argv) {
    const char *path = getenv("CROWLEY_RECORDER");
    if (path == NULL) return 2;
    int fd = open(path, O_WRONLY | O_CREAT | O_APPEND, 0600);
    if (fd < 0) return 3;
    dprintf(fd, "%s %s\\n", argc > 1 ? argv[1] : "", argc > 2 ? argv[2] : "");
    close(fd);
    return 0;
}
""",
        encoding="utf-8",
    )
    subprocess.run(["cc", "-O2", str(recorder_source), "-o", str(recorder)], check=True)
    root = _artifact_copy(tmp_path / "artifact", str(recorder))
    tool = PinnedInputTool.from_root(root)
    marker = tmp_path / "released"
    driver = None
    try:
        published_executable = root / PRIVATE_TOOL_BASENAME
        published_executable.unlink()
        shutil.copyfile("/bin/true", published_executable)
        published_executable.chmod(0o700)

        read_fd, write_fd = os.pipe()
        os.write(write_fd, b'H{"keys":["w"],"buttons":[3]}\n')
        os.close(write_fd)
        from crowley.session.guardian import _watchdog_entry

        release_spec = tool.duplicate_release_spec()
        try:
            _watchdog_entry(
                2**31,
                None,
                read_fd,
                0.01,
                (),
                {"CROWLEY_RECORDER": str(marker)},
                release_spec=release_spec,
            )
        finally:
            try:
                os.close(release_spec.fd)
            except OSError:
                pass
        assert marker.read_text(encoding="utf-8").splitlines() == ["keyup w", "mouseup 3"]

        from crowley.session.driver import DesktopDriver, DriverError, DriverProfile, X11Backend

        monkeypatch.setenv("CROWLEY_RECORDER", str(marker))
        backend = X11Backend(
            DriverProfile(display=":recorder", xauthority=str(tmp_path / "Xauthority")),
            input_tool=tool,
        )
        driver = DesktopDriver(
            profile=backend.profile,
            backend=backend,
            lease_path=tmp_path / "replacement.lock",
        )
        generation = driver.guardian.arm(SimpleNamespace(is_live=True, fingerprint="replacement-proof"))
        with driver.guardian._lock:
            driver.guardian._held = True
            driver.guardian._held_keys = {"w"}
        backend._held_keys = {"w"}
        with pytest.raises(DriverError, match="publication drifted|digest changed"):
            backend._check_published_tool()
        assert marker.read_text(encoding="utf-8").splitlines()[-1] == "keyup w"
        assert not driver.guardian.armed
        assert driver.guardian.generation == generation + 1

        from crowley.session.guardian import InputGuardian

        guardian = InputGuardian(
            tmp_path / "watchdog.lock",
            owner_pid=os.getpid(),
            watchdog_command=(),
            watchdog_env={"CROWLEY_RECORDER": str(marker)},
            watchdog_release_spec=tool.duplicate_release_spec(),
        )
        try:
            # Constructor readiness means the child has detached its own
            # release image before the owner can publish physical holds.
            with guardian._lock:
                guardian._held = True
                guardian._held_keys = {"w"}
                guardian._held_buttons = {3}
            guardian._watchdog_state()
            os.close(guardian._watchdog_write)
            guardian._watchdog_write = -1
            guardian._watchdog_process.join(timeout=3)
            assert not guardian._watchdog_process.is_alive()
            assert set(marker.read_text(encoding="utf-8").splitlines()[-2:]) == {
                "keyup w",
                "mouseup 3",
            }
            with guardian._lock:
                guardian._held = False
                guardian._held_keys.clear()
                guardian._held_buttons.clear()
        finally:
            guardian.close()
    finally:
        if driver is not None:
            driver.close()
        tool.close()


def test_watchdog_drains_fragmented_final_state_before_owner_cleanup(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from crowley.session.guardian import InputGuardian, _watchdog_entry

    recorder_source = tmp_path / "recorder.c"
    recorder = tmp_path / "recorder"
    marker = tmp_path / "released"
    recorder_source.write_text(
        """
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>
int main(int argc, char **argv) {
    const char *path = getenv("CROWLEY_RECORDER");
    if (path == NULL || argc < 3) return 2;
    int fd = open(path, O_WRONLY | O_CREAT | O_APPEND, 0600);
    if (fd < 0) return 3;
    dprintf(fd, "%s %s\\n", argv[1], argv[2]);
    close(fd);
    return 0;
}
""",
        encoding="utf-8",
    )
    subprocess.run(["cc", "-O2", str(recorder_source), "-o", str(recorder)], check=True)
    tool = PinnedInputTool.from_root(_artifact_copy(tmp_path / "tool", str(recorder)))
    read_fd, write_fd = os.pipe()
    ack_read, ack_write = os.pipe()
    os.close(ack_read)
    state = {"alive": True}
    monkeypatch.setattr(
        InputGuardian,
        "_proc_start",
        staticmethod(lambda _pid: "owner-start" if state["alive"] else None),
    )
    release_spec = tool.duplicate_release_spec()
    worker = threading.Thread(
        target=_watchdog_entry,
        args=(424242, "owner-start", read_fd, 0.01, (), {"CROWLEY_RECORDER": str(marker)}),
        kwargs={"release_spec": release_spec, "ack_fd": ack_write},
        daemon=True,
    )
    worker.start()
    try:
        time.sleep(0.05)
        os.write(write_fd, b'T{"groups":[],"keys":["w"],')
        time.sleep(0.05)
        os.write(
            write_fd,
            b'"buttons":[]}\nT{"groups":[],"keys":["w"],"buttons":[3]}\n',
        )
        state["alive"] = False
        os.close(write_fd)
        write_fd = -1
        worker.join(timeout=3)
        assert not worker.is_alive()
        assert marker.read_text(encoding="utf-8").splitlines() == ["keyup w", "mouseup 3"]
    finally:
        if write_fd >= 0:
            os.close(write_fd)
        try:
            os.close(ack_write)
        except OSError:
            pass
        tool.close()


@pytest.mark.parametrize("mutation", ["symlink", "writable", "tampered", "foreign"])
def test_private_artifact_rejects_tampering(tmp_path: Path, mutation: str) -> None:
    root = _artifact_copy(tmp_path)
    executable = root / PRIVATE_TOOL_BASENAME
    if mutation == "symlink":
        executable.unlink()
        executable.symlink_to("/bin/true")
    elif mutation == "writable":
        executable.chmod(executable.stat().st_mode | stat.S_IWGRP)
    elif mutation == "tampered":
        executable.write_bytes(executable.read_bytes() + b"tampered")
    else:
        # lchown is unavailable to an unprivileged test user, so make the
        # directory fail the ownership check through a foreign mode contract.
        root.chmod(0o777)
    with pytest.raises(InputToolError):
        verify_artifact(root)


def test_manifest_rejects_unknown_fields(tmp_path: Path) -> None:
    root = _artifact_copy(tmp_path)
    manifest = json.loads((root / MANIFEST_BASENAME).read_text(encoding="utf-8"))
    manifest["fallback"] = "/usr/bin/xdotool"
    (root / MANIFEST_BASENAME).write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(InputToolError, match="fields"):
        verify_artifact(root)


def test_manifest_rejects_legacy_missing_build_inputs(tmp_path: Path) -> None:
    root = _artifact_copy(tmp_path)
    manifest = json.loads((root / MANIFEST_BASENAME).read_text(encoding="utf-8"))
    del manifest[pins.XDOTOOL_BUILD_INPUTS_KEY]
    (root / MANIFEST_BASENAME).write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(InputToolError, match="fields"):
        verify_artifact(root)


def test_private_artifact_rejects_foreign_owner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    root = _artifact_copy(tmp_path)
    current_uid = os.getuid()
    monkeypatch.setattr("crowley.session.input_tool.os.getuid", lambda: current_uid + 1)
    with pytest.raises(InputToolError, match="not user-owned"):
        verify_artifact(root)
