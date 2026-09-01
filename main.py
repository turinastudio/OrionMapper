"""OrionMapper CLI Entry Point.

Usage:
    python main.py --help
    python main.py scrape --provider all --limit 10
    python main.py match --unmapped-only
    python main.py export --target data/orion_mappings
    python main.py sync --dry-run
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure src/ is in sys.path when invoked directly as python main.py
_src_path = str(Path(__file__).resolve().parent / "src")
if _src_path not in sys.path:
    sys.path.insert(0, _src_path)

from orion_mapper.cli.commands import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
