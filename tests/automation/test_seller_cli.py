from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from crowley_automation.cli import _parser, main
from crowley_automation.ledger import Ledger
from crowley_automation.seller import SELLER_POLICY_EXAMPLE, InventoryFacts, SellerPolicy
from crowley_automation.seller_native import NativeObservation, OwnedAuctionFact, ReturnFact, StockFact
from crowley_automation.seller_store import SellerStore


def policy_file(tmp_path: Path) -> Path:
    path = tmp_path / "seller-policy.json"
    path.write_text(json.dumps(SELLER_POLICY_EXAMPLE), encoding="utf-8")
    return path


def test_seller_inspect_prints_full_canonical_policy_digest(tmp_path: Path, capsys) -> None:
    path = policy_file(tmp_path)
    policy = SellerPolicy.from_json(path.read_bytes())

    assert main(["seller", "inspect", str(path)]) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["canonical"] == policy.object()
    assert inspected["canonicalJson"] == policy.canonical
    assert inspected["policyId"] == policy.policy_id
    assert len(inspected["policyId"]) == 64
    assert inspected["liveQualification"] == "unverified"


def test_seller_start_requires_policy_digest_and_exact_boot_arm(tmp_path: Path, capsys) -> None:
    path = policy_file(tmp_path)

    with pytest.raises(SystemExit) as missing_digest:
        main(["seller", "start", str(path), "--arm", "boot-a"])
    assert missing_digest.value.code == 2
    assert "confirm-policy-id" in json.loads(capsys.readouterr().err)["error"]

    with pytest.raises(SystemExit) as missing_boot:
        main(["seller", "start", str(path), "--confirm-policy-id", "a" * 64])
    assert missing_boot.value.code == 2
    assert "--arm" in json.loads(capsys.readouterr().err)["error"]


def test_seller_start_rejects_digest_that_does_not_match_policy_before_runtime(
    tmp_path: Path, capsys
) -> None:
    path = policy_file(tmp_path)

    assert (
        main(
            [
                "seller",
                "start",
                str(path),
                "--confirm-policy-id",
                "0" * 64,
                "--arm",
                "observed-boot",
            ]
        )
        == 2
    )
    error = json.loads(capsys.readouterr().err)
    assert "full immutable policy SHA256" in error["error"]


def test_seller_resume_requires_exact_digest_and_boot_without_new_policy_path() -> None:
    args = _parser().parse_args(
        [
            "seller",
            "start",
            "--resume",
            "session-a",
            "--confirm-policy-id",
            "a" * 64,
            "--arm",
            "boot-a",
        ]
    )

    assert args.policy is None
    assert args.resume == "session-a"
    assert args.confirm_policy_id == "a" * 64
    assert args.arm == "boot-a"


