#!/usr/bin/env python3
"""Modern GNOME control surface for Tailscale.

The main window uses GTK 4 + Libadwaita.  The legacy GTK 3 AppIndicator is
kept in a separate helper process because both GTK major versions cannot be
loaded safely in one Python process.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
from typing import Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from tailscale_core import (  # noqa: E402
    CommandResult,
    ExitNode,
    LoginProfile,
    StatusSnapshot,
    fetch_status,
    fetch_current_login_profile,
    is_trusted_login_url,
    login_with_auth_key,
    load_settings,
    normalize_auth_key,
    run_browser_login,
    run_with_optional_elevation,
    save_settings,
    switch_login_profile,
)


APP_ID = "io.github.vladislavkve.TailscaleToggle"
APP_NAME = "Tailscale Toggle"
APP_VERSION = "2.1.0"
APP_DIR = Path(__file__).resolve().parent
ASSETS = APP_DIR / "assets"
STYLE_PATH = ASSETS / "style.css"
POLL_SECONDS = 5


def _timeout_result(exc: subprocess.TimeoutExpired) -> CommandResult:
    parts: list[str] = []
    for value in (exc.stderr, exc.stdout):
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if value:
            parts.append(str(value).strip())
    message = "\n".join(part for part in parts if part)
    return CommandResult(124, message or "Команда не завершилась вовремя")


def _preview_snapshot(mode: str) -> StatusSnapshot:
    nodes = (
        ExitNode(
            "demo-exit-node",
            "demo-exit-node",
            "100.64.0.20",
            True,
            mode == "connected",
        ),
        ExitNode("office-node", "office-node", "100.64.0.30", True, False),
        ExitNode("travel-node", "travel-node", "100.64.0.40", False, False),
    )
    if mode == "disconnected":
        state, connected, exit_node, health = "Stopped", False, "", ()
    elif mode == "needs-login":
        state, connected, exit_node, health = "NeedsLogin", False, "", ()
    elif mode == "degraded":
        state, connected, exit_node, health = (
            "Running",
            True,
            "demo-exit-node",
            ("Не удалось связаться с одним из DERP-регионов",),
        )
    else:
        state, connected, exit_node, health = (
            "Running",
            True,
            "demo-exit-node",
            (),
        )
    return StatusSnapshot(
        backend_state=state,
        connected=connected,
        device_name="demo-laptop",
        dns_name="demo-laptop.example.ts.net",
        ip="100.64.0.10",
        tailnet="example.ts.net",
        user="user@example.com",
        exit_node=exit_node,
        health=health,
        nodes=nodes,
        version="1.102.3",
        fetched_at=time.time(),
    )


def _snapshot_has_profile(snapshot: StatusSnapshot | None) -> bool:
    return bool(
        snapshot
        and (snapshot.connected or snapshot.user or snapshot.tailnet)
    )


def _auth_command_for_snapshot(snapshot: StatusSnapshot | None) -> str:
    return "up" if snapshot and snapshot.backend_state == "NeedsLogin" else "login"


def _snapshot_allows_toggle(snapshot: StatusSnapshot | None) -> bool:
    return bool(
        snapshot
        and snapshot.backend_state not in {"NeedsLogin", "NeedsMachineAuth"}
    )


class TailscaleApplication(Adw.Application):
    def __init__(
        self,
        *,
        preview: str = "",
        no_tray: bool = False,
        quit_after: float = 0,
        theme_override: str = "",
    ):
        application_id = APP_ID + (".Preview" if preview else "")
        super().__init__(application_id=application_id)
        self.preview = preview
        self.no_tray = no_tray or bool(preview)
        self.quit_after = max(0.0, quit_after)
        self.settings_values = load_settings()
        if theme_override:
            self.settings_values["color_scheme"] = theme_override
        self.window: TailscaleWindow | None = None
        self.menu_model = Gio.Menu()
        self._build_actions()
        self._build_menu()
        self._apply_color_scheme(self.settings_values["color_scheme"])
        self.set_accels_for_action("app.refresh", ["<Primary>r", "F5"])
        self.set_accels_for_action("app.quit", ["<Primary>q"])

    def _build_actions(self) -> None:
        refresh = Gio.SimpleAction.new("refresh", None)
        refresh.connect("activate", lambda *_: self.window and self.window.refresh_status(manual=True))
        self.add_action(refresh)

        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect(
            "activate",
            lambda *_: self.window.request_quit() if self.window else self.quit(),
        )
        self.add_action(quit_action)

        about = Gio.SimpleAction.new("about", None)
        about.connect("activate", self._show_about)
        self.add_action(about)

        scheme = Gio.SimpleAction.new_stateful(
            "color-scheme",
            GLib.VariantType.new("s"),
            GLib.Variant.new_string(self.settings_values["color_scheme"]),
        )
        scheme.connect("change-state", self._change_color_scheme)
        self.add_action(scheme)

        close_to_tray = Gio.SimpleAction.new_stateful(
            "close-to-tray",
            None,
            GLib.Variant.new_boolean(bool(self.settings_values["close_to_tray"])),
        )
        close_to_tray.connect("activate", self._toggle_close_to_tray)
        self.add_action(close_to_tray)

    def _build_menu(self) -> None:
        appearance = Gio.Menu()
        for label, value in (
            ("Как в системе", "system"),
            ("Светлая", "light"),
            ("Тёмная", "dark"),
        ):
            item = Gio.MenuItem.new(label, None)
            item.set_action_and_target_value("app.color-scheme", GLib.Variant.new_string(value))
            appearance.append_item(item)
        self.menu_model.append_submenu("Оформление", appearance)

        behavior = Gio.Menu()
        behavior.append("Закрывать в трей", "app.close-to-tray")
        self.menu_model.append_section(None, behavior)

        actions = Gio.Menu()
        actions.append("Обновить", "app.refresh")
        actions.append("О приложении", "app.about")
        actions.append("Выйти", "app.quit")
        self.menu_model.append_section(None, actions)

    def _change_color_scheme(self, action: Gio.SimpleAction, value: GLib.Variant) -> None:
        scheme = value.get_string()
        if scheme not in {"system", "light", "dark"}:
            return
        action.set_state(value)
        self.settings_values["color_scheme"] = scheme
        save_settings(self.settings_values)
        self._apply_color_scheme(scheme)

    @staticmethod
    def _apply_color_scheme(scheme: str) -> None:
        mapping = {
            "system": Adw.ColorScheme.DEFAULT,
            "light": Adw.ColorScheme.FORCE_LIGHT,
            "dark": Adw.ColorScheme.FORCE_DARK,
        }
        Adw.StyleManager.get_default().set_color_scheme(mapping.get(scheme, Adw.ColorScheme.DEFAULT))

    def _toggle_close_to_tray(self, action: Gio.SimpleAction, _parameter: Any) -> None:
        enabled = not action.get_state().get_boolean()
        action.set_state(GLib.Variant.new_boolean(enabled))
        self.settings_values["close_to_tray"] = enabled
        save_settings(self.settings_values)
        if self.window:
            message = "Окно будет скрываться в трей" if enabled else "Кнопка закрытия завершит приложение"
            self.window.toast(message)

    def close_to_tray_enabled(self) -> bool:
        action = self.lookup_action("close-to-tray")
        return bool(action and action.get_state().get_boolean())

    def _show_about(self, *_args: Any) -> None:
        if not self.window:
            return
        dialog = Adw.AboutDialog(
            application_name=APP_NAME,
            application_icon="tailscale-toggle",
            developer_name="VladislavKve",
            version=APP_VERSION,
            comments="Современный локальный интерфейс управления Tailscale и exit node.",
        )
        dialog.set_developers(["VladislavKve"])
        dialog.set_website("https://github.com/VladislavKve/tailscale-toggle")
        dialog.set_issue_url("https://github.com/VladislavKve/tailscale-toggle/issues")
        dialog.present(self.window)

    def do_activate(self) -> None:
        if not self.window:
            self.window = TailscaleWindow(self)
        self.window.show_window()
        if self.quit_after:
            GLib.timeout_add(
                max(1, int(self.quit_after * 1000)),
                lambda: (self.window.quit_app(), GLib.SOURCE_REMOVE)[1],
            )


class TailscaleWindow(Adw.ApplicationWindow):
    def __init__(self, app: TailscaleApplication):
        super().__init__(application=app, title=APP_NAME)
        self.app = app
        self.set_default_size(640, 760)
        self.set_size_request(420, 560)
        self.set_icon_name("tailscale-toggle")

        self.snapshot: StatusSnapshot | None = None
        self._selected_exit_id = ""
        self._selection_dirty = False
        self._fetching = False
        self._busy = False
        self._confirming = False
        self._closing = False
        self._refresh_generation = 0
        self._refresh_again = False
        self._refresh_pending_manual = False
        self._refresh_pending_catalog = False
        self._switch_sync = False
        self._node_rows: list[Adw.ActionRow] = []
        self._node_checks: dict[str, Gtk.CheckButton] = {}
        self._nodes_signature: tuple[Any, ...] = ()
        self._last_error = ""
        self._pending_login_url = ""
        self._previous_profile: LoginProfile | None = None

        self._tray_proc: subprocess.Popen[bytes] | None = None
        self._tray_server: socket.socket | None = None
        self._tray_conn: socket.socket | None = None
        self._tray_path = ""
        self._tray_token = ""
        self._tray_ready = False
        self._tray_signature = ""
        self._tray_retry_source = 0
        self._tray_failure_count = 0

        self._load_css()
        self._build_ui()
        self.connect("close-request", self._on_close_request)
        self.app.connect("shutdown", lambda *_: self._shutdown_tray())

        GLib.timeout_add_seconds(POLL_SECONDS, self._poll)
        if not self.app.no_tray:
            GLib.timeout_add(350, self._start_tray)
        GLib.idle_add(lambda: (self.refresh_status(manual=False, refresh_catalog=True), GLib.SOURCE_REMOVE)[1])

    def _load_css(self) -> None:
        if not STYLE_PATH.is_file():
            return
        provider = Gtk.CssProvider()
        provider.load_from_path(str(STYLE_PATH))
        display = Gdk.Display.get_default()
        if display:
            Gtk.StyleContext.add_provider_for_display(
                display,
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

    def _build_ui(self) -> None:
        self.toast_overlay = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()
        self.toast_overlay.set_child(toolbar)
        self.set_content(self.toast_overlay)

        header = Adw.HeaderBar()
        header.set_title_widget(self._build_title_widget())
        toolbar.add_top_bar(header)

        self.refresh_button = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        self.refresh_button.set_tooltip_text("Обновить статус (Ctrl+R)")
        self.refresh_button.add_css_class("flat")
        self.refresh_button.connect("clicked", lambda *_: self.refresh_status(manual=True))
        header.pack_start(self.refresh_button)

        self.activity_spinner = Gtk.Spinner(spinning=False)
        self.activity_spinner.set_tooltip_text("Выполняется операция")
        header.pack_start(self.activity_spinner)

        menu_button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=self.app.menu_model)
        menu_button.set_tooltip_text("Меню")
        header.pack_end(menu_button)

        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.error_banner = Adw.Banner(title="Статус временно недоступен", button_label="Повторить")
        self.error_banner.connect("button-clicked", lambda *_: self.refresh_status(manual=True))
        page.append(self.error_banner)

        self.health_banner = Adw.Banner(title="Tailscale сообщает о проблеме", button_label="Подробнее")
        self.health_banner.add_css_class("warning")
        self.health_banner.connect("button-clicked", self._show_health_details)
        page.append(self.health_banner)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_propagate_natural_height(True)
        page.append(scrolled)
        toolbar.set_content(page)

        clamp = Adw.Clamp(maximum_size=720, tightening_threshold=520)
        scrolled.set_child(clamp)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        content.add_css_class("page-content")
        clamp.set_child(content)

        content.append(self._build_hero())
        content.append(self._build_connection_group())

        self.exit_search = Gtk.SearchEntry(placeholder_text="Найти exit node")
        self.exit_search.set_tooltip_text("Фильтр по имени или IP")
        self.exit_search.connect("search-changed", self._filter_nodes)
        self.exit_search.set_visible(False)
        content.append(self.exit_search)

        self.exit_group = Adw.PreferencesGroup(
            title="Маршрут интернета",
            description="Выберите прямое подключение или exit node. Изменение применяется отдельно.",
        )
        content.append(self.exit_group)

        self.footer_label = Gtk.Label(label="Получение статуса…", xalign=0)
        self.footer_label.add_css_class("dim-label")
        self.footer_label.add_css_class("caption")
        self.footer_label.set_wrap(True)
        content.append(self.footer_label)

        bottom = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        bottom.add_css_class("bottom-actionbar")
        bottom.append(self._build_apply_bar())
        toolbar.add_bottom_bar(bottom)

    def _build_title_widget(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        logo_path = ASSETS / "app-icon-48.png"
        image = Gtk.Image.new_from_file(str(logo_path))
        image.set_pixel_size(26)
        box.append(image)
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        title = Gtk.Label(label="Tailscale", xalign=0)
        title.add_css_class("heading")
        subtitle = Gtk.Label(label="Центр управления", xalign=0)
        subtitle.add_css_class("caption")
        subtitle.add_css_class("dim-label")
        labels.append(title)
        labels.append(subtitle)
        box.append(labels)
        return box

    def _build_hero(self) -> Gtk.Widget:
        self.hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.hero.add_css_class("connection-hero")
        self.hero.add_css_class("state-unknown")

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=14)
        self.status_icon = Gtk.Image.new_from_icon_name("network-vpn-symbolic")
        self.status_icon.set_pixel_size(34)
        self.status_icon.set_valign(Gtk.Align.CENTER)
        self.status_icon.add_css_class("status-icon")
        top.append(self.status_icon)

        copy = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        copy.set_hexpand(True)
        self.status_title = Gtk.Label(label="Проверяем подключение…", xalign=0)
        self.status_title.add_css_class("hero-title")
        self.status_title.set_wrap(True)
        self.status_subtitle = Gtk.Label(label="Ожидание ответа Tailscale", xalign=0)
        self.status_subtitle.add_css_class("hero-subtitle")
        self.status_subtitle.set_wrap(True)
        copy.append(self.status_title)
        copy.append(self.status_subtitle)
        top.append(copy)

        self.status_badge = Gtk.Label(label="…")
        self.status_badge.set_valign(Gtk.Align.CENTER)
        self.status_badge.add_css_class("status-pill")
        self.status_badge.add_css_class("neutral")
        top.append(self.status_badge)
        self.hero.append(top)

        chips = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        chips.set_halign(Gtk.Align.START)
        self.ip_chip = Gtk.Button()
        self.ip_chip.add_css_class("chip")
        self.ip_chip.set_tooltip_text("Скопировать Tailscale IP")
        self.ip_chip.connect("clicked", self._copy_ip)
        ip_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        ip_box.append(Gtk.Image.new_from_icon_name("edit-copy-symbolic"))
        self.ip_label = Gtk.Label(label="Нет IP")
        ip_box.append(self.ip_label)
        self.ip_chip.set_child(ip_box)
        chips.append(self.ip_chip)

        self.route_chip = Gtk.Label(label="Маршрут неизвестен")
        self.route_chip.add_css_class("chip-label")
        chips.append(self.route_chip)
        self.hero.append(chips)
        return self.hero

    def _build_connection_group(self) -> Gtk.Widget:
        group = Adw.PreferencesGroup(title="Подключение")
        self.connection_switch = Adw.SwitchRow(
            title="Сеть Tailscale",
            subtitle="Подключайтесь к устройствам tailnet и используйте exit node",
        )
        self.connection_switch.set_sensitive(False)
        self.connection_switch.connect("notify::active", self._on_connection_switch)
        group.add(self.connection_switch)

        self.details_row = Adw.ExpanderRow(title="Сведения о сети", subtitle="Статус ещё не получен")
        self.detail_device = self._detail_row("Устройство")
        self.detail_account = self._detail_row("Аккаунт")
        self.detail_tailnet = self._detail_row("Tailnet")
        self.detail_backend = self._detail_row("Backend")
        self.detail_version = self._detail_row("Версия")
        for row, _value in (
            self.detail_device,
            self.detail_account,
            self.detail_tailnet,
            self.detail_backend,
            self.detail_version,
        ):
            self.details_row.add_row(row)
        group.add(self.details_row)

        self.auth_row = Adw.ExpanderRow(
            title="Авторизация",
            subtitle="Через браузер или auth key",
        )
        self.auth_row.set_sensitive(False)
        self.browser_auth_row = Adw.ActionRow(
            title="Войти через браузер",
            subtitle="Логин и пароль вводятся только на странице провайдера",
        )
        browser_icon = Gtk.Image.new_from_icon_name("web-browser-symbolic")
        self.browser_auth_row.add_prefix(browser_icon)
        self.browser_login_button = Gtk.Button(label="Войти")
        self.browser_login_button.set_valign(Gtk.Align.CENTER)
        self.browser_login_button.connect("clicked", self._request_browser_login)
        self.browser_auth_row.add_suffix(self.browser_login_button)
        self.browser_auth_row.set_activatable_widget(self.browser_login_button)
        self.auth_row.add_row(self.browser_auth_row)

        self.key_auth_row = Adw.ActionRow(
            title="Auth key",
            subtitle="Используется для этого входа · ключ не сохраняется",
        )
        key_icon = Gtk.Image.new_from_icon_name("dialog-password-symbolic")
        self.key_auth_row.add_prefix(key_icon)
        self.auth_key_button = Gtk.Button(label="Ввести")
        self.auth_key_button.set_valign(Gtk.Align.CENTER)
        self.auth_key_button.connect("clicked", self._request_auth_key)
        self.key_auth_row.add_suffix(self.auth_key_button)
        self.key_auth_row.set_activatable_widget(self.auth_key_button)
        self.auth_row.add_row(self.key_auth_row)

        self.cancel_pending_row = Adw.ActionRow(
            title="Другой способ входа",
            subtitle="Скрыть текущую ссылку и выбрать браузер или auth key заново",
        )
        cancel_icon = Gtk.Image.new_from_icon_name("window-close-symbolic")
        self.cancel_pending_row.add_prefix(cancel_icon)
        self.cancel_pending_button = Gtk.Button(label="Выбрать")
        self.cancel_pending_button.set_valign(Gtk.Align.CENTER)
        self.cancel_pending_button.connect("clicked", self._cancel_pending_browser)
        self.cancel_pending_row.add_suffix(self.cancel_pending_button)
        self.cancel_pending_row.set_activatable_widget(self.cancel_pending_button)
        self.cancel_pending_row.set_visible(False)
        self.auth_row.add_row(self.cancel_pending_row)

        self.restore_auth_row = Adw.ActionRow(
            title="Предыдущий аккаунт",
            subtitle="Можно безопасно вернуться после незавершённого входа",
        )
        restore_icon = Gtk.Image.new_from_icon_name("edit-undo-symbolic")
        self.restore_auth_row.add_prefix(restore_icon)
        self.restore_auth_button = Gtk.Button(label="Вернуться")
        self.restore_auth_button.set_valign(Gtk.Align.CENTER)
        self.restore_auth_button.connect("clicked", self._restore_previous_profile)
        self.restore_auth_row.add_suffix(self.restore_auth_button)
        self.restore_auth_row.set_activatable_widget(self.restore_auth_button)
        self.restore_auth_row.set_visible(False)
        self.auth_row.add_row(self.restore_auth_row)
        group.add(self.auth_row)
        return group

    @staticmethod
    def _detail_row(title: str) -> tuple[Adw.ActionRow, Gtk.Label]:
        row = Adw.ActionRow(title=title)
        value = Gtk.Label(label="—", xalign=1)
        value.add_css_class("dim-label")
        value.set_selectable(True)
        value.set_ellipsize(3)
        value.set_max_width_chars(34)
        row.add_suffix(value)
        return row, value

    def _build_apply_bar(self) -> Gtk.Widget:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        bar.add_css_class("apply-bar")
        self.selection_label = Gtk.Label(label="Текущий маршрут будет отмечен здесь", xalign=0)
        self.selection_label.set_hexpand(True)
        self.selection_label.set_ellipsize(3)
        self.selection_label.add_css_class("dim-label")
        bar.append(self.selection_label)

        self.cancel_button = Gtk.Button(label="Отменить")
        self.cancel_button.connect("clicked", lambda *_: self._reset_exit_selection())
        bar.append(self.cancel_button)

        self.apply_button = Gtk.Button(label="Применить")
        self.apply_button.add_css_class("suggested-action")
        self.apply_button.connect("clicked", self._apply_exit_node)
        bar.append(self.apply_button)
        self._update_apply_bar()
        return bar

    def toast(
        self,
        message: str,
        *,
        button: str = "",
        callback: Any = None,
        timeout: int = 4,
    ) -> None:
        toast = Adw.Toast.new(message)
        toast.set_timeout(timeout)
        if button and callback:
            toast.set_button_label(button)
            toast.connect("button-clicked", lambda *_: callback())
        self.toast_overlay.add_toast(toast)

    def refresh_status(self, *, manual: bool = False, refresh_catalog: bool | None = None) -> None:
        if self._closing:
            return
        if self._operation_active():
            return
        if self.app.preview:
            if self.app.preview == "error":
                self._finish_refresh(
                    self._refresh_generation,
                    None,
                    "Не удалось связаться с локальным tailscaled",
                    manual,
                )
            else:
                self._finish_refresh(
                    self._refresh_generation,
                    _preview_snapshot(self.app.preview),
                    "",
                    manual,
                )
            return
        if refresh_catalog is None:
            refresh_catalog = manual
        if self._fetching:
            self._refresh_again = True
            self._refresh_pending_manual = self._refresh_pending_manual or manual
            self._refresh_pending_catalog = (
                self._refresh_pending_catalog or bool(refresh_catalog)
            )
            return
        self._fetching = True
        self._refresh_generation += 1
        generation = self._refresh_generation
        self._update_activity()
        def worker() -> None:
            snapshot: StatusSnapshot | None = None
            error = ""
            try:
                snapshot = fetch_status(refresh_catalog=bool(refresh_catalog))
            except Exception as exc:  # boundary: errors are shown as stale/unknown, never as OFF
                error = str(exc)
            GLib.idle_add(self._finish_refresh, generation, snapshot, error, manual)

        threading.Thread(target=worker, daemon=True, name="tailscale-status").start()

    def _finish_refresh(
        self,
        generation: int,
        snapshot: StatusSnapshot | None,
        error: str,
        manual: bool,
    ) -> bool:
        if self._closing or generation != self._refresh_generation:
            return GLib.SOURCE_REMOVE
        self._fetching = False
        if error:
            self._last_error = error
        elif snapshot:
            self._last_error = ""
            self.snapshot = snapshot
        self._update_activity()
        if error:
            self._set_auth_controls_sensitive()
            self.error_banner.set_title("Не удалось обновить статус Tailscale")
            self.error_banner.set_revealed(True)
            if not self.snapshot:
                self._render_unknown(error)
            else:
                stamp = datetime.fromtimestamp(self.snapshot.fetched_at).strftime("%H:%M:%S")
                self.footer_label.set_text(
                    f"Показаны последние данные от {stamp} · {error}"
                )
                self.connection_switch.set_sensitive(False)
                self._set_node_rows_sensitive(False)
                self._update_apply_bar()
                self._update_tray(force=True)
            if manual:
                self.toast("Не удалось обновить статус")
        elif snapshot:
            self.error_banner.set_revealed(False)
            self._render_snapshot(snapshot)
            if manual:
                self.toast("Статус обновлён")
        if self._refresh_again:
            manual_again = self._refresh_pending_manual
            catalog_again = self._refresh_pending_catalog
            self._refresh_again = False
            self._refresh_pending_manual = False
            self._refresh_pending_catalog = False
            GLib.idle_add(
                lambda: (
                    self.refresh_status(
                        manual=manual_again,
                        refresh_catalog=catalog_again,
                    ),
                    GLib.SOURCE_REMOVE,
                )[1]
            )
        return GLib.SOURCE_REMOVE

    def _render_snapshot(self, snapshot: StatusSnapshot) -> None:
        if snapshot.connected and (self._pending_login_url or self._previous_profile):
            self._clear_pending_login()
        state = snapshot.display_state
        state_data = {
            "connected": (
                "Подключено",
                "Сеть Tailscale работает",
                "ВКЛ",
                "network-vpn-symbolic",
                "state-connected",
                "success",
            ),
            "degraded": (
                "Подключено",
                "Туннель активен, но Tailscale сообщает о проблеме",
                "!",
                "dialog-warning-symbolic",
                "state-warning",
                "warning",
            ),
            "needs-login": (
                "Требуется вход",
                "Выберите способ авторизации ниже",
                "ВХОД",
                "dialog-password-symbolic",
                "state-warning",
                "warning",
            ),
            "stopped": (
                "Отключено",
                "Устройства tailnet сейчас недоступны",
                "ВЫКЛ",
                "network-offline-symbolic",
                "state-stopped",
                "neutral",
            ),
            "unknown": (
                "Неизвестное состояние",
                f"Backend: {snapshot.backend_state}",
                "?",
                "dialog-question-symbolic",
                "state-unknown",
                "neutral",
            ),
        }[state]
        if snapshot.backend_state == "NeedsMachineAuth":
            state_data = (
                "Требуется одобрение",
                "Администратор tailnet должен одобрить это устройство",
                "ЖДЁМ",
                "dialog-password-symbolic",
                "state-warning",
                "warning",
            )
        title, subtitle, badge, icon_name, hero_class, badge_class = state_data
        self.status_title.set_text(title)
        route = f"Интернет через {snapshot.exit_node}" if snapshot.exit_node else "Прямой интернет-маршрут"
        self.status_subtitle.set_text(f"{subtitle} · {route}" if snapshot.connected else subtitle)
        self.status_icon.set_from_icon_name(icon_name)
        self.status_badge.set_text(badge)
        self._set_state_classes(self.hero, hero_class, ("state-connected", "state-warning", "state-stopped", "state-unknown"))
        self._set_state_classes(self.status_badge, badge_class, ("success", "warning", "neutral", "error"))

        self.ip_label.set_text(snapshot.ip or "Нет IP")
        self.ip_chip.set_sensitive(bool(snapshot.ip))
        self.route_chip.set_text(
            f"Через: {snapshot.exit_node}" if snapshot.exit_node else "Прямой маршрут"
        )

        self._switch_sync = True
        self.connection_switch.set_active(snapshot.connected)
        self._switch_sync = False
        if snapshot.backend_state == "NeedsLogin":
            self.connection_switch.set_subtitle("Войдите через раздел «Авторизация»")
        elif snapshot.backend_state == "NeedsMachineAuth":
            self.connection_switch.set_subtitle("Ожидается одобрение устройства")
        elif snapshot.connected:
            self.connection_switch.set_subtitle("Tailnet доступен; отключение не выключает обычную сеть")
        else:
            self.connection_switch.set_subtitle(f"Backend: {snapshot.backend_state}")

        device_value = snapshot.device_name
        if snapshot.dns_name and snapshot.dns_name != snapshot.device_name:
            device_value = f"{snapshot.device_name} · {snapshot.dns_name}"
        self.detail_device[1].set_text(device_value or "—")
        self.detail_account[1].set_text(snapshot.user or "—")
        self.detail_tailnet[1].set_text(snapshot.tailnet or snapshot.user or "—")
        self.detail_backend[1].set_text(snapshot.backend_state)
        self.detail_version[1].set_text(snapshot.version or "—")
        self.details_row.set_subtitle(
            f"{snapshot.device_name or 'Устройство'} · {snapshot.ip or 'без Tailscale IP'}"
        )
        if snapshot.backend_state == "NeedsLogin":
            login_identity = snapshot.user or snapshot.tailnet
            self.auth_row.set_subtitle(
                f"Требуется вход · {login_identity}" if login_identity else "Требуется вход"
            )
        elif snapshot.backend_state == "NeedsMachineAuth":
            approval_identity = snapshot.user or snapshot.tailnet
            self.auth_row.set_subtitle(
                f"Ожидается одобрение · {approval_identity}"
                if approval_identity
                else "Ожидается одобрение устройства"
            )
        elif snapshot.user or snapshot.tailnet:
            identity = " · ".join(
                part for part in (snapshot.user, snapshot.tailnet) if part
            )
            self.auth_row.set_subtitle(identity)
        else:
            self.auth_row.set_subtitle("Через браузер или auth key")
        if snapshot.backend_state == "NeedsLogin":
            browser_label = "Войти"
        elif snapshot.user or snapshot.tailnet:
            browser_label = "Другой"
        else:
            browser_label = "Войти"
        self.browser_login_button.set_label(browser_label)
        if self._pending_login_url:
            self.auth_row.set_subtitle("Ожидаем завершения входа в браузере…")
            self.browser_auth_row.set_subtitle(
                "Страница входа подготовлена · завершите авторизацию в браузере"
            )
            self.browser_login_button.set_label("Открыть")
        elif self._previous_profile:
            self.auth_row.set_subtitle("Вход не завершён · доступен возврат")

        self.health_banner.set_revealed(bool(snapshot.health))
        if snapshot.health:
            summary = "Tailscale сообщает о проблеме"
            if len(snapshot.health) > 1:
                summary += f" ({len(snapshot.health)})"
            self.health_banner.set_title(summary)

        self._rebuild_nodes_if_needed(snapshot)
        stamp = datetime.fromtimestamp(snapshot.fetched_at).strftime("%H:%M:%S")
        tray_note = " · трей готов" if self._tray_ready else ""
        self.footer_label.set_text(f"Обновлено {stamp}{tray_note}")
        self.connection_switch.set_sensitive(self._connection_switch_available())
        self._set_node_rows_sensitive(not self._operation_active())
        self._set_auth_controls_sensitive()
        self._update_apply_bar()
        self._update_tray()

    def _render_unknown(self, error: str) -> None:
        self.status_title.set_text("Статус недоступен")
        self.status_subtitle.set_text(error or "Не удалось прочитать состояние Tailscale")
        self.status_icon.set_from_icon_name("dialog-error-symbolic")
        self.status_badge.set_text("ОШИБКА")
        self._set_state_classes(self.hero, "state-unknown", ("state-connected", "state-warning", "state-stopped", "state-unknown"))
        self._set_state_classes(self.status_badge, "error", ("success", "warning", "neutral", "error"))
        self.ip_label.set_text("Нет данных")
        self.ip_chip.set_sensitive(False)
        self.route_chip.set_text("Маршрут неизвестен")
        self.connection_switch.set_sensitive(False)
        self.auth_row.set_subtitle("Браузер или auth key")
        self.browser_login_button.set_label("Войти")
        self._set_auth_controls_sensitive()
        self.apply_button.set_sensitive(False)
        self.cancel_button.set_sensitive(False)
        self.footer_label.set_text("Нет актуальных данных · нажмите «Обновить»")
        self._update_tray()

    @staticmethod
    def _set_state_classes(widget: Gtk.Widget, selected: str, choices: tuple[str, ...]) -> None:
        for css_class in choices:
            widget.remove_css_class(css_class)
        widget.add_css_class(selected)

    def _rebuild_nodes_if_needed(self, snapshot: StatusSnapshot) -> None:
        signature = tuple(
            (node.node_id, node.hostname, node.ip, node.online, node.current)
            for node in snapshot.nodes
        )
        if signature == self._nodes_signature:
            self._sync_node_checks()
            return
        self._nodes_signature = signature
        for row in self._node_rows:
            self.exit_group.remove(row)
        self._node_rows.clear()
        self._node_checks.clear()

        if self._selection_dirty:
            valid = {node.node_id for node in snapshot.nodes if node.online}
            if self._selected_exit_id and self._selected_exit_id not in valid:
                self._selection_dirty = False
                self._selected_exit_id = snapshot.exit_node
        else:
            self._selected_exit_id = snapshot.exit_node

        direct = self._make_node_row(
            node_id="",
            title="Прямое подключение",
            subtitle="Без exit node",
            online=True,
            current=not snapshot.exit_node,
            anchor=None,
        )
        anchor = self._node_checks[""]
        self.exit_group.add(direct)
        self._node_rows.append(direct)

        for node in snapshot.nodes:
            row = self._make_node_row(
                node_id=node.node_id,
                title=node.hostname,
                subtitle=node.subtitle,
                online=node.online,
                current=node.node_id == snapshot.exit_node,
                anchor=anchor,
            )
            self.exit_group.add(row)
            self._node_rows.append(row)
        self.exit_search.set_visible(len(snapshot.nodes) >= 5)
        self._sync_node_checks()
        self._filter_nodes()

    def _make_node_row(
        self,
        *,
        node_id: str,
        title: str,
        subtitle: str,
        online: bool,
        current: bool,
        anchor: Gtk.CheckButton | None,
    ) -> Adw.ActionRow:
        row = Adw.ActionRow(title=title, subtitle=subtitle)
        row._node_id = node_id  # type: ignore[attr-defined]
        row._search_text = f"{title} {subtitle}".casefold()  # type: ignore[attr-defined]
        row._node_online = online  # type: ignore[attr-defined]
        check = Gtk.CheckButton()
        if anchor:
            check.set_group(anchor)
        check.connect("toggled", self._node_toggled, node_id)
        row.add_prefix(check)
        row.set_activatable_widget(check)
        self._node_checks[node_id] = check
        if current:
            row.add_suffix(
                self._make_row_badge(
                    "Активен",
                    "current-badge",
                    "object-select-symbolic",
                )
            )
        if node_id and not online:
            row.add_suffix(
                self._make_row_badge(
                    "Офлайн",
                    "offline-badge",
                    "network-offline-symbolic",
                )
            )
            row.set_sensitive(False)
        return row

    @staticmethod
    def _make_row_badge(text: str, css_class: str, icon_name: str) -> Gtk.Box:
        badge = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        badge.set_valign(Gtk.Align.CENTER)
        badge.set_halign(Gtk.Align.END)
        badge.add_css_class("node-badge")
        badge.add_css_class(css_class)
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(12)
        badge.append(icon)
        badge.append(Gtk.Label(label=text))
        return badge

    def _sync_node_checks(self) -> None:
        selected = self._selected_exit_id
        check = self._node_checks.get(selected)
        if not check:
            selected = self.snapshot.exit_node if self.snapshot else ""
            check = self._node_checks.get(selected) or self._node_checks.get("")
            self._selected_exit_id = selected if selected in self._node_checks else ""
            self._selection_dirty = False
        if check and not check.get_active():
            check.set_active(True)

    def _node_toggled(self, check: Gtk.CheckButton, node_id: str) -> None:
        if not check.get_active() or not self.snapshot:
            return
        self._selected_exit_id = node_id
        self._selection_dirty = node_id != self.snapshot.exit_node
        self._update_apply_bar()

    def _filter_nodes(self, *_args: Any) -> None:
        query = self.exit_search.get_text().strip().casefold() if hasattr(self, "exit_search") else ""
        for row in self._node_rows:
            node_id = getattr(row, "_node_id", "")
            haystack = getattr(row, "_search_text", "")
            row.set_visible(not query or not node_id or query in haystack)

    def _reset_exit_selection(self) -> None:
        if not self.snapshot:
            return
        self._selected_exit_id = self.snapshot.exit_node
        self._selection_dirty = False
        self._sync_node_checks()
        self._update_apply_bar()

    def _update_apply_bar(self) -> None:
        current = self.snapshot.exit_node if self.snapshot else ""
        selected = self._selected_exit_id
        if self._selection_dirty:
            label = f"Будет применён: {selected or 'прямое подключение'}"
        elif self.snapshot:
            label = f"Сейчас: {current or 'прямое подключение'}"
        else:
            label = "Маршрут ещё не определён"
        self.selection_label.set_text(label)
        can_apply = bool(
            self.snapshot
            and self.snapshot.connected
            and self._selection_dirty
            and not self._operation_active()
            and not self._fetching
            and not self._last_error
        )
        self.apply_button.set_sensitive(can_apply)
        self.cancel_button.set_sensitive(
            bool(
                self._selection_dirty
                and not self._operation_active()
                and not self._fetching
            )
        )

    def _set_node_rows_sensitive(self, enabled: bool) -> None:
        for row in self._node_rows:
            node_id = getattr(row, "_node_id", "")
            online = bool(getattr(row, "_node_online", True))
            row.set_sensitive(enabled and (not node_id or online))

    def _set_auth_controls_sensitive(self) -> None:
        status_ready = bool(
            self.snapshot and not self._last_error and not self._fetching
        )
        recovery_available = bool(
            (self._pending_login_url or self._previous_profile) and not self._fetching
        )
        enabled = bool(
            not self._operation_active() and (status_ready or recovery_available)
        )
        self.auth_row.set_sensitive(enabled)
        self.browser_auth_row.set_sensitive(
            enabled and bool(status_ready or self._pending_login_url)
        )
        self.key_auth_row.set_sensitive(
            enabled and status_ready and not self._pending_login_url
        )
        self.restore_auth_row.set_sensitive(
            enabled and self._previous_profile is not None
        )
        self.cancel_pending_row.set_sensitive(
            enabled and bool(self._pending_login_url)
        )

    def _connection_switch_available(self) -> bool:
        return bool(
            _snapshot_allows_toggle(self.snapshot)
            and not self._last_error
            and not self._operation_active()
            and not self._fetching
        )

    def _auth_status_ready(self) -> bool:
        if self.snapshot and not self._last_error and not self._fetching:
            return True
        self.toast("Сначала дождитесь актуального статуса Tailscale")
        return False

    def _request_browser_login(self, *_args: Any) -> None:
        if self._operation_active():
            return
        if self._pending_login_url:
            self._open_login_url(self._pending_login_url)
            return
        if not self._auth_status_ready():
            return
        if self.app.preview:
            self.toast("В режиме предпросмотра авторизация отключена")
            return
        command = self._auth_command()
        self._confirm_account_change(
            command,
            lambda: self._start_browser_login(command),
        )

    def _request_auth_key(self, *_args: Any) -> None:
        if self._operation_active():
            return
        if not self._auth_status_ready():
            return
        if self.app.preview:
            self.toast("В режиме предпросмотра авторизация отключена")
            return
        command = self._auth_command()
        self._confirm_account_change(
            command,
            lambda: self._show_auth_key_dialog(command),
        )

    def _auth_command(self) -> str:
        return _auth_command_for_snapshot(self.snapshot)

    def _confirm_account_change(self, command: str, continuation: Any) -> None:
        has_profile = _snapshot_has_profile(self.snapshot)
        if command != "login" or not has_profile:
            continuation()
            return
        assert self.snapshot is not None
        identity = self.snapshot.user or self.snapshot.tailnet or "текущий аккаунт"
        route_note = (
            f" Сейчас интернет идёт через exit node «{self.snapshot.exit_node}»."
            if self.snapshot.exit_node
            else ""
        )
        if self.snapshot.connected:
            impact = (
                "После продолжения текущий tailnet отключится сразу; изменятся "
                "Tailscale IP и активный профиль."
            )
        else:
            impact = "После продолжения будет выбран новый локальный профиль Tailscale."
        self._set_confirming(True)
        dialog = Adw.AlertDialog(
            heading="Добавить другой аккаунт?",
            body=(
                f"Сейчас выбран {identity}. {impact}{route_note} Если вход не завершится, "
                "приложение предложит вернуться к этому аккаунту."
            ),
        )
        dialog.add_response("cancel", "Отмена")
        dialog.add_response("continue", "Продолжить")
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("continue", Adw.ResponseAppearance.DESTRUCTIVE)

        def chosen(alert: Adw.AlertDialog, result: Gio.AsyncResult, _data: Any) -> None:
            response = alert.choose_finish(result)
            if self._closing:
                return
            self._set_confirming(False)
            if response == "continue":
                continuation()

        dialog.choose(self, None, chosen, None)

    def _show_auth_key_dialog(self, command: str) -> None:
        if self._operation_active():
            return
        self._set_confirming(True)
        dialog = Adw.AlertDialog(
            heading="Вход по auth key",
            body=(
                "Ключ будет передан Tailscale через защищённый временный файл "
                "и сразу удалён. В настройках он не сохраняется."
            ),
        )
        dialog.add_response("cancel", "Отмена")
        dialog.add_response("login", "Войти")
        dialog.set_default_response("login")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("login", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_response_enabled("login", False)

        extra = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        extra.set_size_request(360, -1)
        key_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        key_list.add_css_class("boxed-list")
        key_entry = Adw.PasswordEntryRow(title="Auth key")
        key_entry.set_tooltip_text("Вставьте auth key; регистр имеет значение")
        key_list.append(key_entry)
        extra.append(key_list)
        hint = Gtk.Label(
            label="Ключ должен начинаться с tskey-auth-",
            xalign=0,
            wrap=True,
        )
        hint.add_css_class("caption")
        hint.add_css_class("dim-label")
        extra.append(hint)
        dialog.set_extra_child(extra)

        def key_changed(entry: Adw.PasswordEntryRow) -> None:
            raw = entry.get_text()
            try:
                normalize_auth_key(raw)
            except ValueError as exc:
                dialog.set_response_enabled("login", False)
                hint.set_text(
                    str(exc) if raw else "Ключ должен начинаться с tskey-auth-"
                )
            else:
                dialog.set_response_enabled("login", True)
                hint.set_text("Ключ будет использован только для этого входа")

        key_entry.connect("changed", key_changed)

        def chosen(alert: Adw.AlertDialog, result: Gio.AsyncResult, _data: Any) -> None:
            response = alert.choose_finish(result)
            raw_key = key_entry.get_text() if response == "login" else ""
            key_entry.set_text("")
            if self._closing:
                return
            self._set_confirming(False)
            if response != "login":
                return
            try:
                auth_key = normalize_auth_key(raw_key)
            except ValueError as exc:
                self.toast(str(exc))
                return
            self._start_auth_key_login(auth_key, command)

        dialog.choose(self, None, chosen, None)
        GLib.idle_add(lambda: (key_entry.grab_focus(), GLib.SOURCE_REMOVE)[1])

    def _start_browser_login(self, command: str) -> None:
        if self._operation_active():
            return
        self._set_busy(True, "Готовим безопасную страницу входа…")
        capture_previous = command == "login" and _snapshot_has_profile(self.snapshot)

        def worker() -> None:
            previous_profile: LoginProfile | None = None
            try:
                if capture_previous:
                    previous_profile = fetch_current_login_profile()
                    if previous_profile is None:
                        raise RuntimeError("Не удалось определить текущий аккаунт для возврата")
                result = run_browser_login(command)
            except Exception as exc:
                result = CommandResult(1, str(exc))
            GLib.idle_add(self._browser_login_done, result, previous_profile)

        threading.Thread(
            target=worker,
            daemon=True,
            name="tailscale-browser-login",
        ).start()

    def _browser_login_done(
        self,
        result: CommandResult,
        previous_profile: LoginProfile | None,
    ) -> bool:
        if self._closing:
            return GLib.SOURCE_REMOVE
        login_url = result.login_url
        if login_url:
            self._set_pending_login(login_url, previous_profile)
            if self._open_login_url(login_url):
                self.toast(
                    "Завершите вход на странице провайдера",
                    button="Открыть снова",
                    callback=lambda: self._open_login_url(login_url),
                    timeout=15,
                )
        elif result.ok:
            self._clear_pending_login()
            self.toast("Аккаунт Tailscale добавлен")
        else:
            self._offer_profile_restore(previous_profile)
            self._show_error_dialog("Не удалось начать авторизацию", result.output)
        if result.completion and not result.completion.is_set():
            self.footer_label.set_text(
                "Страница входа открыта · ждём завершения команды Tailscale…"
                if login_url
                else "Ждём завершения команды Tailscale…"
            )

            def wait_for_process() -> None:
                result.completion.wait()
                GLib.idle_add(self._browser_login_process_done)

            threading.Thread(
                target=wait_for_process,
                daemon=True,
                name="tailscale-browser-login-wait",
            ).start()
        else:
            self._browser_login_process_done()
        return GLib.SOURCE_REMOVE

    def _browser_login_process_done(self) -> bool:
        if self._closing:
            return GLib.SOURCE_REMOVE
        self._set_busy(False)
        self.refresh_status(manual=False, refresh_catalog=True)
        return GLib.SOURCE_REMOVE

    def _set_pending_login(
        self,
        login_url: str,
        previous_profile: LoginProfile | None,
    ) -> None:
        self._pending_login_url = login_url
        self.browser_auth_row.set_subtitle(
            "Страница входа подготовлена · завершите авторизацию в браузере"
        )
        self.browser_login_button.set_label("Открыть")
        self.key_auth_row.set_sensitive(False)
        self.cancel_pending_row.set_visible(True)
        self.auth_row.set_subtitle("Ожидаем завершения входа в браузере…")
        self.auth_row.set_expanded(True)
        self._offer_profile_restore(previous_profile)
        self._set_auth_controls_sensitive()

    def _offer_profile_restore(self, profile: LoginProfile | None) -> None:
        if profile is None:
            return
        self._previous_profile = profile
        identity = " · ".join(
            part for part in (profile.account, profile.tailnet) if part
        )
        self.restore_auth_row.set_subtitle(identity or profile.display_name)
        self.restore_auth_row.set_visible(True)
        self.auth_row.set_expanded(True)
        self._set_auth_controls_sensitive()

    def _clear_pending_login(self) -> None:
        self._pending_login_url = ""
        self._previous_profile = None
        self.cancel_pending_row.set_visible(False)
        self.restore_auth_row.set_visible(False)
        self.key_auth_row.set_sensitive(True)
        self.browser_auth_row.set_subtitle(
            "Логин и пароль вводятся только на странице провайдера"
        )
        self._set_auth_controls_sensitive()

    def _cancel_pending_browser(self, *_args: Any) -> None:
        if self._operation_active() or not self._pending_login_url:
            return
        self._pending_login_url = ""
        self.cancel_pending_row.set_visible(False)
        self.key_auth_row.set_sensitive(True)
        self.browser_auth_row.set_subtitle(
            "Логин и пароль вводятся только на странице провайдера"
        )
        if self._previous_profile:
            self.auth_row.set_subtitle("Вход не завершён · доступен возврат")
        elif self.snapshot:
            identity = self.snapshot.user or self.snapshot.tailnet
            self.auth_row.set_subtitle(
                f"Требуется вход · {identity}" if identity else "Требуется вход"
            )
        self.browser_login_button.set_label("Войти")
        self._set_auth_controls_sensitive()

    def _restore_previous_profile(self, *_args: Any) -> None:
        if self._operation_active() or not self._previous_profile:
            return
        if self.app.preview:
            self.toast("В режиме предпросмотра переключение отключено")
            return
        profile = self._previous_profile
        self._set_busy(True, f"Возвращаем аккаунт {profile.display_name}…")

        def worker() -> None:
            try:
                result = switch_login_profile(profile.profile_id)
            except Exception as exc:
                result = CommandResult(1, str(exc))
            GLib.idle_add(self._restore_previous_profile_done, profile, result)

        threading.Thread(
            target=worker,
            daemon=True,
            name="tailscale-profile-restore",
        ).start()

    def _restore_previous_profile_done(
        self,
        profile: LoginProfile,
        result: CommandResult,
    ) -> bool:
        if self._closing:
            return GLib.SOURCE_REMOVE
        self._set_busy(False)
        if result.ok:
            self._clear_pending_login()
            self.toast(f"Выбран прежний аккаунт: {profile.display_name}")
        else:
            self._show_error_dialog("Не удалось вернуться к аккаунту", result.output)
        self.refresh_status(manual=False, refresh_catalog=True)
        return GLib.SOURCE_REMOVE

    def _open_login_url(self, login_url: str) -> bool:
        if not is_trusted_login_url(login_url):
            self._show_error_dialog(
                "Небезопасная ссылка авторизации",
                "Tailscale вернул ссылку с неизвестным адресом. Она не была открыта.",
            )
            return False
        try:
            Gio.AppInfo.launch_default_for_uri(login_url, None)
        except GLib.Error:
            self.toast(
                "Не удалось открыть браузер",
                button="Скопировать ссылку",
                callback=lambda: self._copy_login_url(login_url),
                timeout=15,
            )
            return False
        return True

    def _copy_login_url(self, login_url: str) -> None:
        if not is_trusted_login_url(login_url):
            return
        self.get_display().get_clipboard().set(login_url)
        self.toast("Ссылка для входа скопирована")

    def _start_auth_key_login(self, auth_key: str, command: str) -> None:
        if self._operation_active():
            return
        self._set_busy(True, "Авторизуем устройство по auth key…")
        capture_previous = command == "login" and _snapshot_has_profile(self.snapshot)

        def worker() -> None:
            previous_profile: LoginProfile | None = None
            try:
                if capture_previous:
                    previous_profile = fetch_current_login_profile()
                    if previous_profile is None:
                        raise RuntimeError("Не удалось определить текущий аккаунт для возврата")
                result = login_with_auth_key(auth_key, command=command)
            except Exception as exc:
                message = str(exc).replace(auth_key, "[auth key скрыт]")
                result = CommandResult(1, message)
            GLib.idle_add(self._auth_key_login_done, result, previous_profile)

        threading.Thread(
            target=worker,
            daemon=True,
            name="tailscale-auth-key-login",
        ).start()

    def _auth_key_login_done(
        self,
        result: CommandResult,
        previous_profile: LoginProfile | None,
    ) -> bool:
        if self._closing:
            return GLib.SOURCE_REMOVE
        self._set_busy(False)
        if result.ok:
            self._clear_pending_login()
            self.toast("Вход по auth key выполнен")
        else:
            if result.login_url:
                self._set_pending_login(result.login_url, previous_profile)
                if self._open_login_url(result.login_url):
                    self.toast(
                        "Tailscale запросил дополнительный вход",
                        button="Открыть снова",
                        callback=lambda: self._open_login_url(result.login_url),
                        timeout=15,
                    )
            else:
                self._offer_profile_restore(previous_profile)
                self._show_error_dialog("Не удалось войти по auth key", result.output)
        self.refresh_status(manual=False, refresh_catalog=True)
        return GLib.SOURCE_REMOVE

    def _on_connection_switch(self, row: Adw.SwitchRow, _param: Any) -> None:
        if self._switch_sync or self._operation_active():
            return
        desired = row.get_active()
        current = self.snapshot.connected if self.snapshot else False
        if not self.snapshot or self._last_error:
            self._sync_connection_switch(current)
            self.toast("Сначала обновите статус Tailscale")
            return
        if self.snapshot.backend_state in {"NeedsLogin", "NeedsMachineAuth"}:
            self._sync_connection_switch(False)
            self.toast("Используйте раздел «Авторизация»")
            return
        if desired == current:
            return
        if self.app.preview:
            self._sync_connection_switch(current)
            self.toast("В режиме предпросмотра действия отключены")
            return
        if not desired and self.snapshot.exit_node:
            self._confirm_turn_off()
            return
        self._start_connection_change(desired)

    def _sync_connection_switch(self, active: bool) -> None:
        self._switch_sync = True
        self.connection_switch.set_active(active)
        self._switch_sync = False

    def _confirm_turn_off(self) -> None:
        if self._operation_active():
            return
        node = self.snapshot.exit_node if self.snapshot else ""
        self._set_confirming(True)
        dialog = Adw.AlertDialog(
            heading="Отключить Tailscale?",
            body=(
                f"Сейчас интернет идёт через exit node «{node}». После отключения "
                "Tailscale система вернётся к обычному сетевому маршруту."
            ),
        )
        dialog.add_response("cancel", "Отмена")
        dialog.add_response("off", "Отключить")
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("off", Adw.ResponseAppearance.DESTRUCTIVE)

        def chosen(alert: Adw.AlertDialog, result: Gio.AsyncResult, _data: Any) -> None:
            response = alert.choose_finish(result)
            if self._closing:
                return
            self._set_confirming(False)
            if (
                response == "off"
                and self.snapshot
                and self.snapshot.connected
                and not self._last_error
            ):
                self._start_connection_change(False)
            else:
                self._sync_connection_switch(
                    self.snapshot.connected if self.snapshot else False
                )

        dialog.choose(self, None, chosen, None)

    def _start_connection_change(self, desired: bool) -> None:
        if self._operation_active():
            return
        self._set_busy(True, "Подключаем Tailscale…" if desired else "Отключаем Tailscale…")
        self._sync_connection_switch(desired)

        def worker() -> None:
            try:
                result = run_with_optional_elevation(
                    ["up", "--timeout=10s"] if desired else ["down"],
                    timeout=20 if desired else 30,
                )
            except subprocess.TimeoutExpired as exc:
                result = _timeout_result(exc)
            except Exception as exc:
                result = CommandResult(1, str(exc))
            GLib.idle_add(self._connection_change_done, desired, result)

        threading.Thread(target=worker, daemon=True, name="tailscale-toggle").start()

    def _connection_change_done(self, desired: bool, result: CommandResult) -> bool:
        if self._closing:
            return GLib.SOURCE_REMOVE
        self._set_busy(False)
        if result.ok:
            self.toast("Tailscale подключён" if desired else "Tailscale отключён")
        else:
            current = self.snapshot.connected if self.snapshot else False
            self._sync_connection_switch(current)
            if result.login_url:
                self.toast(
                    "Для подключения требуется вход",
                    button="Открыть",
                    callback=lambda: self._open_login_url(result.login_url),
                    timeout=15,
                )
            else:
                self._show_error_dialog("Не удалось изменить подключение", result.output)
        self.refresh_status(manual=False, refresh_catalog=False)
        return GLib.SOURCE_REMOVE

    def _apply_exit_node(self, *_args: Any) -> None:
        if (
            self._operation_active()
            or not self.snapshot
            or not self.snapshot.connected
            or not self._selection_dirty
        ):
            return
        node_id = self._selected_exit_id
        if node_id:
            node = next((item for item in self.snapshot.nodes if item.node_id == node_id), None)
            if not node or not node.online:
                self.toast("Этот exit node сейчас недоступен")
                self._reset_exit_selection()
                return
        if self.app.preview:
            self.toast("В режиме предпросмотра действия отключены")
            return
        self._set_busy(True, "Меняем интернет-маршрут…")

        def worker() -> None:
            try:
                result = run_with_optional_elevation(
                    ["set", f"--exit-node={node_id}"],
                    timeout=45,
                )
            except subprocess.TimeoutExpired as exc:
                result = _timeout_result(exc)
            except Exception as exc:
                result = CommandResult(1, str(exc))
            GLib.idle_add(self._exit_change_done, node_id, result)

        threading.Thread(target=worker, daemon=True, name="tailscale-exit-node").start()

    def _exit_change_done(self, node_id: str, result: CommandResult) -> bool:
        if self._closing:
            return GLib.SOURCE_REMOVE
        self._set_busy(False)
        if result.ok:
            self._selection_dirty = False
            self.toast(f"Маршрут изменён: {node_id or 'прямое подключение'}")
        else:
            self._show_error_dialog("Не удалось изменить exit node", result.output)
        self.refresh_status(manual=False, refresh_catalog=True)
        return GLib.SOURCE_REMOVE

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self._busy = busy
        self.connection_switch.set_sensitive(self._connection_switch_available())
        self.refresh_button.set_sensitive(not busy)
        self._set_node_rows_sensitive(not busy and not self._last_error)
        self._set_auth_controls_sensitive()
        quit_action = self.app.lookup_action("quit")
        if quit_action:
            quit_action.set_enabled(not self._operation_active())
        if message:
            self.footer_label.set_text(message)
        self._update_activity()
        self._update_apply_bar()
        self._update_tray(force=True)

    def _operation_active(self) -> bool:
        return self._busy or self._confirming

    def _set_confirming(self, confirming: bool) -> None:
        self._confirming = confirming
        enabled = not self._operation_active() and bool(self.snapshot) and not self._last_error
        self.connection_switch.set_sensitive(self._connection_switch_available())
        self.refresh_button.set_sensitive(not self._operation_active() and not self._fetching)
        self._set_node_rows_sensitive(enabled)
        self._set_auth_controls_sensitive()
        quit_action = self.app.lookup_action("quit")
        if quit_action:
            quit_action.set_enabled(not self._operation_active())
        self._update_activity()
        self._update_apply_bar()
        self._update_tray(force=True)

    def _update_activity(self) -> None:
        active = self._operation_active() or self._fetching
        self.activity_spinner.set_spinning(active)
        self.activity_spinner.set_visible(active)
        self.refresh_button.set_sensitive(not self._operation_active() and not self._fetching)
        self.connection_switch.set_sensitive(self._connection_switch_available())
        self._set_node_rows_sensitive(
            not self._operation_active() and not self._fetching and not self._last_error
        )
        self._set_auth_controls_sensitive()
        self._update_apply_bar()
        self._update_tray()

    def _copy_ip(self, *_args: Any) -> None:
        if not self.snapshot or not self.snapshot.ip:
            return
        clipboard = self.get_display().get_clipboard()
        clipboard.set(self.snapshot.ip)
        self.toast("Tailscale IP скопирован")

    def _show_health_details(self, *_args: Any) -> None:
        if not self.snapshot or not self.snapshot.health:
            return
        body = "\n\n".join(f"• {message}" for message in self.snapshot.health)
        dialog = Adw.AlertDialog(heading="Диагностика Tailscale", body=body)
        dialog.add_response("close", "Закрыть")
        dialog.set_close_response("close")
        dialog.present(self)

    def _show_error_dialog(self, heading: str, body: str) -> None:
        dialog = Adw.AlertDialog(heading=heading, body=body or "Неизвестная ошибка")
        dialog.add_response("close", "Закрыть")
        dialog.set_close_response("close")
        dialog.present(self)

    def _poll(self) -> bool:
        if self._closing:
            return GLib.SOURCE_REMOVE
        if not self._operation_active():
            self.refresh_status(manual=False, refresh_catalog=False)
        return GLib.SOURCE_CONTINUE

    # ----- tray integration (GTK 3 helper process) -----
    def _runtime_socket_path(self) -> str:
        root = os.environ.get("XDG_RUNTIME_DIR")
        if not root or not os.path.isdir(root):
            root = os.path.join(Path.home(), ".cache")
        return os.path.join(root, f"tailscale-toggle-{os.getuid()}.sock")

    def _start_tray(self) -> bool:
        if self._closing or self.app.no_tray or self._tray_proc:
            return GLib.SOURCE_REMOVE
        path = self._runtime_socket_path()
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            self._tray_failed(f"Не удалось подготовить сокет трея: {exc}")
            return GLib.SOURCE_REMOVE

        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            server.bind(path)
            os.chmod(path, 0o600)
            server.listen(1)
            server.settimeout(8)
        except OSError as exc:
            server.close()
            self._tray_failed(f"Не удалось запустить сокет трея: {exc}")
            return GLib.SOURCE_REMOVE

        token = secrets.token_urlsafe(32)
        helper = APP_DIR / "tray_agent.py"
        env = os.environ.copy()
        env["GDK_BACKEND"] = "x11"
        env["TAILSCALE_TOGGLE_TRAY_TOKEN"] = token
        env.setdefault("DISPLAY", ":0")
        try:
            proc = subprocess.Popen(
                [sys.executable, str(helper), path],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            server.close()
            try:
                os.unlink(path)
            except OSError:
                pass
            self._tray_failed(f"Не удалось запустить трей: {exc}")
            return GLib.SOURCE_REMOVE

        self._tray_server = server
        self._tray_proc = proc
        self._tray_path = path
        self._tray_token = token

        def accept() -> None:
            try:
                conn, _ = server.accept()
                peer = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
                _pid, uid, _gid = struct.unpack("3i", peer)
                if uid != os.getuid():
                    conn.close()
                    raise PermissionError("неверный владелец процесса трея")
                conn.setblocking(True)
                GLib.idle_add(self._tray_connected, conn)
            except Exception as exc:
                GLib.idle_add(self._tray_failed, f"Трей не подключился: {exc}")

        threading.Thread(target=accept, daemon=True, name="tray-accept").start()
        return GLib.SOURCE_REMOVE

    def _tray_connected(self, conn: socket.socket) -> bool:
        if self._closing:
            conn.close()
            return GLib.SOURCE_REMOVE
        self._tray_conn = conn
        threading.Thread(target=self._tray_reader, daemon=True, name="tray-reader").start()
        return GLib.SOURCE_REMOVE

    def _tray_reader(self) -> None:
        conn = self._tray_conn
        buffer = b""
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
                    if secrets.compare_digest(str(obj.get("token") or ""), self._tray_token):
                        GLib.idle_add(self._tray_event, obj)
        except OSError:
            pass
        if not self._closing:
            GLib.idle_add(self._tray_disconnected)

    def _tray_event(self, obj: dict[str, Any]) -> bool:
        event = obj.get("event")
        if event == "ready":
            self._tray_ready = True
            self._tray_failure_count = 0
            self._tray_signature = ""
            self._update_tray(force=True)
            if self.snapshot:
                stamp = datetime.fromtimestamp(self.snapshot.fetched_at).strftime("%H:%M:%S")
                self.footer_label.set_text(f"Обновлено {stamp} · трей готов")
        elif event == "open":
            self.show_window()
        elif event == "refresh" and not self._operation_active():
            self.refresh_status(manual=True)
        elif event == "toggle":
            if not self._connection_switch_available():
                if self.snapshot and self.snapshot.backend_state in {
                    "NeedsLogin",
                    "NeedsMachineAuth",
                }:
                    self.show_window()
                    self.auth_row.set_expanded(True)
                    self.toast("Используйте раздел «Авторизация»")
            else:
                assert self.snapshot is not None
                desired = not self.snapshot.connected
                if not desired and self.snapshot.exit_node:
                    self.show_window()
                    self._confirm_turn_off()
                else:
                    self._start_connection_change(desired)
        elif event == "exit" and not self._operation_active() and not self._last_error:
            node_id = str(obj.get("id") or "")
            self._selected_exit_id = node_id
            self._selection_dirty = bool(self.snapshot and node_id != self.snapshot.exit_node)
            self._apply_exit_node()
        elif event == "quit" and not self._operation_active():
            self.request_quit()
        return GLib.SOURCE_REMOVE

    def _tray_send(self, payload: dict[str, Any]) -> bool:
        if not self._tray_conn:
            return False
        message = dict(payload)
        message["token"] = self._tray_token
        try:
            self._tray_conn.sendall((json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8"))
            return True
        except OSError:
            return False

    def _update_tray(self, *, force: bool = False) -> None:
        if not self._tray_ready:
            return
        snapshot = self.snapshot
        if self._operation_active() or self._fetching:
            state = "busy"
        elif not snapshot or self._last_error:
            state = "error"
        elif snapshot.connected and snapshot.exit_node:
            state = "exit"
        elif snapshot.connected:
            state = "on"
        else:
            state = "off"
        tip = "Tailscale: статус неизвестен"
        nodes: list[dict[str, Any]] = []
        current = ""
        connected = False
        if snapshot:
            connected = snapshot.connected
            current = snapshot.exit_node
            if self._last_error:
                tip = "Tailscale: показаны устаревшие данные"
            else:
                tip = "Tailscale включён" if snapshot.connected else "Tailscale выключен"
            if current and not self._last_error:
                tip += f" · Exit: {current}"
            nodes = [
                {
                    "id": node.node_id,
                    "label": f"{node.hostname}  ·  {node.ip}" if node.ip else node.hostname,
                    "online": node.online,
                }
                for node in snapshot.nodes
            ]
        payload = {
            "cmd": "set",
            "state": state,
            "connected": connected,
            "busy": self._operation_active() or self._fetching,
            "can_toggle": self._connection_switch_available(),
            "nodes": nodes,
            "current": current,
            "tip": tip,
        }
        signature = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        if force or signature != self._tray_signature:
            if self._tray_send(payload):
                self._tray_signature = signature

    def _tray_failed(self, message: str) -> bool:
        self._tray_ready = False
        self._tray_failure_count += 1
        self._cleanup_tray_process(send_quit=False)
        if not self._closing:
            if not self.get_visible():
                self.show_window()
            self.footer_label.set_text(message + " · окно не будет скрываться")
            if self._tray_failure_count == 1:
                self.toast("Трей недоступен; закрытие завершит окно")
            self._schedule_tray_retry()
        return GLib.SOURCE_REMOVE

    def _tray_disconnected(self) -> bool:
        if self._closing:
            return GLib.SOURCE_REMOVE
        self._tray_ready = False
        self._tray_failure_count += 1
        self._cleanup_tray_process(send_quit=False)
        if not self.get_visible():
            self.show_window()
        self.footer_label.set_text("Трей отключился · окно не будет скрываться")
        self._schedule_tray_retry()
        return GLib.SOURCE_REMOVE

    def _schedule_tray_retry(self) -> None:
        if self.app.no_tray or self._tray_retry_source or self._closing:
            return
        delay = min(5 * max(1, self._tray_failure_count), 30)
        self._tray_retry_source = GLib.timeout_add_seconds(delay, self._retry_tray)

    def _retry_tray(self) -> bool:
        self._tray_retry_source = 0
        self._start_tray()
        return GLib.SOURCE_REMOVE

    def _cleanup_tray_process(self, *, send_quit: bool) -> None:
        if send_quit:
            self._tray_send({"cmd": "quit"})
        for sock in (self._tray_conn, self._tray_server):
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass
        self._tray_conn = None
        self._tray_server = None
        proc = self._tray_proc
        self._tray_proc = None
        if proc and proc.poll() is None:
            try:
                proc.wait(timeout=1.5 if send_quit else 0.2)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    proc.kill()
        if self._tray_path:
            try:
                os.unlink(self._tray_path)
            except OSError:
                pass
        self._tray_path = ""
        self._tray_token = ""
        self._tray_signature = ""

    def _shutdown_tray(self) -> None:
        if self._closing and not self._tray_proc and not self._tray_conn:
            return
        self._closing = True
        if self._tray_retry_source:
            GLib.source_remove(self._tray_retry_source)
            self._tray_retry_source = 0
        self._cleanup_tray_process(send_quit=True)
        self._tray_ready = False

    def show_window(self) -> None:
        self.present()

    def _on_close_request(self, *_args: Any) -> bool:
        if self._operation_active():
            if self._tray_ready:
                self.set_visible(False)
            else:
                self.toast("Дождитесь завершения операции Tailscale")
            return True
        if self.app.close_to_tray_enabled() and self._tray_ready:
            self.set_visible(False)
            return True
        if self._pending_login_url or self._previous_profile:
            self.request_quit()
            return True
        self._shutdown_tray()
        return False

    def request_quit(self) -> None:
        if self._operation_active():
            self.show_window()
            self.toast("Дождитесь завершения операции Tailscale")
            return
        if self._pending_login_url or self._previous_profile:
            self._confirm_quit_during_auth()
            return
        self.quit_app(force=True)

    def _confirm_quit_during_auth(self) -> None:
        if self._operation_active():
            return
        self.show_window()
        self._set_confirming(True)
        dialog = Adw.AlertDialog(
            heading="Вход ещё не завершён",
            body=(
                "При выходе приложение забудет подготовленную ссылку и кнопку возврата. "
                "Локальные аккаунты Tailscale не удалятся."
            ),
        )
        dialog.add_response("cancel", "Остаться")
        if self._previous_profile:
            dialog.add_response("restore", "Вернуться к аккаунту")
            dialog.set_response_appearance("restore", Adw.ResponseAppearance.SUGGESTED)
        dialog.add_response("quit", "Выйти")
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.set_response_appearance("quit", Adw.ResponseAppearance.DESTRUCTIVE)

        def chosen(alert: Adw.AlertDialog, result: Gio.AsyncResult, _data: Any) -> None:
            response = alert.choose_finish(result)
            if self._closing:
                return
            self._set_confirming(False)
            if response == "restore":
                self._restore_previous_profile()
            elif response == "quit":
                self.quit_app(force=True)

        dialog.choose(self, None, chosen, None)

    def quit_app(self, *, force: bool = False) -> None:
        if self._operation_active():
            return
        if not force and (self._pending_login_url or self._previous_profile):
            self.request_quit()
            return
        self._shutdown_tray()
        self.app.quit()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument(
        "--preview",
        choices=("connected", "disconnected", "needs-login", "degraded", "error"),
        default="",
        help="show a safe mock state without invoking Tailscale",
    )
    parser.add_argument("--no-tray", action="store_true", help="do not launch the tray helper")
    parser.add_argument(
        "--theme",
        choices=("system", "light", "dark"),
        default="",
        help="override the saved color scheme for this run",
    )
    parser.add_argument("--quit-after", type=float, default=0, help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    app = TailscaleApplication(
        preview=args.preview,
        no_tray=args.no_tray,
        quit_after=args.quit_after,
        theme_override=args.theme,
    )
    return app.run([sys.argv[0]])


if __name__ == "__main__":
    raise SystemExit(main())
