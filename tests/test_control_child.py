from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import crowley_automation.gui.control_child as control_child
import pytest
from crowley_automation.desktop_input import ActError, GuiLease, XtestKeys
from crowley_automation.gui.control_child import (
    ControlChildCancelled,
    ControlChildError,
    ControlChildRunner,
    PinnedControlTool,
    _classify,
)


def _session_environment(tmp_path: Path) -> dict[str, str]:
    authority = tmp_path / "Xauthority"
    authority.touch(exist_ok=True)
    return {"DISPLAY": ":99", "XAUTHORITY": str(authority)}


def test_window_manager_grammar_is_exact_and_binds_one_window() -> None:
    assert (
        _classify(
            "wmctrl",
            ("-i", "-r", "42", "-b", "add,fullscreen"),
            expected_window=42,
        )
        == "wm_fullscreen_add"
    )
    with pytest.raises(ControlChildError):
        _classify(
            "wmctrl",
            ("-i", "-r", "43", "-b", "add,fullscreen"),
            expected_window=42,
        )
    with pytest.raises(ControlChildError):
        _classify("wmctrl", ("-c", "Battle.net"), expected_window=42)


def test_wine_is_not_a_control_tool_or_serialized_child_record(tmp_path: Path) -> None:
    with pytest.raises(ControlChildError, match="outside the allowlist"):
        PinnedControlTool.load("wine")

    assert (
        control_child._main(
            [
                str(tmp_path / "gui.lock"),
                "77",
                "78",
                "99",
                "0",
                "0" * 64,
                "wine",
                "battle_net_product_exec",
                "42",
            ]
        )
        == 125
    )


def test_stop_cancels_exact_input_child_releases_key_and_keeps_lease_until_reap(tmp_path: Path) -> None:
    private_dir = tmp_path / "private"
    private_dir.mkdir(mode=0o700)
    lease = GuiLease(private_dir / "gui.lock", timeout=0.1)
    descriptor = os.open("/dev/null", os.O_RDONLY)
    tool = SimpleNamespace(
        image=SimpleNamespace(fd=descriptor),
        proc_path="/pinned/xdotool",
        digest="0" * 64,
    )
    stop_checks = 0
    launched: list[list[str]] = []

    class Child:
        def __init__(self, pid: int, *, completed: bool = False) -> None:
            self.pid = pid
            self.returncode = 0 if completed else None

        def poll(self):
            return self.returncode

        def terminate(self) -> None:
            self.returncode = -15

        def wait(self, *, timeout: float):
            if self.returncode is None:
                raise AssertionError("terminated child should be waitable")
            return self.returncode

        def communicate(self, *, timeout: float):
            return "", ""

    def fake_popen(command, **_kwargs):
        launched.append(list(command))
        return Child(300 + len(launched), completed=len(launched) > 1)

    def stopping() -> bool:
        nonlocal stop_checks
        stop_checks += 1
        return stop_checks >= 4

    try:
        with lease.hold():
            runner = ControlChildRunner(
                lease,
                tool,  # type: ignore[arg-type]
                environment=_session_environment(tmp_path),
                stopping=stopping,
                guard=lambda: None,
                popen=fake_popen,
                sleep=lambda _duration: None,
            )
            with pytest.raises(ControlChildCancelled):
                runner.run(
                    [tool.proc_path, "keydown", "38", "keyup", "38"],
                    pass_fds=(descriptor,),
                )
            assert runner.child_pid is None
            assert runner.possible_down == frozenset()
            assert launched[1][-2:] == ["keyup", "38"]
            with pytest.raises(ActError):
                with GuiLease(lease.path, timeout=0.1).hold():
                    pass
            runner.close()
    finally:
        os.close(descriptor)


def test_unreaped_exact_child_remains_tracked_for_lease_retention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    descriptor = os.open("/dev/null", os.O_RDONLY)
    tool = SimpleNamespace(image=SimpleNamespace(fd=descriptor), proc_path="/pinned/xdotool", digest="0" * 64)
    lease = SimpleNamespace(held=True, fd=77)
    runner = ControlChildRunner(
        lease,  # type: ignore[arg-type]
        tool,  # type: ignore[arg-type]
        environment=_session_environment(tmp_path),
        stopping=lambda: False,
        guard=lambda: None,
        popen=lambda *_args, **_kwargs: None,
    )

    class Unreapable:
        pid = 404

        def terminate(self) -> None:
            pass

        def wait(self, *, timeout: float):
            raise subprocess.TimeoutExpired("fake-control", timeout)

    runner._child = Unreapable()
    monkeypatch.setattr(control_child.os, "kill", lambda pid, signum: None)
    try:
        with pytest.raises(ControlChildError, match="could not be reaped"):
            runner._cancel_and_reap()
        assert runner.child_pid == 404
        assert runner.cleanup_failed
    finally:
        os.close(descriptor)


