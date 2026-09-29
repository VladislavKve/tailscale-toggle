#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DATA_HOME="${XDG_DATA_HOME:-$HOME/.local/share}"
BIN_HOME="${XDG_BIN_HOME:-$HOME/.local/bin}"
INSTALL_DIR="${TAILSCALE_TOGGLE_INSTALL_DIR:-$DATA_HOME/tailscale-toggle}"
APPLICATIONS_DIR="$DATA_HOME/applications"
ICON_DIR="$DATA_HOME/icons/hicolor/256x256/apps"
DESKTOP_ID="io.github.vladislavkve.TailscaleToggle.desktop"
DESKTOP_TEMPLATE="$SOURCE_DIR/packaging/$DESKTOP_ID.in"
PYTHON_BIN="${TAILSCALE_TOGGLE_PYTHON:-/usr/bin/python3}"
TAILSCALE_BIN="${TAILSCALE_CLI:-/usr/bin/tailscale}"
launcher="$BIN_HOME/tailscale-toggle"

fail() {
  printf 'Error: %s\n' "$*" >&2
  exit 1
}

[[ -x "$PYTHON_BIN" ]] || fail "Python 3 not found at $PYTHON_BIN"
[[ -x "$TAILSCALE_BIN" ]] || fail \
  "Tailscale is not installed. Follow https://tailscale.com/docs/install/linux"
[[ -f "$DESKTOP_TEMPLATE" ]] || fail "Desktop template is missing"
if [[ -e "$launcher" && ! -L "$launcher" ]]; then
  fail "$launcher already exists and is not a symbolic link"
fi

if ! "$PYTHON_BIN" - <<'PY'
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk  # noqa: F401
assert hasattr(Adw, "AlertDialog"), "Libadwaita 1.5 or newer is required"
PY
then
  cat >&2 <<'EOF'
GTK 4 / Libadwaita Python bindings are missing.
On Ubuntu 24.04+ install them with:
  sudo apt install python3 python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 policykit-1
EOF
  exit 1
fi

if ! "$PYTHON_BIN" - <<'PY'
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
from gi.repository import AyatanaAppIndicator3, Gtk  # noqa: F401
PY
then
  cat >&2 <<'EOF'
Warning: the optional tray dependencies are missing. The main window will work,
but the AppIndicator will not. On Ubuntu install:
  sudo apt install gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 gnome-shell-extension-appindicator
EOF
fi

mkdir -p "$INSTALL_DIR/assets" "$APPLICATIONS_DIR" "$ICON_DIR" "$BIN_HOME"

install -m 0755 "$SOURCE_DIR/run.sh" "$INSTALL_DIR/run.sh"
install -m 0755 "$SOURCE_DIR/tailscale_toggle.py" "$INSTALL_DIR/tailscale_toggle.py"
install -m 0755 "$SOURCE_DIR/tailscale_core.py" "$INSTALL_DIR/tailscale_core.py"
install -m 0755 "$SOURCE_DIR/tray_agent.py" "$INSTALL_DIR/tray_agent.py"
install -m 0755 "$SOURCE_DIR/uninstall.sh" "$INSTALL_DIR/uninstall.sh"
install -m 0644 "$SOURCE_DIR/assets/style.css" "$INSTALL_DIR/assets/style.css"
install -m 0644 "$SOURCE_DIR/assets/app-icon-48.png" "$INSTALL_DIR/assets/app-icon-48.png"
for state in on off exit; do
  install -m 0644 \
    "$SOURCE_DIR/assets/tray-$state-22.png" \
    "$INSTALL_DIR/assets/tray-$state-22.png"
done

install -m 0644 "$SOURCE_DIR/assets/app-icon.png" "$ICON_DIR/tailscale-toggle.png"

escaped_install_dir=${INSTALL_DIR//\\/\\\\}
escaped_install_dir=${escaped_install_dir//&/\\&}
escaped_install_dir=${escaped_install_dir//|/\\|}
sed "s|@APP_DIR@|$escaped_install_dir|g" "$DESKTOP_TEMPLATE" \
  > "$APPLICATIONS_DIR/$DESKTOP_ID"
chmod 0644 "$APPLICATIONS_DIR/$DESKTOP_ID"

ln -sfn "$INSTALL_DIR/run.sh" "$launcher"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$APPLICATIONS_DIR" >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f -t "$DATA_HOME/icons/hicolor" >/dev/null 2>&1 || true
fi

printf '\nTailscale Toggle installed successfully.\n'
printf 'Application files: %s\n' "$INSTALL_DIR"
printf 'Launch from GNOME or run: %s\n' "$launcher"
if [[ ":$PATH:" != *":$BIN_HOME:"* ]]; then
  printf 'Note: add %s to PATH to use the command from a terminal.\n' "$BIN_HOME"
fi
