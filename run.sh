#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="${BASH_SOURCE[0]}"
while [[ -L "$SCRIPT_PATH" ]]; do
  SCRIPT_DIR="$(cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd)"
  LINK_TARGET="$(readlink -- "$SCRIPT_PATH")"
  if [[ "$LINK_TARGET" == /* ]]; then
    SCRIPT_PATH="$LINK_TARGET"
  else
    SCRIPT_PATH="$SCRIPT_DIR/$LINK_TARGET"
  fi
done
APP_DIR="$(cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd)"
PYTHON_BIN="${TAILSCALE_TOGGLE_PYTHON:-/usr/bin/python3}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3 || true)"
fi
if [[ -z "$PYTHON_BIN" ]]; then
  echo "Tailscale Toggle requires Python 3." >&2
  exit 1
fi

cd "$APP_DIR"
export DISPLAY="${DISPLAY:-:0}"
exec "$PYTHON_BIN" "$APP_DIR/tailscale_toggle.py" "$@"
