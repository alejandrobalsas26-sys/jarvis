#!/usr/bin/env bash
# JARVIS installer (Linux / macOS).
# Creates a .venv, verifies Python >= 3.11, installs a dependency profile,
# and seeds .env from .env.example. Run from the jarvis/ directory.
#
#   ./scripts/install.sh            # base profile
#   ./scripts/install.sh all        # everything
set -euo pipefail

PROFILE="${1:-base}"
JARVIS_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$JARVIS_DIR"

case "$PROFILE" in
  base|voice|docs|soc|lab|dev|all) ;;
  *) echo "Unknown profile: $PROFILE (use base|voice|docs|soc|lab|dev|all)"; exit 1 ;;
esac

echo "==> JARVIS installer (profile: $PROFILE)"

# 1. Python version
PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "ERROR: $PY not found. Install Python 3.11+." >&2; exit 1
fi
VER="$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
MAJ="${VER%%.*}"; MIN="${VER##*.}"
if [ "$MAJ" -lt 3 ] || { [ "$MAJ" -eq 3 ] && [ "$MIN" -lt 11 ]; }; then
  echo "ERROR: Python $VER found, but 3.11+ is required." >&2; exit 1
fi
echo "    Python $VER OK"

# 2. Virtualenv
if [ ! -d ".venv" ]; then
  echo "    Creating .venv ..."
  "$PY" -m venv .venv
fi
VENV_PY="$JARVIS_DIR/.venv/bin/python"

# 3. Install profile
REQ="requirements/$PROFILE.txt"
[ -f "$REQ" ] || { echo "ERROR: profile file not found: $REQ" >&2; exit 1; }
echo "    Installing $REQ ..."
"$VENV_PY" -m pip install --upgrade pip >/dev/null
"$VENV_PY" -m pip install -r "$REQ"

# 4. Seed .env
if [ ! -f ".env" ] && [ -f ".env.example" ]; then
  cp ".env.example" ".env"
  echo "    Created .env from .env.example"
fi

cat <<EOF

Done. Next steps:
  1. source .venv/bin/activate
  2. python scripts/doctor.py
  3. ollama serve   (then: python scripts/model_doctor.py)
  4. python main.py
EOF