def test_seller_resume_rearms_only_the_stored_policy_on_the_exact_observed_boot(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from crowley_automation import runtime, seller_loop, seller_native

    policy = SellerPolicy.from_json(json.dumps(SELLER_POLICY_EXAMPLE))
    ledger = Ledger(tmp_path / "seller-ledger.sqlite3")
    store = SellerStore(ledger)
    old_boot = "old-seller-boot"
    current_boot = "current-seller-boot"
    now = 1_000
    initial = tuple(
        InventoryFacts(item.item_key, "inventory:old:1-1", old_boot, 1, now, True, item.quantity)
        for item in policy.items
    )
    session_id = store.start(
        policy,
        boot_id=old_boot,
        client_build=policy.client_build,
        confirmed_policy_id=policy.policy_id,
        initial_inventory=initial,
        now=now,
    )
    store.revoke(session_id, "test operator revoke", now=now + 1)

    ah = SimpleNamespace(
        kind="npc",
        expected_ui="ah",
        house=policy.house,
        character=policy.character,
        client_build=policy.client_build,
        instance_id=1,
        guid="NPC-1",
        name="Auctioneer Jaxon",
    )
    mailbox = SimpleNamespace(
        kind="mailbox",
        expected_ui="mail",
        house=policy.house,
        character=policy.character,
        client_build=policy.client_build,
    )
    route = SimpleNamespace(
        house=policy.house,
        character=policy.character,
        client_build=policy.client_build,
    )

    class FakeBackend:
        config = SimpleNamespace(
            house=policy.house,
            character=policy.character,
            build=f"1.60.1.{policy.client_build}",
        )
        targets = {policy.ah_target_id: ah, policy.mailbox_target_id: mailbox}
        routes = {policy.outbound_route_id: route, policy.return_route_id: route}

        def __init__(self) -> None:
            self.io = SimpleNamespace(wall_time=lambda: now + 2)
            self.bound_target: str | None = None
            self.deadline = 0

        def observe_native(self):
            return {
                "boot": current_boot,
                "context": {"character": policy.character, "clientBuild": policy.client_build},
                "ahOpen": True,
                "motion": {
                    "instance": 1,
                    "target": {
                        "present": True,
                        "guid": "NPC-1",
                        "name": "Auctioneer Jaxon",
                        "rangeAvailable": True,
                        "withinRange": True,
                    },
                },
                "auctioneer": {
                    "targetPresent": True,
                    "targetName": "Auctioneer Jaxon",
                    "rangeAvailable": True,
                    "withinTradeRange": True,
                },
            }

        def bind_seller_ah_target(self, target_id: str) -> None:
            self.bound_target = target_id

        def set_deadline(self, expires_at: int) -> None:
            self.deadline = expires_at

    backend = FakeBackend()
    item = policy.items[0]
    inventory = NativeObservation(
        "inventory:new:2-2",
        current_boot,
        2,
        now + 2,
        True,
        (
            StockFact(
                item.item_key,
                f"|Hitem:{item.item_key}:0|h[test]|h",
                item.quantity,
                0,
                1,
                False,
                False,
                True,
            ),
        ),
    )
    owned = NativeObservation[OwnedAuctionFact]("owned:new:3-3", current_boot, 3, now + 2, True, ())

    class FakeNative:
        def __init__(self, _backend) -> None:
            pass

        def inventory(self):
            return inventory

        def owned(self):
            return owned

    class FakeLoop:
        def __init__(self, selected_policy, selected_session, **_kwargs) -> None:
            assert selected_policy.policy_id == policy.policy_id
            assert selected_session == session_id

        def run(self):
            return {"driverState": "stopped", "sessionId": session_id}

    @contextmanager
    def fake_runtime(**_kwargs):
        yield ledger, object(), backend, object()

    monkeypatch.setattr(runtime, "seller_runtime", fake_runtime)
    monkeypatch.setattr(seller_native, "SellerNative", FakeNative)
    monkeypatch.setattr(seller_loop, "SellerLoop", FakeLoop)

    command = [
        "seller",
        "start",
        "--resume",
        session_id,
        "--confirm-policy-id",
        policy.policy_id,
        "--arm",
        "wrong-boot",
        "--ledger",
        str(ledger.path),
    ]
    assert main(command) == 2
    assert "--arm must equal" in json.loads(capsys.readouterr().err)["error"]
    assert store.status(session_id, now=now + 2).state == "revoked"

    command[command.index("wrong-boot")] = current_boot
    assert main(command) == 0

    output = capsys.readouterr().out
    assert '"event":"seller_resumed"' in output
    assert f'"sessionId":"{session_id}"' in output
    resumed = store.status(session_id, now=now + 2)
    assert resumed.session_id == session_id
    assert resumed.boot_id == current_boot
    assert resumed.policy_id == policy.policy_id


@pytest.mark.parametrize(("returning_quantity", "bag_quantity"), [(5, 0), (0, 5)])
def test_seller_resume_at_mailbox_uses_fresh_measured_mail_facts_without_owned_query(
    tmp_path: Path, monkeypatch, capsys, returning_quantity: int, bag_quantity: int
) -> None:
    from crowley_automation import runtime, seller_native, seller_store
    from crowley_automation.seller import ReturnObservationFacts

    policy = SellerPolicy.from_json(json.dumps(SELLER_POLICY_EXAMPLE))
    item = policy.items[0]
    session_id = "paused-mailbox-session"
    current_boot = "mailbox-resume-boot"
    now = 1_000
    ledger = Ledger(tmp_path / "mailbox-resume-ledger.sqlite3")
    ah = SimpleNamespace(
        kind="npc",
        expected_ui="ah",
        house=policy.house,
        character=policy.character,
        client_build=policy.client_build,
    )
    mailbox = SimpleNamespace(
        kind="mailbox",
        expected_ui="mail",
        house=policy.house,
        character=policy.character,
        client_build=policy.client_build,
        instance_id=4,
        anchor_x=10.0,
        anchor_y=20.0,
        max_distance=3.0,
    )
    route = SimpleNamespace(
        house=policy.house,
        character=policy.character,
        client_build=policy.client_build,
    )

    class FakeBackend:
        config = SimpleNamespace(
            house=policy.house,
            character=policy.character,
            build=f"1.60.1.{policy.client_build}",
        )
        targets = {policy.ah_target_id: ah, policy.mailbox_target_id: mailbox}
        routes = {policy.outbound_route_id: route, policy.return_route_id: route}

        def __init__(self) -> None:
            self.io = SimpleNamespace(wall_time=lambda: now)
            self.deadline = 0

        def observe_native(self):
            return {
                "boot": current_boot,
                "context": {"character": policy.character, "clientBuild": policy.client_build},
                "ahOpen": False,
                "mailOpen": True,
                "motion": {
                    "instance": 4,
                    "moving": False,
                    "dead": False,
                    "inCombat": False,
                    "x": 11.0,
                    "y": 21.0,
                    "target": {
                        "present": False,
                        "guid": None,
                        "name": None,
                        "rangeAvailable": False,
                        "withinRange": False,
                    },
                },
                "auctioneer": {
                    "targetPresent": False,
                    "targetName": None,
                    "rangeAvailable": False,
                    "withinTradeRange": False,
                },
            }

        def bind_seller_ah_target(self, _target_id: str) -> None:
            return None

        def set_deadline(self, expires_at: int) -> None:
            self.deadline = expires_at

    inventory = NativeObservation(
        "inventory:mailbox:2-2",
        current_boot,
        2,
        now,
        True,
        (
            StockFact(
                item.item_key,
                f"|Hitem:{item.item_key}:0|h[test]|h",
                bag_quantity,
                0,
                1,
                False,
                False,
                True,
            ),
        ),
    )
    token = "b" * 64
    returns = NativeObservation(
        "returns:mailbox:3-3",
        current_boot,
        3,
        now,
        True,
        (ReturnFact(token, item.item_key, 5, True, "eligible exact return"),),
    )

    class FakeNative:
        def __init__(self, _backend) -> None:
            pass

        def inventory(self):
            return inventory

        def returns(self, selected_item_key: str):
            assert selected_item_key == item.item_key
            return returns

        def owned(self):
            raise AssertionError("mailbox resume must not query owned auctions away from the AH")

    captured: dict[str, object] = {}

    class FakeStore:
        def __init__(self, _ledger) -> None:
            pass

        def policy(self, selected_session: str):
            assert selected_session == session_id
            return policy

        def item_progress(self, selected_session: str, selected_item: str):
            assert selected_session == session_id and selected_item == item.item_key
            return SimpleNamespace(
                recycling=True,
                managed_active_quantity=0,
                returning_quantity=returning_quantity,
                bag_quantity=bag_quantity,
            )

        def resume_at_mailbox(self, selected_session: str, **kwargs):
            assert selected_session == session_id
            captured.update(kwargs)
            return session_id

    class FakeLoop:
        def __init__(self, _policy, selected_session, **kwargs) -> None:
            assert selected_session == session_id
            captured["at_mailbox_item_key"] = kwargs["at_mailbox_item_key"]

        def run(self):
            return {"driverState": "paused", "sessionId": session_id}

    backend = FakeBackend()

    @contextmanager
    def fake_runtime(**_kwargs):
        yield ledger, object(), backend, object()

    monkeypatch.setattr(runtime, "seller_runtime", fake_runtime)
    monkeypatch.setattr(seller_native, "SellerNative", FakeNative)
    monkeypatch.setattr(seller_store, "SellerStore", FakeStore)
    monkeypatch.setattr("crowley_automation.seller_loop.SellerLoop", FakeLoop)

    command = [
        "seller",
        "start",
        "--resume",
        session_id,
        "--confirm-policy-id",
        policy.policy_id,
        "--arm",
        current_boot,
        "--ledger",
        str(ledger.path),
    ]
    assert main(command) == 0
    output = capsys.readouterr().out
    assert '"event":"seller_resumed"' in output
    assert captured["at_mailbox_item_key"] == item.item_key
    assert captured["boot_id"] == current_boot
    mail_facts = captured["mail_observations"]
    assert isinstance(mail_facts, tuple) and len(mail_facts) == 1
    assert isinstance(mail_facts[0], ReturnObservationFacts)
    assert mail_facts[0].candidates[0].mail_fingerprint == token
    assert mail_facts[0].candidates[0].eligible is (returning_quantity > 0)
    if returning_quantity == 0:
        assert "outstanding admitted stock" in mail_facts[0].candidates[0].reason


def test_seller_release_expired_is_read_only_armed_and_uses_fresh_ah_stock_proofs(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from crowley_automation import runtime, seller_native

    policy = SellerPolicy.from_json(json.dumps(SELLER_POLICY_EXAMPLE))
    ledger = Ledger(tmp_path / "expired-release-ledger.sqlite3")
    store = SellerStore(ledger)
    old_boot = "expired-release-old-boot"
    current_boot = "expired-release-current-boot"
    start_time = policy.expires_at - 10
    release_time = policy.expires_at + 2
    initial = tuple(
        InventoryFacts(item.item_key, "inventory:old:1-1", old_boot, 1, start_time, True, item.quantity)
        for item in policy.items
    )
    session_id = store.start(
        policy,
        boot_id=old_boot,
        client_build=policy.client_build,
        confirmed_policy_id=policy.policy_id,
        initial_inventory=initial,
        now=start_time,
    )
    store.revoke(session_id, "test expire bag-only grant", now=start_time + 1)

    ah = SimpleNamespace(
        kind="npc",
        expected_ui="ah",
        house=policy.house,
        character=policy.character,
        client_build=policy.client_build,
        instance_id=1,
        guid="NPC-1",
        name="Auctioneer Jaxon",
    )
    mailbox = SimpleNamespace(
        kind="mailbox",
        expected_ui="mail",
        house=policy.house,
        character=policy.character,
        client_build=policy.client_build,
    )
    route = SimpleNamespace(
        house=policy.house,
        character=policy.character,
        client_build=policy.client_build,
    )

    class FakeBackend:
        config = SimpleNamespace(
            house=policy.house,
            character=policy.character,
            build=f"1.60.1.{policy.client_build}",
        )
        targets = {policy.ah_target_id: ah, policy.mailbox_target_id: mailbox}
        routes = {policy.outbound_route_id: route, policy.return_route_id: route}

        def __init__(self) -> None:
            self.io = SimpleNamespace(wall_time=lambda: release_time)
            self.deadlines: list[int] = []
            self.bound_target: str | None = None

        def observe_native(self):
            return {
                "boot": current_boot,
                "context": {"character": policy.character, "clientBuild": policy.client_build},
                "ahOpen": True,
                "motion": {
                    "instance": 1,
                    "target": {
                        "present": True,
                        "guid": ah.guid,
                        "name": ah.name,
                        "rangeAvailable": True,
                        "withinRange": True,
                    },
                },
                "auctioneer": {
                    "targetPresent": True,
                    "targetName": ah.name,
                    "rangeAvailable": True,
                    "withinTradeRange": True,
                },
            }

        def bind_seller_ah_target(self, target_id: str) -> None:
            self.bound_target = target_id

        def set_deadline(self, expires_at: int) -> None:
            self.deadlines.append(expires_at)

    item = policy.items[0]
    inventory = NativeObservation(
        "inventory:release:2-2",
        current_boot,
        2,
        release_time,
        True,
        (
            StockFact(
                item.item_key,
                f"|Hitem:{item.item_key}:0|h[test]|h",
                item.quantity,
                0,
                1,
                False,
                False,
                True,
            ),
        ),
    )
    owned = NativeObservation[OwnedAuctionFact]("owned:release:3-3", current_boot, 3, release_time, True, ())
    read_counts = {"inventory": 0, "owned": 0}

    class FakeNative:
        def __init__(self, _backend) -> None:
            pass

        def inventory(self):
            read_counts["inventory"] += 1
            return inventory

        def owned(self):
            read_counts["owned"] += 1
            return owned

    class NeverRunRunner:
        def run_seller_child(self, *_args, **_kwargs):
            raise AssertionError("expired stock release must not run a protected child")

    backend = FakeBackend()

    @contextmanager
    def fake_runtime(**_kwargs):
        yield ledger, NeverRunRunner(), backend, object()

    monkeypatch.setattr(runtime, "seller_runtime", fake_runtime)
    monkeypatch.setattr(seller_native, "SellerNative", FakeNative)

    base = [
        "seller",
        "release-expired",
        session_id,
        "--confirm-policy-id",
        policy.policy_id,
        "--arm",
        current_boot,
        "--ledger",
        str(ledger.path),
    ]
    wrong_digest = list(base)
    wrong_digest[wrong_digest.index(policy.policy_id)] = "0" * 64
    assert main(wrong_digest) == 2
    assert "full immutable policy SHA256" in json.loads(capsys.readouterr().err)["error"]
    assert store.status(session_id, now=release_time).state == "revoked"

    wrong_boot = list(base)
    wrong_boot[wrong_boot.index(current_boot)] = "unobserved-boot"
    assert main(wrong_boot) == 2
    assert "--arm must equal" in json.loads(capsys.readouterr().err)["error"]
    assert store.status(session_id, now=release_time).state == "revoked"
    assert read_counts == {"inventory": 0, "owned": 0}

    assert main(base) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["event"] == "seller_expired_stock_released"
    assert output["sessionId"] == session_id
    assert output["session"]["state"] == "completed"
    assert backend.bound_target == policy.ah_target_id
    assert backend.deadlines == [release_time + 120]
    assert read_counts == {"inventory": 1, "owned": 1}
    assert store.item_progress(session_id, item.item_key).bag_quantity == item.quantity


def test_seller_release_expired_help_requires_full_digest_and_boot_arm(capsys) -> None:
    with pytest.raises(SystemExit) as result:
        main(["seller", "release-expired", "session-a", "--help"])
    assert result.value.code == 0
    output = capsys.readouterr().out
    assert "--confirm-policy-id" in output
    assert "--arm" in output
