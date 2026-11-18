"""Bounded JSON interface for local automation contracts and operation state."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from .backend import DesktopBackend
from .config import AutomationConfigError, load_config
from .desktop_input import GuiLease, gui_lock_path
from .ledger import Ledger, LedgerError
from .model import MAX_BATCH_BYTES, Batch, BatchError
from .runner import AdmissionDenied, BatchAuthorization, SessionArm
from .runtime import default_ledger_path, dry_run_batch, run_batch
from .targets import load_profiles

LIVE_QUALIFICATION = "unverified"


class _JsonParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        print(json.dumps({"error": message[:240]}, sort_keys=True), file=sys.stderr)
        raise SystemExit(2)


def _read_batch(path: Path) -> Batch:
    with path.open("rb") as stream:
        source = stream.read(MAX_BATCH_BYTES + 1)
    return Batch.from_json(source)


def _read_seller_policy(path: Path):
    from .seller import MAX_SELLER_POLICY_BYTES, SellerPolicy

    with path.open("rb") as stream:
        source = stream.read(MAX_SELLER_POLICY_BYTES + 1)
    if len(source) > MAX_SELLER_POLICY_BYTES:
        raise ValueError(f"seller policy exceeds {MAX_SELLER_POLICY_BYTES} bytes")
    return SellerPolicy.from_json(source)


def _seller_owned_facts_for_resume(store: Any, session_id: str, item_key: str, observation: Any):
    """Project only previously managed owned auctions into the resume proof."""
    from .seller import OwnedAuction, OwnedFacts

    stored = {row.auction_id: row for row in store.managed_auctions(session_id, item_key)}
    auctions = []
    for row in observation.facts:
        if row.auction_id not in stored:
            continue
        if row.item_key != item_key or row.status not in {"active", "sold"}:
            raise AdmissionDenied("resume owned facts changed an exact managed auction identity")
        prior = stored[row.auction_id]
        if row.status == "sold":
            unit_price = prior.unit_price_copper
        else:
            if (
                not row.buyout_known
                or row.quantity < 1
                or row.buyout_copper < 1
                or row.buyout_copper % row.quantity
            ):
                raise AdmissionDenied("resume owned facts do not prove an exact managed unit price")
            unit_price = row.buyout_copper // row.quantity
        auctions.append(
            OwnedAuction(
                row.auction_id,
                row.item_key,
                row.quantity,
                unit_price,
                row.status,
                row.cancel_allowed,
                row.cancel_cost_known,
                row.cancel_cost_copper if row.cancel_cost_known else 0,
            )
        )
    auctions.sort(key=lambda row: row.auction_id)
    return OwnedFacts(
        item_key,
        observation.request_id,
        observation.boot,
        observation.sequence,
        observation.captured_at,
        observation.complete,
        tuple(auctions),
    )


def _seller_command(args: argparse.Namespace) -> dict[str, object]:
    if args.seller_command == "inspect":
        policy = _read_seller_policy(args.policy)
        return {
            "canonical": policy.object(),
            "canonicalJson": policy.canonical,
            "liveQualification": LIVE_QUALIFICATION,
            "policyId": policy.policy_id,
            "runtimeImplemented": True,
            "schema": "crowley.automation-seller-policy.v1",
        }
    if args.seller_command in {"preview", "start"}:
        return _seller_live_command(args)
    from .seller_store import SellerStore

    if args.seller_command == "release-expired":
        return _seller_release_expired(args)
    store = SellerStore(Ledger(args.ledger))
    if args.seller_command == "status":
        return store.report(args.session_id)
    if args.seller_command == "revoke":
        store.revoke(args.session_id, "operator_revoke", now=int(time.time()))
        return store.report(args.session_id)
    raise ValueError("unsupported seller command")


def _seller_release_expired(args: argparse.Namespace) -> dict[str, object]:
    """Release only measured bag stock from an expired grant, without running children."""
    from .runtime import seller_runtime
    from .seller_loop import inventory_facts_for_policy, validate_seller_runtime
    from .seller_native import SellerNative
    from .seller_store import SellerStore

    with seller_runtime(ledger_path=args.ledger) as (ledger, _runner, backend, _lease):
        if ledger.stop_requested():
            raise AdmissionDenied("durable automation stop blocks expired stock release")
        if ledger.has_uncertain_effect():
            raise AdmissionDenied("an unresolved automation effect blocks expired stock release")
        store = SellerStore(ledger)
        policy = store.policy(args.session_id)
        if args.confirm_policy_id != policy.policy_id:
            raise AdmissionDenied("--confirm-policy-id must match the full immutable policy SHA256")
        current = backend.observe_native()
        validate_seller_runtime(policy, backend.config, current, backend)
        if args.arm != current["boot"]:
            raise AdmissionDenied("--arm must equal the exact currently observed client boot ID")
        now = int(backend.io.wall_time())
        if now < policy.expires_at:
            raise AdmissionDenied("expired stock release is available only after policy expiry")

        # Read-only inventory and ownership snapshots get a short independent deadline.
        # The expired seller policy is never renewed and no protected child is constructed.
        backend.set_deadline(now + 120)
        native = SellerNative(backend)
        inventory_observation = native.inventory()
        owned_observation = native.owned()
        if inventory_observation.boot != current["boot"] or owned_observation.boot != current["boot"]:
            raise AdmissionDenied("expired release facts belong to another client boot")
        inventories = inventory_facts_for_policy(policy, inventory_observation)
        owned_facts = tuple(
            _seller_owned_facts_for_resume(store, args.session_id, item.item_key, owned_observation)
            for item in policy.items
        )
        session_id = store.release_expired_stock(
            args.session_id,
            confirmed_policy_id=args.confirm_policy_id,
            boot_id=current["boot"],
            client_build=current["context"]["clientBuild"],
            inventories=inventories,
            owned_facts=owned_facts,
            now=int(backend.io.wall_time()),
        )
        return {
            "event": "seller_expired_stock_released",
            "liveQualification": LIVE_QUALIFICATION,
            "policyId": policy.policy_id,
            "session": store.report(session_id, now=int(backend.io.wall_time()))["session"],
            "sessionId": session_id,
        }


def _seller_live_command(args: argparse.Namespace) -> dict[str, object]:
    import secrets

    from .runtime import seller_runtime
    from .seller import MarketFacts, ReturnCandidate, ReturnObservationFacts, StockFacts, decide
    from .seller_loop import (
        SellerLoop,
        inventory_facts_for_policy,
        validate_seller_mailbox_runtime,
        validate_seller_runtime,
    )
    from .seller_native import SellerNative
    from .seller_store import SellerStore

    if args.seller_command == "start" and args.resume and args.policy is not None:
        raise AdmissionDenied("--resume uses the immutable stored policy; omit the policy path")
    if args.seller_command == "start" and not args.resume and args.policy is None:
        raise AdmissionDenied("seller start requires a policy path unless --resume is selected")
    if args.seller_command == "start" and args.resume:
        policy = None
    else:
        if not isinstance(args.policy, Path):
            raise AdmissionDenied("seller preview/start requires a policy path")
        policy = _read_seller_policy(args.policy)
    if args.seller_command == "start" and policy is not None and args.confirm_policy_id != policy.policy_id:
        raise AdmissionDenied("--confirm-policy-id must match the full immutable policy SHA256")

    with seller_runtime(ledger_path=args.ledger) as (ledger, runner, backend, _lease):
        if ledger.stop_requested():
            raise AdmissionDenied("durable automation stop blocks seller observation and start")
        if ledger.has_uncertain_effect():
            raise AdmissionDenied("an unresolved automation effect blocks seller observation and start")
        store = SellerStore(ledger)
        if args.seller_command == "start" and args.resume:
            policy = store.policy(args.resume)
        assert policy is not None
        if args.seller_command == "start" and args.confirm_policy_id != policy.policy_id:
            raise AdmissionDenied("--confirm-policy-id must match the full immutable policy SHA256")
        current = backend.observe_native()
        mailbox_resume_item: str | None = None
        if args.seller_command == "start" and args.resume:
            pending_returns = [
                item.item_key
                for item in policy.items
                if (
                    (progress := store.item_progress(args.resume, item.item_key)).recycling
                    and progress.managed_active_quantity == 0
                    and (progress.returning_quantity > 0 or progress.bag_quantity > 0)
                )
            ]
            try:
                validate_seller_runtime(policy, backend.config, current, backend)
            except ValueError as ah_error:
                if len(pending_returns) != 1:
                    raise ah_error
                try:
                    validate_seller_mailbox_runtime(policy, backend.config, current, backend)
                except ValueError:
                    raise ah_error from None
                mailbox_resume_item = pending_returns[0]
        else:
            validate_seller_runtime(policy, backend.config, current, backend)
        now = int(backend.io.wall_time())
        if now >= policy.expires_at:
            raise AdmissionDenied("seller policy is expired")
        if args.seller_command == "start" and args.arm != current["boot"]:
            raise AdmissionDenied("--arm must equal the exact currently observed client boot ID")

        backend.set_deadline(policy.expires_at)
        native = SellerNative(backend)
        if args.seller_command == "preview":
            decisions: list[dict[str, object]] = []
            for item in policy.items:
                backend.set_deadline(policy.expires_at)
                inventory_observation = native.inventory()
                inventory = next(
                    row
                    for row in inventory_facts_for_policy(policy, inventory_observation)
                    if row.item_key == item.item_key
                )
                observation_now = int(backend.io.wall_time())
                if (
                    inventory.client_boot_id != current["boot"]
                    or observation_now - inventory.observed_at > 60
                    or inventory.complete is not True
                ):
                    raise AdmissionDenied(
                        "preview inventory is incomplete, stale or from another client boot"
                    )
                bag = min(item.quantity, inventory.bag_quantity)
                stock = StockFacts(item.item_key, item.quantity, bag, 0, 0, item.quantity - bag, 0)
                backend.set_deadline(policy.expires_at)
                market = native.market(item.item_key, secrets.token_hex(16), frozenset())
                if type(market) is not MarketFacts:
                    raise AdmissionDenied("targeted market adapter did not return exact seller facts")
                decision = decide(policy, item, market, stock, int(backend.io.wall_time()))
                decisions.append(decision.object())
            return {
                "boot": current["boot"],
                "decisions": decisions,
                "liveQualification": LIVE_QUALIFICATION,
                "policyId": policy.policy_id,
                "protectedActionsSent": False,
                "schema": "crowley.automation-seller-preview.v1",
            }

        if args.arm != current["boot"]:
            raise AdmissionDenied("--arm must equal the exact currently observed client boot ID")
        inventory_observation = native.inventory()
        initial_inventory = inventory_facts_for_policy(policy, inventory_observation)
        if inventory_observation.boot != current["boot"]:
            raise AdmissionDenied("initial inventory belongs to another client boot")
        if args.resume:
            if mailbox_resume_item is not None:
                observation = native.returns(mailbox_resume_item)
                outstanding = store.item_progress(args.resume, mailbox_resume_item).returning_quantity
                candidates = tuple(
                    ReturnCandidate(
                        row.item_key,
                        row.quantity,
                        row.mail_fingerprint,
                        observation.request_id,
                        observation.boot,
                        observation.sequence,
                        observation.captured_at,
                        observation.complete,
                        row.eligible
                        and row.item_key == mailbox_resume_item
                        and row.quantity is not None
                        and row.quantity <= outstanding,
                        (
                            row.reason
                            if not row.eligible
                            else "return attachment exceeds outstanding admitted stock"
                            if row.quantity is None or row.quantity > outstanding
                            else "return attachment does not match the exact policy commodity"
                            if row.item_key != mailbox_resume_item
                            else row.reason
                        ),
                    )
                    for row in observation.facts
                )
                mail_facts = ReturnObservationFacts(
                    mailbox_resume_item,
                    observation.request_id,
                    observation.boot,
                    observation.sequence,
                    observation.captured_at,
                    observation.complete,
                    candidates,
                )
                session_id = store.resume_at_mailbox(
                    args.resume,
                    confirmed_policy_id=args.confirm_policy_id,
                    boot_id=current["boot"],
                    client_build=current["context"]["clientBuild"],
                    inventories=initial_inventory,
                    mail_observations=(mail_facts,),
                    now=int(backend.io.wall_time()),
                )
                start_event = "seller_resumed"
            else:
                owned_observation = native.owned()
                owned_facts = tuple(
                    _seller_owned_facts_for_resume(
                        store,
                        args.resume,
                        item.item_key,
                        owned_observation,
                    )
                    for item in policy.items
                )
                session_id = store.resume(
                    args.resume,
                    confirmed_policy_id=args.confirm_policy_id,
                    boot_id=current["boot"],
                    client_build=current["context"]["clientBuild"],
                    inventories=initial_inventory,
                    owned_facts=owned_facts,
                    now=int(backend.io.wall_time()),
                )
                start_event = "seller_resumed"
        else:
            session_id = store.start(
                policy,
                boot_id=current["boot"],
                client_build=current["context"]["clientBuild"],
                confirmed_policy_id=args.confirm_policy_id,
                initial_inventory=initial_inventory,
                now=int(backend.io.wall_time()),
            )
            start_event = "seller_started"
        print(
            json.dumps(
                {
                    "event": start_event,
                    "sessionId": session_id,
                    "policyId": policy.policy_id,
                    "boot": current["boot"],
                    "liveQualification": LIVE_QUALIFICATION,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            flush=True,
        )
        try:
            loop = SellerLoop(
                policy,
                session_id,
                store=store,
                ledger=ledger,
                runner=runner,
                backend=backend,
                native=native,
                at_mailbox_item_key=mailbox_resume_item,
                progress=lambda event: print(
                    json.dumps(event, sort_keys=True, separators=(",", ":")), flush=True
                ),
            )
            return loop.run()
        except KeyboardInterrupt:
            store.revoke(session_id, "foreground seller interrupted", now=int(backend.io.wall_time()))
            raise
        except Exception:
            store.revoke(session_id, "foreground seller driver failed", now=int(backend.io.wall_time()))
            raise


def _backend_context():
    config = load_config()
    targets, routes = load_profiles(config)
    lease = GuiLease(gui_lock_path())
    backend = DesktopBackend(config, targets, routes, lease)
    return config, lease, backend, routes


@contextmanager
def _leased_backend() -> Iterator[tuple[Any, GuiLease, DesktopBackend, dict[str, Any]]]:
    config, lease, backend, routes = _backend_context()
    with lease.hold():
        try:
            yield config, lease, backend, routes
        finally:
            backend.close()


def _arm(args: argparse.Namespace, batch: Batch, config: Any) -> tuple[BatchAuthorization, SessionArm]:
    now = int(time.time())
    expiry = args.arm_expires
    if type(expiry) is not int or not now < expiry <= now + 120:
        raise AdmissionDenied("--arm-expires must be a Unix time within the next 120 seconds")
    return (
        BatchAuthorization(batch.batch_id, batch.house, batch.character, min(batch.expires_at, expiry)),
        SessionArm(args.arm, batch.house, batch.character, expiry),
    )


def _parser() -> argparse.ArgumentParser:
    parser = _JsonParser(prog="crowley-automation")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_JsonParser)
    inspect = commands.add_parser("inspect", help="validate and print a canonical batch")
    inspect.add_argument("batch", type=Path)
    prepare = commands.add_parser("prepare", help="durably record a validated batch without execution")
    prepare.add_argument("batch", type=Path)
    prepare.add_argument("--ledger", type=Path, default=default_ledger_path())
    status = commands.add_parser("status", help="inspect durable local operation state")
    status.add_argument("batch_id", nargs="?")
    status.add_argument("--ledger", type=Path, default=default_ledger_path())
    observe = commands.add_parser("observe", help="read one fresh native observation")
    observe.add_argument("--ledger", type=Path, default=default_ledger_path())
    for name in ("inventory", "owned", "mail"):
        page = commands.add_parser(name, help=f"read one native {name} inspection page")
        page.add_argument("--page", type=int, default=0)
        page.add_argument("--ledger", type=Path, default=default_ledger_path())
    for name in ("dry-run", "run"):
        operation = commands.add_parser(
            name, help="perform native preflight" if name == "dry-run" else "run one confirmed batch"
        )
        operation.add_argument("batch", type=Path)
        operation.add_argument("--confirm", required=True)
        operation.add_argument("--arm", required=True, help="exact native boot ID from observe")
        operation.add_argument(
            "--arm-expires", type=int, required=True, help="short Unix time expiry, at most 120 seconds"
        )
        operation.add_argument("--ledger", type=Path, default=default_ledger_path())
    stop = commands.add_parser("stop", help="set or explicitly clear the durable input stop")
    stop.add_argument(
        "--clear", action="store_true", help="clear the stop only when no uncertain effect exists"
    )
    stop.add_argument("--ledger", type=Path, default=default_ledger_path())
    reconcile = commands.add_parser(
        "reconcile", help="resolve an uncertain action from a fresh causal native receipt"
    )
    reconcile.add_argument("batch_id")
    reconcile.add_argument("--ordinal", type=int, required=True)
    reconcile.add_argument("--step", type=int, required=True)
    reconcile.add_argument("--ledger", type=Path, default=default_ledger_path())
    seller = commands.add_parser("seller", help="inspect and run an explicitly armed seller session")
    seller_commands = seller.add_subparsers(dest="seller_command", required=True, parser_class=_JsonParser)
    seller_inspect = seller_commands.add_parser("inspect", help="validate and canonicalize a seller policy")
    seller_inspect.add_argument("policy", type=Path)
    seller_preview = seller_commands.add_parser(
        "preview", help="show fresh seller decisions without protected actions"
    )
    seller_preview.add_argument("policy", type=Path)
    seller_preview.add_argument("--ledger", type=Path, default=default_ledger_path())
    seller_start = seller_commands.add_parser("start", help="arm and run a seller policy in the foreground")
    seller_start.add_argument("policy", type=Path, nargs="?")
    seller_start.add_argument("--resume", metavar="SESSION_ID")
    seller_start.add_argument("--confirm-policy-id", required=True)
    seller_start.add_argument("--arm", required=True, help="exact current native boot ID from observe")
    seller_start.add_argument("--ledger", type=Path, default=default_ledger_path())
    seller_status = seller_commands.add_parser("status", help="inspect a durable seller session")
    seller_status.add_argument("session_id")
    seller_status.add_argument("--ledger", type=Path, default=default_ledger_path())
    seller_revoke = seller_commands.add_parser("revoke", help="revoke a seller session")
    seller_revoke.add_argument("session_id")
    seller_revoke.add_argument("--ledger", type=Path, default=default_ledger_path())
    seller_release = seller_commands.add_parser(
        "release-expired",
        help="release proven bag-only stock from an expired seller grant without protected actions",
    )
    seller_release.add_argument("session_id")
    seller_release.add_argument("--confirm-policy-id", required=True)
    seller_release.add_argument("--arm", required=True, help="exact current native boot ID from observe")
    seller_release.add_argument("--ledger", type=Path, default=default_ledger_path())
    return parser


def _observe(args: argparse.Namespace) -> dict[str, object]:
    with _leased_backend() as (_config, _lease, backend, _routes):
        value = backend.observe_native()
    context = value["context"]
    inspection = value["inspection"]
    result: dict[str, object] = {
        "schema": "crowley.automation-observation.v1",
        "liveQualification": LIVE_QUALIFICATION,
        "boot": value["boot"],
        "sequence": value["sequence"],
        "identity": {
            key: context[key]
            for key in ("character", "realm", "playerFaction", "clientBuild", "interface", "nativeRegion")
        },
        "zone": context["zone"],
        "ahOpen": value["ahOpen"],
        "mailOpen": value["mailOpen"],
        "goldCopper": value["goldCopper"],
        "ownership": value["ownership"],
        "motion": value["motion"],
        "operation": value["operation"],
        "blockingUI": value["blockingUI"],
        "chatActive": value["chatEdit"]["active"],
        "inventoryPage": inspection if inspection["kind"] == "inventory" else None,
    }
    return result


def _inspect_page(kind: str, page: int) -> dict[str, object]:
    with _leased_backend() as (_config, _lease, backend, _routes):
        inspection = backend.inspect(kind, page)
    return {
        "schema": "crowley.automation-inspection.v1",
        "liveQualification": LIVE_QUALIFICATION,
        **inspection,
    }


def _reconcile(args: argparse.Namespace) -> dict[str, object]:
    ledger = Ledger(args.ledger)
    status = ledger.status(args.batch_id)
    if type(args.ordinal) is not int or type(args.step) is not int or args.ordinal < 0 or args.step < 0:
        raise LedgerError("ordinal and step must be non-negative integers")
    actions = {row["ordinal"]: row["action"] for row in status["actions"]}
    action = actions.get(args.ordinal)
    if action is None:
        raise LedgerError("batch has no requested action ordinal")
    action_data = cast(dict[str, object], action)
    with _leased_backend() as (_config, _lease, backend, routes):
        value = backend.observe_native()
    native_receipt = value["operation"]["receipt"]
    if action_data["kind"] == "collect_mail":
        limit = cast(int, action_data["messageLimit"])
    elif action_data["kind"] == "move_route":
        route_id = cast(str, action_data["routeId"])
        if route_id not in routes:
            raise LedgerError("uncertain route has no matching measured profile")
        limit = len(routes[route_id].waypoints)
    else:
        limit = 1
    ledger.reconcile_receipt(
        args.batch_id,
        args.ordinal,
        args.step,
        native_receipt,
        boot_id=value["boot"],
        step_limit=limit,
    )
    return {"batch_id": args.batch_id, "reconciled": True, "state": ledger.status(args.batch_id)["state"]}


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "inspect":
            batch = _read_batch(args.batch)
            result: object = {
                "batch_id": batch.batch_id,
                "canonical": batch.object(),
                "canonical_json": batch.canonical,
                "action_count": len(batch.actions),
                "runtimeImplemented": True,
                "liveQualification": LIVE_QUALIFICATION,
            }
        elif args.command == "prepare":
            batch = _read_batch(args.batch)
            ledger = Ledger(args.ledger)
            ledger.recover_if_idle()
            state = ledger.prepare(batch)
            result = {
                "batch_id": batch.batch_id,
                "state": state["state"],
                "action_count": len(batch.actions),
                "runtimeImplemented": True,
                "liveQualification": LIVE_QUALIFICATION,
            }
        elif args.command == "status":
            ledger = Ledger(args.ledger)
            ledger.recover_if_idle()
            result = ledger.status(args.batch_id) if args.batch_id else ledger.all_status()
        elif args.command == "observe":
            result = _observe(args)
        elif args.command in {"inventory", "owned", "mail"}:
            result = _inspect_page(args.command, args.page)
        elif args.command in {"dry-run", "run"}:
            batch = _read_batch(args.batch)
            config = load_config()
            authorization, arm = _arm(args, batch, config)
            if args.command == "dry-run":
                result = dry_run_batch(
                    batch, authorization, arm, args.confirm, ledger_path=args.ledger, config=config
                )
                result["liveQualification"] = LIVE_QUALIFICATION
            else:
                result = run_batch(
                    batch, authorization, arm, args.confirm, ledger_path=args.ledger, config=config
                )
                result["liveQualification"] = LIVE_QUALIFICATION
        elif args.command == "stop":
            ledger = Ledger(args.ledger)
            if args.clear:
                ledger.clear_stop()
            else:
                ledger.stop()
            result = {"stopRequested": ledger.stop_requested()}
        elif args.command == "seller":
            result = _seller_command(args)
        else:
            result = _reconcile(args)
        output = json.dumps(result, sort_keys=True, separators=(",", ":"))
        if len(output.encode("utf-8")) > 256_000:
            raise ValueError("JSON response exceeds the 256 KiB output bound")
        print(output)
        return 0
    except (
        BatchError,
        OSError,
        RuntimeError,
        KeyError,
        ValueError,
        sqlite3.Error,
        AutomationConfigError,
    ) as exc:
        print(json.dumps({"error": str(exc).splitlines()[0][:240]}, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
