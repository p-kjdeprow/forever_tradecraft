"""CLI helpers for publishing, deriving and reconciling trade queues."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping, Sequence
from typing import Any

import httpx
import psycopg
from psycopg.rows import dict_row

from crowley.core.queue import (
    TERMINAL_STATUSES,
    build_queue,
    validate_execution_record,
    validate_queue,
)


class QueueCLIError(RuntimeError):
    """A queue command cannot safely complete."""


def _value(intent: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in intent:
            return intent[name]
    raise QueueCLIError(f"paper intent is missing {names[0]}")


def map_pending_intents(
    pending: Mapping[str, Sequence[Mapping[str, Any]]], *, start_seq: int = 1
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Map serialized paper intents to executor actions and report skipped cancels."""

    actions: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    seq = start_seq
    for strategy, intents in pending.items():
        for ordinal, intent in enumerate(intents):
            type_name = str(intent.get("type", intent.get("kind", "")))
            action: dict[str, Any] | None
            if type_name == "Buy":
                action = {
                    "seq": seq,
                    "kind": "buy_commodity",
                    "itemID": _value(intent, "item_id", "itemID"),
                    "quantity": _value(intent, "quantity"),
                    "maxUnitPrice": _value(intent, "max_unit_price", "maxUnitPrice"),
                }
            elif type_name == "Post":
                action = {
                    "seq": seq,
                    "kind": "post_commodity",
                    "itemID": _value(intent, "item_id", "itemID"),
                    "quantity": _value(intent, "quantity"),
                    "unitPrice": _value(intent, "unit_price", "unitPrice"),
                    "durationHours": _value(intent, "duration_hours", "durationHours"),
                }
            elif type_name == "VendorSell":
                action = {
                    "seq": seq,
                    "kind": "vendor_sell",
                    "itemID": _value(intent, "item_id", "itemID"),
                    "quantity": _value(intent, "quantity"),
                }
            elif type_name == "VendorBuy":
                action = {
                    "seq": seq,
                    "kind": "vendor_buy",
                    "itemID": _value(intent, "item_id", "itemID"),
                    "quantity": _value(intent, "quantity"),
                }
            elif type_name == "Craft":
                action = {
                    "seq": seq,
                    "kind": "craft",
                    "recipeID": _value(intent, "spell_id", "spellID", "recipeID"),
                    "times": _value(intent, "times"),
                }
            elif type_name == "Cancel":
                action = None
                skipped.append(
                    {
                        "strategy": strategy,
                        "ordinal": ordinal,
                        "intent": dict(intent),
                        "reason": "paper listing ids are not real queue auction IDs",
                    }
                )
            else:
                raise QueueCLIError(f"unsupported paper intent type {type_name!r}")
            if action is not None:
                actions.append(action)
                seq += 1
    return actions, skipped


def _client(server_url: str, api_token: str | None) -> httpx.Client:
    token = api_token or os.environ.get("CROWLEY_API_TOKEN")
    headers = {} if not token else {"Authorization": f"Bearer {token}"}
    return httpx.Client(base_url=server_url.rstrip("/"), headers=headers, timeout=120)


def _preview(queue: dict[str, Any], skipped: Sequence[Mapping[str, Any]] = ()) -> None:
    print("dry preview:")
    print(json.dumps({"queue": queue, "skipped": list(skipped)}, indent=2, sort_keys=True))
    print(f"queueId={queue['queueId']}")


def _build_final_queue(
    spec: Mapping[str, Any], *, armed: bool, source: str, require_complete_armed: bool = False
) -> dict[str, Any]:
    if source == "paper_pending" and armed:
        raise QueueCLIError("paper-derived queues must be unarmed")
    if spec.get("armed") is True and not armed:
        raise QueueCLIError("an armed queue requires --armed and --confirm-real-trades")

    complete = "queueId" in spec
    if armed and require_complete_armed and not complete:
        raise QueueCLIError(
            "--armed requires a complete queue file; run queue preview --armed --out QUEUE.json first"
        )
    queue = build_queue(
        {**dict(spec), "source": source},
        armed=None if complete else armed,
    )
    if queue["armed"] is not armed:
        if armed:
            raise QueueCLIError("--armed requires the queue file to have armed=true")
        raise QueueCLIError("an armed queue file requires --armed")
    return queue


