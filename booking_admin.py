from __future__ import annotations

import argparse
import getpass
import hashlib
import html
import os
import re
import secrets
import stat
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Mapping, Sequence
from urllib.parse import parse_qs, urlencode, urlsplit
from zoneinfo import ZoneInfo

from dotenv import dotenv_values

from book_badminton import FALLBACK_TIMES, TARGET_BOOKING_TIMES
from booking_email import (
    build_report,
    collect_attempted_booking_dates,
    collect_confirmed_bookings,
)


PROJECT_DIR = Path(__file__).resolve().parent
ENV_FILE = PROJECT_DIR / ".env"
LOGS_DIR = PROJECT_DIR / "logs"
TOKEN_FILE = PROJECT_DIR / ".booking_admin_token"
PASSWORD_FILE = PROJECT_DIR / ".booking_admin_password"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_PUBLIC_PATH = ""
MAX_FORM_BYTES = 32 * 1024
PASSWORD_HASH_ITERATIONS = 600_000
LOGIN_WINDOW_SECONDS = 15 * 60
MAX_LOGIN_FAILURES = 8
ENV_WRITE_LOCK = threading.Lock()


@dataclass(frozen=True)
class AccountSlotSpec:
    key: str
    display_name: str
    enabled_name: str
    username_name: str
    password_name: str
    primary_time_name: str
    court_order_name: str
    default_primary_time: str
    default_court_order: tuple[int, ...]


ACCOUNT_SLOTS = (
    AccountSlotSpec(
        key="account_a",
        display_name="Account A",
        enabled_name="PRIMARY_BOOKING_ENABLED",
        username_name="GYM_USERNAME",
        password_name="GYM_PASSWORD",
        primary_time_name="PRIMARY_BOOKING_TIME",
        court_order_name="PREFERRED_COURTS",
        default_primary_time="18:00",
        default_court_order=(1, 2, 3, 4),
    ),
    AccountSlotSpec(
        key="account_b",
        display_name="Account B",
        enabled_name="SECONDARY_BOOKING_ENABLED",
        username_name="SECONDARY_GYM_USERNAME",
        password_name="SECONDARY_GYM_PASSWORD",
        primary_time_name="SECONDARY_PRIMARY_BOOKING_TIME",
        court_order_name="SECONDARY_PREFERRED_COURTS",
        default_primary_time="18:00",
        default_court_order=(2, 1, 3, 4),
    ),
    AccountSlotSpec(
        key="account_c",
        display_name="Account C",
        enabled_name="TERTIARY_BOOKING_ENABLED",
        username_name="TERTIARY_GYM_USERNAME",
        password_name="TERTIARY_GYM_PASSWORD",
        primary_time_name="TERTIARY_PRIMARY_BOOKING_TIME",
        court_order_name="TERTIARY_PREFERRED_COURTS",
        default_primary_time="17:00",
        default_court_order=(1, 2, 3, 4),
    ),
)
ACCOUNT_SLOT_BY_KEY = {slot.key: slot for slot in ACCOUNT_SLOTS}


def render_playable_slots_summary(
    values: Mapping[str, str],
    *,
    now: datetime | None = None,
) -> str:
    timezone_name = values.get("TIMEZONE", "Europe/London").strip() or "Europe/London"
    timezone = ZoneInfo(timezone_name)
    local_now = datetime.now(timezone) if now is None else now.astimezone(timezone)
    confirmed_bookings = collect_confirmed_bookings(LOGS_DIR)
    attempted_dates = collect_attempted_booking_dates(LOGS_DIR)
    _, _, report_html = build_report(
        (),
        confirmed_bookings,
        local_now.date(),
        timezone_name,
        attempted_booking_dates=attempted_dates,
    )
    marker = "<h2>Playable Slots Summary</h2>"
    marker_index = report_html.find(marker)
    if marker_index < 0:
        raise ValueError("Playable Slots Summary table was not generated")
    return report_html[marker_index:]


