"""Small private SQLite ledger; dispatched operations are never replayed."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import stat
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, TypedDict

from crowley_common.platform.privfs import ensure_private_dir

from .model import Action, Batch, action_object, canonical_json

ActionState = Literal["ready", "dispatched", "succeeded", "failed", "uncertain"]
BatchState = Literal["prepared", "running", "succeeded", "failed", "uncertain", "expired"]


class ActionStatus(TypedDict):
    ordinal: int
    action: dict[str, object]
    state: ActionState
    next_step: int
    reason: str | None


class StepStatus(TypedDict):
    ordinal: int
    step: int
    state: ActionState
    dispatched_at: int | None
    session_boot_id: str | None
    receipt_baseline: int | None
    callback_delivered: bool | None
    postcondition: bool | None
    action_complete: bool | None
    receipt: dict[str, object] | None
    reason: str | None


class BatchStatus(TypedDict):
    batch_id: str
    state: BatchState
    created_at: int
    updated_at: int
    canonical: str
    actions: list[ActionStatus]
    steps: list[StepStatus]


class ReadyStep(TypedDict):
    ordinal: int
    step_index: int


class LedgerError(RuntimeError):
    """An operation conflicts with durable ledger state."""


class IdempotencyConflict(LedgerError):
    """An existing batch ID was presented with different immutable intent."""


class RunnerBusy(LedgerError):
    """Another process currently owns the local automation runner."""


class UncertainEffectInterlock(LedgerError):
    """An unresolved effect prevents new action batches from running."""


class Ledger:
    """Persist immutable batch intent and one-way action state transitions."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().absolute()
        ensure_private_dir(self.path.parent)
        self._create_or_validate_file()
        with self._connect() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS batches (
                    batch_id TEXT PRIMARY KEY,
                    canonical_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK(state IN
                        ('prepared','running','succeeded','failed','uncertain','expired'))
                );
                CREATE TABLE IF NOT EXISTS actions (
                    batch_id TEXT NOT NULL REFERENCES batches(batch_id),
                    ordinal INTEGER NOT NULL,
                    action_json TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN
                        ('ready','dispatched','succeeded','failed','uncertain')),
                    next_step INTEGER NOT NULL DEFAULT 0,
                    reason TEXT,
                    PRIMARY KEY(batch_id, ordinal)
                );
                CREATE TABLE IF NOT EXISTS action_steps (
                    batch_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    step INTEGER NOT NULL,
                    state TEXT NOT NULL CHECK(state IN
                        ('ready','dispatched','succeeded','failed','uncertain')),
                    dispatched_at INTEGER,
                    session_boot_id TEXT,
                    receipt_baseline INTEGER,
                    callback_delivered INTEGER
                        CHECK(callback_delivered IN (0,1) OR callback_delivered IS NULL),
                    postcondition INTEGER CHECK(postcondition IN (0,1) OR postcondition IS NULL),
                    action_complete INTEGER CHECK(action_complete IN (0,1) OR action_complete IS NULL),
                    receipt_json TEXT,
                    reason TEXT,
                    PRIMARY KEY(batch_id, ordinal, step),
                    FOREIGN KEY(batch_id,ordinal) REFERENCES actions(batch_id,ordinal)
                );
                CREATE TABLE IF NOT EXISTS automation_control (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                    stop_reason TEXT,
                    stop_at INTEGER
                );
                INSERT OR IGNORE INTO automation_control(singleton,stop_reason,stop_at) VALUES(1,NULL,NULL);
                """
            )
            columns = {row["name"] for row in db.execute("PRAGMA table_info(action_steps)")}
            if "session_boot_id" not in columns:
                db.execute("ALTER TABLE action_steps ADD COLUMN session_boot_id TEXT")
            if "receipt_baseline" not in columns:
                db.execute("ALTER TABLE action_steps ADD COLUMN receipt_baseline INTEGER")

    def _create_or_validate_file(self) -> None:
        try:
            descriptor = os.open(
                self.path,
                os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
        except FileExistsError:
            info = self.path.lstat()
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise PermissionError("automation ledger must be an owned regular mode-0600 file") from None
        else:
            try:
                os.fchmod(descriptor, 0o600)
                info = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) != 0o600
                ):
                    raise PermissionError("could not create a private automation ledger")
            finally:
                os.close(descriptor)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=0.1, isolation_level="DEFERRED")
        try:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA synchronous=FULL")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def runner_lock(self) -> Iterator[None]:
        """Hold a private process lock so startup recovery cannot race a live dispatch."""
        lock_path = self.path.with_name(self.path.name + ".runner-lock")
        try:
            try:
                descriptor = os.open(
                    lock_path,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                    0o600,
                )
            except FileExistsError:
                descriptor = os.open(
                    lock_path,
                    os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
                )
            else:
                os.fchmod(descriptor, 0o600)
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != os.getuid()
                or info.st_nlink != 1
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise PermissionError("runner lock must be an owned regular mode-0600 file")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RunnerBusy("another local automation runner is active") from exc
            yield
        finally:
            if "descriptor" in locals():
                os.close(descriptor)

    def recover_if_idle(self, *, now: int | None = None) -> int | None:
        """Recover dispatch markers when no runner owns the local execution lock."""
        try:
            with self.runner_lock():
                return self.recover_interrupted(now=now)
        except RunnerBusy:
            return None

    @staticmethod
    def _action_json(action: Action) -> str:
        return canonical_json(action_object(action))

    def prepare(self, batch: Batch, *, now: int | None = None) -> BatchStatus:
        """Insert once; an identical submission returns the existing status."""
        timestamp = int(time.time()) if now is None else now
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT canonical_json FROM batches WHERE batch_id=?", (batch.batch_id,)
            ).fetchone()
            if row is not None:
                if row["canonical_json"] != batch.canonical:
                    raise IdempotencyConflict("batch ID already belongs to different canonical intent")
            else:
                db.execute(
                    "INSERT INTO batches(batch_id,canonical_json,created_at,updated_at,state) "
                    "VALUES(?,?,?,?,?)",
                    (batch.batch_id, batch.canonical, timestamp, timestamp, "prepared"),
                )
                db.executemany(
                    "INSERT INTO actions(batch_id,ordinal,action_json,state) VALUES(?,?,?,?)",
                    [
                        (batch.batch_id, ordinal, self._action_json(action), "ready")
                        for ordinal, action in enumerate(batch.actions)
                    ],
                )
                db.executemany(
                    "INSERT INTO action_steps(batch_id,ordinal,step,state) VALUES(?,?,0,'ready')",
                    [(batch.batch_id, ordinal) for ordinal in range(len(batch.actions))],
                )
        return self.status(batch.batch_id)

    def status(self, batch_id: str) -> BatchStatus:
        with self._connect() as db:
            batch = db.execute("SELECT * FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
            if batch is None:
                raise KeyError(f"unknown batch: {batch_id}")
            actions = db.execute(
                "SELECT ordinal,action_json,state,next_step,reason FROM actions "
                "WHERE batch_id=? ORDER BY ordinal",
                (batch_id,),
            ).fetchall()
            steps = db.execute(
                "SELECT ordinal,step,state,dispatched_at,session_boot_id,receipt_baseline,"
                "callback_delivered,postcondition,action_complete,"
                "receipt_json,reason FROM action_steps WHERE batch_id=? ORDER BY ordinal,step",
                (batch_id,),
            ).fetchall()
        result: BatchStatus = {
            "batch_id": batch["batch_id"],
            "state": batch["state"],
            "created_at": batch["created_at"],
            "updated_at": batch["updated_at"],
            "canonical": batch["canonical_json"],
            "actions": [
                {
                    "ordinal": row["ordinal"],
                    "action": json.loads(row["action_json"]),
                    "state": row["state"],
                    "next_step": row["next_step"],
                    "reason": row["reason"],
                }
                for row in actions
            ],
            "steps": [
                {
                    "ordinal": row["ordinal"],
                    "step": row["step"],
                    "state": row["state"],
                    "dispatched_at": row["dispatched_at"],
                    "session_boot_id": row["session_boot_id"],
                    "receipt_baseline": row["receipt_baseline"],
                    "callback_delivered": _bool_db(row["callback_delivered"]),
                    "postcondition": _bool_db(row["postcondition"]),
                    "action_complete": _bool_db(row["action_complete"]),
                    "receipt": json.loads(row["receipt_json"]) if row["receipt_json"] else None,
                    "reason": row["reason"],
                }
                for row in steps
            ],
        }
        return result

    def all_status(self) -> list[BatchStatus]:
        with self._connect() as db:
            ids = db.execute("SELECT batch_id FROM batches ORDER BY created_at,batch_id").fetchall()
        return [self.status(row["batch_id"]) for row in ids]

    def has_uncertain_effect(self) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT EXISTS(SELECT 1 FROM batches WHERE state='uncertain') OR "
                "EXISTS(SELECT 1 FROM action_steps WHERE state='uncertain') AS found"
            ).fetchone()
        return bool(row["found"])

    def stop_requested(self) -> bool:
        with self._connect() as db:
            row = db.execute("SELECT stop_reason FROM automation_control WHERE singleton=1").fetchone()
        return row is not None and row["stop_reason"] is not None

    def stop(self, reason: str = "operator_stop", *, now: int | None = None) -> None:
        if type(reason) is not str or not reason or len(reason) > 120:
            raise LedgerError("stop reason must be bounded non-empty text")
        timestamp = int(time.time()) if now is None else now
        with self._connect() as db:
            db.execute(
                "UPDATE automation_control SET stop_reason=?,stop_at=? WHERE singleton=1",
                (reason, timestamp),
            )

    def clear_stop(self) -> None:
        if self.has_uncertain_effect():
            raise UncertainEffectInterlock("uncertain effects must be reconciled before clearing stop")
        with self._connect() as db:
            db.execute("UPDATE automation_control SET stop_reason=NULL,stop_at=NULL WHERE singleton=1")

    def reconcile_receipt(
        self,
        batch_id: str,
        ordinal: int,
        step: int,
        native_receipt: dict[str, object],
        *,
        boot_id: str,
        step_limit: int,
        now: int | None = None,
    ) -> None:
        """Resolve one uncertain marker only from a matching later native receipt."""
        expected_fields = {
            "sequence",
            "batchId",
            "ordinal",
            "stepIndex",
            "callbackDelivered",
            "postcondition",
            "actionComplete",
            "detail",
        }
        if type(native_receipt) is not dict or set(native_receipt) != expected_fields:
            raise LedgerError("native receipt fields are not exact")
        if (
            type(boot_id) is not str
            or not boot_id
            or type(step_limit) is not int
            or step_limit < 1
            or type(native_receipt["sequence"]) is not int
            or type(native_receipt["ordinal"]) is not int
            or type(native_receipt["stepIndex"]) is not int
            or type(native_receipt["batchId"]) is not str
            or type(native_receipt["detail"]) is not str
            or len(native_receipt["detail"]) > 240
            or native_receipt["callbackDelivered"] is not True
            or type(native_receipt["postcondition"]) is not bool
            or type(native_receipt["actionComplete"]) is not bool
            or native_receipt["sequence"] <= 0
            or native_receipt["batchId"] != batch_id
            or native_receipt["ordinal"] != ordinal
            or native_receipt["stepIndex"] != step
        ):
            raise LedgerError("native receipt does not prove this dispatched effect")
        timestamp = int(time.time()) if now is None else now
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            batch = db.execute("SELECT state FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
            action = db.execute(
                "SELECT state,next_step,action_json FROM actions WHERE batch_id=? AND ordinal=?",
                (batch_id, ordinal),
            ).fetchone()
            dispatched = db.execute(
                "SELECT state,session_boot_id,receipt_baseline FROM action_steps "
                "WHERE batch_id=? AND ordinal=? AND step=?",
                (batch_id, ordinal, step),
            ).fetchone()
            if batch is None or action is None or dispatched is None:
                raise LedgerError("receipt does not identify a durable action")
            if (
                batch["state"] != "uncertain"
                or action["state"] != "uncertain"
                or dispatched["state"] != "uncertain"
            ):
                raise LedgerError("only an uncertain dispatched effect can be reconciled")
            if dispatched["session_boot_id"] != boot_id:
                raise LedgerError("receipt boot does not match the dispatch marker")
            if (
                dispatched["receipt_baseline"] is None
                or native_receipt["sequence"] <= dispatched["receipt_baseline"]
            ):
                raise LedgerError("native receipt sequence did not advance beyond dispatch")
            action_data = json.loads(action["action_json"])
            digest = hashlib.sha256(action["action_json"].encode("utf-8")).hexdigest()
            if native_receipt["postcondition"] is False:
                if native_receipt["actionComplete"] is not False:
                    raise LedgerError("negative native receipt has contradictory completion evidence")
                evidence = {
                    "action_digest": digest,
                    "boot_id": boot_id,
                    "native_receipt": native_receipt,
                    "reconciled_at": timestamp,
                }
                db.execute(
                    "UPDATE action_steps SET state='failed',callback_delivered=1,postcondition=0,"
                    "action_complete=0,receipt_json=?,reason='reconciled native precondition failure' "
                    "WHERE batch_id=? AND ordinal=? AND step=?",
                    (canonical_json(evidence), batch_id, ordinal, step),
                )
                db.execute(
                    "UPDATE actions SET state='failed',reason='reconciled native precondition failure' "
                    "WHERE batch_id=? AND ordinal=?",
                    (batch_id, ordinal),
                )
                db.execute(
                    "UPDATE batches SET state='failed',updated_at=? WHERE batch_id=?",
                    (timestamp, batch_id),
                )
                return
            advance_step = not native_receipt["actionComplete"] and step + 1 < step_limit
            if not native_receipt["actionComplete"] and not advance_step:
                if action_data.get("kind") != "collect_mail":
                    raise LedgerError("incomplete native receipt cannot advance this action")
            evidence = {
                "action_digest": digest,
                "boot_id": boot_id,
                "native_receipt": native_receipt,
                "reconciled_at": timestamp,
            }
            db.execute(
                "UPDATE action_steps SET state='succeeded',callback_delivered=1,postcondition=1,"
                "action_complete=?,receipt_json=?,reason=NULL WHERE batch_id=? AND ordinal=? AND step=?",
                (int(native_receipt["actionComplete"]), canonical_json(evidence), batch_id, ordinal, step),
            )
            if advance_step:
                next_step = step + 1
                db.execute(
                    "UPDATE actions SET state='ready',next_step=?,reason=NULL WHERE batch_id=? AND ordinal=?",
                    (next_step, batch_id, ordinal),
                )
                db.execute(
                    "INSERT INTO action_steps(batch_id,ordinal,step,state) VALUES(?,?,?,'ready')",
                    (batch_id, ordinal, next_step),
                )
                next_state = "running"
            else:
                db.execute(
                    "UPDATE actions SET state='succeeded',reason=NULL WHERE batch_id=? AND ordinal=?",
                    (batch_id, ordinal),
                )
                incomplete = db.execute(
                    "SELECT COUNT(*) AS n FROM actions WHERE batch_id=? AND state!='succeeded'", (batch_id,)
                ).fetchone()["n"]
                next_state = "succeeded" if incomplete == 0 else "running"
            db.execute(
                "UPDATE batches SET state=?,updated_at=? WHERE batch_id=?", (next_state, timestamp, batch_id)
            )

    def mark_expired(self, batch_id: str, *, now: int) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT state FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
            if row is None:
                raise KeyError(batch_id)
            inflight = db.execute(
                "SELECT COUNT(*) AS n FROM action_steps WHERE batch_id=? AND state='dispatched'", (batch_id,)
            ).fetchone()["n"]
            if row["state"] in ("prepared", "running") and inflight == 0:
                db.execute(
                    "UPDATE batches SET state='expired',updated_at=? WHERE batch_id=?", (now, batch_id)
                )

    def claim_dispatch(
        self,
        batch_id: str,
        ordinal: int,
        step: int,
        *,
        now: int,
        session_boot_id: str | None = None,
        receipt_baseline: int | None = None,
    ) -> None:
        """Commit the write-ahead dispatch marker before returning the action."""
        if session_boot_id is not None and (type(session_boot_id) is not str or not session_boot_id):
            raise LedgerError("dispatch boot identity is malformed")
        if receipt_baseline is not None and (type(receipt_baseline) is not int or receipt_baseline < 0):
            raise LedgerError("native receipt baseline is malformed")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            batch = db.execute("SELECT state FROM batches WHERE batch_id=?", (batch_id,)).fetchone()
            if batch is None:
                raise KeyError(batch_id)
            if batch["state"] not in ("prepared", "running"):
                raise LedgerError(f"batch is terminal: {batch['state']}")
            row = db.execute(
                "SELECT state,next_step FROM actions WHERE batch_id=? AND ordinal=?",
                (batch_id, ordinal),
            ).fetchone()
            if row is None or row["state"] != "ready" or row["next_step"] != step:
                raise LedgerError("action step is not ready; dispatched steps cannot be replayed")
            prior = db.execute(
                "SELECT state FROM actions WHERE batch_id=? AND ordinal<? ORDER BY ordinal",
                (batch_id, ordinal),
            ).fetchall()
            if any(item["state"] != "succeeded" for item in prior):
                raise LedgerError("actions must be dispatched in order after verified success")
            step_row = db.execute(
                "SELECT state FROM action_steps WHERE batch_id=? AND ordinal=? AND step=?",
                (batch_id, ordinal, step),
            ).fetchone()
            if step_row is None or step_row["state"] != "ready":
                raise LedgerError("dispatch step has already started or is missing")
            db.execute(
                "UPDATE actions SET state='dispatched' WHERE batch_id=? AND ordinal=?",
                (batch_id, ordinal),
            )
            db.execute(
                "UPDATE action_steps SET state='dispatched',dispatched_at=?,session_boot_id=?,"
                "receipt_baseline=? "
                "WHERE batch_id=? AND ordinal=? AND step=?",
                (now, session_boot_id, receipt_baseline, batch_id, ordinal, step),
            )
            db.execute("UPDATE batches SET state='running',updated_at=? WHERE batch_id=?", (now, batch_id))

    def finish_step(
        self,
        batch_id: str,
        ordinal: int,
        step: int,
        state: Literal["succeeded", "failed", "uncertain"],
        *,
        now: int,
        callback_delivered: bool | None,
        postcondition: bool | None,
        action_complete: bool | None,
        receipt: dict[str, object] | None,
        continue_action: bool = False,
        reason: str | None = None,
    ) -> None:
        if any(
            type(value) not in (bool, type(None))
            for value in (callback_delivered, postcondition, action_complete)
        ):
            raise LedgerError("callback, postcondition, and completion evidence must be boolean or unknown")
        if type(continue_action) is not bool or (continue_action and state != "succeeded"):
            raise LedgerError("only verified successful steps can advance the action cursor")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state,next_step FROM actions WHERE batch_id=? AND ordinal=?", (batch_id, ordinal)
            ).fetchone()
            if row is None or row["state"] != "dispatched" or row["next_step"] != step:
                raise LedgerError("only the current dispatched step can receive a result")
            step_row = db.execute(
                "SELECT state FROM action_steps WHERE batch_id=? AND ordinal=? AND step=?",
                (batch_id, ordinal, step),
            ).fetchone()
            if step_row is None or step_row["state"] != "dispatched":
                raise LedgerError("dispatch step is not in progress")
            values = (
                state,
                _bool_db(callback_delivered),
                _bool_db(postcondition),
                _bool_db(action_complete),
                canonical_json(receipt) if receipt is not None else None,
                reason[:240] if reason else None,
                batch_id,
                ordinal,
                step,
            )
            db.execute(
                "UPDATE action_steps SET state=?,callback_delivered=?,postcondition=?,action_complete=?,"
                "receipt_json=?,reason=? WHERE batch_id=? AND ordinal=? AND step=?",
                values,
            )
            db.execute(
                "UPDATE actions SET reason=? WHERE batch_id=? AND ordinal=?",
                (values[5], batch_id, ordinal),
            )
            if continue_action:
                next_step = step + 1
                db.execute(
                    "UPDATE actions SET state='ready',next_step=? WHERE batch_id=? AND ordinal=?",
                    (next_step, batch_id, ordinal),
                )
                db.execute(
                    "INSERT INTO action_steps(batch_id,ordinal,step,state) VALUES(?,?,?,'ready')",
                    (batch_id, ordinal, next_step),
                )
                batch_state = "running"
            elif state in ("failed", "uncertain"):
                db.execute(
                    "UPDATE actions SET state=? WHERE batch_id=? AND ordinal=?",
                    (state, batch_id, ordinal),
                )
                batch_state = state
            else:
                db.execute(
                    "UPDATE actions SET state='succeeded' WHERE batch_id=? AND ordinal=?",
                    (batch_id, ordinal),
                )
                remaining = db.execute(
                    "SELECT COUNT(*) AS n FROM actions WHERE batch_id=? AND state!='succeeded'", (batch_id,)
                ).fetchone()["n"]
                batch_state = "succeeded" if remaining == 0 else "running"
            db.execute(
                "UPDATE batches SET state=?,updated_at=? WHERE batch_id=?",
                (batch_state, now, batch_id),
            )

    def fail_before_dispatch(self, batch_id: str, ordinal: int, reason: str, *, now: int) -> None:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT state FROM actions WHERE batch_id=? AND ordinal=?", (batch_id, ordinal)
            ).fetchone()
            if row is None or row["state"] != "ready":
                raise LedgerError("only a ready action can fail before dispatch")
            action = db.execute(
                "SELECT next_step FROM actions WHERE batch_id=? AND ordinal=?", (batch_id, ordinal)
            ).fetchone()
            db.execute(
                "UPDATE actions SET state='failed',reason=? WHERE batch_id=? AND ordinal=?",
                (reason[:240], batch_id, ordinal),
            )
            db.execute(
                "UPDATE action_steps SET state='failed',reason=? WHERE batch_id=? AND ordinal=? AND step=? "
                "AND state='ready'",
                (reason[:240], batch_id, ordinal, action["next_step"]),
            )
            db.execute("UPDATE batches SET state='failed',updated_at=? WHERE batch_id=?", (now, batch_id))

    def recover_interrupted(self, *, now: int | None = None) -> int:
        """Mark leftover dispatch markers uncertain on explicit process startup recovery."""
        timestamp = int(time.time()) if now is None else now
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            batches = db.execute("SELECT DISTINCT batch_id FROM actions WHERE state='dispatched'").fetchall()
            db.execute(
                "UPDATE actions SET state='uncertain',reason='process restarted after dispatch marker' "
                "WHERE state='dispatched'"
            )
            db.execute(
                "UPDATE action_steps SET state='uncertain',reason='process restarted after dispatch marker' "
                "WHERE state='dispatched'"
            )
            db.execute(
                "UPDATE batches SET state='uncertain',updated_at=? WHERE batch_id IN "
                "(SELECT DISTINCT batch_id FROM actions WHERE state='uncertain' AND reason="
                "'process restarted after dispatch marker')",
                (timestamp,),
            )
        return len(batches)


def _bool_db(value: int | None) -> bool | None:
    return None if value is None else bool(value)
