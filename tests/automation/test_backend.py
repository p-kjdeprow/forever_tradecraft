from __future__ import annotations

import copy
import hashlib
import json
import math
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from crowley_automation.backend import (
    CapturedFrame,
    DesktopBackend,
    DesktopBackendError,
    _seller_market_id_commands,
)
from crowley_automation.config import AutomationConfig
from crowley_automation.desktop_input import GuiLease
from crowley_automation.desktop_observe import WindowState
from crowley_automation.ledger import Ledger
from crowley_automation.model import (
    Batch,
    CancelAuction,
    CollectMail,
    CollectReturn,
    Interact,
    MoveRoute,
    PostCommodity,
    PostItem,
    action_object,
    canonical_json,
)
from crowley_automation.movement import RouteProfile
from crowley_automation.runner import (
    Admission,
    AutomationRunner,
    BatchAuthorization,
    RunnerConfig,
    SessionArm,
)
from crowley_automation.runtime import arm_seller_backend
from crowley_automation.targets import TargetProfile, load_profiles
from crowley_common.frames import Sample, decode_frame

from tests.automation.native_harness import NativeHarness


def config() -> AutomationConfig:
    return AutomationConfig(
        wine_prefix=str(Path.cwd() / "synthetic-wine-prefix"),
        game_root=str(Path.cwd() / "synthetic-game-root"),
        display=":0",
        window_title="World of Warcraft",
        game_executable="Wow.exe",
        game_argv0="Wow.exe",
        window_class="wow",
        product="wowf",
        product_dir="_forever_",
        character="Tester",
        realm="Realm",
        character_select_name="Tester",
        faction="Horde",
        house="us-normal-horde",
        build="1.60.1.70938",
        interface=11404,
        native_region=1,
        canvas_crop=(0, 0, 256, 256),
    )


def lua_config() -> AutomationConfig:
    return AutomationConfig(
        **{
            **config().model_dump(),
            "faction": "Alliance",
            "house": "us-normal-alliance",
            "interface": 16001,
            "native_region": 110,
        }
    )


def snapshot() -> dict[str, object]:
    return {
        "boot": "automation-test-boot",
        "wallTime": 1000,
        "sequence": 1,
        "context": {
            "character": "Tester",
            "realm": "Realm",
            "playerFaction": "Horde",
            "clientBuild": "70938",
            "interface": 11404,
            "nativeRegion": 1,
        },
        "chatEdit": {"active": False, "focused": False, "text": ""},
        "operation": {
            "batchId": "",
            "ordinal": 0,
            "stepIndex": 0,
            "expiresAt": 0,
            "action": False,
            "ready": False,
            "error": "",
            "buttonRect": {"x": 10, "y": 10, "width": 100, "height": 30},
            "receipt": {
                "sequence": 0,
                "batchId": "",
                "ordinal": 0,
                "stepIndex": 0,
                "callbackDelivered": "unknown",
                "postcondition": "unknown",
                "actionComplete": "unknown",
                "detail": "",
                "result": False,
            },
        },
        "goldCopper": 12345,
        "ownership": {"ahOwner": "automation"},
        "motion": {
            "instance": 1,
            "bindings": {"forward": "MOVEFORWARD", "left": "TURNLEFT", "right": "TURNRIGHT"},
            "dead": False,
            "inCombat": False,
            "mounted": False,
        },
    }


