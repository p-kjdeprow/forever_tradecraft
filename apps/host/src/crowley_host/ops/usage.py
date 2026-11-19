"""Local Codex token accounting; no prompts, image decoding or provider requests."""

from __future__ import annotations

import datetime as dt
import json
from collections import Counter
from pathlib import Path
from typing import Any

FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")


def usage(
    root: Path,
    *,
    hours: float = 24,
    project: Path | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    if not 0 < hours <= 24 * 31:
        raise ValueError("usage window must be between zero and 744 hours")
    end = dt.datetime.now(dt.UTC) if now is None else now
    if end.tzinfo is None:
        raise ValueError("usage time must include timezone")
    start = end - dt.timedelta(hours=hours)
    if not root.is_dir():
        raise ValueError("Codex session directory is unavailable")
    by_project: dict[str, Counter] = {}
    by_model: dict[str, Counter] = {}
    sessions = []
    malformed = 0
    for path in root.rglob("*.jsonl"):
        if path.stat().st_mtime < start.timestamp():
            continue
        meta: dict[str, Any] = {}
        previous = None
        model = "unknown"
        totals: Counter = Counter()
        requests = 0
        max_context = 0
        last = None
        with path.open("rb") as handle:
            for line in handle:
                # Skip large tool results and images without parsing their contents.
                if not any(word in line for word in (b'"session_meta"', b'"turn_context"', b'"token_count"')):
                    continue
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    malformed += 1
                    continue
                payload = event.get("payload", {})
                if event.get("type") == "session_meta":
                    meta = payload
                    if project is not None and payload.get("cwd") != str(project):
                        break
                    continue
                if event.get("type") == "turn_context":
                    model = payload.get("model", "unknown")
                    continue
                if event.get("type") != "event_msg" or payload.get("type") != "token_count":
                    continue
                info = payload.get("info")
                if not info or not info.get("total_token_usage"):
                    continue
                current = info["total_token_usage"]
                if not isinstance(current, dict) or "total_tokens" not in current:
                    raise ValueError("invalid Codex token counter")
                if any(
                    type(current.get(key, 0)) is not int or current.get(key, 0) < 0
                    for key in (*FIELDS, "total_tokens")
                ):
                    raise ValueError("invalid Codex token counter")
                if previous is None or current["total_tokens"] < previous["total_tokens"]:
                    base = info.get("last_token_usage")
                else:
                    base = {key: max(0, current.get(key, 0) - previous.get(key, 0)) for key in FIELDS}
                previous = current
                if not base:
                    continue
                try:
                    stamp = dt.datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))
                except (KeyError, ValueError):
                    malformed += 1
                    continue
                if stamp.tzinfo is None or not start <= stamp <= end:
                    continue
                delta = {key: base.get(key, 0) for key in FIELDS}
                if any(type(v) is not int or v < 0 for v in delta.values()):
                    raise ValueError("invalid Codex token counter")
                if not delta["input_tokens"] and not delta["output_tokens"]:
                    continue
                if not meta.get("id") or not meta.get("cwd"):
                    raise ValueError("Codex usage lacks session identity")
                delta["requests"] = 1
                for group, key in ((by_project, meta["cwd"]), (by_model, model)):
                    group.setdefault(key, Counter()).update(delta)
                totals.update(delta)
                requests += 1
                max_context = max(max_context, (info.get("last_token_usage") or {}).get("input_tokens", 0))
                last = event["timestamp"]
        if requests:
            source = meta.get("source", {})
            worker = source.get("subagent", {}).get("thread_spawn", {}) if isinstance(source, dict) else {}
            sessions.append(
                {
                    "id": meta["id"],
                    "project": meta["cwd"],
                    "agent": worker.get("agent_path", "main"),
                    **dict(totals),
                    "max_request_input_tokens": max_context,
                    "last_usage": last,
                }
            )
    for group in (by_project, by_model):
        for values in group.values():
            values["uncached_input_tokens"] = values["input_tokens"] - values["cached_input_tokens"]
    return {
        "schema": "crowley.codex-usage.v1",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "project": str(project) if project is not None else None,
        "by_project": by_project,
        "by_model": by_model,
        "sessions": sorted(
            sessions,
            key=lambda s: s["input_tokens"] - s["cached_input_tokens"] + s["output_tokens"],
            reverse=True,
        )[:10],
        "malformed_records": malformed,
        "note": "Local counter increments, not billed quota. Cached input is included in input; "
        "reasoning output is included in output. Inherited totals are excluded.",
    }
