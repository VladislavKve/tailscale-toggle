#!/usr/bin/env python3
"""Backend and pure data helpers for Tailscale Toggle.

The module intentionally has no GUI imports.  It can be tested headlessly and
keeps all calls to the Tailscale CLI in one place.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
import ipaddress
import json
import os
from pathlib import Path
import re
import selectors
import stat
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable, Iterable, Iterator
from urllib.parse import urlsplit


TAILSCALE = os.environ.get("TAILSCALE_CLI", "/usr/bin/tailscale")
DEFAULT_SETTINGS = {
    "color_scheme": "system",
    "close_to_tray": True,
}
_URL_RE = re.compile(r"https://[^\s<>\"']+")
_CATALOG_TTL_SECONDS = 30.0
_catalog_lock = threading.Lock()
_catalog_text = ""
_catalog_updated_at = 0.0
_AUTH_KEY_MAX_LENGTH = 4096


@dataclass(frozen=True, slots=True)
class ExitNode:
    """One selectable exit node."""

    node_id: str
    hostname: str
    ip: str = ""
    online: bool = True
    current: bool = False

    @property
    def subtitle(self) -> str:
        parts = [part for part in (self.ip, "В сети" if self.online else "Не в сети") if part]
        return "  ·  ".join(parts)


@dataclass(frozen=True, slots=True)
class StatusSnapshot:
    """Normalized subset of ``tailscale status --json`` used by the UI."""

    backend_state: str
    connected: bool
    device_name: str
    dns_name: str
    ip: str
    tailnet: str
    user: str
    exit_node: str
    health: tuple[str, ...]
    nodes: tuple[ExitNode, ...]
    version: str = ""
    fetched_at: float = 0.0

    @property
    def degraded(self) -> bool:
        return self.connected and bool(self.health)

    @property
    def display_state(self) -> str:
        if self.degraded:
            return "degraded"
        if self.connected:
            return "connected"
        if self.backend_state in {"NeedsLogin", "NeedsMachineAuth"}:
            return "needs-login"
        if self.backend_state in {"Stopped", "NoState"}:
            return "stopped"
        return "unknown"


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    output: str
    completion: threading.Event | None = field(
        default=None,
        compare=False,
        repr=False,
    )

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def login_url(self) -> str:
        return extract_login_url(self.output)


@dataclass(frozen=True, slots=True)
class LoginProfile:
    """One locally stored Tailscale account profile."""

    profile_id: str
    nickname: str = ""
    tailnet: str = ""
    account: str = ""

    @property
    def display_name(self) -> str:
        return self.nickname or self.account or self.tailnet or self.profile_id


def run_tailscale(
    args: Iterable[str],
    *,
    timeout: float = 20,
    elevate: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run the Tailscale CLI with an argv list (never through a shell)."""

    cmd = [TAILSCALE, *args]
    if elevate:
        cmd = ["pkexec", TAILSCALE, *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def needs_elevation(message: str) -> bool:
    """Recognize common localized and English permission failures."""

    text = (message or "").casefold()
    markers = (
        "access denied",
        "permission denied",
        "operation not permitted",
        "use 'sudo",
        "requires root",
        "доступ запрещ",
        "отказано в доступе",
        "недостаточно прав",
        "требуются права",
    )
    return any(marker in text for marker in markers)


def run_with_optional_elevation(
    args: Iterable[str], *, timeout: float = 30
) -> CommandResult:
    """Run a mutating CLI command and ask PolicyKit only when required."""

    result = run_tailscale(args, timeout=timeout)
    output = _combined_output(result)
    if result.returncode != 0 and needs_elevation(output):
        result = run_tailscale(args, timeout=timeout + 60, elevate=True)
        output = _combined_output(result)
    return CommandResult(result.returncode, output)


def run_browser_login(command: str = "login", *, timeout: float = 15) -> CommandResult:
    """Start browser authentication and return as soon as its trusted URL appears."""

    if command not in {"login", "up"}:
        raise ValueError("Неподдерживаемая команда авторизации")
    args = [command, "--timeout=30s"]
    result = _run_tailscale_until_login_url(args, timeout=timeout)
    if result.returncode != 0 and needs_elevation(result.output):
        result = _run_tailscale_until_login_url(
            args,
            timeout=timeout + 60,
            elevate=True,
        )
    return result


def _run_tailscale_until_login_url(
    args: Iterable[str],
    *,
    timeout: float,
    elevate: bool = False,
) -> CommandResult:
    command = [TAILSCALE, *args]
    if elevate:
        command = ["pkexec", TAILSCALE, *args]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    if process.stdout is None:  # pragma: no cover - guaranteed by PIPE
        process.kill()
        return CommandResult(1, "Не удалось прочитать ответ Tailscale")

    output = bytearray()
    completion = threading.Event()
    deadline = time.monotonic() + timeout
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    hand_off_to_reaper = False
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if elevate:
                    hand_off_to_reaper = True
                else:
                    _stop_process(process)
                return CommandResult(
                    124,
                    output.decode("utf-8", errors="replace").strip()
                    or "Tailscale не вернул ссылку для входа вовремя",
                    completion if elevate else None,
                )
            for key, _events in selector.select(timeout=min(0.25, remaining)):
                chunk = os.read(key.fd, 4096)
                if chunk:
                    output.extend(chunk)
                else:
                    selector.unregister(key.fileobj)
            text = output.decode("utf-8", errors="replace")
            if extract_login_url(text):
                if elevate:
                    hand_off_to_reaper = True
                else:
                    _stop_process(process)
                return CommandResult(
                    124,
                    text.strip(),
                    completion if elevate else None,
                )
            returncode = process.poll()
            if returncode is not None:
                remainder = process.stdout.read()
                if remainder:
                    output.extend(remainder)
                return CommandResult(
                    returncode,
                    output.decode("utf-8", errors="replace").strip(),
                )
    finally:
        selector.close()
        if hand_off_to_reaper:
            if process.poll() is None:
                _reap_process_in_background(process, completion)
            else:
                completion.set()
                process.stdout.close()
        elif process.poll() is None:
            if elevate:
                _reap_process_in_background(process, completion)
                hand_off_to_reaper = True
            else:
                _stop_process(process)
        if not hand_off_to_reaper and not process.stdout.closed:
            process.stdout.close()


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def _reap_process_in_background(
    process: subprocess.Popen[bytes],
    completion: threading.Event | None = None,
) -> None:
    """Drain and reap an elevated CLI that the unprivileged GUI cannot signal."""

    def reap() -> None:
        try:
            if process.stdout is not None:
                try:
                    while process.stdout.read(4096):
                        pass
                except OSError:
                    pass
            process.wait()
        finally:
            if process.stdout is not None:
                process.stdout.close()
            if completion is not None:
                completion.set()

    threading.Thread(
        target=reap,
        daemon=True,
        name=f"tailscale-login-reaper-{process.pid}",
    ).start()


def parse_login_profiles(text: str) -> tuple[LoginProfile | None, tuple[LoginProfile, ...]]:
    """Parse ``tailscale switch --list --json`` and identify the selected profile."""

    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise RuntimeError("Tailscale вернул некорректный список аккаунтов") from exc
    if not isinstance(payload, list):
        raise RuntimeError("Tailscale вернул некорректный список аккаунтов")
    profiles: list[LoginProfile] = []
    current: LoginProfile | None = None
    for item in payload:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        profile = LoginProfile(
            profile_id=str(item["id"]),
            nickname=str(item.get("nickname") or ""),
            tailnet=str(item.get("tailnet") or ""),
            account=str(item.get("account") or ""),
        )
        profiles.append(profile)
        if item.get("selected") is True:
            current = profile
    return current, tuple(profiles)


def fetch_current_login_profile(
    *, runner: Callable[..., CommandResult] | None = None
) -> LoginProfile | None:
    """Read the selected local account so an interrupted login can be rolled back."""

    command_runner = runner or run_with_optional_elevation
    result = command_runner(["switch", "--list", "--json"], timeout=15)
    if not result.ok:
        raise RuntimeError(result.output or "Не удалось прочитать локальные аккаунты")
    current, _profiles = parse_login_profiles(result.output)
    return current


def switch_login_profile(
    profile_id: str, *, runner: Callable[..., CommandResult] | None = None
) -> CommandResult:
    """Switch back to a previously captured profile by its opaque local ID."""

    if not profile_id or any(character.isspace() for character in profile_id):
        raise ValueError("Некорректный идентификатор аккаунта")
    command_runner = runner or run_with_optional_elevation
    return command_runner(["switch", profile_id], timeout=30)


def normalize_auth_key(value: str) -> str:
    """Validate a pasted auth key without changing its case or format."""

    key = (value or "").strip()
    if not key:
        raise ValueError("Введите auth key")
    if len(key) > _AUTH_KEY_MAX_LENGTH:
        raise ValueError("Auth key слишком длинный")
    if any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in key
    ):
        raise ValueError("Auth key не должен содержать пробелы или переносы строк")
    if not key.startswith("tskey-auth-") or len(key) == len("tskey-auth-"):
        raise ValueError("Нужен Tailscale auth key с префиксом tskey-auth-")
    return key