class FakeIO:
    def __init__(self, action: object, *, cached: bool = False) -> None:
        self.value = snapshot()
        self.action = action
        self.now = 10.0
        self.sequence = 1
        self.typed = ""
        self.commands: list[str] = []
        self.cached = cached

    def capture(self, *, timeout: float = 5.0) -> CapturedFrame:
        self.sequence += 1
        value = copy.deepcopy(self.value)
        value["sequence"] = self.sequence
        frame = SimpleNamespace(
            snapshot=SimpleNamespace(value=value),
            sequence=self.sequence,
            boot=value["boot"],
        )
        sample = SimpleNamespace(
            kind="NO_CHANGE" if self.cached else "FRESH",
            frame=frame,
            capture_monotonic=self.now,
            window_identity="101:202",
        )
        return CapturedFrame(sample, WindowState(202, 1, True, True, (1920, 1080)), (101,), False, "idle")  # type: ignore[arg-type]

    def recheck(self, captured: CapturedFrame) -> CapturedFrame:
        return captured

    def press(self, key: str, guard: Callable[[], None]) -> None:
        guard()
        if key != "Return":
            return
        chat = self.value["chatEdit"]
        if chat["active"]:
            command = self.typed
            self.commands.append(command)
            self.typed = ""
            chat.update({"active": False, "focused": False, "text": ""})
            self._apply(command)
        else:
            self.typed = ""
            chat.update({"active": True, "focused": True, "text": ""})

    def _apply(self, command: str) -> None:
        if command == "/cauto clear":
            self.value["operation"].update(  # type: ignore[union-attr]
                {"batchId": "", "ordinal": 0, "stepIndex": 0, "expiresAt": 0, "action": False, "ready": False}
            )
        if command == "/cauto seal":
            begin = next(row for row in self.commands if row.startswith("/cauto begin "))
            _command, _verb, batch, ordinal, step = begin.split()
            operation = self.value["operation"]
            operation.update(  # type: ignore[union-attr]
                {
                    "batchId": batch,
                    "ordinal": int(ordinal),
                    "stepIndex": int(step),
                    "expiresAt": 1100,
                    "action": action_object(self.action),
                    "ready": True,
                    "error": "",
                }
            )

    def type_text(self, text: str, guard: Callable[[], None]) -> None:
        for char in text:
            guard()
            self.typed += char
            self.value["chatEdit"].update({"text": self.typed})  # type: ignore[union-attr]

    def hold(self, key: str, seconds: float, guard: Callable[[], None]) -> None:
        raise AssertionError("stage must not hold movement keys")

    def pointer_position(self) -> tuple[int, int, int]:
        raise AssertionError("stage must not move the pointer")

    def emit(self, args: Sequence[str], timeout: float) -> str:
        raise AssertionError("stage must not emit a pointer action")

    def window_contains(self, child: int, parent: int) -> bool:
        return child == parent

    def permit(self, deadline: float, guard: Callable[[], None]) -> AbstractContextManager:
        raise AssertionError("stage must not acquire a click permit")

    def monotonic(self) -> float:
        return self.now

    def wall_time(self) -> float:
        return 1000.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def close(self) -> None:
        return


class _FakeClickPermit:
    def __init__(self, io: LuaBackendIO) -> None:
        self.io = io

    def precommit(self, *, deadline_monotonic: float | None = None) -> int:
        self.io.precommitted = True
        if self.io.on_precommit is not None:
            self.io.on_precommit()
        return int(self.io.now * 1_000_000_000)

    def record_press(self) -> int:
        return int(self.io.now * 1_000_000_000)


