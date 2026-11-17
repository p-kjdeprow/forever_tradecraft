from __future__ import annotations

import stat
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from crowley_automation import desktop_input
from crowley_automation.gui.control_child import ControlChildCancelled, ControlChildError
from crowley_automation.gui.keyboard import KeyStep
from crowley_automation.gui.process import ProcessIdentity
from crowley_host.scan import act
from crowley_host.scan.observe import to_snapshot
from crowley_host.scan.policy import SET_BINDING_COMMAND, Action, Decision, LoopState, Observation, Target

from tests.support.scan_frames import automation_snapshot, snapshot

TARGET = Target(
    "Arthur", "R", "Alliance", "h", "p" * 64, "70450", 16001, "0.2.0", "0.3.0", "Auctioneer Jaxon", 1
)
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


def keys(runner: Runner, *, before_emit: Any = None) -> desktop_input.XtestKeys:
    tool = SimpleNamespace(proc_path="/proc/self/fd/9", image=SimpleNamespace(fd=9))
    return desktop_input.XtestKeys(
        tool, {"DISPLAY": ":0"}, run=runner, query_run=runner, before_emit=before_emit
    )  # type: ignore[arg-type]


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


def test_type_text_is_one_explicit_transition_chain_with_shift_nested() -> None:
    runner = Runner()
    keys(runner).type_text("/Ad")
    assert [c[1:] for c in sent(runner)] == [
        [
            "keydown",
            "061",
            "keyup",
            "061",
            "keydown",
            "050",
            "keydown",
            "038",
            "keyup",
            "038",
            "keyup",
            "050",
            "keydown",
            "040",
            "keyup",
            "040",
        ]
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


IDENTITY = ProcessIdentity(5, 100, 1000, "C:\\Wow.exe", "/prefix")


class Session:
    fingerprint = "session-1"
    environment = {"DISPLAY": ":0"}

    def current(self) -> Any:
        return self

    def refresh(self, session: Any) -> None:
        self.fingerprint = session.fingerprint
        self.environment = dict(session.environment)


class Matcher:
    def __init__(self) -> None:
        self.current = (IDENTITY,)

    def identities(self) -> tuple[ProcessIdentity, ...]:
        return self.current

    def still_matches(self, identity: ProcessIdentity) -> bool:
        return identity in self.current


class Window:
    def __init__(self) -> None:
        self.current = SimpleNamespace(ok=True, fullscreen=True, xid=77, focused=True, foreign_count=0)
        self.calls: list[list[int]] = []

    def observe(self, pids: list[int]) -> Any:
        self.calls.append(pids)
        return self.current

    def focus_state(self, xid: int, pid: int) -> tuple[bool, bool]:
        return self.current.ok and self.current.xid == xid, self.current.focused


class Recovery:
    def __init__(self, action: str = "wm_close_dispatched") -> None:
        self.action = action
        self.requests: list[tuple[ProcessIdentity, str]] = []

    def request_restart(self, identity: ProcessIdentity, reason: str) -> Any:
        self.requests.append((identity, reason))
        return SimpleNamespace(action=self.action, reason="waiting_for_exit", outcome="waiting")


class FreshScan:
    def __init__(self) -> None:
        self.actor: act.Actor | None = None
        self.current: Observation | None = None
        self.observations: list[Observation] = []
        self.decode_error = ""

    def observe(self, state: LoopState) -> Observation:
        if self.observations:
            current = self.observations.pop(0) if len(self.observations) > 1 else self.observations[0]
            return replace(current, state=state)
        current = self.current
        if current is None and self.actor is not None:
            current = self.actor._observation
        if current is None:
            raise AssertionError("scan observation requested before action admission")
        return replace(current, state=state)


def ready_automation(
    *,
    chat_active: bool = False,
    chat_focused: bool = False,
    chat_text: str = "",
    ah_open: bool = True,
    zone: str = "Orgrimmar",
    target_present: bool = False,
    target_name: str = "",
    range_available: bool = False,
    in_range: bool = False,
    binding_available: bool = True,
    binding_action: str = "",
    interaction_mode: str = "any",
    busy: bool = False,
) -> dict[str, Any]:
    automation = automation_snapshot(1)
    automation["context"]["realm"] = TARGET.realm
    automation["context"]["zone"] = zone
    automation["ahOpen"] = ah_open
    automation["chatEdit"] = {"active": chat_active, "focused": chat_focused, "text": chat_text}
    automation["auctioneer"].update(
        targetPresent=target_present,
        targetName=target_name,
        rangeAvailable=range_available,
        withinTradeRange=in_range,
        bindingAvailable=binding_available,
        ctrlF12Action=binding_action,
        interactionMode=interaction_mode,
    )
    if busy:
        automation["operation"]["batchId"] = "active-batch"
    return automation


def ready_observation(
    *,
    busy: bool = False,
    chat_active: bool = False,
    chat_focused: bool = False,
    chat_text: str = "",
    ah_open: bool = True,
    zone: str = "Orgrimmar",
    target_present: bool = False,
    target_name: str = "",
    range_available: bool = False,
    in_range: bool = False,
    binding_available: bool = True,
    binding_action: str = "",
    interaction_mode: str = "any",
) -> Observation:
    scanner = snapshot(1)
    scanner["house"]["key"] = TARGET.house_key
    scanner["plan"]["hash"] = TARGET.plan_hash
    automation = ready_automation(
        chat_active=chat_active,
        chat_focused=chat_focused,
        chat_text=chat_text,
        ah_open=ah_open,
        zone=zone,
        target_present=target_present,
        target_name=target_name,
        range_available=range_available,
        in_range=in_range,
        binding_available=binding_available,
        binding_action=binding_action,
        interaction_mode=interaction_mode,
    )
    snap = to_snapshot(scanner, automation, 1000.0)
    return Observation(
        wall=1000.0,
        mono=500.0,
        process_alive=True,
        process_started_at=900.0,
        window_ok=True,
        window_focused=True,
        snapshot=snap,
        last_frame_at=1000.0,
        frames_seen_this_process=3,
        disk_saved_ids=frozenset(),
        uploaded_ids=frozenset(),
        permanently_rejected_ids=frozenset(),
        data_acked_ids=frozenset(),
        companion_last_error="",
        human_active=False,
        human_reason="idle",
        target=TARGET,
        native_automation_busy=busy,
        process_identity=IDENTITY,
        window_xid=77,
        window_fullscreen=True,
        session_fingerprint="session-1",
        native_ready=True,
        native_boot=snap.boot,
    )


def actor(
    tmp_path: Path,
    *,
    active: bool = False,
    stopping: Any = lambda: False,
    control_runner: Any = None,
) -> tuple[act.Actor, RecordingKeys, list[str], Matcher, Window, Recovery, Session]:
    order: list[str] = []
    lease = desktop_input.GuiLease(tmp_path / "gui.lock", timeout=0.2)
    recording = RecordingKeys(lease)
    matcher, window, recovery, session = Matcher(), Window(), Recovery(), Session()
    scan_source = FreshScan()
    result = act.Actor(
        keys=recording,
        activate=lambda xid: order.append(f"activate:{xid}"),
        lease=lease,
        presence=Presence(active),
        target=TARGET,
        matcher=matcher,  # type: ignore[arg-type]
        window=window,  # type: ignore[arg-type]
        session_provider=session,  # type: ignore[arg-type]
        session_environment=session,
        scan_observer=scan_source,
        native_observe=lambda: _current_native(result, window),
        recovery=recovery,
        stopping=stopping,
        control_runner=control_runner,
        clock=lambda: 1000.0,
    )
    scan_source.actor = result
    result.scan_source = scan_source  # type: ignore[attr-defined]
    return result, recording, order, matcher, window, recovery, session


def _current_native(actor: act.Actor, window: Window) -> Any:
    observation = actor._observation
    if observation is None or observation.snapshot is None:
        return SimpleNamespace(
            identity=IDENTITY, window=window.current, snapshot=None, session_fingerprint=""
        )
    snap = observation.snapshot
    return SimpleNamespace(
        identity=observation.process_identity,
        window=window.current,
        snapshot=ready_automation(
            chat_active=snap.chat_active,
            chat_focused=snap.chat_focused,
            chat_text=snap.chat_text,
            ah_open=snap.ah_open,
            zone=snap.zone,
            target_present=snap.target_present,
            target_name=snap.auctioneer_name,
            range_available=snap.range_available,
            in_range=snap.in_range,
            binding_available=snap.binding_available,
            binding_action=snap.binding_action,
            interaction_mode=snap.interaction_mode,
            busy=snap.automation_busy,
        ),
        session_fingerprint=observation.session_fingerprint,
    )


def chat_submit_observation() -> Observation:
    return ready_observation(chat_active=True, chat_focused=True, chat_text="/reload")


def chat_submit_decision() -> Decision:
    return decision(Action.PRESS_RETURN, command="/reload")


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
    runner, recording, _order, _matcher, _window, _recovery, _session = actor(tmp_path)
    obs = (
        chat_submit_observation()
        if action is Action.PRESS_RETURN
        else ready_observation(
            ah_open=False,
            zone="Stormwind City",
            target_present=True,
            target_name=TARGET.auctioneer,
            range_available=True,
            in_range=True,
            binding_action="INTERACTTARGET",
            interaction_mode="keyboard",
        )
        if action is Action.PRESS_INTERACT
        else ready_observation()
    )
    assert runner.execute(
        chat_submit_decision() if action is Action.PRESS_RETURN else decision(action), obs
    ) == str(action)
    assert recording.events == [event] and recording.held_during == [True]
    assert not runner._lease.held


def test_type_command_is_allowlisted(tmp_path: Path) -> None:
    runner, recording, _order, _matcher, _window, _recovery, _session = actor(tmp_path)
    obs = ready_observation(chat_active=True, chat_focused=True)
    runner.execute(decision(Action.TYPE_COMMAND, command=SET_BINDING_COMMAND), obs)
    runner.execute(decision(Action.TYPE_COMMAND, command="/targetexact Auctioneer Jaxon"), obs)
    runner.execute(decision(Action.TYPE_COMMAND, command="/reload"), obs)
    assert [e[1] for e in recording.events][-1] == "/reload"
    with pytest.raises(desktop_input.ActError, match="allowlisted"):
        runner.execute(decision(Action.TYPE_COMMAND, command="/logout"), obs)


def test_activate_window_uses_the_observed_window(tmp_path: Path) -> None:
    runner, recording, order, _matcher, window, _recovery, _session = actor(tmp_path)
    window.current.focused = False
    obs = replace(ready_observation(), window_focused=False)
    runner.execute(decision(Action.ACTIVATE_WINDOW), obs)
    assert order == ["activate:77"] and recording.events == []


def test_presence_is_rechecked_under_the_lease(tmp_path: Path) -> None:
    runner, recording, order, _matcher, _window, _recovery, _session = actor(tmp_path, active=True)
    with pytest.raises(act.ActionNotAdmitted, match="human_active"):
        runner.execute(chat_submit_decision(), chat_submit_observation())
    assert recording.events == [] and order == []


@pytest.mark.parametrize("action", [Action.WAIT, Action.HOLD_HUMAN, Action.FAULT])
def test_passive_actions_do_nothing(tmp_path: Path, action: Action) -> None:
    runner, recording, order, _matcher, _window, _recovery, _session = actor(tmp_path)
    assert runner.execute(decision(action), ready_observation()) == "none"
    assert recording.events == [] and order == []


def test_restart_requests_exact_observed_identity_and_reason(tmp_path: Path) -> None:
    runner, recording, _order, _matcher, _window, recovery, _session = actor(tmp_path)
    assert (
        runner.execute(decision(Action.RELAUNCH, detail="no_progress"), ready_observation())
        == "relaunch_started"
    )
    assert recovery.requests == [(IDENTITY, "no_progress")]
    assert recording.events == []


def test_restart_is_not_committed_without_exact_guarded_window_close(tmp_path: Path) -> None:
    runner, _recording, _order, _matcher, _window, recovery, _session = actor(tmp_path)
    recovery.action = "waiting"
    with pytest.raises(act.ActionNotAdmitted, match="waiting_for_exit"):
        runner.execute(decision(Action.RELAUNCH, detail="no_progress"), ready_observation())


def test_restart_reason_outside_established_scan_failures_is_rejected(tmp_path: Path) -> None:
    runner, _recording, _order, _matcher, _window, recovery, _session = actor(tmp_path)
    with pytest.raises(act.ActionNotAdmitted, match="restart_not_admitted"):
        runner.execute(decision(Action.RELAUNCH, detail="window_activation_failed"), ready_observation())
    assert recovery.requests == []


@pytest.mark.parametrize("action", [Action.ACTIVATE_WINDOW, Action.PRESS_RETURN])
def test_maintenance_guard_rechecks_inside_gui_lease(tmp_path: Path, action: Action) -> None:
    runner, recording, order, _matcher, window, _recovery, _session = actor(tmp_path)
    checked: list[bool] = []

    def blocker() -> str:
        checked.append(runner._lease.held)
        return "automation_running"

    runner._maintenance_blocker = blocker
    if action is Action.ACTIVATE_WINDOW:
        window.current.focused = False
        obs = replace(ready_observation(), window_focused=False)
    else:
        obs = chat_submit_observation()
    with pytest.raises(act.MaintenanceBlocked, match="automation_running"):
        runner.execute(chat_submit_decision() if action is Action.PRESS_RETURN else decision(action), obs)
    assert checked == [True]
    assert order == [] and recording.events == []
    assert not runner._lease.held


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


def test_identity_or_window_race_skips_input_without_consuming_sender(tmp_path: Path) -> None:
    runner, recording, _order, matcher, window, _recovery, _session = actor(tmp_path)
    matcher.current = (ProcessIdentity(6, 200, 1000, "C:\\Wow.exe", "/prefix"),)
    with pytest.raises(act.ActionNotAdmitted, match="process_identity_changed"):
        runner.execute(chat_submit_decision(), chat_submit_observation())
    assert recording.events == []
    matcher.current = (IDENTITY,)
    window.current.xid = 88
    with pytest.raises(act.ActionNotAdmitted, match="native_observation_changed"):
        runner.execute(chat_submit_decision(), chat_submit_observation())
    assert recording.events == []


def test_native_read_failure_logs_bounded_details_and_keeps_public_reason(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    runner, recording, _order, _matcher, _window, _recovery, _session = actor(tmp_path)

    def fail_native_read() -> Any:
        raise act.ObserveError("capture stale\nage 3.025s\n" + "private detail " * 30)

    runner._native_observe = fail_native_read
    with caplog.at_level("WARNING", logger=act.__name__):
        with pytest.raises(act.ActionNotAdmitted) as rejected:
            runner.execute(chat_submit_decision(), chat_submit_observation())

    assert rejected.value.reason == "native_observation_changed"
    assert recording.events == []
    assert not runner._lease.held
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert len(message) <= 200
    assert "\n" not in message
    assert "ObserveError" in message
    assert "capture stale age 3.025s" in message
    assert "private detail" in message


def test_foreign_window_candidate_is_treated_as_ambiguous(tmp_path: Path) -> None:
    runner, recording, _order, _matcher, window, _recovery, _session = actor(tmp_path)
    window.current.foreign_count = 1
    with pytest.raises(act.ActionNotAdmitted, match="native_observation_changed"):
        runner.execute(chat_submit_decision(), chat_submit_observation())
    assert recording.events == []


def test_native_busy_session_change_and_stop_all_skip_input(tmp_path: Path) -> None:
    runner, recording, _order, _matcher, _window, _recovery, session = actor(tmp_path)
    with pytest.raises(act.ActionNotAdmitted, match="automation_native_busy"):
        runner.execute(
            chat_submit_decision(),
            ready_observation(busy=True, chat_active=True, chat_focused=True, chat_text="/reload"),
        )
    runner._native_observe = lambda: _current_native(runner, runner._window)  # type: ignore[arg-type]
    session.fingerprint = "session-2"
    with pytest.raises(act.ActionNotAdmitted, match="desktop_session_changed"):
        runner.execute(chat_submit_decision(), chat_submit_observation())
    stopped, _keys, *_rest = actor(tmp_path / "stop", stopping=lambda: True)
    with pytest.raises(act.ActionNotAdmitted, match="stopping"):
        stopped.execute(chat_submit_decision(), chat_submit_observation())
    assert recording.events == []


def test_fresh_chat_and_auction_state_changes_skip_return(tmp_path: Path) -> None:
    runner, recording, _order, _matcher, window, _recovery, _session = actor(tmp_path)
    closed_chat = ready_automation(chat_active=False, chat_focused=False)
    runner._native_observe = lambda: SimpleNamespace(
        identity=IDENTITY,
        window=window.current,
        snapshot=closed_chat,
        session_fingerprint="session-1",
    )
    with pytest.raises(act.ActionNotAdmitted, match="chat_state_changed"):
        runner.execute(chat_submit_decision(), chat_submit_observation())

    changed_ah = ready_automation(ah_open=False)
    runner._native_observe = lambda: SimpleNamespace(
        identity=IDENTITY,
        window=window.current,
        snapshot=changed_ah,
        session_fingerprint="session-1",
    )
    with pytest.raises(act.ActionNotAdmitted, match="native_action_state_changed"):
        runner.execute(chat_submit_decision(), chat_submit_observation())
    assert recording.events == []


def test_fresh_scanner_readiness_and_policy_state_are_required(tmp_path: Path) -> None:
    runner, recording, _order, _matcher, _window, _recovery, _session = actor(tmp_path)
    obs = chat_submit_observation()
    source = runner._scan_observer
    source.current = replace(obs, native_ready=False)  # type: ignore[attr-defined]
    with pytest.raises(act.ActionNotAdmitted, match="scanner_observation_changed"):
        runner.execute(chat_submit_decision(), obs)

    source.current = replace(obs, snapshot=replace(obs.snapshot, phase="item"))  # type: ignore[attr-defined,arg-type]
    with pytest.raises(act.ActionNotAdmitted, match="scanner_state_changed"):
        runner.execute(chat_submit_decision(), obs)

    source.current = obs  # type: ignore[attr-defined]
    source.decode_error = "scanner ProtocolError"  # type: ignore[attr-defined]
    with pytest.raises(act.ActionNotAdmitted, match="scanner_observation_changed"):
        runner.execute(chat_submit_decision(), obs)
    assert recording.events == []


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("human", "human_active"),
        ("focus", "window_focus_changed"),
        ("chat", "chat_submit_state_changed"),
        ("auction_house", "native_action_state_changed"),
        ("auctioneer", "native_action_state_changed"),
        ("operation", "automation_native_busy"),
    ],
)
def test_latest_complete_scan_observation_rechecks_each_admission_fact(
    tmp_path: Path, change: str, reason: str
) -> None:
    runner, recording, _order, _matcher, _window, _recovery, _session = actor(tmp_path)
    original = chat_submit_observation()
    latest = original
    if change == "human":
        latest = replace(original, human_active=True, human_reason="keyboard")
    elif change == "focus":
        latest = replace(original, window_focused=False)
    elif change == "chat":
        assert original.snapshot is not None
        latest = replace(
            original, snapshot=replace(original.snapshot, chat_active=False, chat_focused=False, chat_text="")
        )
    elif change == "auction_house":
        assert original.snapshot is not None
        latest = replace(original, snapshot=replace(original.snapshot, ah_open=False))
    elif change == "auctioneer":
        assert original.snapshot is not None
        latest = replace(
            original,
            snapshot=replace(original.snapshot, auctioneer_name="Different Auctioneer", target_present=False),
        )
    elif change == "operation":
        assert original.snapshot is not None
        latest = replace(
            original,
            native_automation_busy=True,
            snapshot=replace(original.snapshot, automation_busy=True),
        )
    runner._scan_observer.observations = [original, latest]  # type: ignore[attr-defined]

    class GuardedKeys:
        def press(self, key: str) -> None:
            runner.before_emit()
            recording.press(key)

        def chord(self, steps: tuple[KeyStep, ...]) -> None:
            runner.before_emit()
            recording.chord(steps)

        def type_text(self, text: str) -> None:
            runner.before_emit()
            recording.type_text(text)

    runner._keys = GuardedKeys()  # type: ignore[assignment]
    with pytest.raises(act.ActionNotAdmitted, match=reason):
        runner.execute(chat_submit_decision(), original)
    assert recording.events == []


