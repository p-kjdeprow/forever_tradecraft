"""Installed console wrapper for the systemd paper-step timer."""

from __future__ import annotations

import os

from crowley.trading.__main__ import main as trading_main


def main() -> int:
    """Run the configured paper run without requiring a checkout path."""

    name = os.environ.get("CROWLEY_PAPER_RUN")
    if not name:
        raise RuntimeError("CROWLEY_PAPER_RUN is required")
    return trading_main(["paper-step", "--name", name])


__all__ = ["main"]
