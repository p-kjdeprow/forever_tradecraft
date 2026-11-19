"""Model-free waits and token accounting regressions; no game/server access."""

from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path

import pytest
from crowley_host.ops import cli
from crowley_host.ops.usage import usage
from crowley_host.ops.watch import qualification, watch


def scan(sid=1, *, full=True, started=1000):
    return {
        "id": sid,
        "planned": 2,
        "attempted": 2 if full else 1,
        "successful": 2 if full else 1,
        "started": started,
        "finished": started + 600,
        "watched_success": 2 if full else 1,
        "query_times": {"10": started + 1, "20": started + 10},
    }


def evidence(scans, *, running=True, fault=""):
    return {"scans": scans, "running": running, "status": {"updated_at": time.time(), "fault_code": fault}}


def test_partial_breaks_consecutive_qualification_and_cadence_is_explicit():
    items = (10, 20)
    result = qualification([scan(), scan(2, started=1900)], 2, items)
    assert result["qualified"] and result["cadence_matches"]
    assert result["query_gap_seconds"] == {"min": 900, "max": 900}
    partial = qualification([scan(), scan(2, full=False, started=1900), scan(3, started=2800)], 2, items)
    assert not partial["qualified"]
    delayed = qualification([scan(), scan(2, started=2800)], 2, items)
    assert delayed["qualified"] and not delayed["cadence_matches"]


def test_watch_sleeps_locally_and_only_emits_changed_evidence(tmp_path):
    ticks = [0.0]
    events = []
    calls = []

    def read(after, items):
        calls.append((after, items))
        return evidence([scan()] if len(calls) < 3 else [scan(), scan(2, started=1900)])

    def sleep(seconds):
        ticks[0] += seconds

    output = tmp_path / "private" / "result.json"
    result = watch(
        after=0,
        items=(10, 20),
        read=read,
        clock=lambda: ticks[0],
        sleep=sleep,
        timeout=100,
        poll=20,
        emit=events.append,
        output=output,
    )
    assert result["outcome"] == "complete" and ticks[0] == 40
    assert [e["outcome"] for e in events] == ["waiting", "complete"]
    assert json.loads(output.read_text()) == result
    assert output.stat().st_mode & 0o777 == 0o600
    assert output.parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize(
    "running,fault,outcome,code", [(False, "", "stopped", 3), (True, "window_missing", "fault", 2)]
)
def test_watch_stops_on_operator_hold_or_fault(running, fault, outcome, code):
    result = watch(
        after=0,
        items=(10, 20),
        read=lambda *a: evidence([], running=running, fault=fault),
        sleep=lambda _: pytest.fail("must not sleep/restart"),
    )
    assert result["outcome"] == outcome and result["exit_code"] == code


def test_historical_complete_scan_is_not_a_service_start():
    result = watch(after=0, count=1, items=(10, 20), read=lambda *a: evidence([scan()], running=False))
    assert result["qualified"] and result["outcome"] == "complete"
    assert result["services_running"] is False


def test_watch_deadline_is_bounded():
    ticks = [0.0]

    def sleep(seconds):
        ticks[0] += seconds

    result = watch(
        after=0,
        items=(10, 20),
        timeout=25,
        poll=20,
        clock=lambda: ticks[0],
        sleep=sleep,
        read=lambda *a: evidence([]),
    )
    assert ticks[0] == 25 and result["outcome"] == "timeout"


@pytest.mark.parametrize(
    "args", [{"after": -1}, {"count": 11}, {"timeout": float("inf")}, {"poll": 0}, {"items": (True,)}]
)
def test_bad_watch_bounds_do_not_query_or_touch_game(args):
    params = {"after": 0, "items": (10, 20), **args}
    with pytest.raises(ValueError):
        watch(**params, read=lambda *a: pytest.fail("must not query"))


def log(path, events):
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")


def meta(cwd="/project", sid="one"):
    return {"type": "session_meta", "payload": {"id": sid, "cwd": cwd, "source": "cli"}}


def tokens(stamp, total, cached, output, *, last=None):
    current = {
        "input_tokens": total,
        "cached_input_tokens": cached,
        "output_tokens": output,
        "reasoning_output_tokens": output // 2,
        "total_tokens": total + output,
    }
    return {
        "type": "event_msg",
        "timestamp": stamp,
        "payload": {
            "type": "token_count",
            "info": {"total_token_usage": current, "last_token_usage": current if last is None else last},
        },
    }


NOW = dt.datetime(2026, 11, 19, 12, tzinfo=dt.UTC)


def test_usage_counts_window_deltas_and_deduplicates_snapshots(tmp_path):
    first = tokens("2026-11-18T10:00:00Z", 1000, 800, 100)
    second = tokens("2026-11-19T10:00:00Z", 1300, 1000, 160)
    log(
        tmp_path / "one.jsonl",
        [meta(), {"type": "turn_context", "payload": {"model": "sol"}}, first, second, second],
    )
    result = usage(tmp_path, now=NOW, project=Path("/project"))
    totals = result["by_project"]["/project"]
    assert totals["requests"] == 1 and totals["input_tokens"] == 300 and totals["output_tokens"] == 60
    assert totals["cached_input_tokens"] == 200 and totals["uncached_input_tokens"] == 100
    assert totals["reasoning_output_tokens"] == 30
    assert result["by_model"]["sol"] == totals