def test_type_command_stops_on_new_operation_in_the_maintenance_ledger(tmp_path: Path) -> None:
    runner, _recording, _order, _matcher, window, _recovery, _session = actor(tmp_path)
    obs = ready_observation(chat_active=True, chat_focused=True)
    native = ready_automation(chat_active=True, chat_focused=True)
    emitted: list[str] = []

    class CharacterKeys:
        def press(self, _key: str) -> None:
            raise AssertionError("unexpected press")

        def chord(self, _steps: tuple[KeyStep, ...]) -> None:
            raise AssertionError("unexpected chord")

        def type_text(self, text: str) -> None:
            for char in text:
                runner.before_emit()
                emitted.append(char)
                native["chatEdit"]["text"] += char

    runner._native_observe = lambda: SimpleNamespace(
        identity=IDENTITY,
        window=window.current,
        snapshot=native,
        session_fingerprint="session-1",
    )
    runner._keys = CharacterKeys()  # type: ignore[assignment]
    runner.execute(decision(Action.TYPE_COMMAND, command="/reload"), obs)
    assert "".join(emitted) == "/reload"

    native["chatEdit"]["text"] = ""
    emitted.clear()

    class BusyAfterFirstCharacter(CharacterKeys):
        def type_text(self, text: str) -> None:
            for char in text:
                runner.before_emit()
                emitted.append(char)
                native["chatEdit"]["text"] += char
                if len(emitted) == 1:
                    native["operation"]["batchId"] = "new-batch"

    runner._keys = BusyAfterFirstCharacter()  # type: ignore[assignment]
    runner._maintenance_blocker = lambda: "automation_running" if native["operation"]["batchId"] else None
    with pytest.raises(act.MaintenanceBlocked, match="automation_running"):
        runner.execute(decision(Action.TYPE_COMMAND, command="/reload"), obs)
    assert emitted == ["/"]