def test_failed_click_cleanup_never_replaces_an_unreaped_control_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_dir = tmp_path / "private"
    private_dir.mkdir(mode=0o700)
    lease = GuiLease(private_dir / "gui.lock", timeout=0.1)
    descriptor = os.open("/dev/null", os.O_RDONLY)
    tool = SimpleNamespace(
        image=SimpleNamespace(fd=descriptor),
        proc_path="/pinned/xdotool",
        digest="0" * 64,
    )
    launches: list[list[str]] = []
    stop_checks = 0

    class UnreapableClick:
        pid = 404

        def poll(self):
            return None

        def terminate(self) -> None:
            pass

        def wait(self, *, timeout: float):
            raise subprocess.TimeoutExpired("fake-click", timeout)

    child = UnreapableClick()

    def stopping() -> bool:
        nonlocal stop_checks
        stop_checks += 1
        return stop_checks >= 4

    def fake_popen(command, **_kwargs):
        launches.append(list(command))
        return child

    monkeypatch.setattr(control_child.os, "kill", lambda _pid, _signum: None)
    try:
        with lease.hold():
            runner = ControlChildRunner(
                lease,
                tool,  # type: ignore[arg-type]
                environment=_session_environment(tmp_path),
                stopping=stopping,
                guard=lambda: None,
                popen=fake_popen,
                sleep=lambda _duration: None,
            )
            with pytest.raises(ControlChildError, match="could not be reaped"):
                runner.run(
                    [tool.proc_path, "mousedown", "1", "sleep", "0.01", "mouseup", "1"],
                    pass_fds=(descriptor,),
                )
            assert len(launches) == 1
            assert runner.child_pid == 404
            assert runner.cleanup_failed
            assert runner.possible_down == frozenset({"button:1"})
            with pytest.raises(ActError):
                with GuiLease(lease.path, timeout=0.1).hold():
                    pass
            runner.release_only()
            assert len(launches) == 1
            with pytest.raises(ControlChildError, match="could not be reaped"):
                runner.close()
            assert len(launches) == 1
            assert runner.child_pid == 404
            assert runner.cleanup_failed
            assert lease.held
    finally:
        os.close(descriptor)


def test_successful_later_reap_clears_cleanup_failure_without_release_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_dir = tmp_path / "private"
    private_dir.mkdir(mode=0o700)
    lease = GuiLease(private_dir / "gui.lock", timeout=0.1)
    descriptor = os.open("/dev/null", os.O_RDONLY)
    tool = SimpleNamespace(
        image=SimpleNamespace(fd=descriptor),
        proc_path="/pinned/xdotool",
        digest="0" * 64,
    )
    launches: list[list[str]] = []
    stop_checks = 0
    can_reap = [False]

    class DelayedWindowActivation:
        pid = 606

        def poll(self):
            return None

        def terminate(self) -> None:
            pass

        def wait(self, *, timeout: float):
            if not can_reap[0]:
                raise subprocess.TimeoutExpired("fake-activation", timeout)
            return 0

        def communicate(self, *, timeout: float):
            return "", ""

    child = DelayedWindowActivation()

    def stopping() -> bool:
        nonlocal stop_checks
        stop_checks += 1
        return stop_checks >= 4

    def fake_popen(command, **_kwargs):
        launches.append(list(command))
        return child

    monkeypatch.setattr(control_child.os, "kill", lambda _pid, _signum: None)
    try:
        with lease.hold():
            runner = ControlChildRunner(
                lease,
                tool,  # type: ignore[arg-type]
                environment=_session_environment(tmp_path),
                stopping=stopping,
                guard=lambda: None,
                popen=fake_popen,
                sleep=lambda _duration: None,
            )
            with pytest.raises(ControlChildError, match="could not be reaped"):
                runner.run(
                    [tool.proc_path, "windowactivate", "--sync", "42"],
                    pass_fds=(descriptor,),
                )
            assert len(launches) == 1
            assert runner.child_pid == 606
            assert runner.cleanup_failed
            assert runner.possible_down == frozenset()
            can_reap[0] = True
            runner.close()
            assert len(launches) == 1
            assert runner.child_pid is None
            assert not runner.cleanup_failed
            assert runner._closed
            assert lease.held
    finally:
        os.close(descriptor)