def parse_bool(value: str | None, *, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def parse_court_order(value: str | None, default: Sequence[int]) -> tuple[int, ...]:
    try:
        parsed = tuple(int(item.strip()) for item in (value or "").split(",") if item.strip())
    except ValueError:
        return tuple(default)
    return parsed if sorted(parsed) == [1, 2, 3, 4] else tuple(default)


def parse_time_order(
    value: str | None,
    default: Sequence[str],
) -> tuple[str, ...]:
    if value is None:
        return tuple(default)
    parsed = tuple(item.strip() for item in value.split(",") if item.strip())
    if len(set(parsed)) != len(parsed):
        return tuple(default)
    if any(item not in TARGET_BOOKING_TIMES for item in parsed):
        return tuple(default)
    return parsed


def load_admin_values(env_file: Path = ENV_FILE) -> dict[str, str]:
    return {
        key: value or ""
        for key, value in dotenv_values(env_file).items()
        if key is not None
    }


def account_is_enabled(slot: AccountSlotSpec, values: Mapping[str, str]) -> bool:
    credentials_present = bool(
        values.get(slot.username_name, "").strip()
        and values.get(slot.password_name, "").strip()
    )
    default = True if slot.key == "account_a" else credentials_present
    return parse_bool(values.get(slot.enabled_name), default=default) and credentials_present


def quote_dotenv_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
    return f"'{escaped}'"


def update_dotenv(
    changes: Mapping[str, str],
    env_file: Path = ENV_FILE,
) -> None:
    """Atomically update selected dotenv keys without exposing their values."""

    with ENV_WRITE_LOCK:
        original = env_file.read_text(encoding="utf-8") if env_file.exists() else ""
        remaining = dict(changes)
        output: list[str] = []
        assignment = re.compile(r"^(\s*)(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
        for line in original.splitlines(keepends=True):
            match = assignment.match(line)
            key = match.group(2) if match else None
            if key not in remaining:
                output.append(line)
                continue
            newline = "\n" if line.endswith("\n") else ""
            output.append(f"{key}={quote_dotenv_value(remaining.pop(key))}{newline}")

        if output and not output[-1].endswith("\n"):
            output[-1] += "\n"
        for key, value in remaining.items():
            output.append(f"{key}={quote_dotenv_value(value)}\n")

        env_file.parent.mkdir(parents=True, exist_ok=True)
        existing_mode = (
            stat.S_IMODE(env_file.stat().st_mode) if env_file.exists() else 0o600
        )
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{env_file.name}.",
            dir=env_file.parent,
            text=True,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.writelines(output)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary_name, existing_mode & 0o600 or 0o600)
            os.replace(temporary_name, env_file)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)


def ensure_admin_token(token_file: Path = TOKEN_FILE) -> str:
    if token_file.exists():
        token = token_file.read_text(encoding="utf-8").strip()
        if len(token) >= 32:
            os.chmod(token_file, 0o600)
            return token
    token = secrets.token_urlsafe(32)
    token_file.write_text(token + "\n", encoding="utf-8")
    os.chmod(token_file, 0o600)
    return token


def build_admin_password_record(
    password: str,
    *,
    salt: bytes | None = None,
) -> str:
    if len(password) < 12:
        raise ValueError("The admin password must contain at least 12 characters")
    actual_salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        actual_salt,
        PASSWORD_HASH_ITERATIONS,
    )
    return f"pbkdf2_sha256${PASSWORD_HASH_ITERATIONS}${actual_salt.hex()}${digest.hex()}"


def verify_admin_password(password: str, record: str) -> bool:
    try:
        algorithm, iterations_text, salt_hex, expected_hex = record.strip().split("$")
        if algorithm != "pbkdf2_sha256":
            return False
        iterations = int(iterations_text)
        if iterations < PASSWORD_HASH_ITERATIONS:
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(expected_hex)
    except (TypeError, ValueError):
        return False
    actual = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        iterations,
    )
    return secrets.compare_digest(actual, expected)


def save_admin_password(
    password: str,
    password_file: Path = PASSWORD_FILE,
) -> None:
    record = build_admin_password_record(password)
    password_file.write_text(record + "\n", encoding="utf-8")
    os.chmod(password_file, 0o600)


def load_admin_password_record(password_file: Path = PASSWORD_FILE) -> str:
    if not password_file.exists():
        raise ValueError(
            f"Admin password file is missing: {password_file}. "
            "Create it with booking_admin.save_admin_password()."
        )
    os.chmod(password_file, 0o600)
    record = password_file.read_text(encoding="utf-8").strip()
    if not record:
        raise ValueError(f"Admin password file is empty: {password_file}")
    return record