def test_long_text_entry_does_not_capture_the_screen_between_characters(
    tmp_path: Path,
) -> None:
    runner, _recording, _order, _matcher, window, _recovery, _session = actor(tmp_path)
    now = [1000.0]
    runner._clock = lambda: now[0]
    obs = ready_observation(chat_active=True, chat_focused=True)
    native = ready_automation(chat_active=True, chat_focused=True)
    emitted: list[str] = []
    captures = {"native": 0, "paired": 0}
    original_observe = runner._scan_observer.observe

    def paired_observe(state: LoopState) -> Observation:
        captures["paired"] += 1
        now[0] += 1.2
        return original_observe(state)

    runner._scan_observer.observe = paired_observe  # type: ignore[method-assign]

    class FreshCharacterKeys:
        def press(self, _key: str) -> None:
            raise AssertionError("unexpected press")

        def chord(self, _steps: tuple[KeyStep, ...]) -> None:
            raise AssertionError("unexpected chord")

        def type_text(self, text: str) -> None:
            for char in text:
                now[0] += 0.03
                native["chatEdit"]["text"] = "".join(emitted)
                runner.before_emit()
                emitted.append(char)

    def native_observe() -> Any:
        captures["native"] += 1
        now[0] += 1.2
        return SimpleNamespace(
            identity=IDENTITY,
            window=window.current,
            snapshot=native,
            session_fingerprint="session-1",
        )

    runner._native_observe = native_observe
    runner._keys = FreshCharacterKeys()  # type: ignore[assignment]
    command = SET_BINDING_COMMAND
    runner.execute(decision(Action.TYPE_COMMAND, command=command), obs)
    assert "".join(emitted) == command
    assert captures == {"native": 1, "paired": 1}
    assert now[0] - obs.wall == pytest.approx(2.4 + 0.03 * len(command))
    assert now[0] - obs.wall < 7


