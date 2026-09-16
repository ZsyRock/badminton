from __future__ import annotations

import ast
import html
import json
import logging
import os
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo


ACCOUNT_LABELS = ("账号A", "账号B", "账号C")
ACCOUNT_LABEL_PATTERN = r"账号[ABC]"
DEFAULT_TIMEZONE = "Europe/London"
MANUAL_BOOKINGS_FILENAME = "manual_bookings.json"
REPORT_TIMES = ("15:00", "16:00", "17:00", "18:00", "19:00")
TARGET_DATE_PATTERN = re.compile(
    rf"\[({ACCOUNT_LABEL_PATTERN})\].*Target booking date is (\d{{2}}/\d{{2}}/\d{{4}})"
)
SUCCESS_PATTERN = re.compile(
    rf"\[({ACCOUNT_LABEL_PATTERN})\].*Success: confirmed booking for "
    r"(\d{2}:\d{2}) Jubilee Court (\d+)"
)
FAILURE_PATTERN = re.compile(
    rf"\[({ACCOUNT_LABEL_PATTERN})\].*Booking run failed: (.+)"
)
NO_SLOT_PATTERN = re.compile(
    rf"\[({ACCOUNT_LABEL_PATTERN})\].*No preferred slots were available to book\."
)
DRY_RUN_ENABLED_PATTERN = re.compile(r"\bDRY_RUN\s*=\s*True\b", re.IGNORECASE)
LEGACY_TARGET_DATE_PATTERN = re.compile(r"Target booking date is (\d{2}/\d{2}/\d{4})")
LEGACY_SUCCESS_PATTERN = re.compile(
    r"Success: confirmed booking for (\d{2}:\d{2}) Jubilee Court (\d+)"
)


@dataclass(frozen=True)
class ConfirmedBooking:
    booking_date: date
    account_label: str
    start_time: str
    court_number: int


@dataclass(frozen=True)
class AccountRunResult:
    account_label: str
    target_date: date | None
    booking: ConfirmedBooking | None
    detail: str


@dataclass(frozen=True)
class EmailSettings:
    sender: str
    password: str
    recipients: tuple[str, ...]
    smtp_host: str
    smtp_port: int


def email_notifications_enabled() -> bool:
    value = os.getenv("BOOKING_EMAIL_ENABLED", "false").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _read_legacy_string_constants(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    values: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        try:
            value = ast.literal_eval(node.value)
        except (ValueError, TypeError):
            value = None
            call = node.value
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "os"
                and call.func.attr == "getenv"
                and 1 <= len(call.args) <= 2
                and not call.keywords
            ):
                try:
                    environment_name = ast.literal_eval(call.args[0])
                    default_value = (
                        ast.literal_eval(call.args[1]) if len(call.args) == 2 else ""
                    )
                except (ValueError, TypeError):
                    continue
                if isinstance(environment_name, str) and isinstance(default_value, str):
                    value = os.getenv(environment_name, default_value)
        if isinstance(value, str):
            values[target.id] = value
    return values


def load_email_settings() -> EmailSettings:
    reference_path = Path(
        os.getenv(
            "BOOKING_EMAIL_REFERENCE_SCRIPT",
            str(Path.home() / "send_ip_email.py"),
        )
    ).expanduser()
    legacy = _read_legacy_string_constants(reference_path)

    sender = os.getenv("BOOKING_EMAIL_FROM", legacy.get("SENDER_EMAIL", "")).strip()
    password = os.getenv(
        "BOOKING_EMAIL_APP_PASSWORD",
        legacy.get("APP_PASSWORD", ""),
    ).strip()
    password_file_value = os.getenv(
        "BOOKING_EMAIL_APP_PASSWORD_FILE",
        "",
    ).strip()
    if not password and password_file_value:
        password_file = Path(password_file_value).expanduser()
        try:
            password = password_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError(
                f"Could not read booking email app password file: {password_file}"
            ) from exc
    recipient_value = os.getenv(
        "BOOKING_EMAIL_TO",
        legacy.get("RECEIVER_EMAIL", ""),
    ).strip()
    recipients = tuple(
        address.strip()
        for address in re.split(r"[,;]", recipient_value)
        if address.strip()
    )
    smtp_host = os.getenv("BOOKING_EMAIL_SMTP_HOST", "smtp.gmail.com").strip()
    smtp_port = int(os.getenv("BOOKING_EMAIL_SMTP_PORT", "465"))

    missing = [
        name
        for name, value in (
            ("sender", sender),
            ("app password", password),
            ("recipient", recipients),
        )
        if not value
    ]
    if missing:
        raise ValueError(
            "Missing booking email configuration: " + ", ".join(missing)
        )

    return EmailSettings(
        sender=sender,
        password=password,
        recipients=recipients,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
    )


