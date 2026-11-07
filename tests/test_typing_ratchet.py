"""Pyright baseline ratchet tests."""

from __future__ import annotations

from pathlib import Path

from tests.support.typing_ratchet import (
    PackageErrorCount,
    TypingCounts,
    compare_baseline,
    load_baseline,
    run_pyright,
)

ROOT = Path(__file__).parents[1]
BASELINE = ROOT / "tests/fixtures/typing_baseline.json"


def test_repository_typing_matches_baseline() -> None:
    expected = load_baseline(BASELINE)
    actual = run_pyright(ROOT)
    details = compare_baseline(expected, actual)
    assert not details, (
        "typing baseline mismatch:\n"
        + "\n".join(details)
        + "\nRegenerate with: uv run python -m tests.support.typing_ratchet --write-baseline"
    )


def test_compare_baseline_rejects_increases_and_stale_counts() -> None:
    expected = TypingCounts(
        basic=(PackageErrorCount("analysis", 2),),
        strict=(PackageErrorCount("platform", 0),),
    )
    actual = TypingCounts(
        basic=(PackageErrorCount("analysis", 3),),
        strict=(PackageErrorCount("platform", 0),),
    )

    details = compare_baseline(expected, actual)

    assert details == ("basic package 'analysis' increased from 2 to 3 errors",)


def test_compare_baseline_rejects_strict_errors() -> None:
    expected = TypingCounts(
        basic=(PackageErrorCount("analysis", 2),),
        strict=(PackageErrorCount("platform", 0),),
    )
    actual = TypingCounts(
        basic=(PackageErrorCount("analysis", 2),),
        strict=(PackageErrorCount("platform", 1),),
    )

    details = compare_baseline(expected, actual)

    assert details == (
        "strict package 'platform' increased from 0 to 1 errors",
        "strict package 'platform' has 1 errors",
    )