def test_stale_text_entry_observation_cannot_submit_the_command(tmp_path: Path) -> None:
    runner, _recording, _order, _matcher, window, _recovery, _session = actor(tmp_path)
    now = [1000.0]
    runner._clock = lambda: now[0]
    obs = ready_observation(chat_active=True, chat_focused=True)
    native = ready_automation(chat_active=True, chat_focused=True)
    source = runner._scan_observer
    emitted: list[str] = []

    class StaleCharacterKeys:
        def press(self, _key: str) -> None:
            raise AssertionError("unexpected press")

        def chord(self, _steps: tuple[KeyStep, ...]) -> None:
            raise AssertionError("unexpected chord")

        def type_text(self, text: str) -> None:
            now[0] += act.STALE_FRAME_S + 1
            source.current = obs  # type: ignore[attr-defined]
            runner.before_emit()
            emitted.extend(text)

    runner._native_observe = lambda: SimpleNamespace(
        identity=IDENTITY,
        window=window.current,
        snapshot=native,
        session_fingerprint="session-1",
    )
    runner._keys = StaleCharacterKeys()  # type: ignore[assignment]
    runner.execute(decision(Action.TYPE_COMMAND, command="/reload"), obs)
    assert "".join(emitted) == "/reload"
    with pytest.raises(act.ActionNotAdmitted, match="scanner_observation_stale"):
        runner.execute(decision(Action.PRESS_RETURN, command="/reload"), obs)