class LuaBackendIO:
    """Minimal input transport backed by real Lua-produced, schema-decoded frames."""

    def __init__(self, harness: NativeHarness) -> None:
        self.harness = harness
        self.now = 10.0
        self.wall = int(harness.state.now)
        self.x, self.y, self.window_id = 960, 540, 202
        self.emissions: list[list[str]] = []
        self.commands: list[str] = []
        self.holds: list[tuple[str, float]] = []
        self.chord_count = 0
        self.press_count = 0
        self.ownership_after_click: list[str] = []
        self.button_down = False
        self.cached = False
        self.fail_after_down = False
        self.drift_bindings_on_hold = False
        self.precommitted = False
        self.capture_hook: Callable[[], None] | None = None
        self.on_precommit: Callable[[], None] | None = None
        self.drift_target_on_chord = False
        self.latest: CapturedFrame | None = None
        self.harness.lua.globals()["__testChatActive"] = False
        self.harness.lua.globals()["__testChatFocused"] = False
        self.harness.lua.globals()["__testChatText"] = ""
        self.harness.lua.globals()["__testState"] = self.harness.state
        self.harness.lua.execute(
            "time = function() return math.floor(__testState.now) end\n"
            "ChatFrameUtil.GetActiveWindow = function()\n"
            "  if not __testChatActive then return nil end\n"
            "  return { IsShown=function() return true end,\n"
            "    HasFocus=function() return __testChatFocused end,\n"
            "    GetText=function() return __testChatText end }\n"
            "end"
        )

    def capture(self, *, timeout: float = 5.0) -> CapturedFrame:
        if self.cached and self.latest is not None:
            prior = self.latest
            sample = Sample(
                "NO_CHANGE",
                prior.sample.frame,
                prior.sample.capture_monotonic,
                prior.sample.crop,
                prior.sample.crc32,
                prior.sample.window_identity,
            )
            return CapturedFrame(sample, prior.window, prior.pids, False, "idle")
        self.now += 0.01
        self.harness.fire("PLAYER_ENTERING_WORLD")
        frame = decode_frame(self.harness.frame_bytes, expected_kind="automation")
        sample = Sample("FRESH", frame, self.now, (0, 0, 256, 256), frame.crc32, "101:202")
        captured = CapturedFrame(sample, WindowState(202, 1, True, True, (1920, 1080)), (101,), False, "idle")
        self.latest = captured
        if self.capture_hook is not None:
            self.capture_hook()
        return captured

    def recheck(self, captured: CapturedFrame) -> CapturedFrame:
        return captured

    def press(self, key: str, guard: Callable[[], None]) -> None:
        guard()
        self.press_count += 1
        if key != "Return":
            return
        globals_ = self.harness.lua.globals()
        if globals_["__testChatActive"]:
            command = globals_["__testChatText"]
            self.commands.append(command)
            globals_["__testChatActive"] = False
            globals_["__testChatFocused"] = False
            globals_["__testChatText"] = ""
            self._command(command)
        else:
            globals_["__testChatActive"] = True
            globals_["__testChatFocused"] = True
            globals_["__testChatText"] = ""

    def _command(self, command: str) -> None:
        if command.startswith("/cauto "):
            before = self.harness.state.queryOwnedCalls
            self.harness.slash(command[len("/cauto ") :])
            if self.harness.state.queryOwnedCalls > before:
                self.harness.fire("OWNED_AUCTIONS_UPDATED")
        elif command.startswith("/targetexact "):
            self.harness.state.targetPresent = True
            self.harness.state.targetName = command.removeprefix("/targetexact ")
            self.harness.fire("PLAYER_ENTERING_WORLD")

    def type_text(self, text: str, guard: Callable[[], None]) -> None:
        globals_ = self.harness.lua.globals()
        for char in text:
            guard()
            globals_["__testChatText"] = globals_["__testChatText"] + char

    def chord(self, steps: tuple[tuple[str, str], ...], guard: Callable[[], None]) -> None:
        if self.drift_target_on_chord:
            self.harness.state.targetGUID = "Creature-0-0-0-0-2-2"
            self.harness.state.targetName = "Different Target"
        guard()
        self.chord_count += 1
        self.harness.fire("AUCTION_HOUSE_SHOW")

    def hold(self, key: str, seconds: float, guard: Callable[[], None]) -> None:
        if self.drift_bindings_on_hold:
            self.harness.state.bindings["UP"] = "STRAFE"
        guard()
        self.holds.append((key, seconds))
        if key == "Up":
            self.harness.state.speed = 1.0
            self.harness.state.x += seconds * math.cos(self.harness.state.facing)
            self.harness.state.y += seconds * math.sin(self.harness.state.facing)
            self.harness.state.speed = 0.0
        self.now += seconds
        self.harness.state.uptime += seconds

    def pointer_position(self) -> tuple[int, int, int]:
        return self.x, self.y, self.window_id

    def emit(self, args: Sequence[str], timeout: float) -> str:
        values = list(args)
        self.emissions.append(values)
        index = 0
        while index < len(values):
            token = values[index]
            if token == "sleep":
                self.sleep(float(values[index + 1]))
                index += 2
            elif token == "mousemove":
                self.x, self.y = int(values[index + 1]), int(values[index + 2])
                index += 3
            elif token == "mousedown":
                self.button_down = True
                index += 2
                if self.fail_after_down:
                    self.fail_after_down = False
                    raise OSError("simulated emitter interruption after mouse down")
            elif token == "mouseup":
                was_down = self.button_down
                self.button_down = False
                index += 2
                if was_down:
                    self._hardware_click()
            else:
                index += 1
        return ""

    def _hardware_click(self) -> None:
        before = len(self.harness.native_calls)
        self.harness.hardware_click()
        if len(self.harness.native_calls) == before:
            return
        call = self.harness.native_calls[-1]
        if call["kind"] in {"post_commodity", "post_item"}:
            auction_id = 1000 + len(self.harness.native_calls)
            self.harness.add_owned(
                auction_id=auction_id,
                quantity=int(call["args"][2]),
                buyout=int(call["args"][3]),
            )
            self.harness.fire("AUCTION_HOUSE_AUCTION_CREATED", auction_id)
            self.harness.fire("OWNED_AUCTIONS_UPDATED")
        elif call["kind"] == "collect_mail":
            mail_index = int(call["args"][0])
            row = self.harness.state.inbox[mail_index]
            self.harness.state.money += int(row.money)
            count = len(self.harness.state.inbox)
            for index in range(mail_index, count):
                self.harness.state.inbox[index] = self.harness.state.inbox[index + 1]
            self.harness.state.inbox[count] = None
            self.harness.fire("MAIL_INBOX_UPDATE")
        self.ownership_after_click.append(self.harness.snapshot["ownership"]["ahOwner"])

    def window_contains(self, child: int, parent: int) -> bool:
        return child == parent == self.window_id

    @contextmanager
    def permit(self, deadline: float, guard: Callable[[], None]):
        guard()
        yield _FakeClickPermit(self)

    def monotonic(self) -> float:
        return self.now

    def wall_time(self) -> float:
        return self.wall

    def sleep(self, seconds: float) -> None:
        self.now += seconds
        self.harness.state.uptime += seconds

    def close(self) -> None:
        return


