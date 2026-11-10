"""Frame snapshot invariants that the scan loop depends on."""

from __future__ import annotations

import pytest

from crowley.gui.frames import PLAN_COUNT, ProtocolError, validate_snapshot
from tests.support.scan_frames import snapshot


def _with_last_saved(**last: object) -> dict[str, object]:
    value = snapshot()
    saved = dict(value["lastSaved"])
    saved.update(memoryUid="us-normal-alliance-1794268488-Dandune-5418", finishedAt=1_000, **last)
    value["lastSaved"] = saved
    return value


def test_last_saved_sweep_from_a_previous_plan_size_still_decodes() -> None:
    # Live 2026-11-09: a 428-item sweep saved before plan revision 3 (427 items) made
    # every frame invalid, so the loop could never run the sweep that replaces it.
    validate_snapshot(_with_last_saved(planned=PLAN_COUNT + 1, attempted=PLAN_COUNT + 1, failures=0, rows=10))


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
        validate_snapshot(_with_last_saved(**counters))