def test_control_child_stop_is_a_skipped_action_and_helper_failure_is_visible(tmp_path: Path) -> None:
    runner, recording, _order, _matcher, _window, _recovery, _session = actor(tmp_path)

    class CancelledKeys:
        def press(self, _key: str) -> None:
            raise ControlChildCancelled("cancelled by stop")

        def chord(self, _steps: tuple[KeyStep, ...]) -> None:
            raise ControlChildCancelled("cancelled by stop")

        def type_text(self, _text: str) -> None:
            raise ControlChildCancelled("cancelled by stop")

    runner._keys = CancelledKeys()  # type: ignore[assignment]
    with pytest.raises(act.ActionNotAdmitted, match="stopping"):
        runner.execute(chat_submit_decision(), chat_submit_observation())

    class BrokenKeys(CancelledKeys):
        def press(self, _key: str) -> None:
            raise ControlChildError("pinned child failed")

    runner._keys = BrokenKeys()  # type: ignore[assignment]
    with pytest.raises(desktop_input.ActError, match="pinned control helper failed"):
        runner.execute(chat_submit_decision(), chat_submit_observation())
    assert recording.events == []


@pytest.mark.parametrize(
    "sender_error",
    [
        desktop_input.ActError("ordinary sender failure"),
        subprocess.TimeoutExpired(["xdotool"], 30),
        None,
    ],
    ids=["act-error", "timeout", "nominal-return"],
)
def test_pending_cleanup_is_fatal_for_every_action_exit(
    tmp_path: Path, sender_error: BaseException | None
) -> None:
    class PendingControl:
        child_pid: int | None = None
        possible_down: frozenset[str] = frozenset()
        cleanup_failed = False

        def close(self) -> None:
            self.child_pid = None
            self.possible_down = frozenset()
            self.cleanup_failed = False

    control = PendingControl()
    runner, _recording, _order, _matcher, _window, _recovery, _session = actor(
        tmp_path, control_runner=control
    )

    class PendingKeys:
        def press(self, _key: str) -> None:
            control.child_pid = 404
            control.possible_down = frozenset({"key:009"})
            control.cleanup_failed = True
            if sender_error is not None:
                raise sender_error

        def chord(self, _steps: tuple[KeyStep, ...]) -> None:
            raise AssertionError("unexpected chord")

        def type_text(self, _text: str) -> None:
            raise AssertionError("unexpected typing")

    runner._keys = PendingKeys()  # type: ignore[assignment]
    with pytest.raises(ControlChildError, match="cleanup did not complete") as captured:
        runner.execute(decision(Action.PRESS_ESCAPE), ready_observation())
    if sender_error is not None:
        assert captured.value.__context__ is sender_error
    from crowley_host.scan.loop import OPERATIONAL_ERRORS

    assert not isinstance(captured.value, OPERATIONAL_ERRORS)
    assert runner._lease.held
    assert control.child_pid == 404 and control.cleanup_failed

    runner.close()
    assert not runner._lease.held
    assert control.child_pid is None and not control.cleanup_failed


