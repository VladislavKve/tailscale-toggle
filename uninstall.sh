#!/usr/bin/env bash
set -euo pipefail

DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
BIN_HOME="${XDG_BIN_HOME:-$HOME/.local/bin}"
CONFIG_HOME="${XDG_CONFIG_HOME:-$HOME/.config}"
INSTALL_DIR="${TAILSCALE_TOGGLE_INSTALL_DIR:-$DATA_HOME/tailscale-toggle}"
DESKTOP_ID="io.github.vladislavkve.TailscaleToggle.desktop"
launcher="$BIN_HOME/tailscale-toggle"

case "$INSTALL_DIR" in
  ""|/|"$HOME"|"$DATA_HOME")
    printf 'Refusing to remove unsafe install path: %s\n' "$INSTALL_DIR" >&2
    exit 1
    ;;
esac

if [[ -L "$launcher" ]]; then
  launcher_target="$(readlink -f -- "$launcher" || true)"
  if [[ "$launcher_target" == "$INSTALL_DIR/run.sh" ]]; then
    rm -f -- "$launcher"
  fi
fi

rm -f -- "$DATA_HOME/applications/$DESKTOP_ID"
rm -f -- "$DATA_HOME/icons/hicolor/256x256/apps/tailscale-toggle.png"
rm -rf -- "$INSTALL_DIR"

if [[ "${1:-}" == "--purge" ]]; then
  rm -rf -- "$CONFIG_HOME/tailscale-toggle"
fi

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$DATA_HOME/applications" >/dev/null 2>&1 || true
fi

printf 'Tailscale Toggle was removed.\n'
if [[ "${1:-}" != "--purge" ]]; then
  printf 'Preferences were kept in %s/tailscale-toggle (use --purge to remove them).\n' \
    "$CONFIG_HOME"
fi
