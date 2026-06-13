"""Enable `python -m jarvis` as a stable entrypoint.

The package uses a flat layout (top-level `core`, `tools`, `aura`), so we make
this directory importable before delegating to main.main(). `python main.py`
from inside this directory keeps working unchanged.
"""
import sys
from pathlib import Path

_PKG_DIR = Path(__file__).resolve().parent
if str(_PKG_DIR) not in sys.path:
    sys.path.insert(0, str(_PKG_DIR))

from main import main  # noqa: E402

if __name__ == "__main__":
    main()
