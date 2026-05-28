"""Pytest path setup: tests target the `src/` layout where the
`mcp_glimmung` package lives, mirroring how Hatch builds the wheel."""
from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