def stage_lua_action(
    action: object,
    harness: NativeHarness,
    io: LuaBackendIO,
    *,
    targets: dict[str, TargetProfile] | None = None,
    routes: dict[str, RouteProfile] | None = None,
    stop_requested: Callable[[], bool] = lambda: False,
) -> tuple[DesktopBackend, Admission, str, GuiLease]:
    lease = GuiLease(Path("lua-backend-gui-lock-fixture"))
    lease.held = True
    backend = DesktopBackend(
        lua_config(),
        {} if targets is None else targets,
        {} if routes is None else routes,
        lease,
        io=io,
        stop_requested=stop_requested,
    )
    backend.observe()
    digest = hashlib.sha256(canonical_json(action_object(action)).encode()).hexdigest()  # type: ignore[arg-type]
    backend.set_deadline(int(io.wall_time()) + 90)
    backend.stage(action, batch_id="a" * 64, ordinal=0, step_index=0, action_digest=digest)
    admission = backend.observe()
    return backend, admission, digest, lease


@pytest.mark.parametrize(
    "action",
    [
        PostCommodity("123:b2.2", 2, 500, 12, 200),
        PostItem("123:b2.2", 1, 500, 12, 200),
        CancelAuction(42, 300),
        CollectMail(2),
        Interact("target-a"),
        MoveRoute("route-a"),
    ],
)
def test_stage_publishes_exact_action_and_never_dispatches(action: object) -> None:
    import hashlib

    from crowley_automation.model import canonical_json

    io = FakeIO(action)
    lease = GuiLease(Path("test-gui-lock-fixture"))
    lease.held = True
    targets: dict[str, TargetProfile] = {}
    routes: dict[str, RouteProfile] = {}
    if isinstance(action, Interact):
        target = TargetProfile("npc", "us-normal-horde", "Tester", "70938", 1, "ah", "Auctioneer", "guid-a")
        targets[action.target_id] = target
    if isinstance(action, MoveRoute):
        routes[action.route_id] = RouteProfile(
            "us-normal-horde", "Tester", "70938", 1, 1.0, -1.0, 1.0, 0.5, ((1.0, 2.0),)
        )
    backend = DesktopBackend(config(), targets, routes, lease, io=io)
    digest = hashlib.sha256(canonical_json(action_object(action)).encode()).hexdigest()

    backend.stage(action, batch_id="a" * 64, ordinal=0, step_index=0, action_digest=digest)
    admission: Admission = backend.observe()

    assert admission.ready is True
    assert admission.staged_action_digest == digest
    assert io.commands[-1] == "/cauto seal"
    assert all(len(command[1:].encode("ascii")) + 7 <= 128 for command in io.commands)
    assert not any("mousedown" in command or "keydown" in command for command in io.commands)


def test_stale_or_cached_frame_is_refused() -> None:
    io = FakeIO(CollectMail(1), cached=True)
    lease = GuiLease(Path("test-gui-lock-fixture"))
    backend = DesktopBackend(config(), {}, {}, lease, io=io)

    with pytest.raises(DesktopBackendError, match="fresh"):
        backend.observe_native()


@pytest.mark.parametrize(
    ("item_key", "request_id", "message"),
    [
        ("123:b2.2", "a" * 32, "commodity"),
        ("123", "short", "request ID"),
    ],
)
def test_targeted_seller_query_rejects_unsupported_identity_before_input(
    item_key: str, request_id: str, message: str
) -> None:
    io = FakeIO(CollectMail(1))
    lease = GuiLease(Path("test-gui-lock-fixture"))
    lease.held = True
    backend = DesktopBackend(config(), {}, {}, lease, io=io)

    with pytest.raises(DesktopBackendError, match=message):
        backend.observe_seller_market(item_key, request_id)

    assert io.commands == []


def test_market_id_commands_preserve_more_than_thirty_two_ids_with_byte_bounded_chunks() -> None:
    managed_ids = frozenset(1_000_000_000_000_000 + index for index in range(97))

    commands = _seller_market_id_commands("a" * 32, managed_ids)
    emitted = [int(value) for command in commands for value in command.rsplit(" ", 1)[1].split(",")]

    assert len(commands) > 1
    assert emitted == sorted(managed_ids)
    assert len(emitted) == 97
    assert all(len(command[1:].encode("ascii")) + 7 <= 128 for command in commands)