def test_unreaped_control_child_retains_canonical_lease_until_cleanup_succeeds(
    tmp_path: Path,
) -> None:
    class UnreapableControl:
        child_pid: int | None = None
        cleanup_failed = False
        possible_down: frozenset[str] = frozenset()
        can_reap = False
        close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            if not self.can_reap:
                self.child_pid = 404
                self.cleanup_failed = True
                self.possible_down = frozenset({"key:036"})
                raise ControlChildError("exact control child could not be reaped")
            self.child_pid = None
            self.cleanup_failed = False
            self.possible_down = frozenset()

        def release_only(self) -> None:
            if self.child_pid is not None:
                self.cleanup_failed = True
                return
            self.cleanup_failed = False
            self.possible_down = frozenset()

    control = UnreapableControl()
    runner, _recording, _order, _matcher, _window, _recovery, _session = actor(
        tmp_path, control_runner=control
    )

    class UnreapableKeys:
        def press(self, _key: str) -> None:
            control.child_pid = 404
            control.cleanup_failed = True
            control.possible_down = frozenset({"key:036"})
            raise ControlChildError("exact control child could not be reaped")

        def chord(self, _steps: tuple[KeyStep, ...]) -> None:
            raise AssertionError("unexpected chord")

        def type_text(self, _text: str) -> None:
            raise AssertionError("unexpected typing")

    runner._keys = UnreapableKeys()  # type: ignore[assignment]
    with pytest.raises(ControlChildError, match="cleanup did not complete"):
        runner.execute(chat_submit_decision(), chat_submit_observation())
    assert runner._lease.held
    assert control.child_pid == 404 and control.cleanup_failed

    contender = desktop_input.GuiLease(tmp_path / "gui.lock", timeout=0.05)
    with pytest.raises(desktop_input.ActError, match="held elsewhere"), contender.hold():
        pass
    with pytest.raises(act.ActionNotAdmitted, match="control_cleanup_failed"):
        runner.execute(chat_submit_decision(), chat_submit_observation())

    with pytest.raises(ControlChildError, match="could not be reaped"):
        runner.close()
    assert runner._lease.held
    assert control.child_pid == 404 and control.cleanup_failed

    control.can_reap = True
    runner.close()
    assert control.close_calls == 2
    assert not runner._lease.held
    assert control.child_pid is None and not control.cleanup_failed
    with contender.hold():
        assert contender.held
    with pytest.raises(act.ActionNotAdmitted, match="actor_closed"):
        runner.execute(chat_submit_decision(), chat_submit_observation())