def render_login_page(
    base_path: str = "",
    *,
    error: str = "",
) -> str:
    action = html.escape(base_path.rstrip("/") + "/login")
    error_html = f'<p class="error">{html.escape(error)}</p>' if error else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Badminton Booking Login</title>
<style>
  *{{box-sizing:border-box}} body{{margin:0;min-height:100vh;display:grid;place-items:center;background:#eef3f7;color:#182534;font:16px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}}
  main{{width:min(420px,calc(100% - 32px));background:white;border:1px solid #d8e0e8;border-radius:18px;padding:28px;box-shadow:0 14px 40px rgba(23,50,77,.12)}}
  h1{{margin:0 0 8px;color:#17324d;font-size:28px}} p{{color:#617184}} form{{display:grid;gap:14px;margin-top:22px}} label{{display:grid;gap:7px;font-weight:700}} input{{width:100%;border:1px solid #bdc9d5;border-radius:10px;padding:12px;font:inherit}} button{{border:0;border-radius:10px;padding:12px 16px;background:#2563eb;color:white;font:inherit;font-weight:800;cursor:pointer}} .error{{color:#b42318;background:#fff1f0;border:1px solid #ffc9c5;border-radius:10px;padding:10px 12px}}
</style></head><body><main><h1>Badminton Booking Control</h1><p>Enter the shared admin password to continue.</p>{error_html}
<form method="post" action="{action}"><label>Admin password<input name="admin_password" type="password" autocomplete="current-password" required maxlength="256" autofocus></label><button type="submit">Log in</button></form>
</main></body></html>"""


def _render_draggable_list(
    *,
    list_id: str,
    values: Sequence[str],
    hidden_name: str,
    checkable: bool = False,
    checked_values: Sequence[str] = (),
) -> str:
    items: list[str] = []
    for value in values:
        escaped = html.escape(value)
        checkbox = (
            f'<input type="checkbox" name="fallback_enabled" value="{escaped}"'
            f'{" checked" if value in checked_values else ""}>'
            if checkable
            else ""
        )
        label = f"Court {escaped}" if value.isdigit() else escaped
        items.append(
            f'<li draggable="true" data-value="{escaped}"><span class="handle">⋮⋮</span>'
            f"{checkbox}<span>{label}</span></li>"
        )
    return (
        f'<ul class="sortable" id="{html.escape(list_id)}">{"".join(items)}</ul>'
        f'<input type="hidden" name="{html.escape(hidden_name)}" '
        f'value="{html.escape(",".join(values))}">'
    )


def render_page(
    values: Mapping[str, str],
    csrf_token: str,
    message: str = "",
    base_path: str = "",
) -> str:
    action_prefix = html.escape(base_path.rstrip("/"))
    account_states: list[tuple[AccountSlotSpec, bool, str, str, tuple[int, ...]]] = []
    for slot in ACCOUNT_SLOTS:
        enabled = account_is_enabled(slot, values)
        username = values.get(slot.username_name, "").strip()
        primary = values.get(slot.primary_time_name, "").strip()
        if primary not in TARGET_BOOKING_TIMES:
            primary = slot.default_primary_time
        courts = parse_court_order(
            values.get(slot.court_order_name),
            slot.default_court_order,
        )
        account_states.append((slot, enabled, username, primary, courts))

    enabled_count = sum(enabled for _, enabled, _, _, _ in account_states)
    fallback_times = parse_time_order(
        values.get("BOOKING_FALLBACK_TIMES"),
        FALLBACK_TIMES,
    )
    fallback_display_order = tuple(fallback_times) + tuple(
        time for time in TARGET_BOOKING_TIMES if time not in fallback_times
    )

    cards: list[str] = []
    rule_rows: list[str] = []
    logic_rows: list[str] = []
    for slot, enabled, username, primary, courts in account_states:
        status_class = "online" if enabled else "offline"
        status_text = "Active" if enabled else "Logged out"
        if enabled:
            action = f"""
              <p class="identity">{html.escape(username)}</p>
              <p class="hint">Logging out stops future automated bookings. It does not cancel slots already confirmed.</p>
              <form method="post" action="{action_prefix}/account/logout" onsubmit="return confirm('Log out {slot.display_name}? Future booking runs will stop using this account and its saved password will be removed.');">
                <input type="hidden" name="csrf" value="{html.escape(csrf_token)}">
                <input type="hidden" name="account" value="{slot.key}">
                <button class="danger" type="submit">Log out</button>
              </form>
            """
        else:
            action = f"""
              <form method="post" action="{action_prefix}/account/login" class="login-form">
                <input type="hidden" name="csrf" value="{html.escape(csrf_token)}">
                <input type="hidden" name="account" value="{slot.key}">
                <label>Email or username<input name="username" type="text" autocomplete="username" required maxlength="254"></label>
                <label>Password<input name="password" type="password" autocomplete="new-password" required maxlength="512"></label>
                <button type="submit">Add and activate</button>
              </form>
            """
        cards.append(
            f"""
            <article class="account-card">
              <div class="account-heading"><h3>{slot.display_name}</h3><span class="status {status_class}">{status_text}</span></div>
              {action}
            </article>
            """
        )

        options = "".join(
            f'<option value="{time}"{" selected" if time == primary else ""}>{time}</option>'
            for time in TARGET_BOOKING_TIMES
        )
        rule_rows.append(
            f"""
            <div class="rule-card">
              <div><strong>{slot.display_name}</strong><small>{status_text}</small></div>
              <label>Primary time<select name="primary_{slot.key}">{options}</select></label>
              <label>Court order <span class="hint">drag to reorder</span>
                {_render_draggable_list(list_id=f'courts-{slot.key}', values=tuple(str(c) for c in courts), hidden_name=f'courts_{slot.key}')}
              </label>
            </div>
            """
        )
        if enabled:
            fallback_label = " → ".join(fallback_times) or "no fallback"
            logic_rows.append(
                f"<li><strong>{slot.display_name}</strong>: {primary} first; coordinated fallback pool "
                f"{html.escape(fallback_label)}; "
                f"Court {' → '.join(str(court) for court in courts)}</li>"
            )

    if not logic_rows:
        logic_rows.append("<li>No account is active. The midnight task will exit without opening the booking site.</li>")

    flash = f'<div class="flash">{html.escape(message)}</div>' if message else ""
    try:
        playable_slots_summary = render_playable_slots_summary(values)
    except Exception:  # noqa: BLE001 - keep the control page usable if a log is corrupt
        playable_slots_summary = (
            "<h2>Playable Slots Summary</h2>"
            '<p class="hint">The booking history is temporarily unavailable.</p>'
        )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Badminton Booking Control</title>
  <style>
    :root {{ color-scheme: light; --navy:#17324d; --blue:#2563eb; --green:#15803d; --red:#b42318; --line:#d8e0e8; --soft:#f4f7fa; }}
    * {{ box-sizing:border-box; }} body {{ margin:0; font:15px/1.5 system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:#182534; background:#eef3f7; }}
    main {{ width:min(1080px,calc(100% - 32px)); margin:40px auto 80px; }}
    header {{ margin-bottom:24px; }} h1 {{ margin:0 0 6px; color:var(--navy); font-size:clamp(28px,5vw,42px); }} h2 {{ margin:0 0 16px; color:var(--navy); }} h3 {{ margin:0; }}
    .subtitle,.hint,small {{ color:#617184; }} .summary {{ display:inline-flex; gap:10px; align-items:center; padding:9px 13px; border-radius:999px; background:#dbeafe; color:#1e40af; font-weight:700; }}
    .grid {{ display:grid; grid-template-columns:repeat(3,1fr); gap:16px; }} .account-card,.panel,.rule-card {{ background:white; border:1px solid var(--line); border-radius:16px; box-shadow:0 8px 28px rgba(23,50,77,.06); }}
    .account-card {{ padding:20px; min-height:210px; }} .account-heading {{ display:flex; justify-content:space-between; gap:12px; align-items:center; }}
    .status {{ padding:4px 9px; border-radius:999px; font-size:12px; font-weight:800; }} .online {{ background:#dcfce7; color:#166534; }} .offline {{ background:#f1f5f9; color:#64748b; }}
    .identity {{ overflow-wrap:anywhere; min-height:48px; margin:20px 0; }} .panel {{ margin-top:20px; padding:24px; }}
    label {{ display:grid; gap:6px; font-weight:650; }} input,select,button {{ font:inherit; }} input,select {{ width:100%; border:1px solid #bdc9d5; border-radius:9px; padding:10px 11px; background:white; }}
    button {{ border:0; border-radius:9px; padding:10px 14px; background:var(--blue); color:white; font-weight:750; cursor:pointer; }} button:hover {{ filter:brightness(.94); }} button.danger {{ background:white; color:var(--red); border:1px solid #f2b8b5; }}
    .login-form {{ display:grid; gap:11px; margin-top:16px; }} .rules {{ display:grid; gap:12px; }} .rule-card {{ display:grid; grid-template-columns:140px 180px 1fr; gap:20px; padding:16px; align-items:start; box-shadow:none; }} .rule-card small {{ display:block; }}
    .sortable {{ display:flex; flex-wrap:wrap; gap:8px; list-style:none; margin:4px 0 0; padding:0; }} .sortable li {{ display:flex; align-items:center; gap:7px; border:1px solid #cbd5e1; border-radius:9px; background:var(--soft); padding:8px 10px; cursor:grab; user-select:none; }} .sortable li.dragging {{ opacity:.35; }} .sortable input {{ width:auto; }} .handle {{ color:#8291a3; letter-spacing:-4px; margin-right:3px; }}
    .logic {{ margin:0; padding-left:22px; }} .logic li+li {{ margin-top:7px; }} .flash {{ background:#ecfdf3; color:#166534; border:1px solid #bbf7d0; padding:12px 15px; border-radius:12px; margin-bottom:18px; }} .save {{ margin-top:18px; min-width:180px; }}
    .notice {{ margin:16px 0 20px; padding:16px 18px; background:white; border:1px solid var(--line); border-radius:14px; }} .notice p {{ margin:0; }} .notice .urgent {{ margin-top:10px; padding:10px 12px; border-radius:9px; background:#fff2cc; color:#7a4b00; }}
    .schedule-table {{ overflow-x:auto; }} .schedule-table table {{ width:100%; min-width:650px; }}
    .security {{ font-size:13px; color:#64748b; margin-top:18px; }}
    @media(max-width:780px) {{ .grid {{ grid-template-columns:1fr; }} .rule-card {{ grid-template-columns:1fr; }} main {{ margin-top:24px; }} }}
  </style>
</head>
<body><main>
  <header><h1>Badminton Booking Control</h1>
    <div class="notice"><p>The accounts currently logged in will participate in booking badminton slots eight days ahead shortly after midnight each day. Accounts without a confirmed slot will retry at 08:00 and 16:00.</p><p class="urgent"><strong>If you cannot attend, please cancel at least four hours before the slot starts.</strong></p></div>
    <span class="summary">{enabled_count} active account{'s' if enabled_count != 1 else ''}</span>
  </header>
  {flash}
  <section class="grid">{''.join(cards)}</section>
  <section class="panel"><h2>Current booking logic</h2><ul class="logic">{''.join(logic_rows)}</ul></section>
  <section class="panel"><h2>Edit booking logic</h2>
    <form method="post" action="{action_prefix}/rules" id="rules-form">
      <input type="hidden" name="csrf" value="{html.escape(csrf_token)}">
      <div class="rules">{''.join(rule_rows)}</div>
      <div class="rule-card">
        <div><strong>Coordinated fallback</strong><small>All active accounts</small></div>
        <div class="hint">Tick hours to use. Drag them into the preferred tie-break order.</div>
        <label>Fallback hours {_render_draggable_list(list_id='fallback-times', values=fallback_display_order, hidden_name='fallback_order', checkable=True, checked_values=fallback_times)}</label>
      </div>
      <button class="save" type="submit">Save booking logic</button>
    </form>
    <p class="security">Changes are written atomically to the private local configuration and are used by the next booking run. Passwords are never displayed by this page.</p>
  </section>
  <section class="panel schedule-table">{playable_slots_summary}</section>
</main>
<script>
  document.querySelectorAll('.sortable').forEach(list => {{
    let dragged = null;
    list.addEventListener('dragstart', event => {{ dragged = event.target.closest('li'); if (dragged) dragged.classList.add('dragging'); }});
    list.addEventListener('dragend', () => {{ if (dragged) dragged.classList.remove('dragging'); dragged = null; sync(list); }});
    list.addEventListener('dragover', event => {{
      event.preventDefault(); if (!dragged) return;
      const target = event.target.closest('li'); if (!target || target === dragged) return;
      const box = target.getBoundingClientRect();
      list.insertBefore(dragged, event.clientX < box.left + box.width / 2 ? target : target.nextSibling);
    }});
    function sync(node) {{ node.nextElementSibling.value = [...node.querySelectorAll('li')].map(item => item.dataset.value).join(','); }}
  }});
  document.getElementById('rules-form').addEventListener('submit', () => document.querySelectorAll('.sortable').forEach(list => {{ list.nextElementSibling.value = [...list.querySelectorAll('li')].map(item => item.dataset.value).join(','); }}));
</script></body></html>"""


class BookingAdminHandler(BaseHTTPRequestHandler):
    server_version = "BadmintonAdmin/1.0"

    @property
    def admin_token(self) -> str:
        return self.server.admin_token  # type: ignore[attr-defined]

    @property
    def public_base_path(self) -> str:
        return self.server.public_base_path  # type: ignore[attr-defined]

    @property
    def admin_password_record(self) -> str:
        return self.server.admin_password_record  # type: ignore[attr-defined]

    @property
    def password_required(self) -> bool:
        return self.server.password_required  # type: ignore[attr-defined]

    def _internal_path(self, path: str) -> str:
        base_path = self.public_base_path
        if base_path and (path == base_path or path.startswith(base_path + "/")):
            stripped = path[len(base_path) :]
            return stripped or "/"
        return path

    def _public_location(self, path: str) -> str:
        normalized = "/" + path.lstrip("/")
        return self.public_base_path + normalized

    def log_message(self, format: str, *args: object) -> None:
        # Capability URLs contain the private access token. Do not copy request
        # targets into systemd or reverse-proxy-adjacent logs.
        return

    def _is_authenticated(self) -> bool:
        cookie = SimpleCookie(self.headers.get("Cookie", ""))
        morsel = cookie.get("badminton_admin")
        return bool(morsel and secrets.compare_digest(morsel.value, self.admin_token))

    def _login_client_key(self) -> str:
        forwarded = self.headers.get("X-Forwarded-For", "").split(",", 1)[0].strip()
        return forwarded[:128] or self.client_address[0]

    def _login_is_rate_limited(self) -> bool:
        return self.server.login_is_rate_limited(  # type: ignore[attr-defined]
            self._login_client_key()
        )

    def _record_login_failure(self) -> None:
        self.server.record_login_failure(self._login_client_key())  # type: ignore[attr-defined]

    def _clear_login_failures(self) -> None:
        self.server.clear_login_failures(self._login_client_key())  # type: ignore[attr-defined]

    def _send_html(
        self,
        content: str,
        *,
        status: HTTPStatus = HTTPStatus.OK,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        body = content.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Permissions-Policy",
            "camera=(), microphone=(), geolocation=(), payment=()",
        )
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
        )
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str, *, set_cookie: bool = False) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        if set_cookie:
            secure = (
                self.headers.get("X-Forwarded-Proto", "").lower() == "https"
                or self.headers.get("Host", "").split(":", 1)[0].endswith(".ts.net")
            )
            self.send_header(
                "Set-Cookie",
                f"badminton_admin={self.admin_token}; HttpOnly; SameSite=Strict; "
                f"Path=/; Max-Age=2592000{'; Secure' if secure else ''}",
            )
        self.end_headers()

    def _require_authentication(self) -> bool:
        if self._is_authenticated():
            return True
        self._send_html(
            render_login_page(
                self.public_base_path,
                error="Please log in before changing booking settings.",
            ),
            status=HTTPStatus.UNAUTHORIZED,
        )
        return False

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlsplit(self.path)
        internal_path = self._internal_path(parsed.path)
        if internal_path.startswith("/access/"):
            supplied_token = internal_path.removeprefix("/access/").strip("/")
            if supplied_token and secrets.compare_digest(
                supplied_token,
                self.admin_token,
            ):
                self._redirect(self._public_location("/"), set_cookie=True)
                return
            self._send_html(
                "<!doctype html><title>Invalid access link</title>"
                "<h1>Invalid or expired access link</h1>"
                "<p>Ask the owner for the latest complete URL.</p>",
                status=HTTPStatus.UNAUTHORIZED,
            )
            return
        if internal_path == "/health":
            self._send_html("ok")
            return
        if internal_path != "/":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        query = parse_qs(parsed.query)
        supplied_token = query.get("token", [""])[0]
        if supplied_token and secrets.compare_digest(supplied_token, self.admin_token):
            self._redirect(self._public_location("/"), set_cookie=True)
            return
        if self.password_required and not self._is_authenticated():
            self._send_html(render_login_page(self.public_base_path))
            return
        message = query.get("message", [""])[0][:240]
        self._send_html(
            render_page(
                load_admin_values(),
                self.admin_token,
                message,
                self.public_base_path,
            )
        )

    def _read_form(self) -> dict[str, list[str]] | None:
        if self.headers.get_content_type() != "application/x-www-form-urlencoded":
            self.send_error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return None
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(HTTPStatus.BAD_REQUEST)
            return None
        if content_length <= 0 or content_length > MAX_FORM_BYTES:
            self.send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            return None
        return parse_qs(
            self.rfile.read(content_length).decode("utf-8"),
            keep_blank_values=True,
        )

    def _validate_post(self, form: Mapping[str, list[str]]) -> bool:
        supplied = form.get("csrf", [""])[0]
        if not secrets.compare_digest(supplied, self.admin_token):
            self.send_error(HTTPStatus.FORBIDDEN)
            return False
        return True

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        path = self._internal_path(urlsplit(self.path).path)
        if path == "/login":
            form = self._read_form()
            if form is not None:
                self._admin_login(form)
            return
        if self.password_required and not self._require_authentication():
            return
        form = self._read_form()
        if form is None or not self._validate_post(form):
            return
        try:
            if path == "/account/logout":
                self._logout_account(form)
            elif path == "/account/login":
                self._login_account(form)
            elif path == "/rules":
                self._save_rules(form)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._send_html(
                f"<!doctype html><title>Invalid request</title><h1>Invalid request</h1><p>{html.escape(str(exc))}</p>",
                status=HTTPStatus.BAD_REQUEST,
            )

    def _admin_login(self, form: Mapping[str, list[str]]) -> None:
        if self._login_is_rate_limited():
            self._send_html(
                render_login_page(
                    self.public_base_path,
                    error="Too many failed attempts. Try again in 15 minutes.",
                ),
                status=HTTPStatus.TOO_MANY_REQUESTS,
            )
            return
        password = form.get("admin_password", [""])[0]
        if len(password) > 256 or not verify_admin_password(
            password,
            self.admin_password_record,
        ):
            self._record_login_failure()
            self._send_html(
                render_login_page(
                    self.public_base_path,
                    error="Incorrect admin password.",
                ),
                status=HTTPStatus.UNAUTHORIZED,
            )
            return
        self._clear_login_failures()
        self._redirect(self._public_location("/"), set_cookie=True)

    @staticmethod
    def _slot_from_form(form: Mapping[str, list[str]]) -> AccountSlotSpec:
        key = form.get("account", [""])[0]
        try:
            return ACCOUNT_SLOT_BY_KEY[key]
        except KeyError as exc:
            raise ValueError("Unknown account slot") from exc

    def _logout_account(self, form: Mapping[str, list[str]]) -> None:
        slot = self._slot_from_form(form)
        update_dotenv(
            {
                slot.enabled_name: "false",
                slot.password_name: "",
            }
        )
        self._redirect(
            self._public_location("/?")
            + urlencode(
                {
                    "message": (
                        f"{slot.display_name} logged out. Future runs will not use it."
                    )
                }
            )
        )

    def _login_account(self, form: Mapping[str, list[str]]) -> None:
        slot = self._slot_from_form(form)
        username = form.get("username", [""])[0].strip()
        password = form.get("password", [""])[0]
        if not username or len(username) > 254:
            raise ValueError("A valid username is required")
        if not password or len(password) > 512:
            raise ValueError("A valid password is required")
        if any(character in username for character in "\r\n"):
            raise ValueError("The username contains an invalid character")
        update_dotenv(
            {
                slot.enabled_name: "true",
                slot.username_name: username,
                slot.password_name: password,
            }
        )
        self._redirect(
            self._public_location("/?")
            + urlencode({"message": f"{slot.display_name} added and activated."})
        )

    def _save_rules(self, form: Mapping[str, list[str]]) -> None:
        changes: dict[str, str] = {}
        for slot in ACCOUNT_SLOTS:
            primary = form.get(f"primary_{slot.key}", [""])[0]
            if primary not in TARGET_BOOKING_TIMES:
                raise ValueError(f"Invalid primary time for {slot.display_name}")
            court_order = parse_court_order(
                form.get(f"courts_{slot.key}", [""])[0],
                (),
            )
            if sorted(court_order) != [1, 2, 3, 4]:
                raise ValueError(f"Invalid court order for {slot.display_name}")
            changes[slot.primary_time_name] = primary
            changes[slot.court_order_name] = ",".join(str(court) for court in court_order)

        raw_order = parse_time_order(
            form.get("fallback_order", [""])[0],
            (),
        )
        enabled = set(form.get("fallback_enabled", []))
        if any(time not in TARGET_BOOKING_TIMES for time in enabled):
            raise ValueError("Invalid fallback time")
        changes["BOOKING_FALLBACK_TIMES"] = ",".join(
            time for time in raw_order if time in enabled
        )
        update_dotenv(changes)
        self._redirect(
            self._public_location("/?")
            + urlencode({"message": "Booking logic saved for the next run."})
        )


class BookingAdminServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        token: str,
        password_record: str = "",
        public_base_path: str = DEFAULT_PUBLIC_PATH,
        password_required: bool = False,
    ):
        self.admin_token = token
        self.admin_password_record = password_record
        self.password_required = password_required
        self.public_base_path = public_base_path.rstrip("/")
        self.login_failures: dict[str, list[float]] = {}
        self.login_failure_lock = threading.Lock()
        super().__init__(address, BookingAdminHandler)

    def _recent_login_failures(self, client_key: str) -> list[float]:
        cutoff = time.monotonic() - LOGIN_WINDOW_SECONDS
        return [
            attempt
            for attempt in self.login_failures.get(client_key, [])
            if attempt >= cutoff
        ]

    def login_is_rate_limited(self, client_key: str) -> bool:
        with self.login_failure_lock:
            recent = self._recent_login_failures(client_key)
            self.login_failures[client_key] = recent
            return len(recent) >= MAX_LOGIN_FAILURES

    def record_login_failure(self, client_key: str) -> None:
        with self.login_failure_lock:
            recent = self._recent_login_failures(client_key)
            recent.append(time.monotonic())
            self.login_failures[client_key] = recent

    def clear_login_failures(self, client_key: str) -> None:
        with self.login_failure_lock:
            self.login_failures.pop(client_key, None)


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local badminton booking account control")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--base-path", default=DEFAULT_PUBLIC_PATH)
    parser.add_argument(
        "--allow-network",
        action="store_true",
        help="allow a non-loopback bind (not recommended without a reverse proxy)",
    )
    parser.add_argument("--print-url", action="store_true")
    parser.add_argument(
        "--set-password",
        action="store_true",
        help="prompt for and replace the shared admin password, then exit",
    )
    parser.add_argument(
        "--require-password",
        action="store_true",
        help="require the shared admin password before showing the control page",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    if args.set_password:
        password = getpass.getpass("New admin password: ")
        confirmation = getpass.getpass("Confirm admin password: ")
        if password != confirmation:
            raise SystemExit("Passwords did not match")
        save_admin_password(password)
        print("Admin password updated.")
        return 0
    if args.host not in {"127.0.0.1", "::1", "localhost"} and not args.allow_network:
        raise SystemExit("Refusing a non-loopback bind without --allow-network")
    token = ensure_admin_token()
    password_record = load_admin_password_record() if args.require_password else ""
    base_path = "/" + args.base_path.strip("/") if args.base_path.strip("/") else ""
    server = BookingAdminServer(
        (args.host, args.port),
        token,
        password_record,
        base_path,
        args.require_password,
    )
    private_url = (
        f"http://{args.host}:{server.server_port}{base_path}/access/{token}"
    )
    if args.print_url:
        print(private_url, flush=True)
    else:
        print(
            f"Badminton booking control is listening on {args.host}:{server.server_port}",
            flush=True,
        )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
