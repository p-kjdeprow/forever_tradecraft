from __future__ import annotations

import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from crowley.gui.keyboard import KeyStep
from crowley.scan import act
from crowley.scan.policy import SET_BINDING_COMMAND, Action, Decision, LoopState, Target

TARGET = Target("Arthur", "R", "Alliance", "h", "p" * 64, "1", 1, "0.2.0", "Auctioneer Jaxon")
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


def keys(runner: Runner) -> act.XtestKeys:
    tool = SimpleNamespace(proc_path="/proc/self/fd/9", image=SimpleNamespace(fd=9))
    return act.XtestKeys(tool, {"DISPLAY": ":0"}, run=runner)  # type: ignore[arg-type]


def sent(runner: Runner) -> list[list[str]]:
    return [c for c in runner.calls if c[0] != "xmodmap"]


def test_parse_keymap_takes_level_zero_and_first_keycode() -> None:
    keymap = act.parse_keymap(XMODMAP + "keycode 99 = Return\n")
    assert keymap["Return"] == 36 and keymap["Shift_L"] == 50 and keymap["slash"] == 61
    with pytest.raises(act.ActError):
        act.parse_keymap("")


def test_press_and_chord_argv() -> None:
    runner = Runner()
    sender = keys(runner)
    sender.press("Return")
    sender.chord(act.INTERACT_CHORD)
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
    with pytest.raises(act.ActError):
        keys(runner).chord(act.INTERACT_CHORD)
    releases = [c for c in runner.calls if c[1:2] == ["keyup"] and len(c) == 3]
    assert {c[2] for c in releases} == {"037", "096"}


def test_gui_lease_is_exclusive_private_and_released(tmp_path: Path) -> None:
    path = tmp_path / "run" / "gui.lock"
    first, second = act.GuiLease(path, timeout=0.2), act.GuiLease(path, timeout=0.2)
    with first.hold():
        assert first.held
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        with pytest.raises(act.ActError, match="held elsewhere"), second.hold():
            pass
    assert not first.held
    with second.hold():
        pass


def test_gui_lease_rejects_a_non_private_directory(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir(mode=0o755)
    (tmp_path / "d").chmod(0o755)
    with pytest.raises(act.ActError, match="not private"), act.GuiLease(tmp_path / "d" / "gui.lock").hold():
        pass


class RecordingKeys:
    def __init__(self, lease: act.GuiLease) -> None:
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
    lease = act.GuiLease(tmp_path / "gui.lock", timeout=0.2)
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
        (Action.PRESS_INTERACT, ("chord", act.INTERACT_CHORD)),
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
    with pytest.raises(act.ActError, match="allowlisted"):
        runner.execute(decision(Action.TYPE_COMMAND, command="/logout"), 77)


def test_activate_window_uses_the_observed_window(tmp_path: Path) -> None:
    runner, recording, order = actor(tmp_path)
    runner.execute(decision(Action.ACTIVATE_WINDOW), 4242)
    assert order == ["activate:4242"] and recording.events == []
    with pytest.raises(act.ActError, match="no game window"):
        runner.execute(decision(Action.ACTIVATE_WINDOW), None)


def test_presence_is_rechecked_under_the_lease(tmp_path: Path) -> None:
    runner, recording, order = actor(tmp_path, active=True)
    assert runner.execute(decision(Action.PRESS_RETURN), 77) == act.SKIPPED_HUMAN
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
    assert runner.execute(decision(Action.RELAUNCH), None) == "relaunch_started"
    assert order == ["terminate:[5, 6]", "launcher"]
    assert recording.events == [] and not runner._lease.held
    assert runner.launcher_running


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

    run = act.LauncherRun(which=lambda name: f"/bin/{name}", popen=popen, clock=lambda: now[0], timeout=600)
    run.start()
    assert started == [["/bin/crowley-world-of-warcraft"]] and run.running
    with pytest.raises(act.ActError, match="already running"):
        run.start()
    assert run.poll() == (False, "running")
    killed: list[int] = []
    monkeypatch.setattr(act.os, "killpg", lambda pid, sig: killed.append(pid))
    now[0] = 601.0
    assert run.poll() == (True, "timeout") and killed == [4321] and not run.running
    child.code = 0
    run.start()
    assert run.poll() == (True, "exit=0") and not run.running


def test_launcher_must_be_on_path() -> None:
    with pytest.raises(act.ActError, match="not on PATH"):
        act.LauncherRun(which=lambda name: None).start()


def test_activate_window_runs_xdotool_windowactivate_sync() -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    act.activate_window({"DISPLAY": ":0"}, 55, run=run)
    assert calls == [["xdotool", "windowactivate", "--sync", "55"]]
    with pytest.raises(act.ActError):
        act.activate_window({}, 55, run=lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", ""))


def test_terminate_failure_is_an_action_fault_not_a_crash(tmp_path: Path) -> None:
    runner, _recording, order = actor(tmp_path)

    def stuck(pids: Any) -> int:
        raise RuntimeError("owned game process survived SIGKILL")

    runner._terminate = stuck
    with pytest.raises(act.ActError, match="could not be terminated"):
        runner.execute(decision(Action.RELAUNCH), None)
    assert order == []  # the launcher was never started