def test_usage_excludes_inherited_totals_and_accounts_for_resets(tmp_path):
    last = {"input_tokens": 20, "cached_input_tokens": 10, "output_tokens": 4, "reasoning_output_tokens": 2}
    inherited = tokens("2026-11-19T10:00:00Z", 100000, 90000, 2000, last=last)
    reset = tokens("2026-11-19T11:00:00Z", 40, 20, 8)
    log(tmp_path / "one.jsonl", [meta(), inherited, reset])
    result = usage(tmp_path, now=NOW)
    assert result["by_project"]["/project"]["input_tokens"] == 60
    assert result["by_project"]["/project"]["output_tokens"] == 12


def test_usage_project_scope_never_returns_prompts(tmp_path):
    secret = "SECRET_PROMPT_DO_NOT_PRINT"
    for index, cwd in enumerate(["/project", "/other"]):
        log(
            tmp_path / f"{index}.jsonl",
            [
                meta(cwd, str(index)),
                {"type": "response_item", "payload": {"content": secret}},
                tokens("2026-11-19T10:00:00Z", 100, 50, 10),
            ],
        )
    result = usage(tmp_path, now=NOW, project=Path("/project"))
    assert list(result["by_project"]) == ["/project"] and secret not in json.dumps(result)
    assert len(usage(tmp_path, now=NOW)["by_project"]) == 2


def test_usage_reports_truncated_counter_records(tmp_path):
    p = tmp_path / "one.jsonl"
    log(p, [meta(), tokens("2026-11-19T10:00:00Z", 100, 50, 10)])
    with p.open("a") as f:
        f.write('{"type":"token_count",')
    assert usage(tmp_path, now=NOW)["malformed_records"] == 1


def test_usage_cli_does_not_resolve_live_operation_paths(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.health, "operation_paths", lambda: pytest.fail("no live config needed"))
    log(
        tmp_path / "one.jsonl",
        [meta(str(Path.cwd())), tokens(dt.datetime.now(dt.UTC).isoformat(), 100, 50, 10)],
    )
    assert cli.main(["usage", "--sessions", str(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["by_project"][str(Path.cwd())]["requests"] == 1


def test_watch_cli_dispatches_once(monkeypatch, capsys, tmp_path):
    from crowley_host.ops import watch as module

    calls = []

    def run(**kwargs):
        calls.append(kwargs)
        kwargs["emit"]({"outcome": "complete"})
        return {"exit_code": 0}

    monkeypatch.setattr(module, "watch", run)
    monkeypatch.setattr(cli.health, "operation_paths", lambda: pytest.fail("no backups needed"))
    assert (
        cli.main(["watch", "--after-sweep", "12", "--items", "20,10", "--output", str(tmp_path / "x")]) == 0
    )
    assert len(calls) == 1 and calls[0]["after"] == 12 and calls[0]["items"] == (10, 20)
    assert json.loads(capsys.readouterr().out)["outcome"] == "complete"


def test_watch_writes_terminal_receipt_on_io_failure_and_cancellation(tmp_path):
    output = tmp_path / "private" / "result.json"

    def fail(*args):
        raise OSError("private connection error")

    result = watch(after=0, items=(10, 20), output=output, read=fail)
    assert result["outcome"] == "fault" and result["reason"] == "scan_evidence_unavailable"
    assert json.loads(output.read_text()) == result

    def cancel(seconds):
        raise KeyboardInterrupt()

    result = watch(after=0, items=(10, 20), output=output, read=lambda *a: evidence([]), sleep=cancel)
    assert result["outcome"] == "cancelled" and result["exit_code"] == 130
    assert json.loads(output.read_text()) == result


def test_delayed_upload_ids_do_not_reverse_scan_cadence():
    result = qualification([scan(1080, started=1900), scan(1081, started=1000)], 2, (10, 20))
    assert [s["id"] for s in result["sweeps"]] == [1081, 1080]
    assert result["cadence_matches"] and result["query_gap_seconds"]["min"] == 900


def test_sample_only_queries_packaged_database_and_service_state(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from crowley_host.ops import watch as module

    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(
            stdout="[]" if argv[0] == "docker" else "inactive\ninactive\n",
            returncode=0 if argv[0] == "docker" else 3,
        )

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module, "secrets_path", lambda: tmp_path / "config" / "crowley" / "secrets.env")
    monkeypatch.setattr(module.install, "compose_file_path", lambda: tmp_path / "compose.yml")
    result = module.sample(12, (10, 20))
    assert result == {"scans": [], "running": False, "status": {}}
    assert len(calls) == 2 and calls[0][0] == "docker"
    assert "BEGIN READ ONLY;" in calls[0][-1] and "ms.id>12" in calls[0][-1]
    assert calls[1] == [
        "systemctl",
        "--user",
        "is-active",
        "crowley-host.service",
        "crowley-companion.service",
    ]


def test_usage_malformed_total_is_a_clear_error(tmp_path):
    record = tokens("2026-11-19T10:00:00Z", 100, 50, 10)
    del record["payload"]["info"]["total_token_usage"]["total_tokens"]
    log(tmp_path / "one.jsonl", [meta(), record])
    with pytest.raises(ValueError, match="invalid Codex token counter"):
        usage(tmp_path, now=NOW)


def test_service_query_failure_is_not_reported_as_operator_stop(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from crowley_host.ops import watch as module

    def run(argv, **kwargs):
        return SimpleNamespace(
            stdout="[]" if argv[0] == "docker" else "", returncode=0 if argv[0] == "docker" else 1
        )

    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module, "secrets_path", lambda: tmp_path / "config" / "crowley" / "secrets.env")
    monkeypatch.setattr(module.install, "compose_file_path", lambda: tmp_path / "compose.yml")
    with pytest.raises(module.health.OperationsError, match="service state unavailable"):
        module.sample(12, (10, 20))