def test_collect_return_stages_only_exact_fingerprinted_attachment_token() -> None:
    action = CollectReturn("123", 5, "a" * 64)

    commands = DesktopBackend._stage_commands(action, "b" * 64, 0, 0, 2_000)

    assert "/cauto kind collect_return" in commands
    assert "/cauto arg quantity 5" in commands
    assert f"/cauto arg mailFingerprint {'a' * 64}" in commands
    assert "/cauto key 1 123" in commands
    assert commands[-2:] == ("/cauto expires 2000", "/cauto seal")
    assert all(len(command[1:].encode("ascii")) + 7 <= 128 for command in commands)
    assert not any("collect_mail" in command for command in commands)


def test_native_collect_return_capacity_preflight_has_typed_refusal_without_effect() -> None:
    harness = NativeHarness()
    harness.add_mail(attachments=[(123, 5)])
    harness.fire("MAIL_SHOW")
    assert harness.slash("inspect mail 0")
    token = harness.snapshot["inspection"]["rows"][0]["attachments"][0]["selectionToken"]
    harness.state.freeBagSlotsByBag[0] = 0
    io = LuaBackendIO(harness)
    lease = GuiLease(Path("seller-native-preflight-gui-lock-fixture"))
    lease.held = True
    backend = DesktopBackend(lua_config(), {}, {}, lease, io=io)
    action = CollectReturn("123", 5, token)
    digest = hashlib.sha256(canonical_json(action_object(action)).encode()).hexdigest()
    backend.observe()
    backend.set_deadline(int(io.wall_time()) + 90)

    with pytest.raises(DesktopBackendError, match="insufficient_return_bag_slots") as refusal:
        backend.stage(
            action,
            batch_id="a" * 64,
            ordinal=0,
            step_index=0,
            action_digest=digest,
        )

    assert refusal.value.code == "insufficient_return_bag_slots"
    assert harness.snapshot["operation"]["error"] == "insufficient_return_bag_slots"
    assert harness.native_calls == []
    assert io.emissions == []
    assert lease.held


def test_seller_revoke_during_command_typing_blocks_followup_and_cleanup_input(tmp_path: Path) -> None:
    session = {"state": "active"}

    class RevokingIO(FakeIO):
        def type_text(self, text: str, guard: Callable[[], None]) -> None:
            for index, char in enumerate(text):
                guard()
                self.typed += char
                self.value["chatEdit"].update({"text": self.typed})  # type: ignore[union-attr]
                if index == 0:
                    session["state"] = "revoked"

    io = RevokingIO(CollectMail(1))
    lease = GuiLease(Path("test-gui-lock-fixture"))
    lease.held = True
    ah = TargetProfile("npc", "us-normal-horde", "Tester", "70938", 1, "ah", "Auctioneer", guid="npc-1")
    backend = DesktopBackend(config(), {"auctioneer_main": ah}, {}, lease, io=io)

    class Store:
        def status(self, session_id: str):
            assert session_id == "seller-session"
            return SimpleNamespace(state=session["state"])

    arm_seller_backend(
        backend,
        ledger=Ledger(tmp_path / "seller-revoke-test-ledger.sqlite3"),
        seller_store=Store(),
        session_id="seller-session",
        policy_expires_at=2_000,
        ah_target_id="auctioneer_main",
    )

    with pytest.raises(DesktopBackendError, match="durable stop"):
        backend._send_command("/cauto clear")
    with pytest.raises(DesktopBackendError, match="durable stop"):
        backend.clear_stage()

    assert io.typed == "/"
    assert io.commands == []
    assert backend.cleanup_state == "clear_not_confirmed"


@pytest.mark.parametrize("unknown_field", ["callbackDelivered", "postcondition", "actionComplete"])
def test_unknown_native_receipt_interlocks_without_clearing_or_typing(unknown_field: str) -> None:
    io = FakeIO(CollectMail(1))
    io.value["operation"]["receipt"].update(  # type: ignore[index,union-attr]
        {"sequence": 1, unknown_field: "unknown"}
    )
    lease = GuiLease(Path("test-gui-lock-fixture"))
    lease.held = True
    backend = DesktopBackend(config(), {}, {}, lease, io=io)

    with pytest.raises(DesktopBackendError, match="unresolved"):
        backend.stage(CollectMail(1), batch_id="a" * 64, ordinal=0, step_index=0, action_digest="b" * 64)

    backend.clear_stage()
    backend.finish_batch()
    assert io.commands == []
    assert backend.cleanup_state == "refused_unresolved_native_effect"


