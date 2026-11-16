from __future__ import annotations

import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from crowley_automation import desktop_input
from crowley_automation.gui.keyboard import KeyStep
from crowley_host.scan import act
from crowley_host.scan.policy import SET_BINDING_COMMAND, Action, Decision, LoopState, Target

TARGET = Target("Arthur", "R", "Alliance", "h", "p" * 64, "1", 1, "0.2.0", "0.3.0", "Auctioneer Jaxon")
XMODMAP = """\
keycode   9 = Escape NoSymbol Escape
keycode  36 = Return NoSymbol Return
keycode  37 = Control_L NoSymbol Control_L
keycode  50 = Shift_L NoSymbol Shift_L
keycode  96 = F12 F12
keycode  61 = slash question slash
keycode  27 = r R r
keycode  26 = e E e
keycode  46 = l L l
keycode  32 = o O o
keycode  38 = a A a
keycode  40 = d D d
keycode  62 = Shift_R NoSymbol Shift_R
"""


def decision(action: Action, **kw: Any) -> Decision:
    return Decision(action, LoopState(), **kw)


class Runner:
    def __init__(self, fail_on: str = "") -> None:
        self.calls: list[list[str]] = []
        self.fail_on = fail_on

    def __call__(self, argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if argv[0] == "xmodmap":
            return subprocess.CompletedProcess(argv, 0, XMODMAP, "")
        code = 1 if self.fail_on and self.fail_on in argv else 0
        assert kw["pass_fds"] == (9,) or argv[0] == "xdotool"
        return subprocess.CompletedProcess(argv, code, "", "")


def keys(runner: Runner) -> desktop_input.XtestKeys:
    tool = SimpleNamespace(proc_path="/proc/self/fd/9", image=SimpleNamespace(fd=9))
    return desktop_input.XtestKeys(tool, {"DISPLAY": ":0"}, run=runner)  # type: ignore[arg-type]


def sent(runner: Runner) -> list[list[str]]:
    return [c for c in runner.calls if c[0] != "xmodmap"]


def test_parse_keymap_takes_level_zero_and_first_keycode() -> None:
    keymap = desktop_input.parse_keymap(XMODMAP + "keycode 99 = Return\n")
    assert keymap["Return"] == 36 and keymap["Shift_L"] == 50 and keymap["slash"] == 61
    with pytest.raises(desktop_input.ActError):
        desktop_input.parse_keymap("")


def test_press_and_chord_argv() -> None:
    runner = Runner()
    sender = keys(runner)
    sender.press("Return")
    sender.chord(desktop_input.INTERACT_CHORD)
    assert sent(runner) == [
        ["/proc/self/fd/9", "keydown", "036", "keyup", "036"],
        ["/proc/self/fd/9", "keydown", "037", "keydown", "096", "keyup", "096", "keyup", "037"],
    ]


def test_type_text_is_one_action_per_character_with_shift_nested() -> None:
    runner = Runner()
    keys(runner).type_text("/Ad")
    assert [c[1:] for c in sent(runner)] == [
        ["keydown", "061", "keyup", "061"],
        ["keydown", "050", "keydown", "038", "keyup", "038", "keyup", "050"],
        ["keydown", "040", "keyup", "040"],
    ]


def test_failed_transition_releases_held_keys() -> None:
    runner = Runner(fail_on="keydown")
    with pytest.raises(desktop_input.ActError):
        keys(runner).chord(desktop_input.INTERACT_CHORD)
    releases = [c for c in runner.calls if c[1:2] == ["keyup"] and len(c) == 3]
    assert {c[2] for c in releases} == {"037", "096"}


def test_gui_lease_is_exclusive_private_and_released(tmp_path: Path) -> None:
    path = tmp_path / "run" / "gui.lock"
    first, second = desktop_input.GuiLease(path, timeout=0.2), desktop_input.GuiLease(path, timeout=0.2)
    with first.hold():
        assert first.held
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        with pytest.raises(desktop_input.ActError, match="held elsewhere"), second.hold():
            pass
    assert not first.held
    with second.hold():
        pass


def test_inherited_gui_lease_keeps_parent_lock_held(tmp_path: Path) -> None:
    path = tmp_path / "run" / "gui.lock"
    parent = desktop_input.GuiLease(path)
    contender = desktop_input.GuiLease(path, timeout=0.05)
    with parent.hold():
        assert parent.fd is not None
        script = """
from pathlib import Path
import sys
from crowley_automation.desktop_input import GuiLease
lease = GuiLease.from_inherited_fd(Path(sys.argv[1]), int(sys.argv[2]))
with lease.hold():
    assert lease.held
"""
        child = subprocess.run(
            [sys.executable, "-c", script, str(path), str(parent.fd)],
            pass_fds=(parent.fd,),
            capture_output=True,
            text=True,
            check=False,
        )
        assert child.returncode == 0, child.stderr
        with pytest.raises(desktop_input.ActError, match="held elsewhere"), contender.hold():
            pass


def test_gui_lease_rejects_a_non_private_directory(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir(mode=0o755)
    (tmp_path / "d").chmod(0o755)
    with (
        pytest.raises(desktop_input.ActError, match="not private"),
        desktop_input.GuiLease(tmp_path / "d" / "gui.lock").hold(),
    ):
        pass


class RecordingKeys:
    def __init__(self, lease: desktop_input.GuiLease) -> None:
        self.lease = lease
        self.events: list[tuple[str, Any]] = []
        self.held_during: list[bool] = []

    def press(self, key: str) -> None:
        self.events.append(("press", key))
        self.held_during.append(self.lease.held)

    def chord(self, steps: tuple[KeyStep, ...]) -> None:
        self.events.append(("chord", steps))
        self.held_during.append(self.lease.held)

    def type_text(self, text: str) -> None:
        self.events.append(("type", text))
        self.held_during.append(self.lease.held)


class Presence:
    def __init__(self, active: bool = False) -> None:
        self.active = active

    def human_active(self) -> tuple[bool, str]:
        return self.active, "r"


class Launcher:
    def __init__(self, order: list[str]) -> None:
        self.order, self.running = order, False

    def start(self) -> None:
        self.order.append("launcher")
        self.running = True

    def poll(self) -> tuple[bool, str]:
        return True, "exit=0"


def actor(tmp_path: Path, *, active: bool = False) -> tuple[act.Actor, RecordingKeys, list[str]]:
    order: list[str] = []
    lease = desktop_input.GuiLease(tmp_path / "gui.lock", timeout=0.2)
    recording = RecordingKeys(lease)

    def terminate(pids: Any) -> int:
        order.append(f"terminate:{list(pids)}")
        return len(list(pids))

    result = act.Actor(
        keys=recording,
        activate=lambda xid: order.append(f"activate:{xid}"),
        lease=lease,
        presence=Presence(active),
        target=TARGET,
        matcher=SimpleNamespace(pids=lambda: [5, 6]),  # type: ignore[arg-type]
        launcher=Launcher(order),  # type: ignore[arg-type]
        terminate=terminate,
    )
    return result, recording, order


@pytest.mark.parametrize(
    ("action", "event"),
    [
        (Action.OPEN_CHAT, ("press", "Return")),
        (Action.PRESS_RETURN, ("press", "Return")),
        (Action.PRESS_ESCAPE, ("press", "Escape")),
        (Action.CLOSE_AH, ("press", "Escape")),
        (Action.PRESS_INTERACT, ("chord", desktop_input.INTERACT_CHORD)),
    ],
)
def test_input_actions_send_keys_under_the_lease(tmp_path: Path, action: Action, event: object) -> None:
    runner, recording, _order = actor(tmp_path)
    assert runner.execute(decision(action), 77) == str(action)
    assert recording.events == [event] and recording.held_during == [True]


def test_type_command_is_allowlisted(tmp_path: Path) -> None:
    runner, recording, _ = actor(tmp_path)
    runner.execute(decision(Action.TYPE_COMMAND, command=SET_BINDING_COMMAND), 77)
    runner.execute(decision(Action.TYPE_COMMAND, command="/targetexact Auctioneer Jaxon"), 77)
    runner.execute(decision(Action.TYPE_COMMAND, command="/reload"), 77)
    assert [e[1] for e in recording.events][-1] == "/reload"
    with pytest.raises(desktop_input.ActError, match="allowlisted"):
        runner.execute(decision(Action.TYPE_COMMAND, command="/logout"), 77)


def test_activate_window_uses_the_observed_window(tmp_path: Path) -> None:
    runner, recording, order = actor(tmp_path)
    runner.execute(decision(Action.ACTIVATE_WINDOW), 4242)
    assert order == ["activate:4242"] and recording.events == []
    with pytest.raises(desktop_input.ActError, match="no game window"):
        runner.execute(decision(Action.ACTIVATE_WINDOW), None)


def test_presence_is_rechecked_under_the_lease(tmp_path: Path) -> None:
    runner, recording, order = actor(tmp_path, active=True)
    assert runner.execute(decision(Action.PRESS_RETURN), 77) == desktop_input.SKIPPED_HUMAN
    assert recording.events == [] and order == []


@pytest.mark.parametrize("action", [Action.WAIT, Action.HOLD_HUMAN, Action.FAULT])
def test_passive_actions_do_nothing(tmp_path: Path, action: Action) -> None:
    runner, recording, order = actor(tmp_path)
    assert runner.execute(decision(action), 77) == "none"
    assert recording.events == [] and order == []


def test_relaunch_terminates_the_owned_pids_then_starts_the_launcher_without_the_lease(
    tmp_path: Path,
) -> None:
    runner, recording, order = actor(tmp_path)
    terminate = runner._terminate

    def verify_lease_ownership(pids: Any) -> int:
        assert runner._lease.held
        return terminate(pids)

    runner._terminate = verify_lease_ownership
    start_launcher = runner.launcher.start

    def verify_launcher_starts_after_release() -> None:
        assert not runner._lease.held
        start_launcher()

    runner.launcher.start = verify_launcher_starts_after_release
    assert runner.execute(decision(Action.RELAUNCH), None) == "relaunch_started"
    assert order == ["terminate:[5, 6]", "launcher"]
    assert recording.events == [] and not runner._lease.held
    assert runner.launcher_running


def test_relaunch_skips_termination_when_a_human_is_active(tmp_path: Path) -> None:
    runner, recording, order = actor(tmp_path, active=True)
    assert runner.execute(decision(Action.RELAUNCH), None) == desktop_input.SKIPPED_HUMAN
    assert order == []
    assert recording.events == [] and not runner._lease.held


def test_relaunch_cannot_terminate_during_another_gui_lease(tmp_path: Path) -> None:
    runner, recording, order = actor(tmp_path)
    with runner._lease.hold():
        with pytest.raises(desktop_input.ActError, match="GUI lease is held elsewhere"):
            runner.execute(decision(Action.RELAUNCH), None)
    assert order == [] and recording.events == []
    assert not runner.launcher_running and not runner._lease.held


@pytest.mark.parametrize("action", [Action.RELAUNCH, Action.ACTIVATE_WINDOW, Action.PRESS_RETURN])
def test_maintenance_guard_rechecks_inside_gui_lease(tmp_path: Path, action: Action) -> None:
    runner, recording, order = actor(tmp_path)
    checked: list[bool] = []

    def blocker() -> str:
        checked.append(runner._lease.held)
        return "automation_running"

    runner._maintenance_blocker = blocker
    with pytest.raises(act.MaintenanceBlocked, match="automation_running"):
        runner.execute(decision(action), 77)
    assert checked == [True]
    assert order == [] and recording.events == []
    assert not runner._lease.held and not runner.launcher_running


class Child:
    pid = 4321

    def __init__(self, code: int | None = None) -> None:
        self.code, self.killed = code, False

    def poll(self) -> int | None:
        return self.code

    def kill(self) -> None:
        self.killed = True

    def wait(self) -> int:
        return -9


def test_launcher_run_lifecycle_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [0.0]
    child = Child()
    started: list[list[str]] = []

    def popen(argv: list[str], **kw: Any) -> Child:
        started.append(argv)
        assert kw["start_new_session"] is True
        return child

    run = desktop_input.LauncherRun(
        which=lambda name: f"/bin/{name}", popen=popen, clock=lambda: now[0], timeout=600
    )
    run.start()
    assert started == [["/bin/crowley-world-of-warcraft"]] and run.running
    with pytest.raises(desktop_input.ActError, match="already running"):
        run.start()
    assert run.poll() == (False, "running")
    killed: list[int] = []
    monkeypatch.setattr(desktop_input.os, "killpg", lambda pid, sig: killed.append(pid))
    now[0] = 601.0
    assert run.poll() == (True, "timeout") and killed == [4321] and not run.running
    child.code = 0
    run.start()
    assert run.poll() == (True, "exit=0") and not run.running


def test_launcher_must_be_on_path() -> None:
    with pytest.raises(desktop_input.ActError, match="not on PATH"):
        desktop_input.LauncherRun(which=lambda name: None).start()


def test_activate_window_runs_xdotool_windowactivate_sync() -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    desktop_input.activate_window({"DISPLAY": ":0"}, 55, run=run)
    assert calls == [["xdotool", "windowactivate", "--sync", "55"]]
    with pytest.raises(desktop_input.ActError):
        desktop_input.activate_window(
            {}, 55, run=lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "")
        )


def test_terminate_failure_is_an_action_fault_not_a_crash(tmp_path: Path) -> None:
    runner, _recording, order = actor(tmp_path)

    def stuck(pids: Any) -> int:
        raise RuntimeError("owned game process survived SIGKILL")

    runner._terminate = stuck
    with pytest.raises(desktop_input.ActError, match="could not be terminated"):
        runner.execute(decision(Action.RELAUNCH), None)
    assert order == []  # the launcher was never started