def test_guarded_identity_change_prevents_input_child_from_starting(tmp_path: Path) -> None:
    private_dir = tmp_path / "private"
    private_dir.mkdir(mode=0o700)
    lease = GuiLease(private_dir / "gui.lock")
    descriptor = os.open("/dev/null", os.O_RDONLY)
    tool = SimpleNamespace(
        image=SimpleNamespace(fd=descriptor),
        proc_path="/pinned/xdotool",
        digest="0" * 64,
    )
    launched = []
    try:
        with lease.hold():
            runner = ControlChildRunner(
                lease,
                tool,  # type: ignore[arg-type]
                environment=_session_environment(tmp_path),
                stopping=lambda: False,
                guard=lambda: (_ for _ in ()).throw(ControlChildError("fresh identity changed")),
                popen=lambda command, **_kwargs: launched.append(command),
            )
            with pytest.raises(ControlChildError, match="identity changed"):
                runner.run([tool.proc_path, "keydown", "38", "keyup", "38"], pass_fds=(descriptor,))
            assert not launched
            assert runner.possible_down == frozenset()
            runner.close()
    finally:
        os.close(descriptor)


def test_failed_new_guard_releases_only_a_previously_recorded_key(tmp_path: Path) -> None:
    private_dir = tmp_path / "private"
    private_dir.mkdir(mode=0o700)
    lease = GuiLease(private_dir / "gui.lock")
    descriptor = os.open("/dev/null", os.O_RDONLY)
    tool = SimpleNamespace(
        image=SimpleNamespace(fd=descriptor),
        proc_path="/pinned/xdotool",
        digest="0" * 64,
    )
    launched: list[list[str]] = []

    class CompletedChild:
        pid = 505
        returncode = 0

        def poll(self):
            return self.returncode

        def communicate(self, *, timeout: float):
            return "", ""

    def fake_popen(command, **_kwargs):
        launched.append(list(command))
        return CompletedChild()

    def refuse_new_action() -> None:
        raise ControlChildError("fresh ownership changed")

    try:
        with lease.hold():
            runner = ControlChildRunner(
                lease,
                tool,  # type: ignore[arg-type]
                environment=_session_environment(tmp_path),
                stopping=lambda: False,
                guard=refuse_new_action,
                popen=fake_popen,
            )
            runner._possible_down.add("key:37")
            with pytest.raises(ControlChildError, match="ownership changed"):
                runner.run(
                    [tool.proc_path, "keydown", "038", "keyup", "038"],
                    pass_fds=(descriptor,),
                )
            assert len(launched) == 1
            assert launched[0][-2:] == ["keyup", "37"]
            assert "keydown" not in launched[0]
            assert runner.possible_down == frozenset()
            runner.close()
    finally:
        os.close(descriptor)


def test_stop_change_during_environment_refresh_prevents_emitter_but_allows_key_release(
    tmp_path: Path,
) -> None:
    private_dir = tmp_path / "private"
    private_dir.mkdir(mode=0o700)
    lease = GuiLease(private_dir / "gui.lock")
    descriptor = os.open("/dev/null", os.O_RDONLY)
    tool = SimpleNamespace(
        image=SimpleNamespace(fd=descriptor),
        proc_path="/pinned/xdotool",
        digest="0" * 64,
    )
    stopped = [False]
    launched: list[list[str]] = []

    class CompletedChild:
        pid = 501
        returncode = 0

        def poll(self):
            return self.returncode

        def communicate(self, *, timeout: float):
            return "", ""

    def environment():
        stopped[0] = True
        return _session_environment(tmp_path)

    def fake_popen(command, **_kwargs):
        launched.append(list(command))
        return CompletedChild()

    try:
        with lease.hold():
            runner = ControlChildRunner(
                lease,
                tool,  # type: ignore[arg-type]
                environment=environment,
                stopping=lambda: stopped[0],
                guard=lambda: None,
                popen=fake_popen,
                sleep=lambda _duration: None,
            )
            runner._possible_down.add("key:37")
            with pytest.raises(ControlChildCancelled):
                runner.run(
                    [tool.proc_path, "keydown", "38", "keyup", "38"],
                    pass_fds=(descriptor,),
                )
            assert len(launched) == 1
            assert launched[0][-2:] == ["keyup", "37"]
            assert runner.possible_down == frozenset()
            runner.close()
            assert len(launched) == 1
            assert launched[0][-2:] == ["keyup", "37"]
            assert runner.possible_down == frozenset()
    finally:
        os.close(descriptor)


