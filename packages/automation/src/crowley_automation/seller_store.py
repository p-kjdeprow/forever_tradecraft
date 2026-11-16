"""Durable seller session admission and progress in the existing automation ledger."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Literal

from .ledger import Ledger, LedgerError
from .model import (
    Action,
    Batch,
    CancelAuction,
    CollectReturn,
    Interact,
    MoveRoute,
    PostCommodity,
    canonical_json,
)
from .runner import Admission, AdmissionDenied, dispatch_result_matches_action
from .seller import (
    MAX_SELLER_CANCELS_PER_CHILD,
    SELLER_DECISION_MAX_AGE_SECONDS,
    SELLER_PHASE_MAX_AGE_SECONDS,
    InventoryFacts,
    ManagedAuction,
    MarketFacts,
    OwnedAuction,
    OwnedFacts,
    ReturnCandidate,
    ReturnObservationFacts,
    SellerDecision,
    SellerItemPolicy,
    SellerItemProgress,
    SellerPolicy,
    SellerSessionStatus,
    StockFacts,
    decide,
)

MAX_PROGRESS_AGE_SECONDS = 60
SellerSessionState = Literal["active", "completed", "revoked", "expired", "stopped"]


class SellerStoreError(LedgerError):
    """A seller session, observation, budget, or child batch violates persisted policy."""


@dataclass(frozen=True, slots=True)
class _SessionRow:
    session_id: str
    policy: SellerPolicy
    boot_id: str
    client_build: str
    expires_at: int
    state: SellerSessionState
    stop_reason: str | None
    created_at: int


@dataclass(frozen=True, slots=True)
class _ObservationProvenance:
    request_id: str
    client_boot_id: str
    sequence: int
    observed_at: int


class SellerStore:
    """Persist one immutable policy, admitted stock and policy-derived child actions."""

    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger
        with ledger._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS seller_sessions (
                    session_id TEXT PRIMARY KEY,
                    policy_id TEXT NOT NULL,
                    policy_json TEXT NOT NULL,
                    boot_id TEXT NOT NULL,
                    client_build TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('active','completed','revoked','expired','stopped')),
                    stop_reason TEXT,
                    created_at INTEGER NOT NULL,
                    spent_copper INTEGER NOT NULL DEFAULT 0,
                    resume_epoch_id TEXT,
                    resume_location TEXT
                );
                CREATE TABLE IF NOT EXISTS seller_progress (
                    session_id TEXT NOT NULL REFERENCES seller_sessions(session_id),
                    item_key TEXT NOT NULL,
                    admitted_quantity INTEGER NOT NULL,
                    bag_quantity INTEGER NOT NULL,
                    managed_active_quantity INTEGER NOT NULL DEFAULT 0,
                    returning_quantity INTEGER NOT NULL DEFAULT 0,
                    unresolved_quantity INTEGER NOT NULL DEFAULT 0,
                    sold_quantity INTEGER NOT NULL DEFAULT 0,
                    recycle_rounds INTEGER NOT NULL DEFAULT 0,
                    recycling INTEGER NOT NULL DEFAULT 0,
                    finished INTEGER NOT NULL DEFAULT 0,
                    spent_copper INTEGER NOT NULL DEFAULT 0,
                    recycle_anchor_decision_id TEXT,
                    last_post_attempt_at INTEGER,
                    last_post_decision_id TEXT,
                    PRIMARY KEY(session_id,item_key)
                );
                CREATE TABLE IF NOT EXISTS seller_managed_auctions (
                    session_id TEXT NOT NULL REFERENCES seller_sessions(session_id),
                    item_key TEXT NOT NULL,
                    auction_id INTEGER NOT NULL,
                    remaining_quantity INTEGER NOT NULL,
                    unit_price_copper INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('active','sold','unknown','cancelled')),
                    cancel_allowed INTEGER NOT NULL,
                    cancel_cost_known INTEGER NOT NULL,
                    cancel_cost_copper INTEGER NOT NULL,
                    created_batch_id TEXT NOT NULL,
                    recycle_anchor_decision_id TEXT,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(session_id,auction_id)
                );
                CREATE TABLE IF NOT EXISTS seller_observations (
                    session_id TEXT NOT NULL REFERENCES seller_sessions(session_id),
                    item_key TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('inventory','owned')),
                    request_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    observed_at INTEGER NOT NULL,
                    facts_json TEXT NOT NULL,
                    PRIMARY KEY(session_id,item_key,kind,request_id)
                );
                CREATE TABLE IF NOT EXISTS seller_decisions (
                    session_id TEXT NOT NULL REFERENCES seller_sessions(session_id),
                    decision_id TEXT NOT NULL,
                    item_key TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    market_json TEXT NOT NULL,
                    stock_json TEXT NOT NULL,
                    decision_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY(session_id,decision_id),
                    UNIQUE(session_id,item_key,request_id)
                );
                CREATE TABLE IF NOT EXISTS seller_children (
                    session_id TEXT NOT NULL REFERENCES seller_sessions(session_id),
                    decision_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    batch_id TEXT NOT NULL UNIQUE REFERENCES batches(batch_id),
                    canonical_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN (
                        'prepared','running','succeeded','failed','uncertain'
                    )),
                    created_at INTEGER NOT NULL,
                    PRIMARY KEY(session_id,decision_id,phase),
                    FOREIGN KEY(session_id,decision_id) REFERENCES seller_decisions(session_id,decision_id)
                );
                CREATE TABLE IF NOT EXISTS seller_reservations (
                    session_id TEXT NOT NULL,
                    decision_id TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    amount_copper INTEGER NOT NULL,
                    actual_copper INTEGER,
                    state TEXT NOT NULL CHECK(state IN ('reserved','unknown','reconciled','released')),
                    PRIMARY KEY(session_id,decision_id,phase,ordinal),
                    FOREIGN KEY(session_id,decision_id,phase)
                        REFERENCES seller_children(session_id,decision_id,phase)
                );
                CREATE TABLE IF NOT EXISTS seller_returns (
                    session_id TEXT NOT NULL REFERENCES seller_sessions(session_id),
                    recycle_anchor_decision_id TEXT NOT NULL,
                    mail_fingerprint TEXT NOT NULL,
                    item_key TEXT,
                    quantity INTEGER,
                    request_id TEXT NOT NULL,
                    client_boot_id TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    observed_at INTEGER NOT NULL,
                    eligible INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    consumed INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(session_id,mail_fingerprint)
                );
                """
            )
            progress_columns = {row["name"] for row in db.execute("PRAGMA table_info(seller_progress)")}
            if "last_post_attempt_at" not in progress_columns:
                db.execute("ALTER TABLE seller_progress ADD COLUMN last_post_attempt_at INTEGER")
            if "last_post_decision_id" not in progress_columns:
                db.execute("ALTER TABLE seller_progress ADD COLUMN last_post_decision_id TEXT")
            session_columns = {row["name"] for row in db.execute("PRAGMA table_info(seller_sessions)")}
            if "resume_epoch_id" not in session_columns:
                db.execute("ALTER TABLE seller_sessions ADD COLUMN resume_epoch_id TEXT")
            if "resume_location" not in session_columns:
                db.execute("ALTER TABLE seller_sessions ADD COLUMN resume_location TEXT")

    def start(
        self,
        policy: SellerPolicy,
        *,
        boot_id: str,
        client_build: str,
        confirmed_policy_id: str,
        initial_inventory: tuple[InventoryFacts, ...],
        now: int | None = None,
    ) -> str:
        timestamp = int(time.time()) if now is None else now
        if type(policy) is not SellerPolicy:
            raise SellerStoreError("start requires one immutable SellerPolicy")
        if confirmed_policy_id != policy.policy_id:
            raise SellerStoreError("explicit policy confirmation must match the full policy digest")
        self._identity_text(boot_id, "boot_id")
        if client_build != policy.client_build:
            raise SellerStoreError("session client build does not match the approved policy")
        if timestamp >= policy.expires_at:
            raise SellerStoreError("seller policy is expired")
        if self.ledger.stop_requested():
            raise SellerStoreError("durable automation stop is active")
        if self.ledger.has_uncertain_effect():
            raise SellerStoreError("an unresolved automation effect blocks a new seller session")
        observations = {row.item_key: row for row in initial_inventory}
        if len(observations) != len(initial_inventory) or set(observations) != {
            item.item_key for item in policy.items
        }:
            raise SellerStoreError("initial inventory must prove every and only admitted item identity")
        for item in policy.items:
            row = observations[item.item_key]
            self._check_fact_fresh(row.client_boot_id, row.observed_at, row.complete, boot_id, timestamp)
            if row.bag_quantity < item.quantity:
                raise SellerStoreError(f"initial inventory is below admitted quantity for {item.item_key}")
        session_id = uuid.uuid4().hex
        with self.ledger._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute("SELECT session_id FROM seller_sessions WHERE state='active'").fetchone()
            if active is not None:
                raise SellerStoreError("another durable seller session is active")
            unsafe = db.execute(
                "SELECT p.session_id,p.item_key FROM seller_progress p "
                "JOIN seller_sessions s USING(session_id) "
                "WHERE s.state!='completed' "
                "AND (p.bag_quantity+p.managed_active_quantity+p.returning_quantity+"
                "p.unresolved_quantity)>0 LIMIT 1"
            ).fetchone()
            if unsafe is not None:
                raise SellerStoreError(
                    "an earlier seller session still owns admitted stock; reconcile it first"
                )
            db.execute(
                "INSERT INTO seller_sessions(session_id,policy_id,policy_json,boot_id,client_build,"
                "expires_at,"
                "state,stop_reason,created_at,spent_copper) VALUES(?,?,?,?,?,?,'active',NULL,?,0)",
                (
                    session_id,
                    policy.policy_id,
                    policy.canonical,
                    boot_id,
                    client_build,
                    policy.expires_at,
                    timestamp,
                ),
            )
            db.executemany(
                "INSERT INTO seller_progress(session_id,item_key,admitted_quantity,bag_quantity) "
                "VALUES(?,?,?,?)",
                [(session_id, item.item_key, item.quantity, item.quantity) for item in policy.items],
            )
            for row in initial_inventory:
                self._insert_observation(
                    db,
                    session_id,
                    row.item_key,
                    "inventory",
                    row.request_id,
                    row.sequence,
                    row.observed_at,
                    row.object(),
                )
        return session_id

    def status(self, session_id: str, *, now: int | None = None) -> SellerSessionStatus:
        timestamp = int(time.time()) if now is None else now
        with self.ledger._connect() as db:
            row = db.execute("SELECT * FROM seller_sessions WHERE session_id=?", (session_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown seller session: {session_id}")
            state = row["state"]
            if state == "active" and timestamp >= row["expires_at"]:
                db.execute(
                    "UPDATE seller_sessions SET state='expired',stop_reason='policy expired' "
                    "WHERE session_id=?",
                    (session_id,),
                )
                state = "expired"
            return SellerSessionStatus(
                session_id,
                row["policy_id"],
                row["boot_id"],
                row["client_build"],
                row["expires_at"],
                state,
                row["stop_reason"],
                row["created_at"],
                row["resume_location"],
            )

    def policy(self, session_id: str) -> SellerPolicy:
        """Return the immutable persisted policy for explicit resume/review."""
        with self.ledger._connect() as db:
            row = db.execute(
                "SELECT policy_json,policy_id FROM seller_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown seller session: {session_id}")
            policy = SellerPolicy.from_json(row["policy_json"])
            if policy.policy_id != row["policy_id"]:
                raise SellerStoreError("persisted seller policy digest is inconsistent")
            return policy

    def resume(
        self,
        session_id: str,
        *,
        confirmed_policy_id: str,
        boot_id: str,
        client_build: str,
        inventories: tuple[InventoryFacts, ...],
        owned_facts: tuple[OwnedFacts, ...],
        now: int | None = None,
    ) -> str:
        """Re-arm or restart an unexpired session without changing policy, stock caps or spend."""
        timestamp = int(time.time()) if now is None else now
        policy = self.policy(session_id)
        if confirmed_policy_id != policy.policy_id:
            raise SellerStoreError("resume confirmation must match the persisted full policy digest")
        self._identity_text(boot_id, "boot_id")
        if client_build != policy.client_build:
            raise SellerStoreError("resume build differs from the immutable policy")
        if timestamp >= policy.expires_at:
            raise SellerStoreError("expired seller authorization cannot be renewed")
        if self.ledger.stop_requested():
            raise SellerStoreError("durable automation stop is active")
        inventory_by_item = {fact.item_key: fact for fact in inventories}
        owned_by_item = {fact.item_key: fact for fact in owned_facts}
        expected_items = {item.item_key for item in policy.items}
        if (
            type(inventories) is not tuple
            or type(owned_facts) is not tuple
            or len(inventory_by_item) != len(inventories)
            or len(owned_by_item) != len(owned_facts)
            or set(inventory_by_item) != expected_items
            or set(owned_by_item) != expected_items
        ):
            raise SellerStoreError("resume needs complete per-item inventory and owned observations")
        resume_epoch = hashlib.sha256(
            canonical_json(
                {
                    "bootId": boot_id,
                    "inventories": [inventory_by_item[key].object() for key in sorted(expected_items)],
                    "location": "ah",
                    "owned": [owned_by_item[key].object() for key in sorted(expected_items)],
                }
            ).encode("utf-8")
        ).hexdigest()
        with self.ledger.runner_lock():
            self.ledger.recover_interrupted(now=timestamp)
            if self.ledger.has_uncertain_effect():
                raise SellerStoreError("an unresolved automation effect blocks seller resume")
            pending = self._nonterminal_children(session_id)
            preserved_recovery: sqlite3.Row | None = None
            preserved_continuation: sqlite3.Row | None = None
            for child in pending:
                if child["phase"].startswith("recovery_to_mail:"):
                    child_batch = Batch.from_json(child["canonical_json"])
                    if child_batch.expires_at > timestamp:
                        if preserved_recovery is not None:
                            raise SellerStoreError("seller resume found multiple pending recovery routes")
                        preserved_recovery = child
                    else:
                        self.ledger.mark_expired(child["batch_id"], now=timestamp)
                        self.release_before_dispatch(
                            session_id,
                            child["decision_id"],
                            child["phase"],
                            child_batch,
                            now=timestamp,
                        )
                elif child["phase"].startswith("continue_recycle:"):
                    child_batch = Batch.from_json(child["canonical_json"])
                    if child_batch.expires_at > timestamp and self._continuation_child_is_resumable(
                        child["batch_id"]
                    ):
                        if preserved_continuation is not None:
                            raise SellerStoreError("seller resume found multiple pending recycle chunks")
                        preserved_continuation = child
                    else:
                        if child_batch.expires_at <= timestamp:
                            self.ledger.mark_expired(child["batch_id"], now=timestamp)
                        self.release_before_dispatch(
                            session_id,
                            child["decision_id"],
                            child["phase"],
                            child_batch,
                            now=timestamp,
                        )
                else:
                    self.release_before_dispatch(
                        session_id,
                        child["decision_id"],
                        child["phase"],
                        Batch.from_json(child["canonical_json"]),
                        now=timestamp,
                    )
            if preserved_recovery is not None and preserved_continuation is not None:
                raise SellerStoreError("seller resume cannot preserve two concurrent child phases")
            preserved_item_key: str | None = None
            if preserved_recovery is not None:
                preserved_epoch = preserved_recovery["phase"].removeprefix("recovery_to_mail:")
                if len(preserved_epoch) != 64:
                    raise SellerStoreError("pending recovery route has malformed resume provenance")
                resume_epoch = preserved_epoch
                with self.ledger._connect() as lookup_db:
                    owner = lookup_db.execute(
                        "SELECT item_key FROM seller_decisions WHERE session_id=? AND decision_id=?",
                        (session_id, preserved_recovery["decision_id"]),
                    ).fetchone()
                if owner is None:
                    raise SellerStoreError("pending recovery route has no persisted item anchor")
                preserved_item_key = owner["item_key"]
            elif preserved_continuation is not None:
                with self.ledger._connect() as lookup_db:
                    owner = lookup_db.execute(
                        "SELECT item_key FROM seller_decisions WHERE session_id=? AND decision_id=?",
                        (session_id, preserved_continuation["decision_id"]),
                    ).fetchone()
                if owner is None:
                    raise SellerStoreError("pending recycle chunk has no persisted item anchor")
                preserved_item_key = owner["item_key"]
            with self.ledger._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT * FROM seller_sessions WHERE session_id=?", (session_id,)).fetchone()
                if row is None:
                    raise KeyError(f"unknown seller session: {session_id}")
                if row["state"] not in ("active", "revoked", "stopped"):
                    raise SellerStoreError(f"seller session cannot resume from {row['state']}")
                if row["policy_id"] != policy.policy_id or row["policy_json"] != policy.canonical:
                    raise SellerStoreError("persisted seller policy changed during resume")
                if timestamp >= row["expires_at"] or timestamp >= policy.expires_at:
                    db.execute(
                        "UPDATE seller_sessions SET state='expired',stop_reason='policy expired' "
                        "WHERE session_id=?",
                        (session_id,),
                    )
                    raise SellerStoreError("expired seller authorization cannot be renewed")
                if db.execute(
                    "SELECT 1 FROM seller_sessions WHERE state='active' AND session_id!=? LIMIT 1",
                    (session_id,),
                ).fetchone():
                    raise SellerStoreError("another durable seller session is active")
                if preserved_continuation is None:
                    unresolved = db.execute(
                        "SELECT 1 FROM seller_children WHERE session_id=? AND state='uncertain' LIMIT 1",
                        (session_id,),
                    ).fetchone()
                else:
                    unresolved = db.execute(
                        "SELECT 1 FROM seller_children WHERE session_id=? AND state='uncertain' "
                        "AND NOT (decision_id=? AND phase=?) LIMIT 1",
                        (
                            session_id,
                            preserved_continuation["decision_id"],
                            preserved_continuation["phase"],
                        ),
                    ).fetchone()
                if unresolved is not None:
                    raise SellerStoreError("seller resume found an unresolved child")
                db.execute(
                    "UPDATE seller_sessions SET boot_id=?,client_build=?,state='active',stop_reason=NULL,"
                    "resume_epoch_id=?,resume_location='ah' "
                    "WHERE session_id=?",
                    (boot_id, client_build, resume_epoch, session_id),
                )
                for item in policy.items:
                    if item.item_key == preserved_item_key:
                        current = self._progress_row(db, session_id, item.item_key)
                        inventory_fact = inventory_by_item[item.item_key]
                        owned_fact = owned_by_item[item.item_key]
                        self._check_fact_fresh(
                            inventory_fact.client_boot_id,
                            inventory_fact.observed_at,
                            inventory_fact.complete,
                            boot_id,
                            timestamp,
                        )
                        self._check_fact_fresh(
                            owned_fact.client_boot_id,
                            owned_fact.observed_at,
                            owned_fact.complete,
                            boot_id,
                            timestamp,
                        )
                        if inventory_fact.bag_quantity < current["bag_quantity"]:
                            raise SellerStoreError(
                                "pending recovery route cannot rearm after admitted bag stock decreased"
                            )
                        if preserved_continuation is not None:
                            child_batch = Batch.from_json(preserved_continuation["canonical_json"])
                            ready_actions = self._ready_cancel_actions(db, child_batch)
                            self._validate_continuation_snapshot(
                                db,
                                session_id,
                                item,
                                inventory_fact,
                                owned_fact,
                                boot_id,
                                timestamp,
                                ready_actions,
                            )
                        continue
                    self._reconcile_stock_in_transaction(
                        db,
                        session_id,
                        item,
                        inventory_by_item[item.item_key],
                        owned_by_item[item.item_key],
                        boot_id,
                        timestamp,
                    )
                if preserved_continuation is not None:
                    completed = db.execute(
                        "SELECT COUNT(*) AS n FROM actions WHERE batch_id=? AND state='succeeded'",
                        (preserved_continuation["batch_id"],),
                    ).fetchone()["n"]
                    db.execute(
                        "UPDATE seller_children SET state=? WHERE session_id=? AND decision_id=? "
                        "AND phase=? AND state='uncertain'",
                        (
                            "running" if completed else "prepared",
                            session_id,
                            preserved_continuation["decision_id"],
                            preserved_continuation["phase"],
                        ),
                    )
        return session_id

    def resume_at_mailbox(
        self,
        session_id: str,
        *,
        confirmed_policy_id: str,
        boot_id: str,
        client_build: str,
        inventories: tuple[InventoryFacts, ...],
        mail_observations: tuple[ReturnObservationFacts, ...],
        now: int | None = None,
    ) -> str:
        """Re-arm a paused return pass from fresh bag and mailbox evidence without AH facts."""
        timestamp = int(time.time()) if now is None else now
        policy = self.policy(session_id)
        if confirmed_policy_id != policy.policy_id:
            raise SellerStoreError("resume confirmation must match the persisted full policy digest")
        self._identity_text(boot_id, "boot_id")
        if client_build != policy.client_build:
            raise SellerStoreError("resume build differs from the immutable policy")
        if timestamp >= policy.expires_at:
            raise SellerStoreError("expired seller authorization cannot be renewed")
        if self.ledger.stop_requested():
            raise SellerStoreError("durable automation stop is active")
        inventory_by_item = {fact.item_key: fact for fact in inventories}
        mail_by_item = {fact.item_key: fact for fact in mail_observations}
        expected_items = {item.item_key for item in policy.items}
        if (
            type(inventories) is not tuple
            or len(inventory_by_item) != len(inventories)
            or set(inventory_by_item) != expected_items
            or type(mail_observations) is not tuple
            or len(mail_by_item) != len(mail_observations)
            or not mail_by_item
        ):
            raise SellerStoreError("mailbox resume needs exact inventory and mail observation envelopes")
        for observation in mail_observations:
            if type(observation) is not ReturnObservationFacts:
                raise SellerStoreError("mailbox resume contains an unsupported native observation")
        resume_epoch = hashlib.sha256(
            canonical_json(
                {
                    "bootId": boot_id,
                    "inventories": [inventory_by_item[key].object() for key in sorted(expected_items)],
                    "location": "mailbox",
                    "mail": [mail_by_item[key].object() for key in sorted(mail_by_item)],
                }
            ).encode("utf-8")
        ).hexdigest()
        with self.ledger.runner_lock():
            self.ledger.recover_interrupted(now=timestamp)
            if self.ledger.has_uncertain_effect():
                raise SellerStoreError("an unresolved automation effect blocks seller resume")
            for child in self._nonterminal_children(session_id):
                child_batch = Batch.from_json(child["canonical_json"])
                if child["phase"].startswith("recovery_to_ah:") and child_batch.expires_at <= timestamp:
                    self.ledger.mark_expired(child["batch_id"], now=timestamp)
                self.release_before_dispatch(
                    session_id,
                    child["decision_id"],
                    child["phase"],
                    child_batch,
                    now=timestamp,
                )
            with self.ledger._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT * FROM seller_sessions WHERE session_id=?", (session_id,)).fetchone()
                if row is None:
                    raise KeyError(f"unknown seller session: {session_id}")
                if row["state"] not in ("active", "revoked", "stopped"):
                    raise SellerStoreError(f"seller session cannot resume from {row['state']}")
                if row["policy_id"] != policy.policy_id or row["policy_json"] != policy.canonical:
                    raise SellerStoreError("persisted seller policy changed during resume")
                if timestamp >= row["expires_at"]:
                    raise SellerStoreError("expired seller authorization cannot be renewed")
                if db.execute(
                    "SELECT 1 FROM seller_sessions WHERE state='active' AND session_id!=? LIMIT 1",
                    (session_id,),
                ).fetchone():
                    raise SellerStoreError("another durable seller session is active")
                unresolved_child = db.execute(
                    "SELECT 1 FROM seller_children WHERE session_id=? AND state='uncertain' LIMIT 1",
                    (session_id,),
                ).fetchone()
                if unresolved_child is not None:
                    raise SellerStoreError("seller resume found an unresolved child")
                recycling = {
                    progress["item_key"]: progress
                    for progress in db.execute(
                        "SELECT * FROM seller_progress WHERE session_id=? AND recycling=1",
                        (session_id,),
                    ).fetchall()
                }
                if not recycling or set(mail_by_item) != set(recycling):
                    raise SellerStoreError("mailbox proof must cover every persisted recycle pass")
                for progress in recycling.values():
                    if progress["managed_active_quantity"] or not (
                        progress["returning_quantity"] or progress["bag_quantity"]
                    ):
                        raise SellerStoreError(
                            "mailbox resume requires no active auctions and outstanding return or bag stock"
                        )
                for item in policy.items:
                    progress = self._progress_row(db, session_id, item.item_key)
                    inventory = inventory_by_item[item.item_key]
                    self._check_fact_fresh(
                        inventory.client_boot_id,
                        inventory.observed_at,
                        inventory.complete,
                        boot_id,
                        timestamp,
                    )
                    if inventory.bag_quantity < progress["bag_quantity"]:
                        raise SellerStoreError("mailbox resume inventory is below durable admitted bag stock")
                for item_key, observation in mail_by_item.items():
                    self._check_fact_fresh(
                        observation.client_boot_id,
                        observation.observed_at,
                        observation.complete,
                        boot_id,
                        timestamp,
                    )
                    progress = recycling[item_key]
                    anchor = progress["recycle_anchor_decision_id"]
                    if anchor is None:
                        raise SellerStoreError("mailbox resume found a recycle pass without an anchor")
                    for candidate in observation.candidates:
                        if candidate.client_boot_id != boot_id:
                            raise SellerStoreError("mail candidate belongs to another client boot")
                        self._check_fact_fresh(
                            candidate.client_boot_id,
                            candidate.observed_at,
                            candidate.complete,
                            boot_id,
                            timestamp,
                        )
                        if candidate.eligible and (
                            candidate.item_key != item_key
                            or candidate.quantity is None
                            or candidate.quantity > progress["returning_quantity"]
                        ):
                            raise SellerStoreError(
                                "mailbox resume return candidate exceeds its exact outstanding pool"
                            )
                        if candidate.mail_fingerprint:
                            self._upsert_return_candidate(db, session_id, anchor, candidate)
                db.execute(
                    "UPDATE seller_sessions SET boot_id=?,client_build=?,state='active',stop_reason=NULL,"
                    "resume_epoch_id=?,resume_location='mailbox' WHERE session_id=?",
                    (boot_id, client_build, resume_epoch, session_id),
                )
                for item in policy.items:
                    inventory = inventory_by_item[item.item_key]
                    self._insert_observation(
                        db,
                        session_id,
                        item.item_key,
                        "inventory",
                        inventory.request_id,
                        inventory.sequence,
                        inventory.observed_at,
                        inventory.object(),
                    )
        return session_id

    def release_expired_stock(
        self,
        session_id: str,
        *,
        confirmed_policy_id: str,
        boot_id: str,
        client_build: str,
        inventories: tuple[InventoryFacts, ...],
        owned_facts: tuple[OwnedFacts, ...],
        now: int | None = None,
    ) -> str:
        """Close an expired grant only after exact proof that all known stock is in bags."""
        timestamp = int(time.time()) if now is None else now
        policy = self.policy(session_id)
        if confirmed_policy_id != policy.policy_id:
            raise SellerStoreError("release confirmation must match the persisted full policy digest")
        self._identity_text(boot_id, "boot_id")
        if client_build != policy.client_build:
            raise SellerStoreError("release build differs from the immutable policy")
        if timestamp < policy.expires_at:
            raise SellerStoreError("bag-only ownership release is reserved for expired policy grants")
        if self.ledger.stop_requested():
            raise SellerStoreError("durable automation stop is active")
        inventory_by_item = {fact.item_key: fact for fact in inventories}
        owned_by_item = {fact.item_key: fact for fact in owned_facts}
        expected_items = {item.item_key for item in policy.items}
        if (
            type(inventories) is not tuple
            or type(owned_facts) is not tuple
            or len(inventory_by_item) != len(inventories)
            or len(owned_by_item) != len(owned_facts)
            or set(inventory_by_item) != expected_items
            or set(owned_by_item) != expected_items
        ):
            raise SellerStoreError("expired release needs complete per-item inventory and owned proofs")
        with self.ledger.runner_lock():
            self.ledger.recover_interrupted(now=timestamp)
            if self.ledger.has_uncertain_effect():
                raise SellerStoreError("an unresolved automation effect blocks expired stock release")
            pending = self._nonterminal_children(session_id)
            for child in pending:
                self.release_before_dispatch(
                    session_id,
                    child["decision_id"],
                    child["phase"],
                    Batch.from_json(child["canonical_json"]),
                    now=timestamp,
                )
            with self.ledger._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT * FROM seller_sessions WHERE session_id=?", (session_id,)).fetchone()
                if row is None:
                    raise KeyError(f"unknown seller session: {session_id}")
                if row["state"] in ("active", "revoked", "stopped") and timestamp >= row["expires_at"]:
                    db.execute(
                        "UPDATE seller_sessions SET state='expired',stop_reason='policy expired' "
                        "WHERE session_id=?",
                        (session_id,),
                    )
                    row = db.execute(
                        "SELECT * FROM seller_sessions WHERE session_id=?", (session_id,)
                    ).fetchone()
                if row["state"] != "expired":
                    raise SellerStoreError("only an expired seller grant can release bag-only stock")
                if self._nonterminal_children_in_transaction(db, session_id):
                    raise SellerStoreError("expired release found an unresolved child")
                db.execute(
                    "UPDATE seller_sessions SET boot_id=?,client_build=? WHERE session_id=?",
                    (boot_id, client_build, session_id),
                )
                for item in policy.items:
                    progress = self._progress_row(db, session_id, item.item_key)
                    resolved_zero = (
                        progress["bag_quantity"] == 0
                        and progress["managed_active_quantity"] == 0
                        and progress["returning_quantity"] == 0
                        and progress["unresolved_quantity"] == 0
                        and progress["sold_quantity"] == progress["admitted_quantity"]
                    )
                    if (
                        (not progress["bag_quantity"] and not resolved_zero)
                        or progress["managed_active_quantity"]
                        or progress["returning_quantity"]
                        or progress["unresolved_quantity"]
                    ):
                        raise SellerStoreError(
                            "expired stock release requires reconciled bag-only quantities"
                        )
                    tracked_ids = db.execute(
                        "SELECT auction_id,status FROM seller_managed_auctions "
                        "WHERE session_id=? AND item_key=?",
                        (session_id, item.item_key),
                    ).fetchall()
                    if any(row["status"] not in ("sold", "cancelled") for row in tracked_ids):
                        raise SellerStoreError("expired stock release has unresolved managed auction history")
                    inventory = inventory_by_item[item.item_key]
                    owned = owned_by_item[item.item_key]
                    if inventory.bag_quantity < progress["bag_quantity"]:
                        raise SellerStoreError(
                            "expired stock release inventory is below durable admitted bag stock"
                        )
                    self._reconcile_stock_in_transaction(
                        db, session_id, item, inventory, owned, boot_id, timestamp
                    )
                    refreshed = self._progress_row(db, session_id, item.item_key)
                    if (
                        refreshed["bag_quantity"] != progress["bag_quantity"]
                        or refreshed["unresolved_quantity"]
                        or refreshed["managed_active_quantity"]
                        or refreshed["returning_quantity"]
                    ):
                        raise SellerStoreError(
                            "expired stock release could not reconcile exact bag ownership"
                        )
                    db.execute(
                        "UPDATE seller_progress SET finished=1,recycling=0,recycle_anchor_decision_id=NULL "
                        "WHERE session_id=? AND item_key=?",
                        (session_id, item.item_key),
                    )
                db.execute(
                    "UPDATE seller_sessions SET state='completed',"
                    "stop_reason='expired bag-only stock explicitly released' WHERE session_id=?",
                    (session_id,),
                )
        return session_id

    def revoke(self, session_id: str, reason: str = "operator revoke", *, now: int | None = None) -> None:
        if type(reason) is not str or not 1 <= len(reason) <= 240:
            raise SellerStoreError("revoke reason must be bounded non-empty text")
        _ = int(time.time()) if now is None else now
        with self.ledger._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM seller_sessions WHERE session_id=?", (session_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown seller session: {session_id}")
            if row["state"] == "active":
                db.execute(
                    "UPDATE seller_sessions SET state='revoked',stop_reason=? WHERE session_id=?",
                    (reason, session_id),
                )
            db.execute(
                "UPDATE seller_children SET state='uncertain' WHERE session_id=? AND state='running'",
                (session_id,),
            )

    def reconcile_stock(
        self,
        session_id: str,
        inventory: InventoryFacts,
        owned: OwnedFacts,
        *,
        now: int | None = None,
    ) -> StockFacts:
        timestamp = int(time.time()) if now is None else now
        with self.ledger._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            session, policy = self._active_session(db, session_id, timestamp)
            item = policy.item(inventory.item_key)
            return self._reconcile_stock_in_transaction(
                db, session_id, item, inventory, owned, session.boot_id, timestamp
            )

    def _reconcile_stock_in_transaction(
        self,
        db: sqlite3.Connection,
        session_id: str,
        item: SellerItemPolicy,
        inventory: InventoryFacts,
        owned: OwnedFacts,
        expected_boot_id: str,
        now: int,
    ) -> StockFacts:
        if inventory.item_key != owned.item_key:
            raise SellerStoreError("inventory and owned observations identify different items")
        if inventory.item_key != item.item_key:
            raise SellerStoreError("snapshot facts do not identify the selected policy item")
        self._check_fact_fresh(
            inventory.client_boot_id,
            inventory.observed_at,
            inventory.complete,
            expected_boot_id,
            now,
        )
        self._check_fact_fresh(owned.client_boot_id, owned.observed_at, owned.complete, expected_boot_id, now)
        if abs(owned.observed_at - inventory.observed_at) > MAX_PROGRESS_AGE_SECONDS:
            raise SellerStoreError("inventory and owned observations are not from a coherent fresh window")
        self._assert_no_open_child(db, session_id, inventory.item_key)
        progress = self._progress_row(db, session_id, inventory.item_key)
        self._insert_observation(
            db,
            session_id,
            inventory.item_key,
            "inventory",
            inventory.request_id,
            inventory.sequence,
            inventory.observed_at,
            inventory.object(),
        )
        self._insert_observation(
            db,
            session_id,
            owned.item_key,
            "owned",
            owned.request_id,
            owned.sequence,
            owned.observed_at,
            owned.object(),
        )
        stored_rows = {
            row["auction_id"]: row
            for row in db.execute(
                "SELECT * FROM seller_managed_auctions WHERE session_id=? AND item_key=? AND status='active'",
                (session_id, item.item_key),
            ).fetchall()
        }
        observed_rows = {row.auction_id: row for row in owned.auctions}
        bag = progress["bag_quantity"]
        unresolved = progress["unresolved_quantity"]
        active = progress["managed_active_quantity"]
        sold = progress["sold_quantity"]
        if inventory.bag_quantity < bag:
            missing = bag - inventory.bag_quantity
            bag -= missing
            unresolved += missing
        for auction_id, stored in stored_rows.items():
            fact = observed_rows.get(auction_id)
            tracked = stored["remaining_quantity"]
            if fact is None:
                active -= tracked
                unresolved += tracked
                db.execute(
                    "UPDATE seller_managed_auctions SET status='unknown',updated_at=? "
                    "WHERE session_id=? AND auction_id=?",
                    (now, session_id, auction_id),
                )
                continue
            if fact.status == "sold":
                active -= tracked
                sold += tracked
                db.execute(
                    "UPDATE seller_managed_auctions SET status='sold',remaining_quantity=0,updated_at=? "
                    "WHERE session_id=? AND auction_id=?",
                    (now, session_id, auction_id),
                )
                continue
            if fact.remaining_quantity > tracked:
                raise SellerStoreError("owned auction quantity grew beyond its causal post receipt")
            if fact.remaining_quantity < tracked:
                delta = tracked - fact.remaining_quantity
                active -= delta
                unresolved += delta
            db.execute(
                "UPDATE seller_managed_auctions SET remaining_quantity=?,unit_price_copper=?,"
                "cancel_allowed=?,cancel_cost_known=?,cancel_cost_copper=?,updated_at=? "
                "WHERE session_id=? AND auction_id=?",
                (
                    fact.remaining_quantity,
                    fact.unit_price_copper,
                    int(fact.cancel_allowed),
                    int(fact.cancel_cost_known),
                    fact.cancel_cost_copper,
                    now,
                    session_id,
                    auction_id,
                ),
            )
        db.execute(
            "UPDATE seller_progress SET bag_quantity=?,managed_active_quantity=?,unresolved_quantity=?,"
            "sold_quantity=? WHERE session_id=? AND item_key=?",
            (bag, active, unresolved, sold, session_id, item.item_key),
        )
        self._assert_conservation(db, session_id, item.item_key)
        return self._stock_fact(db, session_id, item.item_key)

    def create_decision(
        self,
        session_id: str,
        market: MarketFacts,
        stock: StockFacts,
        *,
        now: int | None = None,
    ) -> SellerDecision:
        timestamp = int(time.time()) if now is None else now
        with self.ledger._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            session, policy = self._active_session(db, session_id, timestamp)
            item = policy.item(market.item_key)
            self._check_fact_fresh(
                market.client_boot_id, market.observed_at, market.complete, session.boot_id, timestamp
            )
            self._assert_no_open_child(db, session_id, item.item_key)
            expected_stock = self._stock_fact(db, session_id, item.item_key)
            if stock != expected_stock:
                raise SellerStoreError("decision stock facts differ from durable reconciled item progress")
            self._validate_stock_snapshot(db, session_id, stock, session.boot_id, timestamp)
            expected_auctions = self._managed_facts(db, session_id, item.item_key)
            if market.managed_auctions != expected_auctions:
                raise SellerStoreError(
                    "market managed auctions differ from causal seller-owned auction records"
                )
            progress = self._progress_row(db, session_id, item.item_key)
            if progress["recycling"]:
                anchor = progress["recycle_anchor_decision_id"]
                if anchor is None:
                    raise SellerStoreError("persisted recycle pass lost its original decision anchor")
                targets = self._recycle_target_ids(db, session_id, anchor)
                if not set(market.managed_auction_ids).issubset(targets):
                    raise SellerStoreError("managed auction ID is outside the frozen recycle pass")
            decision = decide(policy, item, market, stock, timestamp)
            prior = db.execute(
                "SELECT decision_id,market_json,stock_json,decision_json FROM seller_decisions "
                "WHERE session_id=? AND item_key=? AND request_id=?",
                (session_id, item.item_key, market.request_id),
            ).fetchone()
            market_json = canonical_json(market.object())
            stock_json = canonical_json(stock.object())
            decision_json = canonical_json(decision.object())
            if prior is not None:
                if (
                    prior["decision_id"] != decision.decision_id
                    or prior["market_json"] != market_json
                    or prior["stock_json"] != stock_json
                    or prior["decision_json"] != decision_json
                ):
                    raise SellerStoreError(
                        "market request provenance was reused with different decision facts"
                    )
                return decision
            db.execute(
                "INSERT INTO seller_decisions(session_id,decision_id,item_key,request_id,"
                "market_json,stock_json,"
                "decision_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    session_id,
                    decision.decision_id,
                    item.item_key,
                    market.request_id,
                    market_json,
                    stock_json,
                    decision_json,
                    timestamp,
                ),
            )
            if decision.kind == "finish":
                db.execute(
                    "UPDATE seller_progress SET finished=1 WHERE session_id=? AND item_key=?",
                    (session_id, item.item_key),
                )
                remaining = db.execute(
                    "SELECT COUNT(*) AS n FROM seller_progress WHERE session_id=? AND finished=0",
                    (session_id,),
                ).fetchone()["n"]
                if remaining == 0:
                    db.execute(
                        "UPDATE seller_sessions SET state='completed',"
                        "stop_reason='all admitted stock resolved' "
                        "WHERE session_id=?",
                        (session_id,),
                    )
            return decision

    def continue_recycle(
        self,
        session_id: str,
        anchor_decision_id: str,
        inventory: InventoryFacts,
        owned: OwnedFacts,
        *,
        now: int | None = None,
    ) -> tuple[str, tuple[CancelAuction, ...]]:
        """Reserve the next exact cancellation chunk from fresh stock and owned facts."""
        timestamp = int(time.time()) if now is None else now
        if self.ledger.has_uncertain_effect():
            raise SellerStoreError("an unresolved automation effect blocks recycle continuation")
        with self.ledger._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            session, policy = self._active_session(db, session_id, timestamp)
            decision = self._recompute_decision(db, session_id, anchor_decision_id, policy)
            item = policy.item(decision.item_key)
            progress = self._progress_row(db, session_id, item.item_key)
            if (
                decision.kind != "recycle"
                or not progress["recycling"]
                or progress["recycle_anchor_decision_id"] != anchor_decision_id
            ):
                raise SellerStoreError("recycle continuation is outside its persisted pass")
            if owned.item_key != item.item_key or inventory.item_key != item.item_key:
                raise SellerStoreError("recycle continuation facts identify another item")
            children = db.execute(
                "SELECT phase,state,batch_id,canonical_json FROM seller_children "
                "WHERE session_id=? AND decision_id=? "
                "AND phase LIKE 'continue_recycle:%' "
                "ORDER BY CAST(substr(phase,18) AS INTEGER)",
                (session_id, anchor_decision_id),
            ).fetchall()
            expected_chunk = 1
            failed_chunks = 0
            for index, child in enumerate(children):
                if child["phase"] != f"continue_recycle:{expected_chunk}":
                    raise SellerStoreError("recycle continuation chunks are not in exact sequence")
                if child["state"] in ("prepared", "running"):
                    if index != len(children) - 1:
                        raise SellerStoreError("only the latest recycle continuation chunk may remain open")
                    child_batch = Batch.from_json(child["canonical_json"])
                    ready_actions = self._ready_cancel_actions(db, child_batch)
                    self._validate_continuation_snapshot(
                        db,
                        session_id,
                        item,
                        inventory,
                        owned,
                        session.boot_id,
                        timestamp,
                        ready_actions,
                    )
                    targets = self._recycle_target_ids(db, session_id, anchor_decision_id)
                    cancel_actions = tuple(
                        action for action in child_batch.actions if isinstance(action, CancelAuction)
                    )
                    if len(cancel_actions) != len(child_batch.actions) or any(
                        action.auction_id not in targets for action in cancel_actions
                    ):
                        raise SellerStoreError("persisted recycle chunk differs from frozen pass IDs")
                    return child["phase"], cancel_actions
                if child["state"] not in ("succeeded", "failed"):
                    raise SellerStoreError("prior recycle continuation child has unresolved effect")
                if child["state"] == "failed":
                    failed_chunks += 1
                expected_chunk += 1
            if failed_chunks > 1:
                raise SellerStoreError("recycle pass exhausted its single bounded known-no-effect retry")
            self._reconcile_stock_in_transaction(
                db, session_id, item, inventory, owned, session.boot_id, timestamp
            )
            targets = self._recycle_target_ids(db, session_id, anchor_decision_id)
            active = self._managed_facts(db, session_id, item.item_key)
            if not {row.auction_id for row in active}.issubset(targets):
                raise SellerStoreError("managed auction ID is outside the frozen recycle pass")
            if not active:
                raise SellerStoreError("persisted recycle pass has no exact active auction to cancel")
            if any(
                row.status != "active"
                or not row.cancel_allowed
                or not row.cancel_cost_known
                or row.cancel_cost_copper > item.max_cancel_cost_copper
                for row in active
            ):
                raise SellerStoreError("recycle continuation lacks fresh bounded cancellation permission")
            phase = f"continue_recycle:{expected_chunk}"
            actions = tuple(
                CancelAuction(row.auction_id, item.max_cancel_cost_copper)
                for row in active[:MAX_SELLER_CANCELS_PER_CHILD]
            )
            return phase, actions

    def _continuation_child_is_resumable(self, batch_id: str) -> bool:
        with self.ledger._connect() as db:
            actions = db.execute(
                "SELECT state FROM actions WHERE batch_id=? ORDER BY ordinal", (batch_id,)
            ).fetchall()
            steps = db.execute("SELECT state FROM action_steps WHERE batch_id=?", (batch_id,)).fetchall()
        return (
            bool(actions)
            and all(row["state"] in ("ready", "succeeded") for row in actions)
            and all(row["state"] in ("ready", "succeeded") for row in steps)
        )

    @staticmethod
    def _ready_cancel_actions(db: sqlite3.Connection, batch: Batch) -> tuple[Action, ...]:
        rows = db.execute(
            "SELECT ordinal,state FROM actions WHERE batch_id=? ORDER BY ordinal", (batch.batch_id,)
        ).fetchall()
        if len(rows) != len(batch.actions) or any(row["state"] not in ("ready", "succeeded") for row in rows):
            raise SellerStoreError("resumed recycle child has a failed or uncertain action")
        ready = tuple(batch.actions[row["ordinal"]] for row in rows if row["state"] == "ready")
        if not ready or any(not isinstance(action, CancelAuction) for action in ready):
            raise SellerStoreError("resumed recycle child has no exact ready cancellation action")
        return ready

    def _validate_continuation_snapshot(
        self,
        db: sqlite3.Connection,
        session_id: str,
        item: SellerItemPolicy,
        inventory: InventoryFacts,
        owned: OwnedFacts,
        boot_id: str,
        now: int,
        actions: tuple[Action, ...],
    ) -> None:
        if inventory.item_key != item.item_key or owned.item_key != item.item_key:
            raise SellerStoreError("resumed continuation facts identify another policy item")
        self._check_fact_fresh(
            inventory.client_boot_id, inventory.observed_at, inventory.complete, boot_id, now
        )
        self._check_fact_fresh(owned.client_boot_id, owned.observed_at, owned.complete, boot_id, now)
        if abs(owned.observed_at - inventory.observed_at) > MAX_PROGRESS_AGE_SECONDS:
            raise SellerStoreError("resumed recycle inventory and owned facts are incoherent")
        progress = self._progress_row(db, session_id, item.item_key)
        if inventory.bag_quantity < progress["bag_quantity"]:
            raise SellerStoreError("resumed recycle inventory is below admitted bag stock")
        active = self._managed_facts(db, session_id, item.item_key)
        observed = {row.auction_id: row for row in owned.auctions}
        if any(observed.get(row.auction_id) != row for row in active):
            raise SellerStoreError("resumed recycle owned facts changed a frozen managed auction")
        active_by_id = {row.auction_id: row for row in active}
        for action in actions:
            if not isinstance(action, CancelAuction):
                raise SellerStoreError("resumed recycle child contains a non-cancellation action")
            owned_row = active_by_id.get(action.auction_id)
            if (
                owned_row is None
                or not owned_row.cancel_allowed
                or not owned_row.cancel_cost_known
                or owned_row.cancel_cost_copper > item.max_cancel_cost_copper
                or action.max_cancel_cost_copper != item.max_cancel_cost_copper
            ):
                raise SellerStoreError("resumed recycle child no longer has exact bounded cancel proof")
        self._insert_observation(
            db,
            session_id,
            item.item_key,
            "inventory",
            inventory.request_id,
            inventory.sequence,
            inventory.observed_at,
            inventory.object(),
        )
        self._insert_observation(
            db,
            session_id,
            item.item_key,
            "owned",
            owned.request_id,
            owned.sequence,
            owned.observed_at,
            owned.object(),
        )

    def recovery_to_mail(
        self,
        session_id: str,
        anchor_decision_id: str,
        *,
        now: int | None = None,
    ) -> tuple[str, tuple[Action, ...]]:
        """Derive one outbound route phase from the current explicit AH resume epoch."""
        timestamp = int(time.time()) if now is None else now
        if self.ledger.has_uncertain_effect():
            raise SellerStoreError("an unresolved automation effect blocks mailbox route recovery")
        with self.ledger._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            session, policy = self._active_session(db, session_id, timestamp)
            row = db.execute(
                "SELECT resume_epoch_id,resume_location FROM seller_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            epoch = row["resume_epoch_id"]
            if row["resume_location"] != "ah" or type(epoch) is not str or len(epoch) != 64:
                raise SellerStoreError("mailbox route recovery requires a fresh AH resume epoch")
            decision = self._recompute_decision(db, session_id, anchor_decision_id, policy)
            progress = self._progress_row(db, session_id, decision.item_key)
            if (
                decision.kind != "recycle"
                or not progress["recycling"]
                or progress["recycle_anchor_decision_id"] != anchor_decision_id
                or progress["managed_active_quantity"]
                or not progress["returning_quantity"]
            ):
                raise SellerStoreError("mailbox route recovery requires a waiting persisted return pass")
            phase = f"recovery_to_mail:{epoch}"
            existing = db.execute(
                "SELECT canonical_json,state FROM seller_children WHERE session_id=? "
                "AND decision_id=? AND phase=?",
                (session_id, anchor_decision_id, phase),
            ).fetchone()
            if existing is not None:
                if existing["state"] not in ("prepared", "running"):
                    raise SellerStoreError("mailbox recovery child is already terminal for this resume epoch")
                return phase, Batch.from_json(existing["canonical_json"]).actions
            stock = self._stock_fact(db, session_id, decision.item_key)
            self._validate_stock_snapshot(db, session_id, stock, session.boot_id, timestamp)
            return phase, (MoveRoute(policy.outbound_route_id), Interact(policy.mailbox_target_id))

    def child_batch(self, session_id: str, decision_id: str, phase: str) -> Batch | None:
        """Return the exact persisted child intent and deadline for safe process recovery."""
        with self.ledger._connect() as db:
            row = db.execute(
                "SELECT canonical_json FROM seller_children WHERE session_id=? AND decision_id=? AND phase=?",
                (session_id, decision_id, phase),
            ).fetchone()
        return None if row is None else Batch.from_json(row["canonical_json"])

    def recovery_to_ah(
        self,
        session_id: str,
        anchor_decision_id: str,
        *,
        now: int | None = None,
    ) -> tuple[str, tuple[Action, ...]]:
        """Derive one return route from a fresh explicit mailbox resume epoch."""
        timestamp = int(time.time()) if now is None else now
        if self.ledger.has_uncertain_effect():
            raise SellerStoreError("an unresolved automation effect blocks Auction House route recovery")
        with self.ledger._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            session, policy = self._active_session(db, session_id, timestamp)
            row = db.execute(
                "SELECT resume_epoch_id,resume_location FROM seller_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            epoch = row["resume_epoch_id"]
            if row["resume_location"] != "mailbox" or type(epoch) is not str or len(epoch) != 64:
                raise SellerStoreError("Auction House route recovery requires a fresh mailbox resume epoch")
            decision = self._recompute_decision(db, session_id, anchor_decision_id, policy)
            progress = self._progress_row(db, session_id, decision.item_key)
            if (
                decision.kind != "recycle"
                or not progress["recycling"]
                or progress["recycle_anchor_decision_id"] != anchor_decision_id
                or progress["managed_active_quantity"]
                or progress["returning_quantity"]
                or not progress["bag_quantity"]
            ):
                raise SellerStoreError("Auction House route recovery requires reconciled returned bag stock")
            phase = f"recovery_to_ah:{epoch}"
            existing = db.execute(
                "SELECT canonical_json,state FROM seller_children WHERE session_id=? "
                "AND decision_id=? AND phase=?",
                (session_id, anchor_decision_id, phase),
            ).fetchone()
            if existing is not None:
                if existing["state"] not in ("prepared", "running"):
                    raise SellerStoreError("Auction House recovery child is terminal for this resume epoch")
                return phase, Batch.from_json(existing["canonical_json"]).actions
            inventory = self._latest_observation(db, session_id, decision.item_key, "inventory")
            if inventory is None:
                raise SellerStoreError("mailbox resume has no current inventory proof")
            self._check_fact_fresh(
                inventory.client_boot_id,
                inventory.observed_at,
                True,
                session.boot_id,
                timestamp,
            )
            return phase, (MoveRoute(policy.return_route_id), Interact(policy.ah_target_id))

    def complete_recycle_at_ah(
        self,
        session_id: str,
        anchor_decision_id: str,
        *,
        now: int | None = None,
    ) -> None:
        """Complete a returned-stock pass after a fresh explicit Auction House resume."""
        timestamp = int(time.time()) if now is None else now
        if self.ledger.has_uncertain_effect():
            raise SellerStoreError("an unresolved automation effect blocks recycle completion")
        with self.ledger._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            session, policy = self._active_session(db, session_id, timestamp)
            epoch = db.execute(
                "SELECT resume_epoch_id,resume_location FROM seller_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            epoch_id = epoch["resume_epoch_id"]
            if (
                epoch["resume_location"] != "ah"
                or type(epoch_id) is not str
                or len(epoch_id) != 64
                or any(char not in "0123456789abcdef" for char in epoch_id)
            ):
                raise SellerStoreError("recycle completion requires an explicit fresh AH resume epoch")
            decision = self._recompute_decision(db, session_id, anchor_decision_id, policy)
            progress = self._progress_row(db, session_id, decision.item_key)
            if (
                decision.kind != "recycle"
                or not progress["recycling"]
                or progress["recycle_anchor_decision_id"] != anchor_decision_id
                or progress["managed_active_quantity"]
                or progress["returning_quantity"]
                or not progress["bag_quantity"]
            ):
                raise SellerStoreError("recycle pass is not fully reconciled at the Auction House")
            route_rows = db.execute(
                "SELECT phase,state FROM seller_children WHERE session_id=? AND decision_id=? "
                "AND (phase='travel_to_ah' OR phase LIKE 'recovery_to_ah:%')",
                (session_id, anchor_decision_id),
            ).fetchall()
            if any(row["state"] in ("prepared", "running", "uncertain") for row in route_rows):
                raise SellerStoreError("recycle completion has an unfinished Auction House route")
            if route_rows:
                if not any(row["state"] in ("failed", "expired") for row in route_rows):
                    raise SellerStoreError(
                        "recycle completion requires a terminal failed or expired AH route"
                    )
            else:
                collected = db.execute(
                    "SELECT 1 FROM seller_children WHERE session_id=? AND decision_id=? "
                    "AND phase LIKE 'collect_return:%' AND state='succeeded' LIMIT 1",
                    (session_id, anchor_decision_id),
                ).fetchone()
                if collected is None:
                    raise SellerStoreError("recycle completion needs a collected return or failed AH route")
            stock = self._stock_fact(db, session_id, decision.item_key)
            self._validate_stock_snapshot(db, session_id, stock, session.boot_id, timestamp)
            db.execute(
                "UPDATE seller_progress SET recycling=0,recycle_anchor_decision_id=NULL "
                "WHERE session_id=? AND item_key=?",
                (session_id, decision.item_key),
            )
            self._assert_conservation(db, session_id, decision.item_key)

    def record_returns(
        self,
        session_id: str,
        recycle_anchor_decision_id: str,
        candidates: tuple[ReturnCandidate, ...],
        *,
        now: int | None = None,
    ) -> None:
        timestamp = int(time.time()) if now is None else now
        with self.ledger._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            session, policy = self._active_session(db, session_id, timestamp)
            decision_row = self._decision_row(db, session_id, recycle_anchor_decision_id)
            item_key = decision_row["item_key"]
            item = policy.item(item_key)
            progress = self._progress_row(db, session_id, item_key)
            if (
                not progress["recycling"]
                or progress["recycle_anchor_decision_id"] != recycle_anchor_decision_id
            ):
                raise SellerStoreError("return candidates are outside the persisted recycle pass")
            if type(candidates) is not tuple:
                raise SellerStoreError("return candidates must be an immutable tuple")
            for candidate in candidates:
                if type(candidate) is not ReturnCandidate:
                    raise SellerStoreError("return observation contains an unsupported candidate")
                self._check_fact_fresh(
                    candidate.client_boot_id,
                    candidate.observed_at,
                    candidate.complete,
                    session.boot_id,
                    timestamp,
                )
                if candidate.eligible:
                    if (
                        candidate.item_key != item_key
                        or candidate.quantity is None
                        or candidate.quantity > progress["returning_quantity"]
                    ):
                        raise SellerStoreError(
                            "return candidate exceeds the exact outstanding item return pool"
                        )
                if candidate.mail_fingerprint:
                    self._upsert_return_candidate(db, session_id, recycle_anchor_decision_id, candidate)
            _ = item

    def actions_for_phase(
        self,
        session_id: str,
        decision_id: str,
        phase: str = "decision",
        *,
        now: int | None = None,
    ) -> tuple[Action, ...]:
        timestamp = int(time.time()) if now is None else now
        with self.ledger._connect() as db:
            session, policy = self._active_session(db, session_id, timestamp)
            decision = self._recompute_decision(db, session_id, decision_id, policy)
            return self._expected_actions(db, session_id, decision_id, phase, policy, decision, timestamp)

    def prepare_child(
        self,
        session_id: str,
        decision_id: str,
        batch: Batch,
        *,
        phase: str = "decision",
        reservation_copper: int | None = None,
        now: int | None = None,
    ) -> None:
        timestamp = int(time.time()) if now is None else now
        self.ledger.prepare(batch, now=timestamp)
        with self.ledger._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            session, policy = self._active_session(db, session_id, timestamp)
            decision = self._recompute_decision(db, session_id, decision_id, policy)
            self._validate_batch_identity(batch, policy, timestamp, decision, phase)
            if phase == "decision":
                self._validate_decision_stock(db, session_id, decision_id, session.boot_id, timestamp)
            expected = self._expected_actions(db, session_id, decision_id, phase, policy, decision, timestamp)
            if batch.actions != expected:
                raise SellerStoreError("child actions differ from the exact persisted policy-derived phase")
            phase_deadline = self._phase_deadline(db, session_id, decision_id, phase, decision, timestamp)
            if batch.expires_at > min(policy.expires_at, phase_deadline):
                raise SellerStoreError("child deadline exceeds policy expiry or phase evidence deadline")
            progress = self._progress_row(db, session_id, decision.item_key)
            if progress["finished"]:
                raise SellerStoreError("finished item cannot accept a new child batch")
            if phase == "decision" and progress["recycling"] and decision.kind == "post":
                raise SellerStoreError("a persisted recycle pass must finish before new posts are admitted")
            existing = db.execute(
                "SELECT batch_id,canonical_json FROM seller_children "
                "WHERE session_id=? AND decision_id=? AND phase=?",
                (session_id, decision_id, phase),
            ).fetchone()
            if existing is not None:
                if existing["batch_id"] != batch.batch_id or existing["canonical_json"] != batch.canonical:
                    raise SellerStoreError("seller decision phase was already consumed by a different child")
                return
            if phase == "decision" and decision.kind == "post":
                last_attempt = progress["last_post_attempt_at"]
                if last_attempt is not None and timestamp < last_attempt + 60:
                    raise SellerStoreError("a post attempt for this item occurred within the last 60 seconds")
            amounts = [self._reservation_amount(action) for action in batch.actions]
            if reservation_copper is not None and reservation_copper != sum(amounts):
                raise SellerStoreError("requested reservation does not equal policy action caps")
            reserved = db.execute(
                "SELECT COALESCE(SUM(amount_copper),0) AS total FROM seller_reservations "
                "WHERE session_id=? AND state IN ('reserved','unknown')",
                (session_id,),
            ).fetchone()["total"]
            total_spent = db.execute(
                "SELECT spent_copper FROM seller_sessions WHERE session_id=?", (session_id,)
            ).fetchone()["spent_copper"]
            if reserved + total_spent + sum(amounts) > policy.max_total_spend_copper:
                raise SellerStoreError("child action budget exceeds the immutable session spending limit")
            if phase == "decision" and decision.kind == "post":
                post = batch.actions[0]
                if not isinstance(post, PostCommodity) or post.quantity > progress["bag_quantity"]:
                    raise SellerStoreError("post exceeds durably admitted bag stock")
                db.execute(
                    "UPDATE seller_progress SET bag_quantity=bag_quantity-?,"
                    "unresolved_quantity=unresolved_quantity+? "
                    "WHERE session_id=? AND item_key=?",
                    (post.quantity, post.quantity, session_id, decision.item_key),
                )
                db.execute(
                    "UPDATE seller_progress SET last_post_attempt_at=?,last_post_decision_id=? "
                    "WHERE session_id=? AND item_key=?",
                    (timestamp, decision_id, session_id, decision.item_key),
                )
            if phase == "decision" and decision.kind == "recycle":
                if not progress["recycling"]:
                    rounds = db.execute(
                        "SELECT COALESCE(SUM(recycle_rounds),0) AS total FROM seller_progress "
                        "WHERE session_id=?",
                        (session_id,),
                    ).fetchone()["total"]
                    if rounds >= policy.max_recycle_rounds:
                        raise SellerStoreError("session recycle-round cap is exhausted")
                    db.execute(
                        "UPDATE seller_progress SET recycle_rounds=recycle_rounds+1,recycling=1,"
                        "recycle_anchor_decision_id=? WHERE session_id=? AND item_key=?",
                        (decision_id, session_id, decision.item_key),
                    )
                elif progress["recycle_anchor_decision_id"] is None:
                    raise SellerStoreError("recycle pass lost its durable anchor")
            db.execute(
                "INSERT INTO seller_children(session_id,decision_id,phase,batch_id,"
                "canonical_json,state,created_at) "
                "VALUES(?,?,?,?,?,'prepared',?)",
                (session_id, decision_id, phase, batch.batch_id, batch.canonical, timestamp),
            )
            db.executemany(
                "INSERT INTO seller_reservations(session_id,decision_id,phase,ordinal,"
                "amount_copper,actual_copper,state) "
                "VALUES(?,?,?,?,?,NULL,?)",
                [
                    (session_id, decision_id, phase, ordinal, amount, "reserved" if amount else "reconciled")
                    for ordinal, amount in enumerate(amounts)
                ],
            )
            self._assert_conservation(db, session_id, decision.item_key)
            _ = session

    def validate_child(
        self,
        session_id: str,
        decision_id: str,
        batch: Batch,
        admission: Admission | None,
        *,
        phase: str = "decision",
        now: int | None = None,
    ) -> None:
        timestamp = int(time.time()) if now is None else now
        with self.ledger._connect() as db:
            session, policy = self._active_session(db, session_id, timestamp)
            decision = self._recompute_decision(db, session_id, decision_id, policy)
            row = db.execute(
                "SELECT canonical_json,batch_id,state,created_at FROM seller_children "
                "WHERE session_id=? AND decision_id=? AND phase=?",
                (session_id, decision_id, phase),
            ).fetchone()
            if row is None or row["batch_id"] != batch.batch_id or row["canonical_json"] != batch.canonical:
                raise AdmissionDenied("seller child is not durably linked to this decision and policy")
            if row["state"] in ("failed", "uncertain"):
                raise AdmissionDenied("seller child failed or has an unresolved effect")
            if phase == "decision":
                self._validate_decision_stock(db, session_id, decision_id, session.boot_id, timestamp)
            deadline = self._phase_deadline(db, session_id, decision_id, phase, decision, row["created_at"])
            if timestamp >= min(policy.expires_at, session.expires_at, batch.expires_at, deadline):
                raise AdmissionDenied("seller policy, child or phase evidence expired")
            expected = self._expected_actions(db, session_id, decision_id, phase, policy, decision, timestamp)
            if batch.actions != expected:
                raise AdmissionDenied("seller child actions no longer match persisted policy derivation")
            if self.ledger.stop_requested():
                raise AdmissionDenied("durable automation stop is active")
            if admission is not None:
                if (
                    admission.session_boot_id != session.boot_id
                    or admission.house != policy.house
                    or admission.character != policy.character
                    or admission.client_build != policy.client_build
                ):
                    raise AdmissionDenied("seller session boot, build or character identity changed")
            if phase == "decision" and decision.kind == "post":
                progress = self._progress_row(db, session_id, decision.item_key)
                if progress["last_post_decision_id"] != decision_id:
                    raise AdmissionDenied(
                        "post child has no matching persisted 60-second attempt reservation"
                    )

    def release_before_dispatch(
        self,
        session_id: str,
        decision_id: str,
        phase: str,
        batch: Batch,
        *,
        now: int,
    ) -> None:
        with self.ledger._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            child = db.execute(
                "SELECT state FROM seller_children WHERE session_id=? AND decision_id=? AND phase=?",
                (session_id, decision_id, phase),
            ).fetchone()
            if child is None:
                raise SellerStoreError("cannot release a child absent from the seller ledger")
            if child["state"] in ("failed", "succeeded"):
                return
            if child["state"] == "uncertain" and not self._continuation_child_is_resumable(batch.batch_id):
                return
            decision = self._decision_row(db, session_id, decision_id)
            decision_obj = json.loads(decision["decision_json"])
            for ordinal, action in enumerate(batch.actions):
                marker = db.execute(
                    "SELECT COUNT(*) AS n FROM action_steps WHERE batch_id=? AND ordinal=? "
                    "AND dispatched_at IS NOT NULL",
                    (batch.batch_id, ordinal),
                ).fetchone()["n"]
                if marker:
                    continue
                db.execute(
                    "UPDATE seller_reservations SET state='released',actual_copper=0 "
                    "WHERE session_id=? AND decision_id=? AND phase=? AND ordinal=? AND state='reserved'",
                    (session_id, decision_id, phase, ordinal),
                )
                if (
                    phase == "decision"
                    and decision_obj["kind"] == "post"
                    and isinstance(action, PostCommodity)
                ):
                    progress = self._progress_row(db, session_id, action.item_key)
                    if progress["unresolved_quantity"] < action.quantity:
                        raise SellerStoreError("expired post child lost its exact reserved stock provenance")
                    db.execute(
                        "UPDATE seller_progress SET bag_quantity=bag_quantity+?,unresolved_quantity="
                        "unresolved_quantity-? WHERE session_id=? AND item_key=?",
                        (action.quantity, action.quantity, session_id, action.item_key),
                    )
                    self._assert_conservation(db, session_id, action.item_key)
            db.execute(
                "UPDATE seller_children SET state='failed' WHERE session_id=? AND decision_id=? AND phase=? "
                "AND state IN ('prepared','running','uncertain')",
                (session_id, decision_id, phase),
            )
            _ = now

    def apply_terminal_step(
        self,
        db: sqlite3.Connection,
        *,
        session_id: str,
        decision_id: str,
        phase: str,
        batch: Batch,
        ordinal: int,
        action: Action,
        state: Literal["succeeded", "failed", "uncertain"],
        receipt: dict[str, object] | None,
        reason: str | None,
        now: int,
    ) -> None:
        child = db.execute(
            "SELECT batch_id FROM seller_children WHERE session_id=? AND decision_id=? AND phase=?",
            (session_id, decision_id, phase),
        ).fetchone()
        if child is None or child["batch_id"] != batch.batch_id:
            raise SellerStoreError("terminal ledger step does not match a persisted seller child")
        reservation = db.execute(
            "SELECT amount_copper,state FROM seller_reservations WHERE session_id=? AND decision_id=? "
            "AND phase=? AND ordinal=?",
            (session_id, decision_id, phase, ordinal),
        ).fetchone()
        if reservation is None:
            raise SellerStoreError("seller child action has no durable budget reservation")
        callback = receipt.get("callback_delivered") if receipt else None
        verified_no_effect = bool(receipt and receipt.get("postcondition") is False)
        structured = receipt.get("result") if receipt else None
        known_no_effect = (
            callback is False
            or verified_no_effect
            or bool(reason and reason.startswith("pre-dispatch revalidation failed:"))
        )
        if isinstance(action, PostCommodity) and (callback is True or state == "uncertain"):
            dispatched = db.execute(
                "SELECT dispatched_at FROM action_steps WHERE batch_id=? AND ordinal=? "
                "ORDER BY step DESC LIMIT 1",
                (batch.batch_id, ordinal),
            ).fetchone()
            if callback is True or (dispatched is not None and dispatched["dispatched_at"] is not None):
                db.execute(
                    "UPDATE seller_progress SET last_post_attempt_at=MAX(COALESCE(last_post_attempt_at,0),?) "
                    "WHERE session_id=? AND item_key=?",
                    (now, session_id, action.item_key),
                )
        if state == "succeeded":
            if isinstance(action, (PostCommodity, CancelAuction, CollectReturn)):
                if type(structured) is not dict or not dispatch_result_matches_action(structured, action):
                    raise SellerStoreError("seller effect receipt lacks exact structured result data")
                self._apply_effect_result(
                    db,
                    session_id,
                    decision_id,
                    phase,
                    batch,
                    ordinal,
                    action,
                    structured,
                    now,
                    reservation["amount_copper"],
                )
            elif reservation["amount_copper"]:
                raise SellerStoreError("non-economic child action unexpectedly reserved a charge")
            if (phase == "travel_to_ah" or phase.startswith("recovery_to_ah:")) and ordinal == len(
                batch.actions
            ) - 1:
                decision_row = self._decision_row(db, session_id, decision_id)
                decision_obj = json.loads(decision_row["decision_json"])
                if decision_obj["kind"] == "recycle":
                    progress = self._progress_row(db, session_id, decision_obj["itemKey"])
                    if progress["returning_quantity"] or not progress["bag_quantity"]:
                        raise SellerStoreError(
                            "return route completed before all exact admitted returns were reconciled"
                        )
                    db.execute(
                        "UPDATE seller_progress SET recycling=0,recycle_anchor_decision_id=NULL "
                        "WHERE session_id=? AND item_key=?",
                        (session_id, decision_obj["itemKey"]),
                    )
                    db.execute(
                        "UPDATE seller_sessions SET resume_location='ah' WHERE session_id=?",
                        (session_id,),
                    )
            if phase.startswith("recovery_to_mail:") and ordinal == len(batch.actions) - 1:
                db.execute(
                    "UPDATE seller_sessions SET resume_location='mailbox' WHERE session_id=?",
                    (session_id,),
                )
        elif known_no_effect:
            if isinstance(action, PostCommodity):
                progress = self._progress_row(db, session_id, action.item_key)
                if progress["unresolved_quantity"] < action.quantity:
                    raise SellerStoreError("failed post has no exact reserved stock to restore")
                db.execute(
                    "UPDATE seller_progress SET bag_quantity=bag_quantity+?,unresolved_quantity="
                    "unresolved_quantity-? WHERE session_id=? AND item_key=?",
                    (action.quantity, action.quantity, session_id, action.item_key),
                )
            db.execute(
                "UPDATE seller_reservations SET state='released',actual_copper=0 "
                "WHERE session_id=? AND decision_id=? AND phase=? AND ordinal=?",
                (session_id, decision_id, phase, ordinal),
            )
            if state == "failed":
                self._release_future_reservations(db, session_id, decision_id, phase, batch, ordinal + 1)
        else:
            db.execute(
                "UPDATE seller_reservations SET state='unknown' WHERE session_id=? AND decision_id=? "
                "AND phase=? AND ordinal=? AND amount_copper>0",
                (session_id, decision_id, phase, ordinal),
            )
        self._update_child_state(db, session_id, decision_id, phase, batch.batch_id)

    def _release_future_reservations(
        self,
        db: sqlite3.Connection,
        session_id: str,
        decision_id: str,
        phase: str,
        batch: Batch,
        first_ordinal: int,
    ) -> None:
        for ordinal in range(first_ordinal, len(batch.actions)):
            action_row = db.execute(
                "SELECT state FROM actions WHERE batch_id=? AND ordinal=?",
                (batch.batch_id, ordinal),
            ).fetchone()
            marker = db.execute(
                "SELECT 1 FROM action_steps WHERE batch_id=? AND ordinal=? "
                "AND dispatched_at IS NOT NULL LIMIT 1",
                (batch.batch_id, ordinal),
            ).fetchone()
            if action_row is None or action_row["state"] != "ready" or marker is not None:
                continue
            db.execute(
                "UPDATE seller_reservations SET state='released',actual_copper=0 "
                "WHERE session_id=? AND decision_id=? AND phase=? AND ordinal=? AND state='reserved'",
                (session_id, decision_id, phase, ordinal),
            )

    def item_progress(self, session_id: str, item_key: str) -> SellerItemProgress:
        with self.ledger._connect() as db:
            return self._progress(db, session_id, item_key)

    def child_status(self, session_id: str, decision_id: str, phase: str) -> str | None:
        """Read the persisted state of one exact phase for safe loop resumption."""
        with self.ledger._connect() as db:
            row = db.execute(
                "SELECT state FROM seller_children WHERE session_id=? AND decision_id=? AND phase=?",
                (session_id, decision_id, phase),
            ).fetchone()
        return None if row is None else row["state"]

    def report(self, session_id: str, *, now: int | None = None) -> dict[str, object]:
        status = self.status(session_id, now=now)
        with self.ledger._connect() as db:
            policy = SellerPolicy.from_json(
                db.execute(
                    "SELECT policy_json FROM seller_sessions WHERE session_id=?", (session_id,)
                ).fetchone()["policy_json"]
            )
            items: list[dict[str, object]] = []
            for item in policy.items:
                progress = self._progress(db, session_id, item.item_key)
                last = db.execute(
                    "SELECT decision_id,request_id,decision_json,created_at FROM seller_decisions "
                    "WHERE session_id=? AND item_key=? ORDER BY created_at DESC LIMIT 1",
                    (session_id, item.item_key),
                ).fetchone()
                child = None
                if last is not None:
                    child = db.execute(
                        "SELECT phase,batch_id,state FROM seller_children "
                        "WHERE session_id=? AND decision_id=? "
                        "ORDER BY created_at DESC LIMIT 1",
                        (session_id, last["decision_id"]),
                    ).fetchone()
                last_decision = json.loads(last["decision_json"]) if last is not None else None
                items.append(
                    {
                        "progress": asdict(progress),
                        "lastDecision": last_decision,
                        "lastObservedAt": last["created_at"] if last is not None else None,
                        "lastRequestId": last["request_id"] if last is not None else None,
                        "child": dict(child) if child is not None else None,
                    }
                )
        return {"session": asdict(status), "items": items}

    def managed_auctions(self, session_id: str, item_key: str) -> tuple[ManagedAuction, ...]:
        with self.ledger._connect() as db:
            rows = db.execute(
                "SELECT * FROM seller_managed_auctions WHERE session_id=? AND item_key=? AND status='active' "
                "ORDER BY auction_id",
                (session_id, item_key),
            ).fetchall()
        return tuple(
            ManagedAuction(
                row["auction_id"],
                row["item_key"],
                row["remaining_quantity"],
                row["unit_price_copper"],
                True,
                row["created_batch_id"],
            )
            for row in rows
        )

    def _apply_effect_result(
        self,
        db: sqlite3.Connection,
        session_id: str,
        decision_id: str,
        phase: str,
        batch: Batch,
        ordinal: int,
        action: Action,
        result: dict[str, object],
        now: int,
        reserved: int,
    ) -> None:
        actual: int | None = 0
        item_key: str | None = None
        if isinstance(action, PostCommodity):
            item_key = action.item_key
            deposit = result["depositCopper"]
            if deposit is not None and (type(deposit) is not int or deposit > action.max_deposit_copper):
                raise SellerStoreError("actual listing deposit exceeds the action or policy cap")
            actual = deposit
            if actual is None:
                actual = None
            auction_id = result["auctionId"]
            assert type(auction_id) is int
            existing = db.execute(
                "SELECT 1 FROM seller_managed_auctions WHERE session_id=? AND auction_id=?",
                (session_id, auction_id),
            ).fetchone()
            if existing is not None:
                raise SellerStoreError("causal post receipt reused a managed auction ID")
            session = self._session_by_id(db, session_id)
            persisted_policy_id = db.execute(
                "SELECT policy_id FROM seller_sessions WHERE session_id=?", (session_id,)
            ).fetchone()["policy_id"]
            if session.policy.policy_id != persisted_policy_id:
                raise SellerStoreError("persisted seller policy digest is inconsistent")
            item = session.policy.item(item_key)
            progress = self._progress_row(db, session_id, item_key)
            if progress["unresolved_quantity"] < action.quantity:
                raise SellerStoreError("post receipt has no matching durable stock reservation")
            db.execute(
                "UPDATE seller_progress SET unresolved_quantity=unresolved_quantity-?,"
                "managed_active_quantity=managed_active_quantity+? WHERE session_id=? AND item_key=?",
                (action.quantity, action.quantity, session_id, item_key),
            )
            db.execute(
                "INSERT INTO seller_managed_auctions(session_id,item_key,auction_id,remaining_quantity,"
                "unit_price_copper,status,cancel_allowed,cancel_cost_known,cancel_cost_copper,created_batch_id,"
                "recycle_anchor_decision_id,updated_at) VALUES(?,?,?,?,?,'active',0,0,0,?,NULL,?)",
                (
                    session_id,
                    item_key,
                    auction_id,
                    action.quantity,
                    action.unit_price_copper,
                    batch.batch_id,
                    now,
                ),
            )
            _ = item
        elif isinstance(action, CancelAuction):
            raw_item_key = result["itemKey"]
            if type(raw_item_key) is not str:
                raise SellerStoreError("cancel result has no exact item identity")
            item_key = raw_item_key
            cancel_cost = result["cancelCostCopper"]
            if cancel_cost is not None and type(cancel_cost) is not int:
                raise SellerStoreError("cancel result cost must be an exact integer or unknown")
            if type(cancel_cost) is int and cancel_cost > action.max_cancel_cost_copper:
                raise SellerStoreError("actual cancellation cost exceeds the action or policy cap")
            actual = cancel_cost if type(cancel_cost) is int else None
            auction_id = action.auction_id
            managed = db.execute(
                "SELECT * FROM seller_managed_auctions WHERE session_id=? AND auction_id=?",
                (session_id, auction_id),
            ).fetchone()
            if managed is None or managed["status"] != "active" or managed["item_key"] != item_key:
                raise SellerStoreError("cancel receipt does not identify an active causal managed auction")
            tracked = managed["remaining_quantity"]
            remaining = result["remainingQuantity"]
            assert type(remaining) is int
            if remaining > tracked:
                raise SellerStoreError("cancel result quantity exceeds the causal managed listing")
            progress = self._progress_row(db, session_id, item_key)
            db.execute(
                "UPDATE seller_progress SET managed_active_quantity=managed_active_quantity-?,"
                "returning_quantity=returning_quantity+?,unresolved_quantity=unresolved_quantity+? "
                "WHERE session_id=? AND item_key=?",
                (tracked, remaining, tracked - remaining, session_id, item_key),
            )
            anchor = progress["recycle_anchor_decision_id"]
            if anchor is None:
                raise SellerStoreError("cancel result has no active recycle pass")
            db.execute(
                "UPDATE seller_managed_auctions SET status='cancelled',remaining_quantity=0,"
                "recycle_anchor_decision_id=?,updated_at=? WHERE session_id=? AND auction_id=?",
                (anchor, now, session_id, auction_id),
            )
            if (
                remaining == 0
                and progress["returning_quantity"] == 0
                and progress["managed_active_quantity"] == tracked
            ):
                db.execute(
                    "UPDATE seller_progress SET recycling=0,recycle_anchor_decision_id=NULL "
                    "WHERE session_id=? AND item_key=?",
                    (session_id, item_key),
                )
        elif isinstance(action, CollectReturn):
            item_key = action.item_key
            if result["mailFingerprint"] != action.mail_fingerprint:
                raise SellerStoreError("collected return result differs from exact eligible candidate")
            progress = self._progress_row(db, session_id, item_key)
            if progress["returning_quantity"] < action.quantity:
                raise SellerStoreError("return collection exceeds outstanding cancelled quantity")
            db.execute(
                "UPDATE seller_progress SET returning_quantity=returning_quantity-?,"
                "bag_quantity=bag_quantity+? "
                "WHERE session_id=? AND item_key=?",
                (action.quantity, action.quantity, session_id, item_key),
            )
            candidate = db.execute(
                "SELECT consumed,recycle_anchor_decision_id,item_key,quantity,eligible FROM seller_returns "
                "WHERE session_id=? AND mail_fingerprint=?",
                (session_id, action.mail_fingerprint),
            ).fetchone()
            if (
                candidate is None
                or candidate["consumed"]
                or candidate["recycle_anchor_decision_id"] != progress["recycle_anchor_decision_id"]
                or candidate["item_key"] != action.item_key
                or candidate["quantity"] != action.quantity
                or not candidate["eligible"]
            ):
                raise SellerStoreError("return candidate was consumed or detached from this recycle pass")
            db.execute(
                "UPDATE seller_returns SET consumed=1 WHERE session_id=? AND mail_fingerprint=?",
                (session_id, action.mail_fingerprint),
            )
        if reserved > 0:
            if actual is None:
                db.execute(
                    "UPDATE seller_reservations SET state='unknown' WHERE session_id=? AND decision_id=? "
                    "AND phase=? AND ordinal=?",
                    (session_id, decision_id, phase, ordinal),
                )
            else:
                if actual > reserved:
                    raise SellerStoreError("actual charge exceeds atomically reserved budget")
                db.execute(
                    "UPDATE seller_reservations SET state='reconciled',actual_copper=? WHERE session_id=? "
                    "AND decision_id=? AND phase=? AND ordinal=?",
                    (actual, session_id, decision_id, phase, ordinal),
                )
                db.execute(
                    "UPDATE seller_sessions SET spent_copper=spent_copper+? WHERE session_id=?",
                    (actual, session_id),
                )
                if item_key is not None:
                    db.execute(
                        "UPDATE seller_progress SET spent_copper=spent_copper+? "
                        "WHERE session_id=? AND item_key=?",
                        (actual, session_id, item_key),
                    )
        if item_key is not None:
            self._assert_conservation(db, session_id, item_key)

    def _expected_actions(
        self,
        db: sqlite3.Connection,
        session_id: str,
        decision_id: str,
        phase: str,
        policy: SellerPolicy,
        decision: SellerDecision,
        now: int,
    ) -> tuple[Action, ...]:
        if phase == "decision":
            if decision.kind not in ("post", "recycle") or not decision.actions:
                raise SellerStoreError("this seller decision has no protected child action")
            return decision.actions
        item = policy.item(decision.item_key)
        progress = self._progress_row(db, session_id, item.item_key)
        if not progress["recycling"] or progress["recycle_anchor_decision_id"] != decision_id:
            raise SellerStoreError("recycle phase is not bound to its persisted pass anchor")
        if phase.startswith("recovery_to_mail:"):
            epoch = phase.removeprefix("recovery_to_mail:")
            session_row = db.execute(
                "SELECT resume_epoch_id,resume_location FROM seller_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if (
                len(epoch) != 64
                or any(char not in "0123456789abcdef" for char in epoch)
                or epoch != session_row["resume_epoch_id"]
                or session_row["resume_location"] != "ah"
                or progress["managed_active_quantity"]
                or not progress["returning_quantity"]
            ):
                raise SellerStoreError("recovery route is outside the current AH resume and return pass")
            existing = db.execute(
                "SELECT state FROM seller_children WHERE session_id=? AND decision_id=? AND phase=?",
                (session_id, decision_id, phase),
            ).fetchone()
            if existing is None:
                stock = self._stock_fact(db, session_id, item.item_key)
                session = self._session_by_id(db, session_id)
                self._validate_stock_snapshot(db, session_id, stock, session.boot_id, now)
            elif existing["state"] not in ("prepared", "running"):
                raise SellerStoreError("recovery route child is already terminal")
            return (MoveRoute(policy.outbound_route_id), Interact(policy.mailbox_target_id))
        if phase.startswith("recovery_to_ah:"):
            epoch = phase.removeprefix("recovery_to_ah:")
            session_row = db.execute(
                "SELECT resume_epoch_id,resume_location FROM seller_sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if (
                len(epoch) != 64
                or any(char not in "0123456789abcdef" for char in epoch)
                or epoch != session_row["resume_epoch_id"]
                or session_row["resume_location"] != "mailbox"
                or progress["managed_active_quantity"]
                or progress["returning_quantity"]
                or not progress["bag_quantity"]
            ):
                raise SellerStoreError("return route is outside the current mailbox resume and return pass")
            existing = db.execute(
                "SELECT state FROM seller_children WHERE session_id=? AND decision_id=? AND phase=?",
                (session_id, decision_id, phase),
            ).fetchone()
            if existing is None:
                inventory = self._latest_observation(db, session_id, item.item_key, "inventory")
                session = self._session_by_id(db, session_id)
                if inventory is None:
                    raise SellerStoreError("mailbox resume has no current inventory proof")
                self._check_fact_fresh(
                    inventory.client_boot_id,
                    inventory.observed_at,
                    True,
                    session.boot_id,
                    now,
                )
            elif existing["state"] not in ("prepared", "running"):
                raise SellerStoreError("return route child is already terminal")
            return (MoveRoute(policy.return_route_id), Interact(policy.ah_target_id))
        if phase == "travel_to_mail":
            if progress["managed_active_quantity"] or not progress["returning_quantity"]:
                raise SellerStoreError(
                    "mail travel requires canceled managed auctions with outstanding returns"
                )
            return (MoveRoute(policy.outbound_route_id), Interact(policy.mailbox_target_id))
        if phase == "travel_to_ah":
            if progress["managed_active_quantity"] or progress["returning_quantity"]:
                raise SellerStoreError("return travel requires managed and return quantities reconciled")
            if not progress["bag_quantity"]:
                raise SellerStoreError("return travel has no newly verified admitted stock")
            return (MoveRoute(policy.return_route_id), Interact(policy.ah_target_id))
        if phase.startswith("collect_return:"):
            fingerprint = phase.removeprefix("collect_return:")
            if len(fingerprint) != 64 or any(char not in "0123456789abcdef" for char in fingerprint):
                raise SellerStoreError("collect_return phase must pin one full mail fingerprint")
            candidate = db.execute(
                "SELECT * FROM seller_returns WHERE session_id=? AND recycle_anchor_decision_id=? "
                "AND mail_fingerprint=?",
                (session_id, decision_id, fingerprint),
            ).fetchone()
            if (
                candidate is None
                or not candidate["eligible"]
                or candidate["consumed"]
                or candidate["item_key"] != item.item_key
                or candidate["quantity"] is None
                or candidate["quantity"] > progress["returning_quantity"]
                or candidate["observed_at"] + MAX_PROGRESS_AGE_SECONDS <= now
            ):
                raise SellerStoreError("mail candidate is stale, ambiguous, unrelated or already consumed")
            return (
                CollectReturn(
                    item.item_key,
                    candidate["quantity"],
                    fingerprint,
                ),
            )
        if phase.startswith("continue_recycle:"):
            actions = self._continue_recycle_actions(
                db, session_id, decision_id, phase, policy, decision, now
            )
            return actions
        raise SellerStoreError(f"unsupported seller action phase: {phase}")

    def _continue_recycle_actions(
        self,
        db: sqlite3.Connection,
        session_id: str,
        decision_id: str,
        phase: str,
        policy: SellerPolicy,
        decision: SellerDecision,
        now: int,
    ) -> tuple[CancelAuction, ...]:
        prefix, _, raw_chunk = phase.partition(":")
        if prefix != "continue_recycle" or not raw_chunk.isascii() or not raw_chunk.isdigit():
            raise SellerStoreError("recycle continuation phase must carry an exact chunk number")
        chunk = int(raw_chunk)
        if chunk < 1 or str(chunk) != raw_chunk or decision.kind != "recycle":
            raise SellerStoreError("recycle continuation phase is malformed or detached from its anchor")
        item = policy.item(decision.item_key)
        progress = self._progress_row(db, session_id, item.item_key)
        if not progress["recycling"] or progress["recycle_anchor_decision_id"] != decision_id:
            raise SellerStoreError("recycle continuation is outside its persisted pass")
        targets = self._recycle_target_ids(db, session_id, decision_id)
        existing = db.execute(
            "SELECT canonical_json,state FROM seller_children WHERE session_id=? AND decision_id=? "
            "AND phase=?",
            (session_id, decision_id, phase),
        ).fetchone()
        if existing is not None:
            if existing["state"] not in ("prepared", "running"):
                raise SellerStoreError("recycle continuation child is already terminal")
            actions = Batch.from_json(existing["canonical_json"]).actions
            cancel_actions = tuple(action for action in actions if isinstance(action, CancelAuction))
            if (
                not actions
                or len(actions) > MAX_SELLER_CANCELS_PER_CHILD
                or len(cancel_actions) != len(actions)
                or any(
                    action.auction_id not in targets
                    or action.max_cancel_cost_copper != item.max_cancel_cost_copper
                    for action in cancel_actions
                )
            ):
                raise SellerStoreError(
                    "persisted continuation child differs from frozen cancellation targets"
                )
            if len({action.auction_id for action in cancel_actions}) != len(actions):
                raise SellerStoreError("persisted continuation child repeats a cancellation ID")
            return cancel_actions
        stock = self._stock_fact(db, session_id, item.item_key)
        session = self._session_by_id(db, session_id)
        self._validate_stock_snapshot(db, session_id, stock, session.boot_id, now)
        children = db.execute(
            "SELECT phase,state FROM seller_children WHERE session_id=? AND decision_id=? "
            "AND phase LIKE 'continue_recycle:%' "
            "ORDER BY CAST(substr(phase,18) AS INTEGER)",
            (session_id, decision_id),
        ).fetchall()
        prior = [row for row in children if row["phase"] != phase]
        failed_prior = 0
        if (
            any(
                row["phase"] != f"continue_recycle:{index}" or row["state"] not in ("succeeded", "failed")
                for index, row in enumerate(prior, start=1)
            )
            or len(prior) != chunk - 1
        ):
            raise SellerStoreError("recycle continuation chunks must complete once in order")
        failed_prior = sum(row["state"] == "failed" for row in prior)
        if failed_prior > 1:
            raise SellerStoreError("recycle pass exhausted its single bounded known-no-effect retry")
        if any(row["phase"] == phase and row["state"] == "succeeded" for row in children):
            raise SellerStoreError("recycle continuation chunk is already complete")
        if any(row["phase"] != phase and int(row["phase"].split(":", 1)[1]) > chunk for row in children):
            raise SellerStoreError("recycle continuation phase skipped an earlier chunk")
        active = self._managed_facts(db, session_id, item.item_key)
        if not {row.auction_id for row in active}.issubset(targets):
            raise SellerStoreError("managed auction ID is outside the frozen recycle pass")
        if not active:
            raise SellerStoreError("persisted recycle pass has no exact active auction to cancel")
        if any(
            row.status != "active"
            or not row.cancel_allowed
            or not row.cancel_cost_known
            or row.cancel_cost_copper > item.max_cancel_cost_copper
            for row in active
        ):
            raise SellerStoreError("recycle continuation lacks fresh bounded cancellation permission")
        return tuple(
            CancelAuction(row.auction_id, item.max_cancel_cost_copper)
            for row in active[:MAX_SELLER_CANCELS_PER_CHILD]
        )

    def _recompute_decision(
        self,
        db: sqlite3.Connection,
        session_id: str,
        decision_id: str,
        policy: SellerPolicy,
    ) -> SellerDecision:
        row = self._decision_row(db, session_id, decision_id)
        market_value = json.loads(row["market_json"])
        stock_value = json.loads(row["stock_json"])
        market = _market_from_object(market_value)
        stock = _stock_from_object(stock_value)
        recomputed = decide(policy, policy.item(row["item_key"]), market, stock, row["created_at"])
        if (
            recomputed.decision_id != decision_id
            or canonical_json(recomputed.object()) != row["decision_json"]
        ):
            raise SellerStoreError(
                "persisted seller decision no longer recomputes from its exact source facts"
            )
        return recomputed

    def _active_session(
        self, db: sqlite3.Connection, session_id: str, now: int
    ) -> tuple[_SessionRow, SellerPolicy]:
        row = db.execute("SELECT * FROM seller_sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            raise SellerStoreError(f"unknown seller session: {session_id}")
        state = row["state"]
        if state != "active":
            raise SellerStoreError(f"seller session is {state}")
        policy = SellerPolicy.from_json(row["policy_json"])
        if now >= row["expires_at"] or now >= policy.expires_at:
            db.execute(
                "UPDATE seller_sessions SET state='expired',stop_reason='policy expired' WHERE session_id=?",
                (session_id,),
            )
            raise SellerStoreError("seller policy is expired")
        session = _SessionRow(
            row["session_id"],
            policy,
            row["boot_id"],
            row["client_build"],
            row["expires_at"],
            state,
            row["stop_reason"],
            row["created_at"],
        )
        return session, policy

    def _session_by_id(self, db: sqlite3.Connection, session_id: str) -> _SessionRow:
        row = db.execute("SELECT * FROM seller_sessions WHERE session_id=?", (session_id,)).fetchone()
        if row is None:
            raise SellerStoreError(f"unknown seller session: {session_id}")
        return _SessionRow(
            row["session_id"],
            SellerPolicy.from_json(row["policy_json"]),
            row["boot_id"],
            row["client_build"],
            row["expires_at"],
            row["state"],
            row["stop_reason"],
            row["created_at"],
        )

    def _decision_row(self, db: sqlite3.Connection, session_id: str, decision_id: str) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM seller_decisions WHERE session_id=? AND decision_id=?",
            (session_id, decision_id),
        ).fetchone()
        if row is None:
            raise SellerStoreError("seller decision is not persisted for this session")
        return row

    def _recycle_target_ids(
        self, db: sqlite3.Connection, session_id: str, anchor_decision_id: str
    ) -> frozenset[int]:
        row = self._decision_row(db, session_id, anchor_decision_id)
        decision = json.loads(row["decision_json"])
        if type(decision) is not dict or decision.get("kind") != "recycle":
            raise SellerStoreError("recycle anchor is not a persisted recycle decision")
        actions = decision.get("actions")
        remaining = decision.get("remainingAuctionIds")
        if type(actions) is not list or type(remaining) is not list:
            raise SellerStoreError("persisted recycle target set is malformed")
        targets: list[int] = []
        for action in actions:
            if type(action) is not dict or action.get("kind") != "cancel_auction":
                raise SellerStoreError("recycle anchor contains a non-cancellation action")
            auction_id = action.get("auctionId")
            if type(auction_id) is not int:
                raise SellerStoreError("recycle anchor contains a malformed auction ID")
            targets.append(auction_id)
        if any(type(auction_id) is not int for auction_id in remaining):
            raise SellerStoreError("recycle anchor contains malformed remaining auction IDs")
        targets.extend(remaining)
        if len(targets) != len(set(targets)):
            raise SellerStoreError("recycle anchor repeats a frozen auction ID")
        return frozenset(targets)

    def _progress_row(self, db: sqlite3.Connection, session_id: str, item_key: str) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM seller_progress WHERE session_id=? AND item_key=?",
            (session_id, item_key),
        ).fetchone()
        if row is None:
            raise SellerStoreError("item does not belong to this seller session")
        return row

    def _progress(self, db: sqlite3.Connection, session_id: str, item_key: str) -> SellerItemProgress:
        row = self._progress_row(db, session_id, item_key)
        reserved = db.execute(
            "SELECT COALESCE(SUM(amount_copper),0) AS n FROM seller_reservations "
            "WHERE session_id=? AND state IN ('reserved','unknown') "
            "AND EXISTS (SELECT 1 FROM seller_decisions d WHERE d.session_id=seller_reservations.session_id "
            "AND d.decision_id=seller_reservations.decision_id AND d.item_key=?)",
            (session_id, item_key),
        ).fetchone()["n"]
        return SellerItemProgress(
            session_id,
            item_key,
            row["admitted_quantity"],
            row["bag_quantity"],
            row["managed_active_quantity"],
            row["returning_quantity"],
            row["unresolved_quantity"],
            row["sold_quantity"],
            row["recycle_rounds"],
            bool(row["recycling"]),
            bool(row["finished"]),
            reserved,
            row["spent_copper"],
            row["recycle_anchor_decision_id"],
            row["last_post_attempt_at"],
        )

    def _stock_fact(self, db: sqlite3.Connection, session_id: str, item_key: str) -> StockFacts:
        p = self._progress_row(db, session_id, item_key)
        inventory = self._latest_observation(db, session_id, item_key, "inventory")
        owned = self._latest_observation(db, session_id, item_key, "owned")
        last_post = db.execute(
            "SELECT a.unit_price_copper FROM seller_managed_auctions a "
            "JOIN batches b ON b.batch_id=a.created_batch_id "
            "WHERE a.session_id=? AND a.item_key=? ORDER BY b.created_at DESC,a.auction_id DESC LIMIT 1",
            (session_id, item_key),
        ).fetchone()
        return StockFacts(
            item_key=item_key,
            admitted_quantity=p["admitted_quantity"],
            bag_quantity=p["bag_quantity"],
            managed_active_quantity=p["managed_active_quantity"],
            returning_quantity=p["returning_quantity"],
            unresolved_quantity=p["unresolved_quantity"],
            sold_quantity=p["sold_quantity"],
            inventory_request_id=inventory.request_id if inventory else None,
            inventory_client_boot_id=inventory.client_boot_id if inventory else None,
            inventory_sequence=inventory.sequence if inventory else None,
            inventory_observed_at=inventory.observed_at if inventory else None,
            owned_request_id=owned.request_id if owned else None,
            owned_client_boot_id=owned.client_boot_id if owned else None,
            owned_sequence=owned.sequence if owned else None,
            owned_observed_at=owned.observed_at if owned else None,
            last_post_attempt_at=p["last_post_attempt_at"],
            last_posted_unit_price_copper=last_post["unit_price_copper"] if last_post else None,
            recycling=bool(p["recycling"]),
        )

    @staticmethod
    def _latest_observation(
        db: sqlite3.Connection, session_id: str, item_key: str, kind: str
    ) -> _ObservationProvenance | None:
        session = db.execute(
            "SELECT boot_id FROM seller_sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if session is None:
            raise SellerStoreError("seller observation has no owning session")
        rows = db.execute(
            "SELECT request_id,sequence,observed_at,facts_json FROM seller_observations "
            "WHERE session_id=? AND item_key=? AND kind=? ORDER BY sequence DESC",
            (session_id, item_key, kind),
        ).fetchall()
        for row in rows:
            facts = json.loads(row["facts_json"])
            if type(facts) is not dict or type(facts.get("clientBootId")) is not str:
                raise SellerStoreError("persisted native observation provenance is malformed")
            if facts["clientBootId"] == session["boot_id"]:
                return _ObservationProvenance(
                    row["request_id"],
                    facts["clientBootId"],
                    row["sequence"],
                    row["observed_at"],
                )
        return None

    def _validate_decision_stock(
        self,
        db: sqlite3.Connection,
        session_id: str,
        decision_id: str,
        expected_boot_id: str,
        now: int,
    ) -> None:
        row = self._decision_row(db, session_id, decision_id)
        stock = _stock_from_object(json.loads(row["stock_json"]))
        self._validate_stock_snapshot(db, session_id, stock, expected_boot_id, now)

    def _validate_stock_snapshot(
        self,
        db: sqlite3.Connection,
        session_id: str,
        stock: StockFacts,
        expected_boot_id: str,
        now: int,
    ) -> None:
        pairs = (
            (
                "inventory",
                stock.inventory_request_id,
                stock.inventory_client_boot_id,
                stock.inventory_sequence,
                stock.inventory_observed_at,
            ),
            (
                "owned",
                stock.owned_request_id,
                stock.owned_client_boot_id,
                stock.owned_sequence,
                stock.owned_observed_at,
            ),
        )
        for kind, request_id, boot_id, sequence, observed_at in pairs:
            if request_id is None or boot_id is None or sequence is None or observed_at is None:
                raise SellerStoreError("decision requires exact inventory and owned snapshot provenance")
            latest = self._latest_observation(db, session_id, stock.item_key, kind)
            if latest is None or (
                latest.request_id,
                latest.client_boot_id,
                latest.sequence,
                latest.observed_at,
            ) != (request_id, boot_id, sequence, observed_at):
                raise SellerStoreError("decision stock provenance is no longer the latest native snapshot")
            self._check_fact_fresh(boot_id, observed_at, True, expected_boot_id, now)

    @staticmethod
    def _upsert_return_candidate(
        db: sqlite3.Connection,
        session_id: str,
        anchor_decision_id: str,
        candidate: ReturnCandidate,
    ) -> None:
        prior = db.execute(
            "SELECT * FROM seller_returns WHERE session_id=? AND mail_fingerprint=?",
            (session_id, candidate.mail_fingerprint),
        ).fetchone()
        if prior is not None:
            if prior["consumed"]:
                raise SellerStoreError("a consumed native mail selection token cannot be reused")
            identity = (
                prior["item_key"],
                prior["quantity"],
                prior["recycle_anchor_decision_id"],
                prior["client_boot_id"],
            )
            candidate_identity = (
                candidate.item_key,
                candidate.quantity,
                anchor_decision_id,
                candidate.client_boot_id,
            )
            if identity != candidate_identity:
                raise SellerStoreError("a refreshed native selection token changed attachment identity")
            if candidate.sequence < prior["sequence"]:
                raise SellerStoreError("return candidate observation sequence did not advance")
            if candidate.sequence == prior["sequence"]:
                if (
                    prior["request_id"] != candidate.request_id
                    or prior["observed_at"] != candidate.observed_at
                    or bool(prior["eligible"]) != candidate.eligible
                    or prior["reason"] != candidate.reason
                ):
                    raise SellerStoreError("return candidate reused observation sequence with changed facts")
                return
            db.execute(
                "UPDATE seller_returns SET request_id=?,client_boot_id=?,sequence=?,"
                "eligible=?,reason=? WHERE session_id=? AND mail_fingerprint=? AND consumed=0",
                (
                    candidate.request_id,
                    candidate.client_boot_id,
                    candidate.sequence,
                    int(candidate.eligible),
                    candidate.reason,
                    session_id,
                    candidate.mail_fingerprint,
                ),
            )
            return
        db.execute(
            "INSERT INTO seller_returns(session_id,recycle_anchor_decision_id,mail_fingerprint,item_key,"
            "quantity,request_id,client_boot_id,sequence,observed_at,eligible,reason,consumed) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,0)",
            (
                session_id,
                anchor_decision_id,
                candidate.mail_fingerprint,
                candidate.item_key,
                candidate.quantity,
                candidate.request_id,
                candidate.client_boot_id,
                candidate.sequence,
                candidate.observed_at,
                int(candidate.eligible),
                candidate.reason,
            ),
        )

    def _managed_facts(
        self, db: sqlite3.Connection, session_id: str, item_key: str
    ) -> tuple[OwnedAuction, ...]:
        rows = db.execute(
            "SELECT * FROM seller_managed_auctions WHERE session_id=? AND item_key=? AND status='active' "
            "ORDER BY auction_id",
            (session_id, item_key),
        ).fetchall()
        return tuple(
            OwnedAuction(
                row["auction_id"],
                row["item_key"],
                row["remaining_quantity"],
                row["unit_price_copper"],
                "active",
                bool(row["cancel_allowed"]),
                bool(row["cancel_cost_known"]),
                row["cancel_cost_copper"],
            )
            for row in rows
        )

    @staticmethod
    def _validate_batch_identity(
        batch: Batch, policy: SellerPolicy, now: int, decision: SellerDecision, phase: str
    ) -> None:
        if batch.house != policy.house or batch.character != policy.character:
            raise SellerStoreError("seller child identity differs from immutable policy")
        if batch.expires_at <= now:
            raise SellerStoreError("seller child batch is already expired")
        if phase == "decision" and batch.expires_at > min(
            policy.expires_at, decision.observed_at + SELLER_DECISION_MAX_AGE_SECONDS
        ):
            raise SellerStoreError("seller child expiry exceeds policy or fresh decision")

    def _phase_deadline(
        self,
        db: sqlite3.Connection,
        session_id: str,
        decision_id: str,
        phase: str,
        decision: SellerDecision,
        prepared_at: int,
    ) -> int:
        if phase == "decision":
            return decision.observed_at + SELLER_DECISION_MAX_AGE_SECONDS
        if phase.startswith("collect_return:"):
            candidate = db.execute(
                "SELECT observed_at FROM seller_returns WHERE session_id=? AND recycle_anchor_decision_id=? "
                "AND mail_fingerprint=?",
                (session_id, decision_id, phase.removeprefix("collect_return:")),
            ).fetchone()
            if candidate is None:
                raise SellerStoreError("return phase has no persisted candidate timestamp")
            return candidate["observed_at"] + MAX_PROGRESS_AGE_SECONDS
        if phase.startswith("continue_recycle:"):
            stock = self._stock_fact(db, session_id, decision.item_key)
            if stock.inventory_observed_at is None or stock.owned_observed_at is None:
                raise SellerStoreError("recycle continuation has no fresh inventory and owned evidence")
            return min(
                prepared_at + SELLER_PHASE_MAX_AGE_SECONDS,
                stock.inventory_observed_at + MAX_PROGRESS_AGE_SECONDS,
                stock.owned_observed_at + MAX_PROGRESS_AGE_SECONDS,
            )
        return prepared_at + SELLER_PHASE_MAX_AGE_SECONDS

    @staticmethod
    def _reservation_amount(action: Action) -> int:
        if isinstance(action, PostCommodity):
            return action.max_deposit_copper
        if isinstance(action, CancelAuction):
            return action.max_cancel_cost_copper
        return 0

    @staticmethod
    def _check_fact_fresh(
        boot_id: str,
        observed_at: int,
        complete: bool,
        expected_boot_id: str,
        now: int,
    ) -> None:
        if not complete:
            raise SellerStoreError("native seller observation is incomplete")
        if boot_id != expected_boot_id:
            raise SellerStoreError("native seller observation belongs to another client boot")
        if observed_at > now or now - observed_at > MAX_PROGRESS_AGE_SECONDS:
            raise SellerStoreError("native seller observation is stale")

    @staticmethod
    def _identity_text(value: str, name: str) -> None:
        if type(value) is not str or not value or len(value) > 128:
            raise SellerStoreError(f"{name} must be bounded non-empty text")

    @staticmethod
    def _insert_observation(
        db: sqlite3.Connection,
        session_id: str,
        item_key: str,
        kind: str,
        request_id: str,
        sequence: int,
        observed_at: int,
        value: dict[str, object],
    ) -> None:
        prior = db.execute(
            "SELECT sequence,facts_json FROM seller_observations WHERE session_id=? AND item_key=? "
            "AND kind=? AND request_id=?",
            (session_id, item_key, kind, request_id),
        ).fetchone()
        facts_json = canonical_json(value)
        if prior is not None:
            if prior["sequence"] != sequence or prior["facts_json"] != facts_json:
                raise SellerStoreError("native observation request ID was reused with different facts")
            return
        current_boot = value.get("clientBootId")
        prior_rows = db.execute(
            "SELECT sequence,facts_json FROM seller_observations "
            "WHERE session_id=? AND item_key=? AND kind=?",
            (session_id, item_key, kind),
        ).fetchall()
        prior_sequences = [
            row["sequence"]
            for row in prior_rows
            if json.loads(row["facts_json"]).get("clientBootId") == current_boot
        ]
        previous = max(prior_sequences) if prior_sequences else None
        if previous is not None and sequence <= previous:
            raise SellerStoreError("native seller observation sequence did not advance")
        db.execute(
            "INSERT INTO seller_observations(session_id,item_key,kind,request_id,"
            "sequence,observed_at,facts_json) "
            "VALUES(?,?,?,?,?,?,?)",
            (session_id, item_key, kind, request_id, sequence, observed_at, facts_json),
        )

    @staticmethod
    def _assert_conservation(db: sqlite3.Connection, session_id: str, item_key: str) -> None:
        row = db.execute(
            "SELECT admitted_quantity,bag_quantity,managed_active_quantity,returning_quantity,"
            "unresolved_quantity,sold_quantity FROM seller_progress WHERE session_id=? AND item_key=?",
            (session_id, item_key),
        ).fetchone()
        total = sum(
            row[key]
            for key in (
                "bag_quantity",
                "managed_active_quantity",
                "returning_quantity",
                "unresolved_quantity",
                "sold_quantity",
            )
        )
        if (
            total != row["admitted_quantity"]
            or min(
                row[key]
                for key in (
                    "bag_quantity",
                    "managed_active_quantity",
                    "returning_quantity",
                    "unresolved_quantity",
                    "sold_quantity",
                )
            )
            < 0
        ):
            raise SellerStoreError("persisted seller stock conservation failed")

    def _nonterminal_children(self, session_id: str) -> list[sqlite3.Row]:
        with self.ledger._connect() as db:
            return db.execute(
                "SELECT decision_id,phase,batch_id,canonical_json,state FROM seller_children "
                "WHERE session_id=? AND state IN ('prepared','running','uncertain')",
                (session_id,),
            ).fetchall()

    @staticmethod
    def _nonterminal_children_in_transaction(db: sqlite3.Connection, session_id: str) -> int:
        return db.execute(
            "SELECT COUNT(*) AS n FROM seller_children "
            "WHERE session_id=? AND state IN ('prepared','running','uncertain')",
            (session_id,),
        ).fetchone()["n"]

    @staticmethod
    def _assert_no_open_child(db: sqlite3.Connection, session_id: str, item_key: str) -> None:
        row = db.execute(
            "SELECT 1 FROM seller_children c JOIN seller_decisions d USING(session_id,decision_id) "
            "WHERE c.session_id=? AND d.item_key=? AND c.state IN ('prepared','running') LIMIT 1",
            (session_id, item_key),
        ).fetchone()
        if row is not None:
            raise SellerStoreError("an earlier seller child is still open or unresolved")

    @staticmethod
    def _update_child_state(
        db: sqlite3.Connection, session_id: str, decision_id: str, phase: str, batch_id: str
    ) -> None:
        rows = db.execute("SELECT state FROM actions WHERE batch_id=?", (batch_id,)).fetchall()
        states = [row["state"] for row in rows]
        if not states:
            raise SellerStoreError("seller child has no ledger actions")
        if any(state == "uncertain" for state in states):
            next_state = "uncertain"
        elif any(state == "failed" for state in states):
            next_state = "failed"
        elif all(state == "succeeded" for state in states):
            next_state = "succeeded"
        else:
            next_state = "running"
        db.execute(
            "UPDATE seller_children SET state=? WHERE session_id=? AND decision_id=? AND phase=?",
            (next_state, session_id, decision_id, phase),
        )


def _market_from_object(value: object) -> MarketFacts:
    if type(value) is not dict:
        raise SellerStoreError("persisted market facts are malformed")
    auctions = value.get("managedAuctions")
    if type(auctions) is not list:
        raise SellerStoreError("persisted managed auction facts are malformed")
    rows: list[OwnedAuction] = []
    for raw in auctions:
        if type(raw) is not dict:
            raise SellerStoreError("persisted managed auction row is malformed")
        rows.append(
            OwnedAuction(
                raw["auctionId"],
                raw["itemKey"],
                raw["remainingQuantity"],
                raw["unitPriceCopper"],
                raw["status"],
                raw["cancelAllowed"],
                raw["cancelCostKnown"],
                raw["cancelCostCopper"],
            )
        )
    return MarketFacts(
        item_key=value["itemKey"],
        request_id=value["requestId"],
        client_boot_id=value["clientBootId"],
        sequence=value["sequence"],
        observed_at=value["observedAt"],
        complete=value["complete"],
        priority=value["priority"],
        best_competing_unit_price_copper=value["bestCompetingUnitPriceCopper"],
        managed_unit_price_copper=value["managedUnitPriceCopper"],
        managed_active_quantity=value["managedActiveQuantity"],
        managed_auction_ids=tuple(value["managedAuctionIds"]),
        managed_auctions=tuple(rows),
    )


def _stock_from_object(value: object) -> StockFacts:
    if type(value) is not dict:
        raise SellerStoreError("persisted stock facts are malformed")
    return StockFacts(
        item_key=value["itemKey"],
        admitted_quantity=value["admittedQuantity"],
        bag_quantity=value["bagQuantity"],
        managed_active_quantity=value["managedActiveQuantity"],
        returning_quantity=value["returningQuantity"],
        unresolved_quantity=value["unresolvedQuantity"],
        sold_quantity=value["soldQuantity"],
        inventory_request_id=value.get("inventoryRequestId"),
        inventory_client_boot_id=value.get("inventoryClientBootId"),
        inventory_sequence=value.get("inventorySequence"),
        inventory_observed_at=value.get("inventoryObservedAt"),
        owned_request_id=value.get("ownedRequestId"),
        owned_client_boot_id=value.get("ownedClientBootId"),
        owned_sequence=value.get("ownedSequence"),
        owned_observed_at=value.get("ownedObservedAt"),
        last_post_attempt_at=value.get("lastPostAttemptAt"),
        last_posted_unit_price_copper=value.get("lastPostedUnitPriceCopper"),
        recycling=value.get("recycling", False),
    )
