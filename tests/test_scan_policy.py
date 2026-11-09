"""Table-driven tests for the pure scan-loop policy (plan rows P and 1-13)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from crowley.scan.policy import (
    SET_BINDING_COMMAND,
    Action,
    CommandTx,
    Decision,
    LoopState,
    Observation,
    Snapshot,
    Step,
    Target,
    allowed_commands,
    decide,
    durable_fields,
    relaunch_gap,
    restore,
)

WALL = 1_000_000.0
MONO = 500.0
JAXON = "Auctioneer Jaxon"
TARGET = Target("Char", "Realm", "Alliance", "house-a", "plan-1", "70862", 11500, "1.0", JAXON)
S1, S2, S3 = "uid-1", "uid-2", "uid-3"


def snap(**kw: object) -> Snapshot:
    base = Snapshot(
        boot="boot-1",
        native_phase="normal",
        native_ordinal=0,
        wall_time=1,
        received_at=WALL - 1,
        character="Char",
        realm="Realm",
        faction="Alliance",
        zone="Stormwind City",
        build="70862",
        interface=11500,
        addon_version="1.0",
        house_key="house-a",
        plan_hash="plan-1",
        ah_open=True,
        target_present=True,
        auctioneer_name=JAXON,
        range_available=True,
        in_range=True,
        binding_available=True,
        binding_action="INTERACTTARGET",
        interaction_mode="keyboard",
        blocking_ui="",
        phase="scheduled",
        due_at=WALL + 600,
        last_saved_uid="",
        pending_ids=frozenset(),
        pending_count=0,
        pending_cap=6,
        pending_blocked=False,
        pending_reason="",
        loaded_acked_ids=frozenset(),
        chat_active=False,
        chat_focused=False,
        chat_text="",
    )
    return replace(base, **kw)  # type: ignore[arg-type]


def obs(snapshot: Snapshot | None = None, **kw: object) -> Observation:
    base = Observation(
        wall=WALL,
        mono=MONO,
        process_alive=True,
        process_started_at=WALL - 3600,
        window_ok=True,
        window_focused=True,
        snapshot=snapshot if snapshot is not None else snap(),
        last_frame_at=WALL - 1,
        frames_seen_this_process=100,
        disk_saved_ids=frozenset(),
        uploaded_ids=frozenset(),
        permanently_rejected_ids=frozenset(),
        data_acked_ids=frozenset(),
        companion_last_error="",
        human_active=False,
        human_reason="",
        target=TARGET,
        state=LoopState(last_new_sweep_at=WALL - 60),
    )
    return replace(base, **kw)  # type: ignore[arg-type]


def with_state(o: Observation, **kw: object) -> Observation:
    return replace(o, state=replace(o.state, **kw))  # type: ignore[arg-type]


def check(d: Decision, action: Action, code: str = "") -> None:
    assert (d.action, d.fault_code) == (action, code), d


# --- rows 1-3, P -----------------------------------------------------------------------


def test_row_p_human_holds_everything_including_relaunch() -> None:
    check(decide(obs(human_active=True, human_reason="key")), Action.HOLD_HUMAN)
    check(decide(obs(human_active=True, process_alive=False)), Action.HOLD_HUMAN)


def test_row_1_no_process_relaunches_then_backs_off() -> None:
    d = decide(obs(process_alive=False, snapshot=None))
    check(d, Action.RELAUNCH)
    assert d.state.relaunch_attempts == 1 and d.state.last_relaunch_at == WALL
    again = replace(obs(process_alive=False, snapshot=None), wall=WALL + 899, state=d.state)
    check(decide(again), Action.WAIT)
    check(decide(replace(again, wall=WALL + 900)), Action.RELAUNCH)


def test_backoff_schedule_15_min_three_times_then_hourly_uncapped() -> None:
    assert [relaunch_gap(n) for n in range(6)] == [900, 900, 900, 3600, 3600, 3600]
    assert relaunch_gap(500) == 3600
    st = LoopState(last_relaunch_at=WALL - 1000, relaunch_attempts=3)
    o = obs(process_alive=False, snapshot=None, state=st)
    check(decide(o), Action.WAIT)  # 1000 s < 3600 s after three attempts
    check(decide(replace(o, wall=WALL + 2600)), Action.RELAUNCH)
    big = replace(o, state=replace(st, relaunch_attempts=40), wall=WALL + 3600)
    check(decide(big), Action.RELAUNCH)


def test_attempt_counter_resets_on_a_new_saved_sweep_not_a_healthy_frame() -> None:
    st = LoopState(relaunch_attempts=5, last_new_sweep_at=WALL)
    assert decide(obs(state=st)).state.relaunch_attempts == 5
    assert decide(obs(snap(last_saved_uid=S1), state=st)).state.relaunch_attempts == 0


def test_row_2_lived_then_silent_10_min() -> None:
    quiet = obs(snapshot=snap(received_at=WALL - 599), last_frame_at=WALL - 599)
    check(decide(quiet), Action.WAIT)  # stale frame, not yet 10 min
    gone = obs(snapshot=snap(received_at=WALL - 600), last_frame_at=WALL - 600)
    check(decide(gone), Action.RELAUNCH)


def test_row_3_fresh_launch_without_frames_reports_code() -> None:
    young = obs(snapshot=None, frames_seen_this_process=0, last_frame_at=None, process_started_at=WALL - 599)
    check(decide(young), Action.WAIT)
    old = replace(young, process_started_at=WALL - 600)
    check(decide(old), Action.RELAUNCH, "no_frames_after_launch")
    held = replace(old, state=LoopState(last_relaunch_at=WALL - 100, relaunch_attempts=1))
    check(decide(held), Action.WAIT, "no_frames_after_launch")  # backoff keeps the report


def test_unfocused_window_is_activated_before_any_key() -> None:
    o = obs(snap(ah_open=False), window_focused=False)
    check(decide(o), Action.ACTIVATE_WINDOW)
    check(decide(replace(o, window_ok=False)), Action.FAULT, "window_invalid")


# --- row 4 -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change", "name"),
    [
        ({"character": "X"}, "character"),
        ({"realm": "X"}, "realm"),
        ({"faction": "Horde"}, "faction"),
        ({"house_key": "house-b"}, "house"),
        ({"plan_hash": "zz"}, "plan_hash"),
        ({"build": "1"}, "build"),
        ({"interface": 1}, "interface"),
        ({"addon_version": "9"}, "addon_version"),
    ],
)
def test_row_4_pin_mismatch_faults_without_input(change: dict[str, object], name: str) -> None:
    d = decide(obs(snap(ah_open=False, **change)))
    check(d, Action.FAULT, "pin_mismatch")
    assert d.detail == name


def test_row_4_plan_invalid_and_construction_marker() -> None:
    bad = snap(pending_blocked=True, pending_reason="plan_invalid", phase="blocked", pending_count=0)
    check(decide(obs(bad)), Action.FAULT, "plan_invalid")
    check(decide(obs(snap(boot="native-construction:abc"))), Action.FAULT, "construction_marker")
    check(decide(obs(snap(native_phase="held"))), Action.FAULT, "construction_marker")
    check(decide(obs(snap(native_ordinal=3, native_phase="updates"))), Action.FAULT, "construction_marker")


# --- row 5 and typed-command transactions ----------------------------------------------


def tx(step: Step, command: str = "/reload", purpose: str = "save_flush", at: float = MONO - 1) -> CommandTx:
    return CommandTx(command, purpose, step, at, typed_at=WALL - 0.5)


def chat_obs(text: str, step: Step | None, *, active: bool = True, **kw: object) -> Observation:
    command = kw.pop("command", "/reload")
    at = float(kw.pop("at", MONO - 1))  # type: ignore[arg-type]
    focused = bool(kw.pop("focused", active))
    state = LoopState(last_new_sweep_at=WALL, tx=tx(step, str(command), at=at) if step else None)
    return obs(snap(chat_active=active, chat_focused=focused, chat_text=text, **kw), state=state)


def test_row_5_foreign_text_faults_and_never_erases() -> None:
    check(decide(chat_obs("hello", None)), Action.FAULT, "chat_foreign_text")
    check(decide(chat_obs("hello", Step.TYPED)), Action.FAULT, "chat_foreign_text")
    assert "hello" not in decide(chat_obs("hello", None)).detail


def test_row_5_strict_prefix_of_our_command_is_escaped() -> None:
    check(decide(chat_obs("/rel", Step.TYPED)), Action.WAIT)  # render lag: wait first
    d = decide(chat_obs("/rel", Step.TYPED, at=MONO - 6))
    check(d, Action.PRESS_ESCAPE)
    assert d.state.tx is not None and d.state.tx.step is Step.ABORTING


def test_transaction_walks_open_type_return() -> None:
    start = obs(snap(ah_open=True, last_saved_uid=S1, pending_ids=frozenset({S1}), pending_count=1))
    d = decide(start)
    check(d, Action.OPEN_CHAT)
    assert d.command == "/reload" and d.purpose == "save_flush"
    opened = replace(
        start,
        state=d.state,
        snapshot=replace(start.snapshot, chat_active=True, chat_focused=True, chat_text=""),
    )
    typed = decide(opened)
    check(typed, Action.TYPE_COMMAND)
    assert typed.command == "/reload"
    read = replace(opened, state=typed.state, wall=WALL + 1)
    readback = replace(read.snapshot, chat_text="/reload", received_at=WALL + 0.9)
    final = decide(replace(read, snapshot=readback))
    check(final, Action.PRESS_RETURN)
    assert final.arm_boot_change
    done = replace(read, state=final.state, snapshot=snap(last_saved_uid=S1, pending_ids=frozenset({S1})))
    done = replace(done, disk_saved_ids=frozenset({S1}), wall=WALL + 3)
    cleared = decide(done)
    assert cleared.state.tx is None
    check(cleared, Action.WAIT)


def test_readback_older_than_typing_is_not_trusted() -> None:
    o = chat_obs("/reload", Step.TYPED)
    stale = replace(o, snapshot=replace(o.snapshot, received_at=WALL - 1))
    check(decide(stale), Action.WAIT)


def test_readback_mismatch_escapes() -> None:
    o = chat_obs("", Step.TYPED, at=MONO - 6)
    o = replace(o, snapshot=replace(o.snapshot, received_at=WALL))
    o = replace(o, wall=WALL + 1)
    check(decide(o), Action.PRESS_ESCAPE)


def test_presence_rechecked_before_return() -> None:
    o = chat_obs("/reload", Step.TYPED)
    o = replace(o, snapshot=replace(o.snapshot, received_at=WALL - 0.2))
    check(decide(o), Action.PRESS_RETURN)
    check(decide(replace(o, human_active=True)), Action.HOLD_HUMAN)
    check(decide(replace(o, window_focused=False)), Action.ACTIVATE_WINDOW)
    old_frame = replace(o.snapshot, received_at=WALL - 5)
    check(decide(replace(o, snapshot=old_frame)), Action.WAIT)


def test_reload_aborts_when_margin_vanished_before_return() -> None:
    o = chat_obs("/reload", Step.TYPED, due_at=WALL + 30)
    o = replace(o, snapshot=replace(o.snapshot, received_at=WALL - 0.2))
    check(decide(o), Action.PRESS_ESCAPE)


def test_allowlist_rejects_other_commands() -> None:
    assert allowed_commands(TARGET) == {"/reload", f"/targetexact {JAXON}", SET_BINDING_COMMAND}
    assert SET_BINDING_COMMAND.startswith("/run local a=GetBindingAction('CTRL-F12');")


# --- rows 6, 7 ---------------------------------------------------------------------------


def test_row_6_blocking_ui_faults() -> None:
    check(decide(obs(snap(blocking_ui="static_popup_shown"))), Action.FAULT, "blocking_ui")


@pytest.mark.parametrize("phase", ["item", "owned"])
def test_row_7_running_sweep_waits(phase: str) -> None:
    check(decide(obs(snap(phase=phase, ah_open=False))), Action.WAIT)


# --- row 8 -------------------------------------------------------------------------------


def saved(**kw: object) -> Snapshot:
    return snap(last_saved_uid=S1, pending_ids=frozenset({S1}), pending_count=1, **kw)


@pytest.mark.parametrize(("margin", "action"), [(89, Action.WAIT), (90, Action.OPEN_CHAT)])
def test_row_8_margin_boundary(margin: int, action: Action) -> None:
    check(decide(obs(saved(due_at=WALL + margin))), action)


def test_row_8_not_when_already_on_disk_or_settling() -> None:
    check(decide(obs(saved(), disk_saved_ids=frozenset({S1}))), Action.WAIT)
    o = obs(saved(), state=LoopState(last_new_sweep_at=WALL, last_ack_reload_at=WALL - 59))
    check(decide(o), Action.WAIT)


# --- rows 9, 10 --------------------------------------------------------------------------

FULL = frozenset({f"u{i}" for i in range(6)})


def blocked(**kw: object) -> Snapshot:
    return snap(phase="blocked", pending_ids=FULL, pending_count=6, pending_blocked=True,
                pending_reason="queue_full", **kw)  # fmt: skip


def test_row_9_ack_reload_rate_limit_and_margin() -> None:
    acks = {"uploaded_ids": FULL, "data_acked_ids": frozenset({"u1", "u2"})}
    d = decide(obs(blocked(), **acks))
    check(d, Action.OPEN_CHAT)
    assert d.purpose == "ack_load"
    recent = obs(blocked(), state=LoopState(last_new_sweep_at=WALL, last_ack_reload_at=WALL - 599), **acks)
    check(decide(recent), Action.WAIT)
    check(decide(replace(recent, wall=WALL + 1)), Action.OPEN_CHAT)
    check(decide(obs(blocked(due_at=WALL + 10), **acks)), Action.OPEN_CHAT)  # no sweeps run when blocked
    already_loaded = obs(blocked(loaded_acked_ids=frozenset({"u1", "u2"})), **acks)
    check(decide(already_loaded), Action.FAULT, "queue_full")


def test_row_10_queue_full_reports_companion_and_rejections() -> None:
    d = decide(obs(blocked(), permanently_rejected_ids=frozenset({"u3"}), companion_last_error="HTTP 500"))
    check(d, Action.FAULT, "queue_full")
    assert d.detail == "rejected=1 companion_error=set"
    assert "500" not in d.detail


# --- row 11 ------------------------------------------------------------------------------


def closed(**kw: object) -> Observation:
    return obs(snap(ah_open=False, **kw))


def test_row_11_steps_target_bind_interact() -> None:
    d = decide(closed(target_present=False, auctioneer_name="", range_available=False, in_range=False))
    check(d, Action.OPEN_CHAT)
    assert d.command == f"/targetexact {JAXON}"
    d = decide(closed(binding_action=""))
    check(d, Action.OPEN_CHAT)
    assert d.command == SET_BINDING_COMMAND
    d = decide(closed())
    check(d, Action.PRESS_INTERACT)
    assert d.state.ah_stage == "interact_sent" and len(d.state.ah_attempts) == 1


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"zone": "Orgrimmar"}, "not_in_stormwind"),
        ({"in_range": False}, "auctioneer_out_of_range"),
        ({"interaction_mode": "disabled"}, "interaction_mode_unavailable"),
        ({"binding_available": False, "binding_action": ""}, "binding_unavailable"),
        ({"binding_action": "SOMETHING"}, "binding_conflict"),
    ],
)
def test_row_11_precondition_faults(change: dict[str, object], code: str) -> None:
    check(decide(closed(**change)), Action.FAULT, code)


def test_not_in_stormwind_never_relaunches() -> None:
    o = closed(zone="Orgrimmar")
    for later in (0, 3600, 86_400, 7 * 86_400):
        o2 = replace(o, wall=WALL + later, snapshot=replace(o.snapshot, received_at=WALL + later - 1))
        o2 = replace(o2, last_frame_at=WALL + later - 1)
        check(decide(o2), Action.FAULT, "not_in_stormwind")


def test_ah_open_postcondition_and_three_attempts_per_ten_minutes() -> None:
    o = closed()
    d = decide(o)
    check(d, Action.PRESS_INTERACT)
    waiting = replace(o, state=d.state, wall=WALL + 9.9)
    waiting = replace(waiting, snapshot=replace(o.snapshot, received_at=WALL + 9))
    check(decide(waiting), Action.WAIT)
    # attempts 2 and 3 after the 10 s postcondition lapses
    state = d.state
    t = WALL
    for _ in range(2):
        t += 11
        o = replace(
            o, wall=t, state=state, snapshot=replace(o.snapshot, received_at=t - 1), last_frame_at=t - 1
        )
        lapsed = decide(o)  # clears the failed attempt
        check(lapsed, Action.PRESS_INTERACT)
        state = lapsed.state
    t += 11
    o = replace(o, wall=t, state=state, snapshot=replace(o.snapshot, received_at=t - 1), last_frame_at=t - 1)
    check(decide(o), Action.FAULT, "ah_open_failed")


def test_ah_open_failed_relaunches_after_30_min_and_window_reopens() -> None:
    attempts = (WALL - 30, WALL - 20, WALL - 10)
    o = closed()
    failed = with_state(o, ah_attempts=attempts)
    d = decide(failed)
    check(d, Action.FAULT, "ah_open_failed")
    assert d.state.ah_failed_at == WALL
    later = replace(o, state=d.state, wall=WALL + 1799, last_frame_at=WALL + 1798)
    later = replace(later, snapshot=replace(o.snapshot, received_at=WALL + 1798))
    later = with_state(later, ah_attempts=())
    check(decide(later), Action.PRESS_INTERACT)  # window slid, retries resume
    later = with_state(replace(later, wall=WALL + 1800), ah_attempts=())
    later = replace(later, snapshot=replace(o.snapshot, received_at=WALL + 1799), last_frame_at=WALL + 1799)
    check(decide(later), Action.RELAUNCH, "ah_open_failed")


# --- row 12 / 13 -------------------------------------------------------------------------


def open_obs(open_for: float, since_sweep: float, stage: int = 0, stage_at: float = 0.0) -> Observation:
    st = LoopState(
        ah_open_since=WALL - open_for,
        last_new_sweep_at=WALL - since_sweep,
        last_seen_saved_uid="",
        no_progress_stage=stage,
        no_progress_at=stage_at,
    )
    return obs(snap(), state=st)


def test_row_12_no_progress_escalation() -> None:
    check(decide(open_obs(1799, 1799)), Action.WAIT)
    d = decide(open_obs(1800, 1800))
    check(d, Action.CLOSE_AH, "no_progress")
    assert d.state.no_progress_stage == 1
    waiting = open_obs(10, 1810, stage=1, stage_at=WALL - 1799)
    check(decide(waiting), Action.WAIT, "no_progress")
    check(decide(open_obs(10, 3600, stage=1, stage_at=WALL - 1800)), Action.RELAUNCH, "no_progress")


def test_row_12_new_sweep_resets_escalation() -> None:
    o = open_obs(100, 2000, stage=1, stage_at=WALL - 100)
    o = replace(o, snapshot=replace(o.snapshot, last_saved_uid=S2))
    d = decide(o)
    assert d.state.no_progress_stage == 0 and d.state.last_new_sweep_at == WALL
    check(d, Action.WAIT)


def test_row_13_waits() -> None:
    check(decide(obs()), Action.WAIT)


# --- review fixes -------------------------------------------------------------------------

POPUP = "static_popup_shown"


@pytest.mark.parametrize(
    ("text", "step"),
    [("/reload", Step.TYPED), ("/rel", Step.TYPED), ("", Step.TYPED), ("", Step.OPENING), ("hello", None)],
)
def test_blocking_ui_beats_every_chat_action(text: str, step: Step | None) -> None:
    o = chat_obs(text, step, at=MONO - 10, blocking_ui=POPUP)
    o = replace(o, snapshot=replace(o.snapshot, received_at=WALL - 0.2))
    d = decide(o)
    check(d, Action.FAULT, "blocking_ui")
    assert d.state.tx == o.state.tx


def test_blocking_ui_blocks_interact_and_chat_start() -> None:
    check(decide(closed(blocking_ui=POPUP)), Action.FAULT, "blocking_ui")
    check(decide(obs(saved(blocking_ui=POPUP))), Action.FAULT, "blocking_ui")


def test_type_only_into_focused_empty_box_else_reopen() -> None:
    check(decide(chat_obs("", Step.OPENING)), Action.TYPE_COMMAND)
    d = decide(chat_obs("", Step.OPENING, focused=False))
    check(d, Action.OPEN_CHAT)
    assert d.state.tx is not None and d.state.tx.reopened
    again = replace(chat_obs("", Step.OPENING, focused=False), state=d.state)
    check(decide(again), Action.WAIT)  # reopened once; now wait
    gone = replace(again, state=replace(d.state, tx=replace(d.state.tx, at=MONO - 6)))
    check(decide(gone), Action.FAULT, "chat_open_failed")


def test_never_escape_an_unfocused_box() -> None:
    o = chat_obs("/rel", Step.TYPED, at=MONO - 10, focused=False)
    check(decide(o), Action.FAULT, "chat_unfocused")
    stray = chat_obs("/rel", None, focused=False)
    check(decide(stray), Action.FAULT, "chat_unfocused")
    full = chat_obs("/reload", Step.TYPED, focused=False)
    full = replace(full, snapshot=replace(full.snapshot, received_at=WALL - 0.2))
    check(decide(full), Action.WAIT)  # no Return into an unfocused box
    check(
        decide(replace(full, state=replace(full.state, tx=tx(Step.TYPED, at=MONO - 6)))),
        Action.FAULT,
        "chat_unfocused",
    )


def flush_obs(boot: str, flush_at: float, seen: float | None, **kw: object) -> Observation:
    st = LoopState(
        last_new_sweep_at=WALL, flush_uid=S1, flush_boot="boot-1", flush_at=flush_at, flush_seen_at=seen
    )
    return obs(saved(boot=boot), state=st, **kw)


def test_flush_return_records_uid_boot_and_reload_time() -> None:
    o = chat_obs("/reload", Step.TYPED, last_saved_uid=S1, pending_ids=frozenset({S1}))
    o = replace(o, snapshot=replace(o.snapshot, received_at=WALL - 0.2))
    d = decide(o)
    check(d, Action.PRESS_RETURN)
    s = d.state
    assert (s.flush_uid, s.flush_boot, s.flush_at, s.last_ack_reload_at) == (S1, "boot-1", WALL, WALL)


def test_flush_not_persisted_fault_and_30_min_retry() -> None:
    first = decide(flush_obs("boot-2", WALL - 300, None))
    check(first, Action.WAIT)  # newly seen boot starts the 2 min clock
    assert first.state.flush_seen_at == WALL
    check(decide(flush_obs("boot-2", WALL - 300, WALL - 119)), Action.WAIT)
    check(decide(flush_obs("boot-2", WALL - 300, WALL - 120)), Action.FAULT, "flush_not_persisted")
    check(decide(flush_obs("boot-1", WALL - 100, None)), Action.WAIT)  # reload not completed yet
    check(decide(flush_obs("boot-2", WALL - 1799, WALL - 1700)), Action.FAULT, "flush_not_persisted")
    retry = decide(flush_obs("boot-2", WALL - 1800, WALL - 1700))
    check(retry, Action.OPEN_CHAT)  # once per 30 min, never every 60 s


def test_flush_memory_clears_when_uid_reaches_disk() -> None:
    d = decide(flush_obs("boot-2", WALL - 300, WALL - 200, disk_saved_ids=frozenset({S1})))
    assert d.state.flush_uid == "" and d.state.flush_at is None


def test_reload_window_ah_closed_or_scheduled_only() -> None:
    check(decide(obs(saved(ah_open=False, phase="ready", due_at=0))), Action.OPEN_CHAT)
    check(decide(obs(saved(ah_open=True, phase="ready", due_at=0))), Action.WAIT)
    check(decide(obs(saved(ah_open=True, phase="ready", due_at=WALL + 600))), Action.WAIT)
    check(decide(obs(saved(ah_open=True, phase="scheduled", due_at=WALL + 90))), Action.OPEN_CHAT)


def test_step_timeouts_start_at_the_observation_that_confirmed_the_step() -> None:
    # Right after TYPE_COMMAND the transaction has no start time; the next observation arms it.
    o = chat_obs("", Step.TYPED, at=0.0)
    d = decide(o)
    check(d, Action.WAIT)
    assert d.state.tx is not None and d.state.tx.at == MONO


def test_frames_stopping_in_typed_escape_after_30_s() -> None:
    old = snap(chat_active=True, chat_focused=True, chat_text="/rel", received_at=WALL - 60)
    st = LoopState(last_new_sweep_at=WALL, tx=tx(Step.TYPED, at=MONO - 29))
    stalled = obs(old, state=st, last_frame_at=WALL - 60)
    check(decide(stalled), Action.WAIT)
    late = replace(stalled, state=replace(st, tx=tx(Step.TYPED, at=MONO - 30)))
    check(decide(late), Action.PRESS_ESCAPE)
    popup = replace(late, snapshot=replace(old, blocking_ui=POPUP))
    check(decide(popup), Action.WAIT)
    unfocused = replace(late, snapshot=replace(old, chat_focused=False))
    check(decide(unfocused), Action.WAIT)


def test_wrong_target_is_retargeted_with_targetexact() -> None:
    d = decide(closed(auctioneer_name="Other Npc", in_range=False))
    check(d, Action.OPEN_CHAT)
    assert d.command == f"/targetexact {JAXON}"


def test_window_invalid_relaunches_after_30_min_with_backoff() -> None:
    o = replace(obs(), window_ok=False)
    first = decide(o)
    check(first, Action.FAULT, "window_invalid")
    assert first.state.window_bad_since == WALL
    later = replace(o, wall=WALL + 1800, state=first.state)
    later = replace(later, snapshot=replace(o.snapshot, received_at=WALL + 1799), last_frame_at=WALL + 1799)
    check(decide(later), Action.RELAUNCH, "window_invalid")
    backed = replace(later, state=replace(first.state, last_relaunch_at=WALL + 1000))
    check(decide(backed), Action.WAIT, "window_invalid")


def test_persistent_blocking_ui_relaunches_after_30_min() -> None:
    first = decide(obs(snap(blocking_ui=POPUP)))
    assert first.state.blocking_since == WALL
    o = obs(snap(blocking_ui=POPUP, received_at=WALL + 1799), state=first.state)
    o = replace(o, wall=WALL + 1800, last_frame_at=WALL + 1799)
    check(decide(o), Action.RELAUNCH, "blocking_ui")
    cleared = decide(replace(o, snapshot=snap(received_at=WALL + 1799)))
    assert cleared.state.blocking_since is None


def test_activation_cap_then_fault_then_relaunch_after_30_min() -> None:
    o = replace(closed(), window_focused=False)
    for i in range(3):
        d = decide(replace(o, wall=WALL + i))
        check(d, Action.ACTIVATE_WINDOW)
        o = replace(o, state=d.state)
    fault = decide(replace(o, wall=WALL + 3))
    check(fault, Action.FAULT, "window_activation_failed")
    assert fault.state.window_activation_failed_at == WALL + 3
    far = replace(o, state=fault.state, wall=WALL + 3 + 1800)
    far = replace(far, snapshot=replace(o.snapshot, received_at=WALL + 1802), last_frame_at=WALL + 1802)
    far = with_state(far, window_activations=(WALL + 1800,) * 3)
    check(decide(far), Action.RELAUNCH, "window_activation_failed")


def test_activations_older_than_5_min_do_not_count() -> None:
    o = with_state(replace(closed(), window_focused=False), window_activations=(WALL - 301,) * 3)
    check(decide(o), Action.ACTIVATE_WINDOW)


@pytest.mark.parametrize("text", ["/rel", "/reload", "/targetexact Auc", SET_BINDING_COMMAND[:20]])
def test_restart_escapes_our_own_leftover_command(text: str) -> None:
    check(decide(chat_obs(text, None)), Action.PRESS_ESCAPE)
    check(decide(chat_obs(text, None, focused=False)), Action.FAULT, "chat_unfocused")
    check(decide(chat_obs("/rel hello", None)), Action.FAULT, "chat_foreign_text")
    check(decide(replace(chat_obs(text, None), human_active=True)), Action.HOLD_HUMAN)


def test_durable_fields_round_trip_through_json() -> None:
    import json

    st = LoopState(
        last_relaunch_at=5.0,
        relaunch_attempts=2,
        last_ack_reload_at=6.0,
        flush_uid=S1,
        flush_boot="b",
        flush_at=7.0,
        flush_seen_at=8.0,
        ah_attempts=(1.0, 2.0),
        ah_failed_at=9.0,
        window_activations=(3.0,),
        tx=tx(Step.TYPED),
        ah_stage="binding",
    )
    data = json.loads(json.dumps(durable_fields(st)))
    back = restore(data)
    assert back == replace(
        LoopState(),
        **{k: getattr(st, k) for k in data if k not in ("ah_attempts", "window_activations")},
        ah_attempts=(1.0, 2.0),
        window_activations=(3.0,),
    )  # noqa: E501
    assert back.tx is None and back.ah_stage == ""
    with pytest.raises(ValueError):
        restore({**data, "extra": 1})
    with pytest.raises(ValueError):
        restore({k: v for k, v in data.items() if k != "flush_uid"})
