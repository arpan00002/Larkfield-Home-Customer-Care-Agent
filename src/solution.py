"""Entry point for the Larkfield Home Customer Care service.

Run with ``python src/solution.py`` (or ``python -m uvicorn src.app:app``).
The HTTP application lives in ``src/app.py``; the policy engine is in
``src/policy.py``. This module only starts the server.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    import uvicorn

    uvicorn.run(
        "src.app:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "8000")),
        log_level="info",
    )


if __name__ == "__main__":
    main()
