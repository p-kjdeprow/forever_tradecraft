from __future__ import annotations

import sys
from pathlib import Path

_REPOSITORY = Path(__file__).resolve().parents[2]
for _source in ("packages/scanner/src", "packages/market/src", "packages/common/src"):
    sys.path.insert(0, str(_REPOSITORY / _source))