def test_mailbox_target_profile_id_binds_anchor_and_measured_rect(tmp_path: Path) -> None:
    profile = TargetProfile(
        "mailbox",
        "us-normal-horde",
        "Tester",
        "70938",
        1,
        "mail",
        "Mailbox",
        rect=(100, 200, 90, 30),
        anchor_x=10.0,
        anchor_y=20.0,
        max_distance=2.5,
    )
    decoded = TargetProfile.from_json(json.dumps(profile.object(), sort_keys=True, separators=(",", ":")))

    assert decoded == profile
    assert decoded.profile_id.startswith("target-")
    assert len(decoded.profile_id) == 64
    assert (
        decoded.profile_id
        != TargetProfile(
            "mailbox",
            "us-normal-horde",
            "Tester",
            "70938",
            1,
            "mail",
            "Mailbox",
            rect=(100, 200, 90, 30),
            anchor_x=10.1,
            anchor_y=20.0,
            max_distance=2.5,
        ).profile_id
    )

    target_dir = tmp_path / "targets"
    target_dir.mkdir(mode=0o700)
    profile_path = target_dir / f"{profile.profile_id}.json"
    profile_path.write_text(json.dumps(profile.object()), encoding="utf-8")
    profile_path.chmod(0o600)
    loaded, _routes = load_profiles(config(), root=tmp_path)
    configured = RunnerConfig(frozenset(loaded))
    assert profile.profile_id in configured.interaction_targets


