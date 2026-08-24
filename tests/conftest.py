from __future__ import annotations

import sys
from pathlib import Path

FLOWS = Path(__file__).parents[1] / "flows"
sys.path.insert(0, str(FLOWS))