def parse_run_log(log_path: Path) -> tuple[AccountRunResult, ...]:
    text = log_path.read_text(encoding="utf-8", errors="replace")
    target_dates: dict[str, date] = {}
    bookings: dict[str, ConfirmedBooking] = {}
    details: dict[str, str] = {}

    for account_label, raw_date in TARGET_DATE_PATTERN.findall(text):
        target_dates[account_label] = datetime.strptime(raw_date, "%d/%m/%Y").date()

    is_legacy_single_account_log = "Configured booking accounts:" not in text
    if is_legacy_single_account_log:
        legacy_target_matches = LEGACY_TARGET_DATE_PATTERN.findall(text)
        if legacy_target_matches:
            target_dates["账号A"] = datetime.strptime(
                legacy_target_matches[-1],
                "%d/%m/%Y",
            ).date()

    for account_label, start_time, court_number in SUCCESS_PATTERN.findall(text):
        target_date = target_dates.get(account_label)
        if target_date is None:
            continue
        bookings[account_label] = ConfirmedBooking(
            booking_date=target_date,
            account_label=account_label,
            start_time=start_time,
            court_number=int(court_number),
        )
        details[account_label] = "confirmed"

    if is_legacy_single_account_log and "账号A" in target_dates:
        legacy_success_matches = LEGACY_SUCCESS_PATTERN.findall(text)
        if legacy_success_matches:
            start_time, court_number = legacy_success_matches[-1]
            bookings["账号A"] = ConfirmedBooking(
                booking_date=target_dates["账号A"],
                account_label="账号A",
                start_time=start_time,
                court_number=int(court_number),
            )
            details["账号A"] = "confirmed"

    for account_label, failure_detail in FAILURE_PATTERN.findall(text):
        if account_label not in bookings:
            details[account_label] = f"failed: {failure_detail.strip()}"

    for account_label in NO_SLOT_PATTERN.findall(text):
        if account_label not in bookings and account_label not in details:
            details[account_label] = "no preferred slot was available"

    labels = [label for label in ACCOUNT_LABELS if label in target_dates or f"[{label}]" in text]
    return tuple(
        AccountRunResult(
            account_label=label,
            target_date=target_dates.get(label),
            booking=bookings.get(label),
            detail=details.get(label, "no confirmed booking was recorded"),
        )
        for label in labels
    )


def _latest_booking_per_account_and_date(
    bookings: Iterable[ConfirmedBooking],
) -> tuple[ConfirmedBooking, ...]:
    latest_by_date_and_account: dict[tuple[date, str], ConfirmedBooking] = {}
    for booking in bookings:
        key = (booking.booking_date, booking.account_label)
        latest_by_date_and_account[key] = booking
    return tuple(
        sorted(
            latest_by_date_and_account.values(),
            key=lambda item: (
                item.booking_date,
                item.start_time,
                item.court_number,
                item.account_label,
            ),
        )
    )