def _secure_runtime_directory(path: Path | None = None) -> Path:
    """Return a private, user-owned runtime directory suitable for a secret."""

    candidate = path
    if candidate is None:
        configured = os.environ.get("XDG_RUNTIME_DIR")
        candidate = Path(configured) if configured else Path(f"/run/user/{os.getuid()}")
    try:
        candidate = candidate.resolve(strict=True)
        info = candidate.stat()
    except OSError as exc:
        raise RuntimeError("Защищённый runtime-каталог недоступен") from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise RuntimeError("Runtime-каталог принадлежит другому пользователю")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeError("Runtime-каталог имеет небезопасные права доступа")
    return candidate


@contextmanager
def _temporary_auth_key_file(
    auth_key: str, *, runtime_dir: Path | None = None
) -> Iterator[Path]:
    """Expose an auth key to the CLI briefly through a mode-0600 runtime file."""

    directory = _secure_runtime_directory(runtime_dir)
    descriptor = -1
    temp_name = ""
    try:
        descriptor, temp_name = tempfile.mkstemp(
            prefix=".tailscale-toggle-auth-",
            dir=directory,
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            descriptor = -1
            handle.write(auth_key)
            handle.flush()
            os.fsync(handle.fileno())
        yield Path(temp_name)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temp_name:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def login_with_auth_key(
    value: str,
    *,
    command: str = "login",
    runtime_dir: Path | None = None,
    runner: Callable[..., CommandResult] | None = None,
) -> CommandResult:
    """Authenticate without putting the auth key in argv or persistent settings."""

    auth_key = normalize_auth_key(value)
    if command not in {"login", "up"}:
        raise ValueError("Неподдерживаемая команда авторизации")
    command_runner = runner or run_with_optional_elevation
    with _temporary_auth_key_file(auth_key, runtime_dir=runtime_dir) as key_path:
        try:
            result = command_runner(
                [command, f"--auth-key=file:{key_path}", "--timeout=20s"],
                timeout=30,
            )
        except subprocess.TimeoutExpired as exc:
            result = CommandResult(124, _timeout_output(exc))
        except Exception as exc:
            message = _redact_auth_material(str(exc), auth_key, key_path)
            raise RuntimeError(message or "Не удалось выполнить вход по auth key") from None
    # Be defensive even though neither the key nor its value was present in argv.
    output = _redact_auth_material(result.output, auth_key, key_path)
    return CommandResult(result.returncode, output)


def _timeout_output(exc: subprocess.TimeoutExpired) -> str:
    parts: list[str] = []
    for value in (exc.stderr, exc.stdout):
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if value:
            parts.append(str(value).strip())
    return "\n".join(part for part in parts if part) or "Команда не завершилась вовремя"


def _redact_auth_material(text: str, auth_key: str, key_path: Path) -> str:
    return (text or "").replace(auth_key, "[auth key скрыт]").replace(
        str(key_path), "[временный файл auth key]"
    )


def _combined_output(result: subprocess.CompletedProcess[str]) -> str:
    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()
    return "\n".join(part for part in (stderr, stdout) if part)


def extract_login_url(text: str) -> str:
    """Return the first trusted Tailscale login URL in CLI output."""

    for match in _URL_RE.finditer(text or ""):
        candidate = match.group(0).rstrip(".,);]")
        if is_trusted_login_url(candidate):
            return candidate
    return ""


def is_trusted_login_url(value: str) -> bool:
    """Allow automatic browser opening only for Tailscale's HTTPS login host."""

    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return bool(
        parsed.scheme == "https"
        and (parsed.hostname or "").rstrip(".").casefold() == "login.tailscale.com"
        and parsed.username is None
        and parsed.password is None
        and port in {None, 443}
    )


def _short_hostname(value: str) -> str:
    return (value or "").rstrip(".").split(".")[0]


def _normalize_ip(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    try:
        return str(ipaddress.ip_interface(text).ip)
    except ValueError:
        return text


def _online_from_peer(peer: dict[str, Any]) -> bool:
    value = peer.get("Online")
    if isinstance(value, bool):
        return value
    return bool(peer.get("Active"))


def _nodes_from_json(data: dict[str, Any]) -> dict[str, ExitNode]:
    nodes: dict[str, ExitNode] = {}
    for peer in (data.get("Peer") or {}).values():
        if not isinstance(peer, dict) or not peer.get("ExitNodeOption"):
            continue
        hostname = peer.get("HostName") or _short_hostname(peer.get("DNSName") or "") or "unknown"
        ips = peer.get("TailscaleIPs") or []
        ip = _normalize_ip(ips[0]) if ips else ""
        node_id = str(hostname)
        nodes[node_id] = ExitNode(
            node_id=node_id,
            hostname=str(hostname),
            ip=ip,
            online=_online_from_peer(peer),
            current=bool(peer.get("ExitNode")),
        )
    return nodes


def _nodes_from_catalog(text: str) -> dict[str, ExitNode]:
    """Parse the stable first two columns of ``tailscale exit-node list``."""

    nodes: dict[str, ExitNode] = {}
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.casefold().startswith("ip "):
            continue
        parts = line.split()
        if len(parts) < 2 or not re.match(r"^[0-9a-fA-F:.]+$", parts[0]):
            continue
        ip, dns_name = _normalize_ip(parts[0]), parts[1]
        hostname = _short_hostname(dns_name) or dns_name
        offline = "offline" in line.casefold() or "не в сети" in line.casefold()
        nodes[hostname] = ExitNode(
            node_id=hostname,
            hostname=hostname,
            ip=ip,
            online=not offline,
        )
    return nodes


def parse_status_data(
    data: dict[str, Any], catalog_text: str = "", *, fetched_at: float | None = None
) -> StatusSnapshot:
    """Normalize raw status JSON and optional exit-node catalogue text."""

    state = str(data.get("BackendState") or "Unknown")
    self_data = data.get("Self") or {}
    ips = self_data.get("TailscaleIPs") or []
    ip = _normalize_ip(ips[0]) if ips else ""
    device_name = str(self_data.get("HostName") or _short_hostname(self_data.get("DNSName") or ""))
    dns_name = str(self_data.get("DNSName") or "").rstrip(".")

    tailnet_data = data.get("CurrentTailnet") or {}
    tailnet = str(
        tailnet_data.get("Name")
        or tailnet_data.get("MagicDNSSuffix")
        or data.get("MagicDNSSuffix")
        or ""
    ).rstrip(".")

    user_map = data.get("User") or {}
    user_id = str(self_data.get("UserID") or "")
    user_info: Any = user_map.get(user_id, {}) if isinstance(user_map, dict) else {}
    if not user_info and user_id.isdigit() and isinstance(user_map, dict):
        user_info = user_map.get(int(user_id), {})
    if not isinstance(user_info, dict):
        user_info = {}
    user = str(user_info.get("LoginName") or user_info.get("DisplayName") or "")

    nodes = _nodes_from_json(data)
    for node_id, catalog_node in _nodes_from_catalog(catalog_text).items():
        if node_id not in nodes:
            nodes[node_id] = catalog_node

    current = ""
    for node in nodes.values():
        if node.current:
            current = node.node_id
            break
    exit_status = data.get("ExitNodeStatus") or {}
    if not current and isinstance(exit_status, dict):
        status_name = exit_status.get("DNSName") or exit_status.get("Name") or ""
        current = _short_hostname(str(status_name))
        exit_id = str(exit_status.get("ID") or "")
        status_ips = {
            _normalize_ip(ip) for ip in (exit_status.get("TailscaleIPs") or []) if ip
        }
        if not current and exit_id:
            for peer_key, peer in (data.get("Peer") or {}).items():
                if not isinstance(peer, dict):
                    continue
                peer_ids = {
                    str(peer_key),
                    str(peer.get("ID") or ""),
                    str(peer.get("StableID") or ""),
                }
                if exit_id in peer_ids:
                    current = str(
                        peer.get("HostName")
                        or _short_hostname(peer.get("DNSName") or "")
                        or exit_id
                    )
                    break
        if not current and status_ips:
            current = next(
                (node.node_id for node in nodes.values() if node.ip in status_ips),
                "",
            )
        if not current and status_ips:
            current = sorted(status_ips)[0]
            nodes[current] = ExitNode(
                node_id=current,
                hostname=current,
                ip=current,
                online=bool(exit_status.get("Online", True)),
                current=True,
            )

    normalized_nodes = []
    for node in nodes.values():
        normalized_nodes.append(
            ExitNode(
                node_id=node.node_id,
                hostname=node.hostname,
                ip=node.ip,
                online=node.online,
                current=node.node_id == current,
            )
        )
    normalized_nodes.sort(key=lambda node: (not node.online, node.hostname.casefold()))

    health = tuple(str(item) for item in (data.get("Health") or []) if item)
    return StatusSnapshot(
        backend_state=state,
        connected=state == "Running",
        device_name=device_name,
        dns_name=dns_name,
        ip=ip,
        tailnet=tailnet,
        user=user,
        exit_node=current,
        health=health,
        nodes=tuple(normalized_nodes),
        version=str(data.get("Version") or ""),
        fetched_at=fetched_at if fetched_at is not None else time.time(),
    )


def _get_exit_catalog(*, force: bool = False) -> str:
    global _catalog_text, _catalog_updated_at
    now = time.monotonic()
    with _catalog_lock:
        if not force and _catalog_text and now - _catalog_updated_at < _CATALOG_TTL_SECONDS:
            return _catalog_text
        try:
            listed = run_tailscale(["exit-node", "list"], timeout=6)
        except (OSError, subprocess.SubprocessError):
            return _catalog_text
        if listed.returncode == 0:
            _catalog_text = listed.stdout or ""
            _catalog_updated_at = now
        return _catalog_text


def fetch_status(*, refresh_catalog: bool = False) -> StatusSnapshot:
    """Read current status; raises a descriptive exception on failure."""

    try:
        result = run_tailscale(["status", "--json"], timeout=7)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Не найден Tailscale CLI: {TAILSCALE}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Tailscale не ответил вовремя") from exc
    if result.returncode != 0:
        raise RuntimeError(_combined_output(result) or "Не удалось прочитать статус Tailscale")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Tailscale вернул некорректный статус") from exc
    catalog = _get_exit_catalog(force=refresh_catalog)
    return parse_status_data(data, catalog)


def settings_path() -> Path:
    config_root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_root / "tailscale-toggle" / "settings.json"


def load_settings(path: Path | None = None) -> dict[str, Any]:
    """Load preferences while tolerating an absent or damaged config file."""

    target = path or settings_path()
    values = dict(DEFAULT_SETTINGS)
    try:
        loaded = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return values
    if isinstance(loaded, dict):
        if loaded.get("color_scheme") in {"system", "light", "dark"}:
            values["color_scheme"] = loaded["color_scheme"]
        if isinstance(loaded.get("close_to_tray"), bool):
            values["close_to_tray"] = loaded["close_to_tray"]
    return values


def save_settings(settings: dict[str, Any], path: Path | None = None) -> None:
    """Atomically persist the small user preference file."""

    target = path or settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    clean = {
        "color_scheme": settings.get("color_scheme", DEFAULT_SETTINGS["color_scheme"]),
        "close_to_tray": bool(settings.get("close_to_tray", True)),
    }
    fd, temp_name = tempfile.mkstemp(prefix="settings-", suffix=".json", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(clean, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, target)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def snapshot_as_dict(snapshot: StatusSnapshot) -> dict[str, Any]:
    """Small debugging helper used by tests and diagnostics."""

    return asdict(snapshot)