def test_fresh_native_busy_or_popup_race_skips_input(tmp_path: Path) -> None:
    runner, recording, _order, _matcher, window, _recovery, _session = actor(tmp_path)
    busy = ready_automation()
    busy["operation"]["batchId"] = "active-batch"
    runner._native_observe = lambda: SimpleNamespace(
        identity=IDENTITY,
        window=window.current,
        snapshot=busy,
        session_fingerprint="session-1",
    )
    with pytest.raises(act.ActionNotAdmitted, match="automation_native_busy"):
        runner.execute(chat_submit_decision(), chat_submit_observation())
    popup = ready_automation()
    popup["blockingUI"]["gameMenuShown"] = True
    runner._native_observe = lambda: SimpleNamespace(
        identity=IDENTITY,
        window=window.current,
        snapshot=popup,
        session_fingerprint="session-1",
    )
    with pytest.raises(act.ActionNotAdmitted, match="blocking_ui_changed"):
        runner.execute(chat_submit_decision(), chat_submit_observation())
    assert recording.events == []


def test_text_entry_is_admitted_once_instead_of_spawning_per_character() -> None:
    runner, guards = Runner(), []
    sender = keys(runner, before_emit=lambda: guards.append("checked"))
    sender.type_text("/Ad")
    assert len(guards) == 1
    assert len(sent(runner)) == 1