def test_real_lua_frames_dispatch_two_posts_then_each_mail_step(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    live_config = lua_config()
    harness = NativeHarness()
    harness.add_item(count=8, commodity=True, available=8)
    harness.lua.execute(
        "local post = C_AuctionHouse.PostCommodity; "
        "local _, state = debug.getupvalue(post, 1); "
        "C_AuctionHouse.PostCommodity = function(...) "
        "local result = post(...); state.money = state.money - state.deposit; return result end"
    )
    harness.add_mail(money=300)
    harness.add_mail(sender="Second House", subject="Second settlement", money=500)
    harness.fire("AUCTION_HOUSE_SHOW")
    harness.fire("MAIL_SHOW")
    io = LuaBackendIO(harness)
    lease = GuiLease(tmp_path / "gui.lock")
    lease.held = True
    backend = DesktopBackend(live_config, {}, {}, lease, io=io)
    observed = backend.observe()
    now = int(io.wall_time())
    batch = Batch(
        live_config.house,
        live_config.character,
        now + 100,
        (
            PostCommodity("123", 1, 10, 12, 1_000),
            PostCommodity("123", 1, 11, 12, 1_000),
            CollectMail(2),
        ),
    )
    authorization = BatchAuthorization(batch.batch_id, batch.house, batch.character, now + 90)
    arm = SessionArm(observed.session_boot_id, batch.house, batch.character, now + 90)
    runner = AutomationRunner(Ledger(tmp_path / "ledger.sqlite3"), clock=lambda: io.wall_time())

    status = runner.run(
        batch,
        authorization=authorization,
        session_arm=arm,
        confirmed_batch_id=batch.batch_id,
        backend=backend,
    )

    assert status["state"] == "succeeded", status
    assert [call["kind"] for call in harness.native_calls] == [
        "post_commodity",
        "post_commodity",
        "collect_mail",
        "collect_mail",
    ]
    assert io.ownership_after_click == ["automation", "automation", "none", "none"]
    assert [row["action_complete"] for row in status["steps"]] == [True, True, False, False]


def test_real_lua_route_uses_fresh_final_sample_and_actual_key_guards() -> None:
    harness = NativeHarness()
    harness.state.facing = 0.0
    harness.fire("PLAYER_ENTERING_WORLD")
    route = RouteProfile("us-normal-alliance", "Tester", "70938", 0, -1.0, 1.0, 1.0, 0.1, ((10.4, 20.0),))
    action = MoveRoute(route.route_id)
    io = LuaBackendIO(harness)
    backend, admission, digest, _lease = stage_lua_action(action, harness, io, routes={route.route_id: route})

    receipt = backend.dispatch(
        action,
        batch_id="a" * 64,
        ordinal=0,
        step_index=0,
        action_digest=digest,
        admission=admission,
    )

    assert receipt.postcondition is True
    assert receipt.action_complete is True
    assert io.holds and all(key == "Up" and 0 < duration <= 0.25 for key, duration in io.holds)
    assert harness.snapshot["motion"]["x"] == pytest.approx(10.4)


def test_real_lua_route_refuses_duplicate_sample_and_last_moment_binding_drift() -> None:
    route = RouteProfile("us-normal-alliance", "Tester", "70938", 0, -1.0, 1.0, 1.0, 0.1, ((10.4, 20.0),))
    action = MoveRoute(route.route_id)

    harness = NativeHarness()
    harness.state.facing = 0.0
    io = LuaBackendIO(harness)
    backend, admission, digest, _lease = stage_lua_action(action, harness, io, routes={route.route_id: route})
    io.cached = True
    with pytest.raises(DesktopBackendError, match="fresh"):
        backend.dispatch(
            action,
            batch_id="a" * 64,
            ordinal=0,
            step_index=0,
            action_digest=digest,
            admission=admission,
        )
    assert io.holds == []

    harness = NativeHarness()
    harness.state.facing = 0.0
    io = LuaBackendIO(harness)
    backend, admission, digest, _lease = stage_lua_action(action, harness, io, routes={route.route_id: route})
    io.drift_bindings_on_hold = True
    with pytest.raises(DesktopBackendError, match="arrow bindings"):
        backend.dispatch(
            action,
            batch_id="a" * 64,
            ordinal=0,
            step_index=0,
            action_digest=digest,
            admission=admission,
        )
    assert io.holds == []


def test_real_lua_npc_chord_rechecks_target_guid_range_and_binding() -> None:
    harness = NativeHarness()
    io = LuaBackendIO(harness)
    config_ = lua_config()
    profile = TargetProfile(
        "npc",
        config_.house,
        config_.character,
        "70938",
        0,
        "ah",
        "Auctioneer Jaxon",
        "Creature-0-0-0-0-1-1",
    )
    action = Interact(profile.profile_id)
    backend, admission, digest, _lease = stage_lua_action(
        action, harness, io, targets={profile.profile_id: profile}
    )
    receipt = backend.dispatch(
        action,
        batch_id="a" * 64,
        ordinal=0,
        step_index=0,
        action_digest=digest,
        admission=admission,
    )
    assert receipt.postcondition is True
    assert io.chord_count == 1
    assert harness.snapshot["ahOpen"] is True

    harness = NativeHarness()
    io = LuaBackendIO(harness)
    backend, admission, digest, _lease = stage_lua_action(
        action, harness, io, targets={profile.profile_id: profile}
    )
    io.drift_target_on_chord = True
    with pytest.raises(DesktopBackendError, match="target.*changed"):
        backend.dispatch(
            action,
            batch_id="a" * 64,
            ordinal=0,
            step_index=0,
            action_digest=digest,
            admission=admission,
        )
    assert io.chord_count == 0
    assert harness.snapshot["ahOpen"] is False


@pytest.mark.parametrize("change", ["stop", "expiry"])
def test_blocking_capture_rechecks_stop_and_deadline_before_return_input(change: str) -> None:
    harness = NativeHarness()
    io = LuaBackendIO(harness)
    stop = [False]
    backend = DesktopBackend(
        lua_config(), {}, {}, GuiLease(Path("stop-gui-lock-fixture")), io=io, stop_requested=lambda: stop[0]
    )
    backend.lease.held = True
    expires = int(io.wall_time()) + 10
    backend.set_deadline(expires)

    def trip_guard() -> None:
        io.capture_hook = None
        if change == "stop":
            stop[0] = True
        else:
            io.wall = expires

    io.capture_hook = trip_guard
    reason = "durable stop" if change == "stop" else "expired"
    with pytest.raises(DesktopBackendError, match=reason):
        backend._send_command("/cauto clear")
    assert io.press_count == 0
    assert io.commands == []


def test_click_pre_admission_failure_never_releases_button_but_partial_emit_does() -> None:
    harness = NativeHarness()
    harness.add_item(count=4, commodity=True)
    harness.fire("AUCTION_HOUSE_SHOW")
    io = LuaBackendIO(harness)
    stop = [False]
    action = PostCommodity("123", 1, 10, 12, 1_000)
    backend, admission, digest, _lease = stage_lua_action(action, harness, io, stop_requested=lambda: stop[0])
    io.on_precommit = lambda: stop.__setitem__(0, True)
    with pytest.raises(DesktopBackendError, match="durable stop"):
        backend.dispatch(
            action,
            batch_id="a" * 64,
            ordinal=0,
            step_index=0,
            action_digest=digest,
            admission=admission,
        )
    assert not any(args == ["mouseup", "1"] for args in io.emissions)
    assert not any("mousedown" in args for args in io.emissions)

    stop[0] = False
    io.on_precommit = None
    io.fail_after_down = True
    with pytest.raises(OSError, match="after mouse down"):
        backend.dispatch(
            action,
            batch_id="a" * 64,
            ordinal=0,
            step_index=0,
            action_digest=digest,
            admission=admission,
        )
    assert io.button_down is False
    assert io.emissions[-1] == ["mouseup", "1"]
    assert len(harness.native_calls) == 1


def test_standalone_text_is_one_timed_job_with_one_admission_and_no_capture(monkeypatch) -> None:
    from crowley_automation.backend import DesktopIO

    io = DesktopIO.__new__(DesktopIO)
    jobs, admitted = [], []
    monkeypatch.setattr(io, "_keymap", lambda: {"slash": 61, "a": 38, "Shift_L": 50})
    monkeypatch.setattr(io, "_emit", lambda args, timeout: jobs.append((list(args), timeout)))
    io.type_text("/Aa", lambda: admitted.append(True))
    assert len(jobs) == len(admitted) == 1
    args, timeout = jobs[0]
    assert args.count("keydown") == args.count("keyup") == 4
    assert "sleep" in args and timeout >= 5
    with pytest.raises(RuntimeError):
        io.type_text("/a\n", lambda: admitted.append(True))
    assert len(jobs) == len(admitted) == 1


def test_stationary_click_keeps_all_native_gates_without_motion() -> None:
    harness = NativeHarness()
    harness.add_item(count=4, commodity=True)
    harness.fire("AUCTION_HOUSE_SHOW")
    io = LuaBackendIO(harness)
    action = PostCommodity("123", 1, 10, 12, 1_000)
    backend, admission, digest, _ = stage_lua_action(action, harness, io)
    rect = harness.snapshot["operation"]["buttonRect"]
    io.x, io.y = rect["x"] + 2, rect["y"] + 2
    backend.dispatch(
        action, batch_id="a" * 64, ordinal=0, step_index=0, action_digest=digest, admission=admission
    )
    assert not any("mousemove" in args for args in io.emissions)
    assert sum("mousedown" in args for args in io.emissions) == 1
    assert sum("mouseup" in args for args in io.emissions) == 1
    assert len(harness.native_calls) == 1


@pytest.mark.parametrize("failure", ["release", "unreaped"])
def test_desktop_close_retains_real_lease_and_sealed_tool_until_cleanup(tmp_path, failure):
    from crowley_automation.backend import DesktopIO
    from crowley_automation.desktop_input import ActError
    from crowley_automation.gui.control_child import ControlChildError

    lease = GuiLease(tmp_path / "private" / "gui.lock")
    closed = []
    io = DesktopIO.__new__(DesktopIO)

    def close():
        if failure == "unreaped" and control.child_pid is not None:
            raise ControlChildError("unreaped child")

    control = SimpleNamespace(
        child_pid=123 if failure == "unreaped" else None,
        possible_down=frozenset({"038"}),
        cleanup_failed=True,
        close=close,
    )
    io._control, io._devices = control, None
    io._tool = SimpleNamespace(close=lambda: closed.append(True))
    io.bind_input(
        lease, during=lambda: None, stopping=lambda: False, deadline=lambda: 0, preparing=lambda: None
    )
    try:
        with pytest.raises(ActError, match="retained"):
            with lease.hold():
                io.close()
        assert lease.held and lease.retained
        assert closed == []
        with pytest.raises(ActError):
            with GuiLease(lease.path, timeout=0.01).hold():
                pass
        control.child_pid = None
        control.possible_down = frozenset()
        control.cleanup_failed = False
        io.close()
        assert closed == [True]
        assert not lease.held
        with GuiLease(lease.path, timeout=0.01).hold():
            pass
    finally:
        control.child_pid = None
        control.possible_down = frozenset()
        control.cleanup_failed = False
        lease.release_retained()


def test_forward_delivery_preserves_absolute_travel_budget_during_keymap_latency(monkeypatch):
    from crowley_automation.backend import DesktopIO

    io = DesktopIO.__new__(DesktopIO)
    now = [10.0]
    requests = []

    def keymap():
        now[0] += 0.5
        return {"Up": 111}

    monkeypatch.setattr(io, "_keymap", keymap)
    monkeypatch.setattr(
        io,
        "_device_session",
        lambda: SimpleNamespace(forward=lambda *args, **kwargs: requests.append(kwargs)),
    )
    monkeypatch.setattr(io, "monotonic", lambda: now[0])
    io._calibration = {"x_to_evdev": {"111": 103}}
    io.forward(11.0, 10.0)
    assert requests == [{"feedback_deadline": 12.0, "travel_deadline": 11.0}]
