"""Strict independent scanner and automation publication schemas."""

from __future__ import annotations

import pytest

from crowley.gui.frames import PLAN_COUNT, ProtocolError, validate_snapshot
from tests.support.scan_frames import automation_snapshot, snapshot


def _with_last_saved(**last: object) -> dict[str, object]:
    value = snapshot()
    saved = dict(value["lastSaved"])
    saved.update(memoryUid="us-normal-alliance-1794268488-Dandune-5418", finishedAt=1_000, **last)
    value["lastSaved"] = saved
    return value


def test_last_saved_sweep_from_a_previous_plan_size_still_decodes() -> None:
    # A saved sweep can predate the installed plan and remains uploadable.
    validate_snapshot(
        _with_last_saved(planned=PLAN_COUNT + 1, attempted=PLAN_COUNT + 1, failures=0, rows=10),
        expected_kind="scanner",
    )


@pytest.mark.parametrize(
    "counters",
    [
        {"planned": 0, "attempted": 0, "failures": 0, "rows": 0},
        {"planned": 5, "attempted": 6, "failures": 0, "rows": 0},
        {"planned": 5, "attempted": 3, "failures": 1, "rows": 0},
        {"planned": 5, "attempted": 5, "failures": 6, "rows": 0},
    ],
)
def test_inconsistent_last_saved_counters_are_rejected(counters: dict[str, int]) -> None:
    with pytest.raises(ProtocolError, match="lastSaved"):
        validate_snapshot(_with_last_saved(**counters), expected_kind="scanner")


def test_scanner_schema_has_no_automation_or_executor_state() -> None:
    value = snapshot()
    assert validate_snapshot(value, expected_kind="scanner")["kind"] == "scanner"
    value["motion"] = {}
    with pytest.raises(ProtocolError, match="unknown"):
        validate_snapshot(value, expected_kind="scanner")


def test_automation_schema_has_no_scan_plan_dependency() -> None:
    value = automation_snapshot()
    assert validate_snapshot(value, expected_kind="automation")["kind"] == "automation"
    value["plan"] = False
    with pytest.raises(ProtocolError, match="unknown"):
        validate_snapshot(value, expected_kind="automation")


def test_cross_stream_kind_is_rejected() -> None:
    with pytest.raises(ProtocolError, match="expected scanner"):
        validate_snapshot(automation_snapshot(), expected_kind="scanner")


@pytest.mark.parametrize(
    ("setup", "path", "value", "message"),
    [
        (None, ("auctioneer", "targetPresent"), "true", "auctioneer.targetPresent"),
        (
            {("auctioneer", "bindingAvailable"): False},
            ("auctioneer", "ctrlF12Action"),
            "INTERACTTARGET",
            "unavailable binding",
        ),
        (
            {("auctioneer", "targetPresent"): True, ("auctioneer", "targetName"): "Auctioneer"},
            ("auctioneer", "withinTradeRange"),
            True,
            "cannot be true when range is unavailable",
        ),
        (
            {("blockingUI", "staticPopupAvailable"): False},
            ("blockingUI", "staticPopupShown"),
            True,
            "cannot be true when unavailable",
        ),
        (None, ("chatEdit", "focused"), True, "cannot be true when chat is inactive"),
    ],
)
def test_automation_rejects_inconsistent_native_facts(
    setup: dict[tuple[str, str], object] | None,
    path: tuple[str, str],
    value: object,
    message: str,
) -> None:
    snapshot_value = automation_snapshot()
    if setup is not None:
        for setup_path, setup_value in setup.items():
            snapshot_value[setup_path[0]][setup_path[1]] = setup_value
    snapshot_value[path[0]][path[1]] = value
    with pytest.raises(ProtocolError, match=message):
        validate_snapshot(snapshot_value, expected_kind="automation")