def _write_new_queue(path: os.PathLike[str] | str, queue: Mapping[str, Any]) -> None:
    target = os.fspath(path)
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise QueueCLIError(f"refusing to overwrite existing queue file {target}") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(queue, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.chmod(target, 0o600)
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            os.unlink(target)
        except OSError:
            pass
        raise


def preview_spec(
    spec: Mapping[str, Any], out: os.PathLike[str] | str, *, armed: bool = False
) -> dict[str, Any]:
    """Build and save the exact queue that a later publish will submit."""

    queue = _build_final_queue(spec, armed=armed, source="spec")
    _write_new_queue(out, queue)
    _preview(queue)
    return queue


def publish_spec(
    spec: Mapping[str, Any],
    *,
    server_url: str,
    api_token: str | None = None,
    armed: bool = False,
    confirm_real_trades: str | None = None,
    source: str = "spec",
) -> dict[str, Any]:
    """Preview and publish one queue, enforcing the explicit real-trade gate."""

    queue = _build_final_queue(spec, armed=armed, source=source, require_complete_armed=armed)
    _preview(queue)
    if armed:
        if not confirm_real_trades:
            raise QueueCLIError("--armed requires --confirm-real-trades <queueId>")
        if confirm_real_trades != queue["queueId"]:
            raise QueueCLIError("--confirm-real-trades must equal the full queueId")
    elif confirm_real_trades is not None:
        raise QueueCLIError("--confirm-real-trades requires --armed")
    body = {**queue, "source": source}
    if armed:
        body["confirmRealTrades"] = confirm_real_trades
    with _client(server_url, api_token) as client:
        response = client.post("/v1/queues", json=body)
        response.raise_for_status()
        return response.json()


def _paper_pending(db_url: str, run_name: str) -> tuple[str, Mapping[str, Sequence[Mapping[str, Any]]]]:
    with psycopg.connect(db_url) as conn:
        row = conn.execute(
            """
            SELECT h.key, p.intents
            FROM paper_pending AS p
            JOIN paper_runs AS r ON r.id = p.run_id
            JOIN houses AS h ON h.id = r.house_id
            WHERE r.name = %s
            """,
            (run_name,),
        ).fetchone()
    if row is None:
        raise QueueCLIError(f"unknown or non-pending paper run {run_name!r}")
    return str(row[0]), row[1]


def from_paper(
    db_url: str,
    run_name: str,
    character: str,
    *,
    expires_minutes: int = 30,
    server_url: str,
    api_token: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    if expires_minutes <= 0:
        raise QueueCLIError("expires-minutes must be positive")
    house, pending = _paper_pending(db_url, run_name)
    actions, skipped = map_pending_intents(pending)
    spec = {
        "house": house,
        "character": character,
        "generatedAt": int(time.time()),
        "expiresAt": int(time.time()) + expires_minutes * 60,
        "actions": actions,
        "armed": False,
    }
    queue = build_queue({**spec, "source": "paper_pending"}, armed=False)
    _preview(queue, skipped)
    with _client(server_url, api_token) as client:
        response = client.post("/v1/queues", json={**queue, "source": "paper_pending"})
        response.raise_for_status()
        published = response.json()
    return queue, skipped, published


def status(
    house: str,
    character: str,
    *,
    server_url: str,
    api_token: str | None = None,
) -> Any:
    with _client(server_url, api_token) as client:
        response = client.get(f"/v1/houses/{house}/queue/active", params={"character": character})
        response.raise_for_status()
        value = response.json()
    print(json.dumps(value, indent=2, sort_keys=True))
    return value


def reconcile_queue(queue: Mapping[str, Any], execution: Mapping[str, Any]) -> dict[str, Any]:
    """Compare requested actions with observed result events."""

    queue = validate_queue(dict(queue))
    execution = validate_execution_record(dict(execution), queue)
    by_seq: dict[int, list[dict[str, Any]]] = {}
    for result in execution["results"]:
        by_seq.setdefault(result["seq"], []).append(result)
    actions = []
    issues: list[dict[str, Any]] = []
    for action in queue["actions"]:
        action_issues: list[str] = []
        results = by_seq.get(action["seq"], [])
        terminals = [result for result in results if result["status"] in TERMINAL_STATUSES]
        if not terminals:
            action_issues.append("missing_terminal_result")
        for result in results:
            detail = result["detail"]
            if action["kind"] == "buy_commodity" and "unitPrice" in detail:
                observed = detail["unitPrice"]
                if not isinstance(observed, int) or isinstance(observed, bool) or observed < 0:
                    action_issues.append("invalid_observed_unit_price")
                elif observed > action["maxUnitPrice"]:
                    action_issues.append("price_above_max")
            if action["kind"] == "buy_commodity" and result["status"] == "succeeded":
                if "unitPrice" not in detail:
                    action_issues.append("missing_unit_price")
            if action["kind"] == "post_commodity" and result["status"] == "succeeded":
                deposit = detail.get("deposit", detail.get("observedDeposit"))
                if deposit is None:
                    action_issues.append("missing_deposit")
                elif isinstance(deposit, bool) or not isinstance(deposit, int) or deposit < 0:
                    action_issues.append("invalid_deposit")
        action_report = {
            "requested": action,
            "results": results,
            "terminal": bool(terminals),
            "issues": sorted(set(action_issues)),
        }
        actions.append(action_report)
        for issue in sorted(set(action_issues)):
            issues.append({"seq": action["seq"], "kind": action["kind"], "issue": issue})
    return {"queueId": queue["queueId"], "actions": actions, "issues": issues, "ok": not issues}


def reconcile_from_database(db_url: str, queue_id: str) -> dict[str, Any]:
    with psycopg.connect(db_url) as conn, conn.cursor(row_factory=dict_row) as cur:
        queue_row = cur.execute(
            """
            SELECT q.queue_id, q.generated_at, q.expires_at, h.key AS house,
                   q.character, q.armed, q.actions
            FROM trade_queues q JOIN houses h ON h.id = q.house_id
            WHERE q.queue_id = %s
            """,
            (queue_id,),
        ).fetchone()
        if queue_row is None:
            raise QueueCLIError(f"unknown queue {queue_id!r}")
        queue = {
            "format": 1,
            "queueId": queue_row["queue_id"],
            "generatedAt": int(queue_row["generated_at"].timestamp()),
            "expiresAt": int(queue_row["expires_at"].timestamp()),
            "house": queue_row["house"],
            "character": queue_row["character"],
            "armed": queue_row["armed"],
            "actions": queue_row["actions"],
        }
        record = cur.execute(
            "SELECT raw FROM trade_execution_records WHERE queue_id = %s", (queue_id,)
        ).fetchone()
    execution = (
        record["raw"]
        if record is not None
        else {"queueId": queue_id, "house": queue["house"], "character": queue["character"], "results": []}
    )
    result = reconcile_queue(queue, execution)
    print(json.dumps(result, indent=2, sort_keys=True))
    return result


__all__ = [
    "QueueCLIError",
    "from_paper",
    "map_pending_intents",
    "preview_spec",
    "publish_spec",
    "reconcile_from_database",
    "reconcile_queue",
    "status",
]