def test_stop_change_inside_final_admission_guard_prevents_spawn_and_releases_key(
    tmp_path: Path,
) -> None:
    private_dir = tmp_path / "private"
    private_dir.mkdir(mode=0o700)
    lease = GuiLease(private_dir / "gui.lock")
    descriptor = os.open("/dev/null", os.O_RDONLY)
    tool = SimpleNamespace(
        image=SimpleNamespace(fd=descriptor),
        proc_path="/pinned/xdotool",
        digest="0" * 64,
    )
    stopped = [False]
    guard_checks = 0
    launched: list[list[str]] = []

    class CompletedChild:
        pid = 502
        returncode = 0

        def poll(self):
            return self.returncode

        def communicate(self, *, timeout: float):
            return "", ""

    def guard():
        nonlocal guard_checks
        guard_checks += 1
        if guard_checks == 1:
            stopped[0] = True

    def fake_popen(command, **_kwargs):
        launched.append(list(command))
        return CompletedChild()

    try:
        with lease.hold():
            runner = ControlChildRunner(
                lease,
                tool,  # type: ignore[arg-type]
                environment=_session_environment(tmp_path),
                stopping=lambda: stopped[0],
                guard=guard,
                popen=fake_popen,
                sleep=lambda _duration: None,
            )
            with pytest.raises(ControlChildCancelled):
                runner.run(
                    [tool.proc_path, "keydown", "38", "keyup", "38"],
                    pass_fds=(descriptor,),
                )
            assert guard_checks == 1
            assert launched == []
            assert runner.possible_down == frozenset()
            runner.close()
            assert launched == []
    finally:
        os.close(descriptor)


def test_one_fresh_full_guard_runs_after_environment_resolution_for_each_control(
    tmp_path: Path,
) -> None:
    private_dir = tmp_path / "private"
    private_dir.mkdir(mode=0o700)
    lease = GuiLease(private_dir / "gui.lock")
    descriptor = os.open("/dev/null", os.O_RDONLY)
    tool = SimpleNamespace(
        image=SimpleNamespace(fd=descriptor),
        proc_path="/pinned/xdotool",
        digest="0" * 64,
    )
    events: list[str] = []

    class CompletedChild:
        pid = 503
        returncode = 0

        def poll(self):
            return self.returncode

        def communicate(self, *, timeout: float):
            return "X=1\nY=2\nSCREEN=0\nWINDOW=42\n", ""

    def fake_popen(_command, **_kwargs):
        events.append("popen")
        return CompletedChild()

    try:
        with lease.hold():
            runner = ControlChildRunner(
                lease,
                tool,  # type: ignore[arg-type]
                environment=lambda: events.append("environment") or _session_environment(tmp_path),
                stopping=lambda: False,
                guard=lambda: events.append("guard"),
                popen=fake_popen,
            )
            runner.run([tool.proc_path, "getmouselocation", "--shell"], pass_fds=(descriptor,))
            assert events == ["environment", "guard", "popen"]
            runner.close()
    finally:
        os.close(descriptor)


