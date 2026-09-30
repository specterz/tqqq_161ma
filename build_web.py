"""Sync the backtest engine + data into docs/ for the Pyodide website.

GitHub Pages serves the site from the ``docs/`` folder. The browser needs:
  * the real Python engine  -> copied to docs/py/
  * the bundled price data   -> copied to docs/data/

Running this after any change to src/ or data/ keeps the website's engine and
the CLI in perfect lockstep (single source of truth — no hand-ported JS math).

Usage:  python build_web.py
"""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DOCS = ROOT / "docs"

# Python modules the browser imports (web_api pulls in the rest).
PY_FILES = ["data.py", "strategy.py", "backtest.py", "web_api.py"]
# Data files mounted into Pyodide's virtual filesystem.
DATA_FILES = ["NDX.csv", "tbill_dgs3mo.csv", "SPX.csv"]


def main() -> None:
    (DOCS / "py").mkdir(parents=True, exist_ok=True)
    (DOCS / "data").mkdir(parents=True, exist_ok=True)

    # copy2 preserves timestamps/metadata so the copied engine is byte-identical
    # to src/ — the whole point is that the browser runs the SAME code as the CLI.
    for f in PY_FILES:
        shutil.copy2(ROOT / "src" / f, DOCS / "py" / f)
    for f in DATA_FILES:
        shutil.copy2(ROOT / "data" / f, DOCS / "data" / f)

    # A manifest the browser reads to know what to mount (kept in sync here).
    # Deriving it from the same lists that drove the copies guarantees app.js
    # never tries to mount a file we didn't ship.
    manifest = {
        "py": PY_FILES,
        "data": DATA_FILES,
    }
    import json
    (DOCS / "manifest.json").write_text(json.dumps(manifest, indent=2))

    print(f"[build_web] synced {len(PY_FILES)} py + {len(DATA_FILES)} data "
          f"files into {DOCS}")


if __name__ == "__main__":
    main()
