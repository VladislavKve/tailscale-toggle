#!/usr/bin/env python3
"""GTK 3 AppIndicator helper for the GTK 4 main application.

GNOME exposes AppIndicator through its enabled extension.  This helper stays in
a separate process so the main process can use GTK 4/Libadwaita safely.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import socket
import sys
import threading
import time
from typing import Any


os.environ.setdefault("DISPLAY", ":0")
APP_DIR = Path(__file__).resolve().parent
ASSETS = APP_DIR / "assets"


def icon_for_state(state: str) -> str:
    name = {
        "on": "tray-on-22.png",
        "exit": "tray-exit-22.png",
        "busy": "tray-exit-22.png",
        "off": "tray-off-22.png",
        "error": "tray-off-22.png",
    }.get(state, "tray-off-22.png")
    return str(ASSETS / name)


class TrayAgent:
    def __init__(self, sock_path: str, token: str):
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("AyatanaAppIndicator3", "0.1")
        from gi.repository import AyatanaAppIndicator3, GLib, Gtk

        self.GLib = GLib
        self.Gtk = Gtk
        self.AppIndicator = AyatanaAppIndicator3
        self.sock_path = sock_path
        self.token = token
        self.conn: socket.socket | None = None
        self.state = "error"
        self.connected = False
        self.busy = False
        self.can_toggle = False
        self.nodes: list[dict[str, Any]] = []
        self.current = ""
        self.tip = "Tailscale: запуск"
        self._signature = ""
        self._closing = False
        self._main_running = False

        self.indicator = AyatanaAppIndicator3.Indicator.new(
            "tailscale-toggle",
            icon_for_state(self.state),
            AyatanaAppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_status(AyatanaAppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_title("Tailscale Toggle")
        self.menu = Gtk.Menu()
        self.indicator.set_menu(self.menu)
        self._rebuild_menu()

    def connect(self) -> None:
        last_error: Exception | None = None
        for _ in range(60):
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(self.sock_path)
                self.conn = sock
                break
            except OSError as exc:
                last_error = exc
                sock.close()
                time.sleep(0.1)
        else:
            raise RuntimeError(f"cannot connect to GUI socket: {last_error}")
        threading.Thread(target=self._reader, daemon=True, name="tray-socket-reader").start()
        self.send({"event": "ready"})

    def send(self, payload: dict[str, Any]) -> None:
        if not self.conn or self._closing:
            return
        message = dict(payload)
        message["token"] = self.token
        try:
            self.conn.sendall((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
        except OSError:
            self._request_quit()

    def _reader(self) -> None:
        buffer = b""
        conn = self.conn
        try:
            while conn and not self._closing:
                chunk = conn.recv(4096)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if not secrets.compare_digest(str(obj.get("token") or ""), self.token):
                        continue
                    self.GLib.idle_add(self._handle_command, obj)
        except OSError:
            pass
        self.GLib.idle_add(self._request_quit)

    def _handle_command(self, obj: dict[str, Any]) -> bool:
        command = obj.get("cmd")
        if command == "quit":
            self._request_quit()
            return False
        if command != "set":
            return False
        state = str(obj.get("state") or "error")
        connected = bool(obj.get("connected"))
        busy = bool(obj.get("busy"))
        can_toggle = bool(obj.get("can_toggle"))
        nodes = obj.get("nodes") or []
        current = str(obj.get("current") or "")
        tip = str(obj.get("tip") or "Tailscale")
        signature = json.dumps(
            {
                "state": state,
                "connected": connected,
                "busy": busy,
                "can_toggle": can_toggle,
                "nodes": nodes,
                "current": current,
                "tip": tip,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        if signature == self._signature:
            return False
        self._signature = signature
        self.state = state
        self.connected = connected
        self.busy = busy
        self.can_toggle = can_toggle
        self.nodes = [node for node in nodes if isinstance(node, dict)]
        self.current = current
        self.tip = tip
        self.indicator.set_icon_full(icon_for_state(state), tip)
        self._rebuild_menu()
        return False

    def _menu_item(self, label: str, payload: dict[str, Any], *, sensitive: bool = True):
        item = self.Gtk.MenuItem(label=label)
        item.set_sensitive(sensitive)
        item.connect("activate", lambda *_: self.send(payload))
        return item

    def _rebuild_menu(self) -> None:
        for child in list(self.menu.get_children()):
            self.menu.remove(child)

        status_item = self.Gtk.MenuItem(label=self.tip)
        status_item.set_sensitive(False)
        self.menu.append(status_item)
        self.menu.append(self.Gtk.SeparatorMenuItem())

        self.menu.append(self._menu_item("Открыть Tailscale Toggle", {"event": "open"}))
        toggle_label = "Отключить Tailscale" if self.connected else "Включить Tailscale"
        toggle_enabled = self.can_toggle and not self.busy and self.state != "error"
        self.menu.append(
            self._menu_item(toggle_label, {"event": "toggle"}, sensitive=toggle_enabled)
        )

        exit_root = self.Gtk.MenuItem(label="Exit node")
        exit_menu = self.Gtk.Menu()
        exit_root.set_submenu(exit_menu)
        exit_root.set_sensitive(self.connected and not self.busy and self.state != "error")

        direct = self.Gtk.RadioMenuItem.new_with_label(None, "Прямое подключение")
        direct.set_active(not self.current)
        direct.connect("activate", self._exit_item_activated, "")
        exit_menu.append(direct)

        for node in self.nodes:
            node_id = str(node.get("id") or "")
            label = str(node.get("label") or node_id)
            online = bool(node.get("online", True))
            item = self.Gtk.RadioMenuItem.new_with_label_from_widget(direct, label)
            item.set_active(self.current == node_id)
            item.set_sensitive(online)
            item.connect("activate", self._exit_item_activated, node_id)
            exit_menu.append(item)
        self.menu.append(exit_root)

        self.menu.append(self.Gtk.SeparatorMenuItem())
        self.menu.append(
            self._menu_item("Обновить", {"event": "refresh"}, sensitive=not self.busy)
        )
        self.menu.append(self.Gtk.SeparatorMenuItem())
        self.menu.append(
            self._menu_item("Выйти", {"event": "quit"}, sensitive=not self.busy)
        )
        self.menu.show_all()

    def _exit_item_activated(self, item, node_id: str) -> None:
        # RadioMenuItem emits ``activate`` for both the old and new item.
        # Only the item that ended up active represents the user's choice.
        if item.get_active():
            self.send({"event": "exit", "id": node_id})

    def _request_quit(self) -> bool:
        if self._closing:
            return False
        self._closing = True
        if self.conn:
            try:
                self.conn.close()
            except OSError:
                pass
            self.conn = None
        self.indicator.set_status(self.AppIndicator.IndicatorStatus.PASSIVE)
        if self._main_running:
            self.Gtk.main_quit()
        return False

    def run(self) -> int:
        self.connect()
        if not self._closing:
            self._main_running = True
            self.Gtk.main()
            self._main_running = False
        return 0


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    token = os.environ.get("TAILSCALE_TOGGLE_TRAY_TOKEN", "")
    if len(args) != 1 or not token:
        print("usage: tray_agent.py SOCKET (token is passed in the environment)", file=sys.stderr)
        return 2
    sock_path = args[0]
    try:
        return TrayAgent(sock_path, token).run()
    except Exception as exc:
        print(f"tray helper failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