def load_manual_bookings(path: Path) -> tuple[ConfirmedBooking, ...]:
    if not path.is_file():
        return ()

    try:
        raw_bookings = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read manual booking corrections from {path}") from exc
    if not isinstance(raw_bookings, list):
        raise ValueError(f"Manual booking corrections in {path} must be a JSON list")

    bookings: list[ConfirmedBooking] = []
    for index, entry in enumerate(raw_bookings, start=1):
        if not isinstance(entry, dict):
            raise ValueError(f"Manual booking correction #{index} must be an object")
        try:
            booking_date = datetime.strptime(
                str(entry["booking_date"]),
                "%Y-%m-%d",
            ).date()
            account_label = str(entry["account_label"])
            start_time = datetime.strptime(
                str(entry["start_time"]),
                "%H:%M",
            ).strftime("%H:%M")
            court_number = int(entry["court_number"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"Manual booking correction #{index} has invalid fields"
            ) from exc
        if account_label not in ACCOUNT_LABELS:
            raise ValueError(
                f"Manual booking correction #{index} has unsupported account "
                f"label {account_label!r}"
            )
        if court_number not in {1, 2, 3, 4}:
            raise ValueError(
                f"Manual booking correction #{index} has unsupported Court "
                f"number {court_number}"
            )
        bookings.append(
            ConfirmedBooking(
                booking_date=booking_date,
                account_label=account_label,
                start_time=start_time,
                court_number=court_number,
            )
        )
    return _latest_booking_per_account_and_date(bookings)


def apply_manual_bookings_to_run_results(
    run_results: tuple[AccountRunResult, ...],
    manual_bookings: tuple[ConfirmedBooking, ...],
) -> tuple[AccountRunResult, ...]:
    overrides = {
        (booking.booking_date, booking.account_label): booking
        for booking in manual_bookings
    }
    updated: list[AccountRunResult] = []
    for result in run_results:
        override = (
            overrides.get((result.target_date, result.account_label))
            if result.target_date is not None
            else None
        )
        if override is None:
            updated.append(result)
            continue
        updated.append(
            AccountRunResult(
                account_label=result.account_label,
                target_date=result.target_date,
                booking=override,
                detail="manually corrected",
            )
        )
    return tuple(updated)


def collect_confirmed_bookings(
    logs_dir: Path,
    manual_bookings_path: Path | None = None,
) -> tuple[ConfirmedBooking, ...]:
    collected: list[ConfirmedBooking] = []
    for log_path in sorted(logs_dir.glob("book-badminton-*.log")):
        try:
            run_results = parse_run_log(log_path)
        except (OSError, ValueError):
            continue
        for result in run_results:
            if result.booking is None:
                continue
            collected.append(result.booking)
    corrections_path = manual_bookings_path or (
        logs_dir.parent / MANUAL_BOOKINGS_FILENAME
    )
    collected.extend(load_manual_bookings(corrections_path))
    return _latest_booking_per_account_and_date(collected)


def collect_attempted_booking_dates(logs_dir: Path) -> tuple[date, ...]:
    """Collect real-run target dates even when no account secured a slot."""

    attempted_dates: set[date] = set()
    for log_path in sorted(logs_dir.glob("book-badminton-*.log")):
        try:
            log_text = log_path.read_text(encoding="utf-8", errors="replace")
            if DRY_RUN_ENABLED_PATTERN.search(log_text):
                continue
            run_results = parse_run_log(log_path)
        except (OSError, ValueError):
            continue
        attempted_dates.update(
            result.target_date
            for result in run_results
            if result.target_date is not None
        )
    return tuple(sorted(attempted_dates))


def _bookings_by_date_and_time(
    bookings: Iterable[ConfirmedBooking],
) -> dict[date, dict[str, list[ConfirmedBooking]]]:
    grouped: dict[date, dict[str, list[ConfirmedBooking]]] = {}
    for booking in bookings:
        grouped.setdefault(booking.booking_date, {}).setdefault(
            booking.start_time,
            [],
        ).append(booking)
    return grouped


def _format_slot_cell(bookings: Iterable[ConfirmedBooking]) -> str:
    court_numbers = sorted({booking.court_number for booking in bookings})
    if not court_numbers:
        return ""
    return "Court " + " + ".join(str(court_number) for court_number in court_numbers)


def _time_as_minutes(start_time: str) -> int:
    parsed = datetime.strptime(start_time, "%H:%M")
    return parsed.hour * 60 + parsed.minute


def _join_with_and(parts: list[str]) -> str:
    if len(parts) <= 1:
        return "".join(parts)
    if len(parts) == 2:
        return " and ".join(parts)
    return ", ".join(parts[:-1]) + f", and {parts[-1]}"


def _format_result_courts(court_numbers: Iterable[int]) -> str:
    ordered = sorted(set(court_numbers))
    if len(ordered) == 1:
        return f"Court {ordered[0]}"
    if len(ordered) == 2:
        return f"Court {ordered[0]} and {ordered[1]}"
    return "Court " + _join_with_and([str(number) for number in ordered])


def _has_multiple_courts_at_same_time(
    bookings: Iterable[ConfirmedBooking],
) -> bool:
    courts_by_time: dict[str, set[int]] = {}
    for booking in bookings:
        courts_by_time.setdefault(booking.start_time, set()).add(
            booking.court_number
        )
    return any(len(courts) > 1 for courts in courts_by_time.values())


def _booking_result_groups(
    bookings: Iterable[ConfirmedBooking],
) -> list[tuple[str, tuple[ConfirmedBooking, ...]]]:
    ordered = sorted(
        bookings,
        key=lambda booking: (booking.start_time, booking.court_number),
    )
    if _has_multiple_courts_at_same_time(ordered):
        bookings_by_time: dict[str, list[ConfirmedBooking]] = {}
        for booking in ordered:
            bookings_by_time.setdefault(booking.start_time, []).append(booking)
        return [
            (
                f"{start_time} at "
                f"{_format_result_courts(booking.court_number for booking in grouped)}",
                tuple(grouped),
            )
            for start_time, grouped in bookings_by_time.items()
        ]

    groups: list[tuple[str, tuple[ConfirmedBooking, ...]]] = []
    index = 0
    while index < len(ordered):
        group = [ordered[index]]
        next_index = index + 1
        while next_index < len(ordered):
            previous = group[-1]
            candidate = ordered[next_index]
            if (
                candidate.court_number != previous.court_number
                or _time_as_minutes(candidate.start_time)
                - _time_as_minutes(previous.start_time)
                != 60
            ):
                break
            group.append(candidate)
            next_index += 1

        if len(group) > 1:
            times_text = _join_with_and([booking.start_time for booking in group])
            phrase = f"{times_text} at Court {group[0].court_number}"
        else:
            booking = group[0]
            phrase = f"{booking.start_time} at Court {booking.court_number}"
        groups.append((phrase, tuple(group)))
        index = next_index

    return groups


def format_booking_result(bookings: Iterable[ConfirmedBooking]) -> str:
    phrases = [phrase for phrase, _ in _booking_result_groups(bookings)]
    return _join_with_and(phrases)


def _get_consecutive_times(start_times: Iterable[str]) -> set[str]:
    ordered_times = sorted(set(start_times), key=_time_as_minutes)
    consecutive: set[str] = set()
    for earlier_time, later_time in zip(ordered_times, ordered_times[1:]):
        if _time_as_minutes(later_time) - _time_as_minutes(earlier_time) == 60:
            consecutive.update((earlier_time, later_time))
    return consecutive


def format_booking_result_html(bookings: Iterable[ConfirmedBooking]) -> str:
    return _join_with_and(_format_booking_result_html_groups(bookings))


def _format_booking_result_html_groups(
    bookings: Iterable[ConfirmedBooking],
) -> list[str]:
    groups = _booking_result_groups(bookings)
    consecutive_times = _get_consecutive_times(
        booking.start_time
        for _, grouped_bookings in groups
        for booking in grouped_bookings
    )
    coloured_phrases: list[str] = []
    for phrase, grouped_bookings in groups:
        is_consecutive = any(
            booking.start_time in consecutive_times for booking in grouped_bookings
        )
        colour = "#e2f0d9" if is_consecutive else "#fff2cc"
        coloured_phrases.append(
            f'<span style="background:{colour};padding:3px 6px">'
            f"{html.escape(phrase)}</span>"
        )

    return coloured_phrases


def build_report(
    run_results: tuple[AccountRunResult, ...],
    confirmed_bookings: tuple[ConfirmedBooking, ...],
    report_date: date,
    timezone_name: str = DEFAULT_TIMEZONE,
    attempted_booking_dates: Iterable[date] = (),
) -> tuple[str, str, str]:
    confirmed_bookings = _latest_booking_per_account_and_date(confirmed_bookings)
    grouped = _bookings_by_date_and_time(confirmed_bookings)
    future_schedule_dates = [
        booking.booking_date
        for booking in confirmed_bookings
        if booking.booking_date >= report_date
    ]
    future_schedule_dates.extend(
        attempted_date
        for attempted_date in attempted_booking_dates
        if attempted_date >= report_date
    )
    future_schedule_dates.extend(
        result.target_date
        for result in run_results
        if result.target_date is not None and result.target_date >= report_date
    )
    latest_schedule_date = max(future_schedule_dates, default=report_date)
    subject = (
        f"[Badminton Booking Report] {report_date:%a %d %b} - "
        f"{latest_schedule_date:%a %d %b}"
    )
    schedule_dates = [
        report_date + timedelta(days=offset)
        for offset in range((latest_schedule_date - report_date).days + 1)
    ]
    times = REPORT_TIMES

    target_dates = [result.target_date for result in run_results if result.target_date]
    target_date = target_dates[0] if target_dates else None
    if target_date is not None:
        target_text = f"{target_date:%A}, {target_date.day} {target_date:%B}"
        if target_date > report_date:
            target_text = f"next {target_text}"
    else:
        target_text = "the requested date"
    current_bookings = sorted(
        (result.booking for result in run_results if result.booking is not None),
        key=lambda booking: (booking.start_time, booking.court_number),
    )
    if current_bookings:
        if _has_multiple_courts_at_same_time(current_bookings):
            result_groups = _booking_result_groups(current_bookings)
            html_groups = _format_booking_result_html_groups(current_bookings)
            result_detail_lines = [
                f"{phrase}{',' if index < len(result_groups) - 1 else '.'}"
                for index, (phrase, _) in enumerate(result_groups)
            ]
            result_detail_html = "<br>".join(
                f"{phrase}{',' if index < len(html_groups) - 1 else '.'}"
                for index, phrase in enumerate(html_groups)
            )
        else:
            booking_text = format_booking_result(current_bookings)
            result_detail_lines = [f"{booking_text}."]
            result_detail_html = f"{format_booking_result_html(current_bookings)}."
    else:
        result_detail_lines = ["No slot was successfully booked."]
        result_detail_html = (
            '<span style="background:#fff2cc;padding:3px 6px">'
            "No slot was successfully booked.</span>"
        )
    result_prefix = f"The slot booking result for {target_text} is:"

    plain_lines = [
        result_prefix,
        *result_detail_lines,
        "",
        "Playable Slots Summary",
        "",
    ]
    table_rows: list[str] = []
    for schedule_date in schedule_dates:
        by_time = grouped.get(schedule_date, {})
        consecutive_times = _get_consecutive_times(
            start_time for start_time in times if by_time.get(start_time)
        )
        plain_slots = []
        html_cells = []
        for start_time in times:
            slot_bookings = by_time.get(start_time, [])
            formatted_slot = _format_slot_cell(slot_bookings)
            plain_value = formatted_slot or "—"
            html_value = html.escape(formatted_slot) if formatted_slot else "—"
            if start_time in consecutive_times:
                cell_colour = "#e2f0d9"
            elif slot_bookings:
                cell_colour = "#fff2cc"
            else:
                cell_colour = "#ffffff"
            plain_slots.append(f"{start_time}: {plain_value}")
            html_cells.append(
                f'<td style="padding:8px;border:1px solid #bbb;text-align:center;'
                f'background:{cell_colour}">{html_value}</td>'
            )

        plain_lines.append(
            f"{schedule_date:%a %d %b}: " + " | ".join(plain_slots)
        )
        table_rows.append(
            "<tr>"
            f'<td style="padding:8px;border:1px solid #bbb;white-space:nowrap">{schedule_date:%a %d %b}</td>'
            + "".join(html_cells)
            + "</tr>"
        )

    html_body = (
        '<p style="font-size:20px;line-height:1.6">'
        f"{html.escape(result_prefix)}<br>{result_detail_html}</p>"
        "<h2>Playable Slots Summary</h2>"
        '<table style="border-collapse:collapse;font-family:Arial,sans-serif;font-size:14px">'
        '<thead><tr style="background:#d9eaf7">'
        '<th style="padding:8px;border:1px solid #bbb">Date</th>'
        + "".join(
            f'<th style="padding:8px;border:1px solid #bbb">{time}</th>' for time in times
        )
        + "</tr></thead><tbody>"
        + "".join(table_rows)
        + "</tbody></table>"
    )
    return subject, "\n".join(plain_lines), html_body


def build_email_message(
    settings: EmailSettings,
    subject: str,
    plain_body: str,
    html_body: str,
) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = f"Badminton Slots <{settings.sender}>"
    message["To"] = ", ".join(settings.recipients)
    message.set_content(plain_body)
    message.add_alternative(html_body, subtype="html")
    return message


def send_booking_report(
    current_log_path: Path,
    logs_dir: Path,
    timezone_name: str,
    logger: logging.Logger,
) -> None:
    settings = load_email_settings()
    manual_bookings_path = logs_dir.parent / MANUAL_BOOKINGS_FILENAME
    manual_bookings = load_manual_bookings(manual_bookings_path)
    run_results = apply_manual_bookings_to_run_results(
        parse_run_log(current_log_path),
        manual_bookings,
    )
    confirmed_bookings = collect_confirmed_bookings(
        logs_dir,
        manual_bookings_path=manual_bookings_path,
    )
    attempted_booking_dates = collect_attempted_booking_dates(logs_dir)
    report_date = datetime.now(ZoneInfo(timezone_name)).date()
    subject, plain_body, html_body = build_report(
        run_results,
        confirmed_bookings,
        report_date,
        timezone_name,
        attempted_booking_dates=attempted_booking_dates,
    )

    message = build_email_message(
        settings,
        subject,
        plain_body,
        html_body,
    )

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(
        settings.smtp_host,
        settings.smtp_port,
        timeout=20,
        context=context,
    ) as server:
        server.login(settings.sender, settings.password)
        server.send_message(message)
    logger.info(
        "Booking report email sent successfully to %s",
        ", ".join(settings.recipients),
    )