def test_single_character_helper_runs_exactly_one_fresh_full_admission_guard(tmp_path: Path) -> None:
    private_dir = tmp_path / "private"
    private_dir.mkdir(mode=0o700)
    lease = GuiLease(private_dir / "gui.lock")
    descriptor = os.open("/dev/null", os.O_RDONLY)
    tool = SimpleNamespace(
        image=SimpleNamespace(fd=descriptor),
        proc_path="/pinned/xdotool",
        digest="0" * 64,
    )
    events: list[str] = []

    class CompletedChild:
        pid = 504
        returncode = 0

        def poll(self):
            return self.returncode

        def communicate(self, *, timeout: float):
            return "", ""

    def fake_popen(command, **_kwargs):
        events.append("popen")
        assert command[-4:] == ["keydown", "038", "keyup", "038"]
        return CompletedChild()

    def query_run(*_args, **_kwargs):
        events.append("query")
        return subprocess.CompletedProcess(["xmodmap", "-pke"], 0, "keycode 38 = a A\n", "")

    try:
        with lease.hold():
            runner = ControlChildRunner(
                lease,
                tool,  # type: ignore[arg-type]
                environment=lambda: events.append("environment") or _session_environment(tmp_path),
                stopping=lambda: False,
                guard=lambda: events.append("guard"),
                popen=fake_popen,
            )
            keys = XtestKeys(tool, _session_environment(tmp_path), run=runner.run, query_run=query_run)  # type: ignore[arg-type]
            keys.type_text("a")
            assert events == ["query", "environment", "guard", "popen"]
            runner.close()
    finally:
        os.close(descriptor)


def test_parent_death_kills_exact_delayed_control_and_releases_lease(tmp_path: Path) -> None:
    compiler = subprocess.run(["cc", "--version"], capture_output=True, check=False)
    if compiler.returncode != 0:
        pytest.skip("a C compiler is required for the inert delayed-control fixture")
    fake_source = tmp_path / "delayed.c"
    fake_binary = tmp_path / "wmctrl"
    fake_source.write_text(
        textwrap.dedent(
            """
            #include <stdio.h>
            #include <stdlib.h>
            #include <unistd.h>
            int main(void) {
                const char *path = getenv("CONTROL_CHILD_STARTED");
                FILE *stream = fopen(path, "w");
                if (stream == NULL) return 9;
                fprintf(stream, "%d\\n", getpid());
                fclose(stream);
                sleep(60);
                return 0;
            }
            """
        ),
        encoding="utf-8",
    )
    subprocess.run(["cc", str(fake_source), "-o", str(fake_binary)], check=True)
    fake_binary.chmod(0o755)

    private_dir = tmp_path / "private"
    private_dir.mkdir(mode=0o700)
    lock_path = private_dir / "gui.lock"
    marker = tmp_path / "child.pid"
    helper = textwrap.dedent(
        """
        import os
        import sys
        from pathlib import Path
        from types import SimpleNamespace
        from crowley_automation.desktop_input import GuiLease
        from crowley_automation.gui.control_child import ControlChildRunner, PinnedControlTool

        lock_path, executable, marker = map(Path, sys.argv[1:])
        descriptor = os.open("/dev/null", os.O_RDONLY)
        image = SimpleNamespace(fd=descriptor)
        input_tool = SimpleNamespace(image=image, proc_path="/unused/xdotool", digest="0" * 64)
        lease = GuiLease(lock_path)
        environment = {"DISPLAY": ":99", "XAUTHORITY": str(lock_path.parent / "Xauthority"),
                       "CONTROL_CHILD_STARTED": str(marker)}
        with lease.hold():
            pinned = PinnedControlTool.load("wmctrl", executable)
            runner = ControlChildRunner(lease, input_tool, environment=environment,
                stopping=lambda: False, guard=lambda: None,
                extra_tools={"wmctrl": pinned}, window_xid=42)
            runner.run([pinned.proc_path, "-i", "-r", "42", "-b", "add,fullscreen"],
                timeout=30, pass_fds=(pinned.fd,))
        """
    )
    child_parent = subprocess.Popen(
        [sys.executable, "-c", helper, str(lock_path), str(fake_binary), str(marker)],
        env=os.environ.copy(),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    control_pid: int | None = None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and not marker.exists() and child_parent.poll() is None:
        time.sleep(0.025)
    if marker.exists():
        control_pid = int(marker.read_text(encoding="ascii").strip())
    if control_pid is None:
        detail = child_parent.communicate(timeout=1)[1]
        pytest.fail(f"control helper did not start: {detail}")

    os.kill(child_parent.pid, signal.SIGKILL)
    child_parent.wait(timeout=2)
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            state = Path(f"/proc/{control_pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
        except OSError:
            break
        if state == "Z":
            break
        time.sleep(0.025)
    else:
        pytest.fail("the delayed exact control process survived its parent")

    with GuiLease(lock_path, timeout=0.2).hold():
        pass
