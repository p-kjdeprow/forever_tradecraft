"""Bounded, read-only scan qualification without an agent polling the game."""

from __future__ import annotations

import json
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from crowley_common.game_target import FIXED_COLLECTION_CADENCE_SECONDS, canonical_plan, selected_target
from crowley_market.backup import secrets_path

from crowley_host.ops import health, install
from crowley_host.scan.loop import status_path


def sample(after: int, item_ids: tuple[int, ...]) -> dict[str, Any]:
    """Read the installed deployment's database and status; never start a service."""
    target = selected_target()
    plan = canonical_plan()
    # All interpolated values originate in the validated target or integer arguments.
    ids = ",".join(str(i) for i in item_ids)
    query = f"""
    BEGIN READ ONLY;
    SELECT COALESCE(json_agg(row_to_json(result)), '[]'::json) FROM (
      SELECT ms.id, EXTRACT(EPOCH FROM ms.started_at)::bigint AS started,
        EXTRACT(EPOCH FROM ms.finished_at)::bigint AS finished,
        ms.num_observations AS planned,
        COUNT(*) FILTER (WHERE mo.query->>'kind'='queried') AS attempted,
        COUNT(*) FILTER (WHERE mo.query->>'kind'='queried' AND mo.reason_code='ok') AS successful,
        COUNT(*) FILTER (WHERE mo.item_id IN ({ids}) AND mo.query->>'kind'='queried'
          AND mo.reason_code='ok') AS watched_success,
        jsonb_object_agg(mo.item_id, mo.query->'startedAt')
          FILTER (WHERE mo.item_id IN ({ids})) AS query_times
      FROM market_sweeps ms JOIN houses h ON h.id=ms.house_id
        JOIN market_observations mo ON mo.sweep_id=ms.id
      WHERE h.key='{target.house}' AND ms.plan_hash='{plan["planHash"]}'
        AND ms.client_version || '.' || ms.client_build='{target.full_build}' AND ms.id>{after}
      GROUP BY ms.id ORDER BY ms.started_at DESC,ms.finished_at DESC,ms.id DESC LIMIT 50
    ) result;
    COMMIT;
    """
    secrets = secrets_path()
    command = [
        "docker",
        "compose",
        "--env-file",
        str(secrets),
        "-f",
        str(install.compose_file_path()),
        "exec",
        "-T",
        "db",
        "psql",
        "-X",
        "-qAt",
        "-U",
        "crowley",
        "-d",
        "crowley",
        "-v",
        "ON_ERROR_STOP=1",
        "-c",
        query,
    ]
    try:
        done = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
            env={**os.environ, "XDG_CONFIG_HOME": str(secrets.parent.parent)},
        )
        scans = json.loads(done.stdout)
        active = subprocess.run(
            ["systemctl", "--user", "is-active", "crowley-host.service", "crowley-companion.service"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if active.returncode not in (0, 3) or len(active.stdout.splitlines()) != 2:
            raise health.OperationsError("service state unavailable")
        running = active.returncode == 0
        status = json.loads(status_path().read_text()) if running else {}
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise health.OperationsError("scan evidence unavailable") from exc
    return {"scans": scans, "running": running, "status": status}


def qualification(scans: list[dict[str, Any]], count: int, items: tuple[int, ...]) -> dict[str, Any]:
    """A failed or partial sweep breaks the consecutive full-sweep sequence."""
    ordered = sorted(scans, key=lambda s: (s["started"], s["finished"], s["id"]))
    recent = ordered[-count:]
    full = len(recent) == count and all(
        s["planned"] > 0
        and s["planned"] == s["attempted"] == s["successful"]
        and s["watched_success"] == len(items)
        for s in recent
    )
    result: dict[str, Any] = {
        "sweeps": [{k: v for k, v in s.items() if k != "query_times"} for s in recent],
        "watched_count": len(items),
        "qualified": full,
    }
    if full and count > 1:
        gaps = [
            b["query_times"][str(i)] - a["query_times"][str(i)]
            for a, b in zip(recent, recent[1:], strict=False)
            for i in items
        ]
        result["start_gaps_seconds"] = [
            b["started"] - a["started"] for a, b in zip(recent, recent[1:], strict=False)
        ]
        result["query_gap_seconds"] = {"min": min(gaps), "max": max(gaps)}
        result["cadence_seconds"] = FIXED_COLLECTION_CADENCE_SECONDS
        result["cadence_matches"] = all(
            gap == FIXED_COLLECTION_CADENCE_SECONDS for gap in result["start_gaps_seconds"]
        )
    return result


def watch(
    *,
    after: int,
    count: int = 2,
    timeout: float = 2400,
    poll: float = 20,
    items: tuple[int, ...],
    output: Path | None = None,
    read: Callable[[int, tuple[int, ...]], dict[str, Any]] = sample,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    emit: Callable[[dict[str, Any]], None] = lambda event: None,
) -> dict[str, Any]:
    if (
        type(after) is not int
        or after < 0
        or type(count) is not int
        or not 1 <= count <= 10
        or not 0 < timeout <= 86400
        or not 1 <= poll <= 60
        or not items
        or any(type(i) is not int or i <= 0 for i in items)
        or len(set(items)) != len(items)
    ):
        raise ValueError("invalid scan watch bounds")
    deadline = clock() + timeout
    previous = None
    result: dict[str, Any] = {}

    def publish(event: dict[str, Any]) -> None:
        if output is not None:
            health._atomic_bytes(output, json.dumps(event, sort_keys=True).encode())
        emit(event)

    def cancelled() -> dict[str, Any]:
        event = {
            **result,
            "schema": "crowley.scan-watch.v1",
            "after_sweep": after,
            "qualified": False,
            "outcome": "cancelled",
            "exit_code": 130,
        }
        publish(event)
        return event

    while True:
        try:
            evidence = read(after, items)
        except KeyboardInterrupt:
            return cancelled()
        except (health.OperationsError, OSError, ValueError):
            result = {
                "schema": "crowley.scan-watch.v1",
                "after_sweep": after,
                "qualified": False,
                "outcome": "fault",
                "reason": "scan_evidence_unavailable",
                "exit_code": 2,
            }
            publish(result)
            return result
        result = {
            "schema": "crowley.scan-watch.v1",
            "after_sweep": after,
            "services_running": evidence["running"],
            "host_fault": evidence["status"].get("fault_code", ""),
            **qualification(evidence["scans"], count, items),
        }
        status = evidence["status"]
        if result["qualified"]:
            result.update(outcome="complete", exit_code=0)
        elif not evidence["running"]:
            result.update(outcome="stopped", reason="scanner_services_inactive", exit_code=3)
        elif status.get("fault_code"):
            result.update(outcome="fault", reason=status["fault_code"], exit_code=2)
        elif time.time() - status.get("updated_at", 0) > health.STATUS_MAX_AGE_SECONDS:
            result.update(outcome="fault", reason="host_status_stale", exit_code=2)
        elif clock() >= deadline:
            result.update(outcome="timeout", exit_code=4)
        else:
            result.update(outcome="waiting", exit_code=0)
        marker = (result["outcome"], tuple(s["id"] for s in result["sweeps"]))
        if marker != previous:
            publish(result)
            previous = marker
        if result["outcome"] != "waiting":
            return result
        try:
            sleep(min(poll, max(0, deadline - clock())))
        except KeyboardInterrupt:
            return cancelled()
