"""Test helper for writing the current companion state contract."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from crowley.core.companion_state import STATE_VERSION, CompanionState, dump_bytes


def write_companion_state(
    path: Path,
    *,
    uploaded: Mapping[str, int] | None = None,
    probes: tuple[int, ...] = (),
) -> None:
    state = CompanionState(
        version=STATE_VERSION,
        uploaded={} if uploaded is None else uploaded,
        probes=probes,
        references=(),
        accounts=(),
        executions={},
        rejections={},
    )
    path.write_bytes(dump_bytes(state))
    path.chmod(0o600)
