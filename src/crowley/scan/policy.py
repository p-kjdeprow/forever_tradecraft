"""Pure scan-loop policy: one observation in, one decision (and the next LoopState) out.

No I/O and no clocks: every time comes from the Observation.  The plan's policy table
(row P, then 1-13, first match wins) lives in `decide`.  Typed commands, the AH-open
sequence and relaunch/reload rate limits are state transitions carried in `LoopState`.
Row 6 (blocking UI) is evaluated before row 5 (chat) so no key is ever sent over a popup.

Snapshot field mapping (schema-6 frame, `addon/Crowley/Agent.lua` Snapshot, validated by
`session/protocol.py`; the runtime slice builds Snapshot from the decoded frame):

    boot                    boot                         (string; "native-construction:*" = not ordinary)
    native_phase/_ordinal   render.nativePhase / render.nativeOrdinal  (ordinary = "normal", 0)
    wall_time               wallTime
    character, realm        context.character / context.realm
    faction                 context.playerFaction
    zone                    context.zone
    build, interface        context.clientBuild / context.interface
    addon_version           context.addonVersion
    house_key               house.key
    plan_hash               plan.hash
    ah_open                 ahOpen
    target_present          auctioneer.targetPresent
    auctioneer_name         auctioneer.targetName        ("" when absent)
    range_available         auctioneer.rangeAvailable
    in_range                auctioneer.withinTradeRange
    binding_available       auctioneer.bindingAvailable
    binding_action          auctioneer.ctrlF12Action     ("", "INTERACTTARGET" or another action)
    interaction_mode        auctioneer.interactionMode
    blocking_ui             "" when staticPopup/gameMenu are available and hidden, else a reason
                            built by the runtime from blockingUI.*
    phase, due_at           scanner.phase / scanner.dueAt  (due_at in the same epoch seconds as wall)
    last_saved_uid          lastSaved.memoryUid
    pending_*               pending.ids / count / cap / blocked / reason
    loaded_acked_ids        loaded.ackedIds
    chat_active/_focused/_text   chatEdit.active / focused / text
    received_at             wall time the canvas frame was captured (runtime)

Presence (`human_active`) comes from the runtime (Mutter IdleMonitor); it is an input here.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from enum import StrEnum
from typing import Any

STORMWIND = "Stormwind City"
SWEEP_PHASES = frozenset({"item", "owned"})
CONSTRUCTION_PREFIX = "native-construction:"
# Copied from session/driver.py CONDITIONAL_INTERACTION_BINDING_COMMAND.
SET_BINDING_COMMAND = (
    "/run local a=GetBindingAction('CTRL-F12'); "
    "if a=='' or a=='INTERACTTARGET' then SetBinding('CTRL-F12','INTERACTTARGET') end"
)
RELOAD_COMMAND = "/reload"

STALE_FRAME_S = 10.0  # frames older than this are not acted on
NO_FRAME_S = 600.0  # rows 2 and 3
RELOAD_MARGIN_S = 90.0  # rows 8 and 9
ACK_RELOAD_GAP_S = 600.0  # row 9: at most one reload per 10 min
RELOAD_MIN_GAP_S = 60.0  # between reloads for different sweeps
FLUSH_PERSIST_S = 120.0  # after the reloaded boot is seen, the uid must be on disk
FLUSH_RETRY_S = 1800.0  # one flush attempt per uid per 30 min
NO_PROGRESS_S = 1800.0  # row 12, per stage
STUCK_RELAUNCH_S = 1800.0  # window_invalid / blocking_ui / activation failure this long
AH_ATTEMPTS = 3
AH_ATTEMPT_WINDOW_S = 600.0
AH_POST_S = 10.0  # ahOpen must follow Ctrl-F12 within this
AH_STAGE_S = 60.0  # targeting/binding must finish within this
AH_FAILED_RELAUNCH_S = 1800.0
ACTIVATIONS = 3
ACTIVATION_WINDOW_S = 300.0
STEP_WAIT_S = 5.0  # how long a typed-command step may wait for its readback
TYPED_STALL_S = 30.0  # frames stopped during TYPED: Escape after this
FRESH_BEFORE_RETURN_S = 3.0
RELAUNCH_FAST_COUNT = 3
RELAUNCH_FAST_GAP_S = 900.0
RELAUNCH_SLOW_GAP_S = 3600.0


class Action(StrEnum):
    WAIT = "wait"
    HOLD_HUMAN = "hold_human"
    RELAUNCH = "relaunch"
    FAULT = "fault"
    ACTIVATE_WINDOW = "activate_window"
    OPEN_CHAT = "open_chat"
    TYPE_COMMAND = "type_command"
    PRESS_RETURN = "press_return"
    PRESS_ESCAPE = "press_escape"
    PRESS_INTERACT = "press_interact"
    CLOSE_AH = "close_ah"


# Everything that sends a key to the game window; the window must be focused first.
INPUT_ACTIONS = frozenset(
    {
        Action.OPEN_CHAT,
        Action.TYPE_COMMAND,
        Action.PRESS_RETURN,
        Action.PRESS_ESCAPE,
        Action.PRESS_INTERACT,
        Action.CLOSE_AH,
    }
)


class Step(StrEnum):
    OPENING = "opening"  # OPEN_CHAT sent, waiting for an empty focused edit box
    TYPED = "typed"  # TYPE_COMMAND sent, waiting for the exact readback
    SUBMITTED = "submitted"  # PRESS_RETURN sent
    ABORTING = "aborting"  # PRESS_ESCAPE sent


@dataclass(frozen=True)
class Snapshot:
    boot: str
    native_phase: str
    native_ordinal: int
    wall_time: int
    received_at: float
    character: str
    realm: str
    faction: str
    zone: str
    build: str
    interface: int
    addon_version: str
    house_key: str
    plan_hash: str
    ah_open: bool
    target_present: bool
    auctioneer_name: str
    range_available: bool
    in_range: bool
    binding_available: bool
    binding_action: str
    interaction_mode: str
    blocking_ui: str
    phase: str
    due_at: float
    last_saved_uid: str
    pending_ids: frozenset[str]
    pending_count: int
    pending_cap: int
    pending_blocked: bool
    pending_reason: str
    loaded_acked_ids: frozenset[str]
    chat_active: bool
    chat_focused: bool
    chat_text: str


@dataclass(frozen=True)
class Target:
    """Expected pins, from the selected target and host config."""

    character: str
    realm: str
    faction: str
    house_key: str
    plan_hash: str
    build: str
    interface: int
    addon_version: str
    auctioneer: str


@dataclass(frozen=True)
class CommandTx:
    command: str
    purpose: str  # save_flush, ack_load, target_auctioneer, bind_interact
    step: Step
    # Monotonic time the step began, taken from the first observation after the action
    # (0.0 = not yet observed).  Timeouts run from there, not from the decision.
    at: float = 0.0
    typed_at: float = 0.0  # wall time TYPE_COMMAND was issued; readback must be newer
    reopened: bool = False


@dataclass(frozen=True)
class LoopState:
    """Rate-limit memory.  `DURABLE_FIELDS` survive a restart; the rest restart empty."""

    last_relaunch_at: float | None = None
    relaunch_attempts: int = 0
    last_ack_reload_at: float | None = None  # any reload, not only ack loads
    flush_uid: str = ""  # sweep uid whose save-flush reload was submitted
    flush_boot: str = ""  # boot at that moment
    flush_at: float | None = None
    flush_seen_at: float | None = None  # when a later boot was first observed
    ah_attempts: tuple[float, ...] = ()
    ah_failed_at: float | None = None
    last_new_sweep_at: float | None = None
    no_progress_stage: int = 0  # 0 none, 1 AH closed once
    no_progress_at: float = 0.0
    window_bad_since: float | None = None
    blocking_since: float | None = None
    window_activations: tuple[float, ...] = ()
    window_activation_failed_at: float | None = None
    # Not durable (process-local or monotonic-clock based):
    ah_stage: str = ""  # "", begun, targeting, binding, interact_sent
    ah_stage_at: float = 0.0
    ah_open_since: float | None = None
    last_seen_saved_uid: str = ""
    tx: CommandTx | None = None
    tx_cooldown_until: float = 0.0


DURABLE_FIELDS = (
    "last_relaunch_at",
    "relaunch_attempts",
    "last_ack_reload_at",
    "flush_uid",
    "flush_boot",
    "flush_at",
    "flush_seen_at",
    "ah_attempts",
    "ah_failed_at",
    "last_new_sweep_at",
    "no_progress_stage",
    "no_progress_at",
    "window_bad_since",
    "blocking_since",
    "window_activations",
    "window_activation_failed_at",
)
_TUPLE_FIELDS = frozenset({"ah_attempts", "window_activations"})


def durable_fields(state: LoopState) -> dict[str, Any]:
    """JSON-ready subset of the state for scan-loop.json."""
    out: dict[str, Any] = {}
    for name in DURABLE_FIELDS:
        value = getattr(state, name)
        out[name] = list(value) if name in _TUPLE_FIELDS else value
    return out


def restore(data: dict[str, Any]) -> LoopState:
    """Inverse of `durable_fields`; any missing or unknown key is an error."""
    if set(data) != set(DURABLE_FIELDS):
        raise ValueError(f"scan-loop state keys mismatch: {sorted(set(data) ^ set(DURABLE_FIELDS))}")
    known = {f.name for f in fields(LoopState)}
    values = {k: (tuple(v) if k in _TUPLE_FIELDS else v) for k, v in data.items() if k in known}
    return LoopState(**values)


@dataclass(frozen=True)
class Observation:
    wall: float
    mono: float
    process_alive: bool
    process_started_at: float  # ignored while no process is alive
    window_ok: bool  # exactly one visible wow.exe window at display size and origin
    window_focused: bool
    snapshot: Snapshot | None
    last_frame_at: float | None
    frames_seen_this_process: int
    disk_saved_ids: frozenset[str]
    uploaded_ids: frozenset[str]
    permanently_rejected_ids: frozenset[str]
    data_acked_ids: frozenset[str]
    companion_last_error: str
    human_active: bool
    human_reason: str
    target: Target
    state: LoopState = field(default_factory=LoopState)


@dataclass(frozen=True)
class Decision:
    action: Action
    state: LoopState
    command: str = ""  # TYPE_COMMAND text
    purpose: str = ""
    fault_code: str = ""  # reported with any action (a WAIT can carry a standing fault)
    detail: str = ""  # never chat text or frame contents
    arm_boot_change: bool = False  # PRESS_RETURN of /reload: the next boot change is ours


def allowed_commands(target: Target) -> frozenset[str]:
    return frozenset({RELOAD_COMMAND, f"/targetexact {target.auctioneer}", SET_BINDING_COMMAND})


def _fault(st: LoopState, code: str, detail: str = "") -> Decision:
    return Decision(Action.FAULT, st, fault_code=code, detail=detail)


def _wait(st: LoopState, detail: str = "", code: str = "") -> Decision:
    return Decision(Action.WAIT, st, fault_code=code, detail=detail)


def relaunch_gap(attempts: int) -> float:
    return RELAUNCH_FAST_GAP_S if attempts < RELAUNCH_FAST_COUNT else RELAUNCH_SLOW_GAP_S


def _relaunch(obs: Observation, st: LoopState, why: str, code: str = "") -> Decision:
    last = st.last_relaunch_at
    if last is not None and obs.wall - last < relaunch_gap(st.relaunch_attempts):
        return _wait(st, f"relaunch_backoff:{why}", code)
    fresh = LoopState(
        last_relaunch_at=obs.wall,
        relaunch_attempts=st.relaunch_attempts + 1,
        last_new_sweep_at=obs.wall,
    )
    return Decision(Action.RELAUNCH, fresh, fault_code=code, detail=why)


def _begin_tx(obs: Observation, st: LoopState, command: str, purpose: str) -> Decision:
    if command not in allowed_commands(obs.target):
        raise ValueError(f"command is not allowlisted: {command!r}")
    tx = CommandTx(command, purpose, Step.OPENING)
    return Decision(Action.OPEN_CHAT, replace(st, tx=tx), command=command, purpose=purpose)


def _reload_ok(snap: Snapshot, now: float) -> bool:
    """A /reload cannot abort a sweep: AH closed (the timer does nothing), the queue is
    blocked (no sweeps run), or the next timer sweep is at least 90 s away."""
    if not snap.ah_open or snap.phase == "blocked":
        return True
    return snap.phase == "scheduled" and snap.due_at - now >= RELOAD_MARGIN_S


def _pins_detail(snap: Snapshot, pins: Target) -> str:
    pairs = (
        ("character", snap.character, pins.character),
        ("realm", snap.realm, pins.realm),
        ("faction", snap.faction, pins.faction),
        ("house", snap.house_key, pins.house_key),
        ("plan_hash", snap.plan_hash, pins.plan_hash),
        ("build", snap.build, pins.build),
        ("interface", snap.interface, pins.interface),
        ("addon_version", snap.addon_version, pins.addon_version),
    )
    return ",".join(name for name, got, want in pairs if got != want)


def _track(obs: Observation, snap: Snapshot) -> LoopState:
    """Bookkeeping from a fresh frame: sweep progress, AH-open clock, fault clocks, flush."""
    st = obs.state
    now = obs.wall
    ch: dict[str, object] = {"window_bad_since": None}
    if st.last_new_sweep_at is None:
        ch["last_new_sweep_at"] = now
    if snap.last_saved_uid and snap.last_saved_uid != st.last_seen_saved_uid:
        ch.update(
            last_seen_saved_uid=snap.last_saved_uid,
            last_new_sweep_at=now,
            no_progress_stage=0,
            relaunch_attempts=0,
        )
    if snap.ah_open:
        ch.update(ah_stage="", ah_failed_at=None)
        if st.ah_open_since is None:
            ch["ah_open_since"] = now
    else:
        ch["ah_open_since"] = None
    if not snap.blocking_ui:
        ch["blocking_since"] = None
    if st.flush_uid:
        if st.flush_uid in obs.disk_saved_ids:
            ch.update(flush_uid="", flush_boot="", flush_at=None, flush_seen_at=None)
        elif snap.boot != st.flush_boot and st.flush_seen_at is None:
            ch["flush_seen_at"] = now
    return replace(st, **ch)  # type: ignore[arg-type]


def decide(obs: Observation) -> Decision:
    d = _decide(obs)
    if d.action not in INPUT_ACTIONS:
        return d
    if obs.window_focused:
        if d.state.window_activation_failed_at is None:
            return d
        return replace(d, state=replace(d.state, window_activation_failed_at=None))
    st, now = obs.state, obs.wall
    recent = tuple(t for t in st.window_activations if now - t < ACTIVATION_WINDOW_S)
    if len(recent) >= ACTIVATIONS:
        failed = st.window_activation_failed_at if st.window_activation_failed_at is not None else now
        st = replace(st, window_activations=recent, window_activation_failed_at=failed)
        if now - failed >= STUCK_RELAUNCH_S:
            return _relaunch(obs, st, "window_activation_failed", "window_activation_failed")
        return _fault(st, "window_activation_failed")
    st = replace(st, window_activations=(*recent, now))
    return Decision(Action.ACTIVATE_WINDOW, st, detail=f"before:{d.action}")


def _decide(obs: Observation) -> Decision:
    st, now, snap = obs.state, obs.wall, obs.snapshot
    if obs.human_active:  # P
        return Decision(Action.HOLD_HUMAN, st, detail=obs.human_reason)
    if not obs.process_alive:  # 1
        return _relaunch(obs, st, "no_process")
    age = None if obs.last_frame_at is None else now - obs.last_frame_at
    if obs.frames_seen_this_process > 0:
        if age is None or age >= NO_FRAME_S:  # 2
            return _relaunch(obs, st, "no_frame_after_running")
    elif now - obs.process_started_at >= NO_FRAME_S:  # 3
        return _relaunch(obs, st, "no_frames_after_launch", "no_frames_after_launch")
    if snap is None or now - snap.received_at > STALE_FRAME_S:
        return _stale(obs, st, snap)
    if not obs.window_ok:
        since = now if st.window_bad_since is None else st.window_bad_since
        st = replace(st, window_bad_since=since)
        if now - since >= STUCK_RELAUNCH_S:
            return _relaunch(obs, st, "window_invalid", "window_invalid")
        return _fault(st, "window_invalid")
    st = _track(obs, snap)
    pins = obs.target

    mismatch = _pins_detail(snap, pins)  # 4
    if mismatch:
        return _fault(st, "pin_mismatch", mismatch)
    if snap.pending_blocked and snap.pending_reason == "plan_invalid":
        return _fault(st, "plan_invalid")
    if snap.boot.startswith(CONSTRUCTION_PREFIX) or snap.native_phase != "normal" or snap.native_ordinal != 0:
        return _fault(st, "construction_marker")

    if snap.blocking_ui:  # 6, before 5: no key is ever sent over a popup
        since = now if st.blocking_since is None else st.blocking_since
        st = replace(st, blocking_since=since)
        if now - since >= STUCK_RELAUNCH_S:
            return _relaunch(obs, st, "blocking_ui", "blocking_ui")
        return _fault(st, "blocking_ui", snap.blocking_ui)
    if st.tx is not None or snap.chat_active:  # 5
        result = _chat(obs, st, snap)
        if isinstance(result, Decision):
            return result
        st = result
    if snap.phase in SWEEP_PHASES:  # 7
        return _wait(st, "sweep_running")

    since_reload = None if st.last_ack_reload_at is None else now - st.last_ack_reload_at
    uid = snap.last_saved_uid
    if uid in snap.pending_ids and uid not in obs.disk_saved_ids:  # 8
        if st.flush_uid == uid and st.flush_at is not None and now - st.flush_at < FLUSH_RETRY_S:
            if st.flush_seen_at is not None and now - st.flush_seen_at >= FLUSH_PERSIST_S:
                return _fault(st, "flush_not_persisted")
            return _wait(st, "flush_settling")
        if not _reload_ok(snap, now):
            return _wait(st, "reload_margin")
        if since_reload is not None and since_reload < RELOAD_MIN_GAP_S:
            return _wait(st, "reload_settling")
        return _start_reload(obs, st, "save_flush")
    if snap.pending_blocked:
        loadable = (snap.pending_ids & obs.data_acked_ids & obs.uploaded_ids) - snap.loaded_acked_ids
        if loadable:  # 9
            if not _reload_ok(snap, now):
                return _wait(st, "reload_margin")
            if since_reload is not None and since_reload < ACK_RELOAD_GAP_S:
                return _wait(st, "ack_reload_rate_limit")
            return _start_reload(obs, st, "ack_load")
        rejected = len(snap.pending_ids & obs.permanently_rejected_ids)  # 10
        error = "set" if obs.companion_last_error else "none"
        return _fault(st, "queue_full", f"rejected={rejected} companion_error={error}")

    if st.ah_failed_at is not None and now - st.ah_failed_at >= AH_FAILED_RELAUNCH_S:
        return _relaunch(obs, st, "ah_open_failed", "ah_open_failed")
    if st.no_progress_stage == 1 and now - st.no_progress_at >= NO_PROGRESS_S:
        return _relaunch(obs, st, "no_progress", "no_progress")

    if not snap.ah_open:  # 11
        return _open_ah(obs, st, snap)
    base = max(st.last_new_sweep_at or now, st.ah_open_since or now)  # 12
    if st.no_progress_stage == 0 and now - base >= NO_PROGRESS_S:
        st = replace(st, no_progress_stage=1, no_progress_at=now)
        return Decision(Action.CLOSE_AH, st, fault_code="no_progress", detail="close_and_reopen")
    if st.no_progress_stage == 1:
        return _wait(st, "no_progress_waiting", "no_progress")
    return _wait(st)  # 13


def _stale(obs: Observation, st: LoopState, snap: Snapshot | None) -> Decision:
    """No fresh frame.  A command stuck in TYPED for 30 s is escaped (last known box state)."""
    tx = st.tx
    if tx is None or tx.step is not Step.TYPED:
        return _wait(st, "no_fresh_frame")
    if tx.at == 0.0:
        return _wait(replace(st, tx=replace(tx, at=obs.mono)), "no_fresh_frame")
    if obs.mono - tx.at >= TYPED_STALL_S and snap is not None:
        if snap.chat_active and snap.chat_focused and not snap.blocking_ui:
            return _abort(obs, st, tx)
    return _wait(st, "no_fresh_frame")


def _start_reload(obs: Observation, st: LoopState, purpose: str) -> Decision:
    if obs.mono < st.tx_cooldown_until:
        return _wait(st, "tx_cooldown")
    return _begin_tx(obs, st, RELOAD_COMMAND, purpose)


def _chat(obs: Observation, st: LoopState, snap: Snapshot) -> Decision | LoopState:
    """Row 5 (blocking UI is already clear).  Returns a Decision, or the state with a
    finished transaction cleared."""
    tx, mono = st.tx, obs.mono
    text = snap.chat_text if snap.chat_active else ""
    if tx is None:
        if not snap.chat_active:
            return st
        if text:
            # After a restart our own half-typed command may still be in the box.  Human input
            # already held the loop (row P), so a prefix of an allowlisted command is ours.
            ours = any(c.startswith(text) for c in allowed_commands(obs.target))
            if not ours:
                return _fault(st, "chat_foreign_text", f"len={len(text)}")
        if not snap.chat_focused:
            if text:
                return _fault(st, "chat_unfocused")  # Escape here would open the Game Menu
            return st  # an unfocused empty box is harmless; OPEN_CHAT will focus it
        return Decision(Action.PRESS_ESCAPE, st, detail="chat_without_transaction")
    if tx.at == 0.0:
        tx = replace(tx, at=mono)
        st = replace(st, tx=tx)
    waited = mono - tx.at
    if snap.chat_active and text:
        if not tx.command.startswith(text):
            return _fault(st, "chat_foreign_text", f"len={len(text)}")
        if text == tx.command and tx.step is Step.TYPED:
            return _before_return(obs, st, snap, tx)
        if waited < STEP_WAIT_S:  # render lag: a prefix is still being typed or Return pending
            return _wait(st, f"tx_{tx.step}")
        return _abort(obs, st, tx)
    if snap.chat_active:  # empty edit box
        if tx.step is Step.OPENING:
            if snap.chat_focused:
                typed = replace(tx, step=Step.TYPED, at=0.0, typed_at=obs.wall)
                return Decision(
                    Action.TYPE_COMMAND, replace(st, tx=typed), command=tx.command, purpose=tx.purpose
                )
            if not tx.reopened:  # active but unfocused: reopen rather than type
                again = replace(tx, at=0.0, reopened=True)
                return Decision(Action.OPEN_CHAT, replace(st, tx=again), command=tx.command)
            if waited < STEP_WAIT_S:
                return _wait(st, "tx_opening")
            return _fault(replace(st, tx=None, tx_cooldown_until=mono + 30), "chat_open_failed")
        if waited < STEP_WAIT_S:
            return _wait(st, f"tx_{tx.step}")
        return _abort(obs, st, tx)  # readback never showed the command
    # chat closed
    if tx.step is Step.OPENING:
        if waited < STEP_WAIT_S:
            return _wait(st, "tx_opening")
        return _fault(replace(st, tx=None, tx_cooldown_until=mono + 30), "chat_open_failed")
    if tx.step is Step.TYPED:
        if snap.received_at <= tx.typed_at and waited < STEP_WAIT_S:
            return _wait(st, "tx_typed_readback")
        return _fault(replace(st, tx=None, tx_cooldown_until=mono + 30), "chat_closed_unexpectedly")
    return replace(st, tx=None)  # submitted or aborted: transaction over


def _abort(obs: Observation, st: LoopState, tx: CommandTx) -> Decision:
    """Escape our own transaction.  Never Escape an unfocused box (it opens the Game Menu)."""
    snap = obs.snapshot
    if snap is not None and snap.chat_active and not snap.chat_focused:
        return _fault(replace(st, tx=None, tx_cooldown_until=obs.mono + 30), "chat_unfocused")
    aborting = replace(tx, step=Step.ABORTING, at=0.0)
    state = replace(st, tx=aborting, tx_cooldown_until=obs.mono + 30)
    return Decision(Action.PRESS_ESCAPE, state, purpose=tx.purpose, detail="tx_mismatch")


def _before_return(obs: Observation, st: LoopState, snap: Snapshot, tx: CommandTx) -> Decision:
    """Readback matched.  Re-check frame age, box focus, idleness (reload) first.

    Presence is row P of this same observation (HOLD_HUMAN wins before we get here) and
    window focus is enforced by `decide`; the runtime must take this observation after
    the readback frame.
    """
    if snap.received_at <= tx.typed_at:
        return _wait(st, "tx_typed_readback")
    if obs.wall - snap.received_at > FRESH_BEFORE_RETURN_S:
        return _wait(st, "frame_not_fresh")
    if not snap.chat_focused:
        if obs.mono - tx.at < STEP_WAIT_S:
            return _wait(st, "chat_unfocused")
        return _fault(replace(st, tx=None, tx_cooldown_until=obs.mono + 30), "chat_unfocused")
    reload = tx.command == RELOAD_COMMAND
    if reload and (snap.phase in SWEEP_PHASES or not _reload_ok(snap, obs.wall)):
        return _abort(obs, st, tx)
    if not reload and snap.ah_open:
        return _abort(obs, st, tx)
    submitted = replace(tx, step=Step.SUBMITTED, at=0.0)
    st = replace(st, tx=submitted)
    if reload:
        st = replace(st, last_ack_reload_at=obs.wall)
        if tx.purpose == "save_flush":
            st = replace(
                st,
                flush_uid=snap.last_saved_uid,
                flush_boot=snap.boot,
                flush_at=obs.wall,
                flush_seen_at=None,
            )
    return Decision(Action.PRESS_RETURN, st, command=tx.command, purpose=tx.purpose, arm_boot_change=reload)


def _open_ah(obs: Observation, st: LoopState, snap: Snapshot) -> Decision:
    """Row 11: bounded AH-open transaction (preconditions, steps, postcondition, retry cap)."""
    now, pins = obs.wall, obs.target
    if snap.zone != STORMWIND:
        return _fault(st, "not_in_stormwind", snap.zone)
    exact = snap.target_present and snap.auctioneer_name == pins.auctioneer
    if exact and not (snap.range_available and snap.in_range):
        return _fault(st, "auctioneer_out_of_range")
    if snap.interaction_mode not in ("keyboard", "any"):
        return _fault(st, "interaction_mode_unavailable", snap.interaction_mode)
    if not snap.binding_available:
        return _fault(st, "binding_unavailable")
    if snap.binding_action not in ("", "INTERACTTARGET"):
        return _fault(st, "binding_conflict")

    if st.ah_stage == "interact_sent" and now - st.ah_stage_at >= AH_POST_S:
        st = replace(st, ah_stage="")  # postcondition missed: that attempt failed
    elif st.ah_stage not in ("", "interact_sent") and now - st.ah_stage_at >= AH_STAGE_S:
        st = replace(st, ah_stage="")
    if st.ah_stage == "interact_sent":
        return _wait(st, "ah_open_postcondition")
    if st.ah_stage == "":
        recent = tuple(t for t in st.ah_attempts if now - t < AH_ATTEMPT_WINDOW_S)
        if len(recent) >= AH_ATTEMPTS:
            failed_at = st.ah_failed_at if st.ah_failed_at is not None else now
            return _fault(replace(st, ah_attempts=recent, ah_failed_at=failed_at), "ah_open_failed")
        st = replace(st, ah_attempts=(*recent, now), ah_stage="begun", ah_stage_at=now)
    if obs.mono < st.tx_cooldown_until:
        return _wait(st, "tx_cooldown")

    def stage(name: str) -> LoopState:
        return replace(st, ah_stage=name, ah_stage_at=now)

    if not exact:  # absent or some other target: /targetexact replaces it
        if st.ah_stage != "begun":
            return _wait(st, "waiting_for_target")
        return _begin_tx(obs, stage("targeting"), f"/targetexact {pins.auctioneer}", "target_auctioneer")
    if snap.binding_action == "":
        if st.ah_stage == "binding":
            return _wait(st, "waiting_for_binding")
        return _begin_tx(obs, stage("binding"), SET_BINDING_COMMAND, "bind_interact")
    return Decision(Action.PRESS_INTERACT, stage("interact_sent"), purpose="open_ah")
