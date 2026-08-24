from __future__ import annotations

import sys
from pathlib import Path

FLOW = Path(__file__).parents[1] / "flows" / "parallel_flame_chase"
sys.path.insert(0, str(FLOW))
