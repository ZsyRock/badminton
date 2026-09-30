from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import os
import re
import smtplib
import ssl
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from booking_email import (
    MANUAL_BOOKINGS_FILENAME,
    ConfirmedBooking,
    EmailSettings,
    collect_confirmed_bookings,
    load_email_settings,
)


PROJECT_DIR = Path(__file__).resolve().parent
ENV_FILE = PROJECT_DIR / ".env"
LOGS_DIR = PROJECT_DIR / "logs"
STATE_FILE = PROJECT_DIR / "cancellation_reminder_state.json"
DEFAULT_TIMEZONE = "Europe/London"
REMINDER_LEAD_TIME = timedelta(hours=5)
CANCELLATION_NOTICE = timedelta(hours=4)
REMINDER_SUMMARY = "Badminton Cancellation DDL (1h left)"
BOOKING_URL = "https://soton.gladstonego.cloud/account"
STATE_VERSION = 2
ACCOUNT_EMAIL_ENV_NAMES = {
    "账号A": "GYM_USERNAME",
    "账号B": "SECONDARY_GYM_USERNAME",
    "账号C": "TERTIARY_GYM_USERNAME",
}


@dataclass(frozen=True)
class SlotReminder:
    booking_date: date
    start_time: str
    bookings: tuple[ConfirmedBooking, ...]

    @property
    def court_numbers(self) -> tuple[int, ...]:
        return tuple(sorted({booking.court_number for booking in self.bookings}))

    @property
    def account_label(self) -> str:
        account_labels = {booking.account_label for booking in self.bookings}
        if len(account_labels) != 1:
            raise ValueError(
                "Each cancellation reminder must belong to exactly one booking "
                "account"
            )
        return next(iter(account_labels))


def cancellation_reminders_enabled() -> bool:
    value = os.getenv(
        "BOOKING_CANCELLATION_REMINDER_ENABLED",
        "false",
    ).strip().lower()
    return value in {"1", "true", "yes", "on"}


def parse_recipient_list(value: str) -> tuple[str, ...]:
    recipients: list[str] = []
    seen: set[str] = set()
    for raw_address in re.split(r"[,;]", value):
        address = raw_address.strip()
        normalized = address.casefold()
        if not address or normalized in seen:
            continue
        if "\r" in address or "\n" in address or "@" not in address:
            raise ValueError(f"Invalid cancellation-reminder recipient: {address!r}")
        recipients.append(address)
        seen.add(normalized)
    return tuple(recipients)


def load_account_recipient_map() -> dict[str, str]:
    recipients: dict[str, str] = {}
    for account_label, environment_name in ACCOUNT_EMAIL_ENV_NAMES.items():
        configured = os.getenv(environment_name, "").strip()
        if not configured:
            continue
        parsed = parse_recipient_list(configured)
        if len(parsed) != 1:
            raise ValueError(
                f"{environment_name} must contain exactly one account email address"
            )
        recipients[account_label] = parsed[0]
    return recipients


def group_confirmed_bookings(
    bookings: Iterable[ConfirmedBooking],
) -> tuple[SlotReminder, ...]:
    grouped: dict[tuple[date, str, str], list[ConfirmedBooking]] = {}
    for booking in bookings:
        grouped.setdefault(
            (
                booking.booking_date,
                booking.start_time,
                booking.account_label,
            ),
            [],
        ).append(booking)

    return tuple(
        SlotReminder(
            booking_date=booking_date,
            start_time=start_time,
            bookings=tuple(
                sorted(
                    group,
                    key=lambda booking: (
                        booking.court_number,
                        booking.account_label,
                    ),
                )
            ),
        )
        for (booking_date, start_time, _account_label), group in sorted(grouped.items())
    )


def slot_start_datetime(
    reminder: SlotReminder,
    timezone_name: str,
) -> datetime:
    parsed_time = datetime.strptime(reminder.start_time, "%H:%M").time()
    return datetime.combine(
        reminder.booking_date,
        time(parsed_time.hour, parsed_time.minute),
        tzinfo=ZoneInfo(timezone_name),
    )


def cancellation_window(
    reminder: SlotReminder,
    timezone_name: str,
) -> tuple[datetime, datetime]:
    slot_start = slot_start_datetime(reminder, timezone_name)
    return (
        slot_start - REMINDER_LEAD_TIME,
        slot_start - CANCELLATION_NOTICE,
    )


def reminder_is_due(
    reminder: SlotReminder,
    now: datetime,
    timezone_name: str,
) -> bool:
    if now.tzinfo is None:
        raise ValueError("Reminder evaluation time must be timezone-aware")
    local_now = now.astimezone(ZoneInfo(timezone_name))
    reminder_at, cancellation_deadline = cancellation_window(
        reminder,
        timezone_name,
    )
    return reminder_at <= local_now < cancellation_deadline


def _format_courts(court_numbers: Sequence[int]) -> str:
    labels = [f"Court {court}" for court in court_numbers]
    if len(labels) <= 1:
        return labels[0]
    if len(labels) == 2:
        return " and ".join(labels)
    return ", ".join(labels[:-1]) + f", and {labels[-1]}"


def _ical_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
    )


def _fold_ical_line(line: str) -> list[str]:
    """Fold one iCalendar line without splitting a UTF-8 code point."""

    folded: list[str] = []
    current = ""
    byte_limit = 75
    for character in line:
        candidate = current + character
        if current and len(candidate.encode("utf-8")) > byte_limit:
            folded.append(current)
            current = " " + character
        else:
            current = candidate
    folded.append(current)
    return folded


def reminder_delivery_key(reminder: SlotReminder, recipient: str) -> str:
    return "|".join(
        (
            reminder.booking_date.isoformat(),
            reminder.start_time,
            reminder.account_label,
            recipient.strip().casefold(),
        )
    )


def reminder_uid(
    reminder: SlotReminder,
    recipient: str,
    namespace: str = "production",
) -> str:
    digest = hashlib.sha256(
        f"{namespace}|{reminder_delivery_key(reminder, recipient)}".encode("utf-8")
    ).hexdigest()[:24]
    return f"badminton-cancellation-{digest}@badminton-slots.local"


def build_calendar_invitation(
    reminder: SlotReminder,
    recipient: str,
    sender: str,
    timezone_name: str,
    generated_at: datetime | None = None,
    uid_namespace: str = "production",
) -> str:
    generated = generated_at or datetime.now(timezone.utc)
    if generated.tzinfo is None:
        raise ValueError("Calendar generation time must be timezone-aware")

    slot_start = slot_start_datetime(reminder, timezone_name)
    reminder_at, cancellation_deadline = cancellation_window(
        reminder,
        timezone_name,
    )
    courts = _format_courts(reminder.court_numbers)
    description = (
        f"Badminton starts at {slot_start:%H:%M} on {slot_start:%A, %d %B %Y} "
        f"at {courts}. Cancel before {cancellation_deadline:%H:%M} if you will "
        "not play; this event is the final one-hour cancellation window. "
        f"Manage bookings: {BOOKING_URL}"
    )
    lines = [
        "BEGIN:VCALENDAR",
        "PRODID:-//Badminton Slots//Cancellation Reminder//EN",
        "VERSION:2.0",
        "CALSCALE:GREGORIAN",
        "METHOD:REQUEST",
        "BEGIN:VEVENT",
        f"UID:{reminder_uid(reminder, recipient, uid_namespace)}",
        f"DTSTAMP:{generated.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}",
        f"DTSTART:{reminder_at.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}",
        f"DTEND:{cancellation_deadline.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}",
        f"SUMMARY:{_ical_escape(REMINDER_SUMMARY)}",
        f"DESCRIPTION:{_ical_escape(description)}",
        "LOCATION:Online cancellation reminder",
        f"URL:{BOOKING_URL}",
        f"ORGANIZER;CN=Badminton Slots:mailto:{sender}",
        (
            "ATTENDEE;CN="
            f"{_ical_escape(recipient)};ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;"
            f"RSVP=TRUE:mailto:{recipient}"
        ),
        "SEQUENCE:0",
        "STATUS:CONFIRMED",
        "CLASS:PRIVATE",
        "TRANSP:TRANSPARENT",
        "X-MICROSOFT-CDO-BUSYSTATUS:FREE",
        "BEGIN:VALARM",
        "ACTION:DISPLAY",
        "TRIGGER:PT0M",
        f"DESCRIPTION:{_ical_escape(REMINDER_SUMMARY)}",
        "END:VALARM",
        "END:VEVENT",
        "END:VCALENDAR",
    ]
    return "\r\n".join(
        folded_line
        for line in lines
        for folded_line in _fold_ical_line(line)
    ) + "\r\n"


def build_reminder_message(
    settings: EmailSettings,
    reminder: SlotReminder,
    recipient: str,
    timezone_name: str,
    generated_at: datetime | None = None,
    uid_namespace: str = "production",
) -> EmailMessage:
    slot_start = slot_start_datetime(reminder, timezone_name)
    _reminder_at, cancellation_deadline = cancellation_window(
        reminder,
        timezone_name,
    )
    courts = _format_courts(reminder.court_numbers)
    subject = (
        f"{REMINDER_SUMMARY} — {slot_start:%a %d %b, %H:%M}"
    )
    plain_body = (
        f"{REMINDER_SUMMARY}\n\n"
        f"Booked slot: {slot_start:%A, %d %B %Y at %H:%M}, {courts}.\n"
        f"Cancel before {cancellation_deadline:%H:%M} if you will not play.\n"
        f"Manage bookings: {BOOKING_URL}\n\n"
        "Accept this invitation to place the one-hour cancellation window in "
        "your calendar."
    )
    html_body = (
        f'<p style="font-size:20px"><strong>{html.escape(REMINDER_SUMMARY)}</strong></p>'
        f"<p>Booked slot: <strong>{slot_start:%A, %d %B %Y at %H:%M}</strong>, "
        f"{html.escape(courts)}.</p>"
        f"<p>Cancel before <strong>{cancellation_deadline:%H:%M}</strong> if you "
        "will not play.</p>"
        f'<p><a href="{html.escape(BOOKING_URL)}">Manage badminton bookings</a></p>'
        "<p>Accept this invitation to place the one-hour cancellation window "
        "in your calendar.</p>"
    )
    calendar_body = build_calendar_invitation(
        reminder,
        recipient,
        settings.sender,
        timezone_name,
        generated_at=generated_at,
        uid_namespace=uid_namespace,
    )

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = f"Badminton Slots <{settings.sender}>"
    message["To"] = recipient
    message.set_content(plain_body)
    message.add_alternative(html_body, subtype="html")
    message.add_alternative(
        calendar_body,
        subtype="calendar",
        params={"method": "REQUEST", "name": "badminton-cancellation.ics"},
    )
    calendar_part = message.get_payload()[-1]
    calendar_part["Content-Class"] = "urn:content-classes:calendarmessage"
    calendar_part["Content-Disposition"] = (
        'inline; filename="badminton-cancellation.ics"'
    )
    return message


def load_sent_reminders(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        raw_state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read reminder state safely: {path}") from exc
    if (
        not isinstance(raw_state, dict)
        or raw_state.get("version") != STATE_VERSION
        or not isinstance(raw_state.get("sent"), list)
        or not all(isinstance(item, str) for item in raw_state["sent"])
    ):
        raise ValueError(f"Reminder state has an unsupported format: {path}")
    return set(raw_state["sent"])


def save_sent_reminders(path: Path, sent_reminders: set[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    payload = {
        "version": STATE_VERSION,
        "sent": sorted(sent_reminders),
    }
    try:
        temporary_path.write_text(
            json.dumps(payload, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary_path, 0o600)
        temporary_path.replace(path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _open_smtp(settings: EmailSettings):
    context = ssl.create_default_context()
    return smtplib.SMTP_SSL(
        settings.smtp_host,
        settings.smtp_port,
        timeout=20,
        context=context,
    )


def send_due_cancellation_reminders(
    logs_dir: Path,
    timezone_name: str,
    logger: logging.Logger,
    *,
    now: datetime | None = None,
    state_path: Path = STATE_FILE,
    settings: EmailSettings | None = None,
    account_recipients: dict[str, str] | None = None,
) -> int:
    effective_settings = settings or load_email_settings()
    effective_account_recipients = (
        dict(account_recipients)
        if account_recipients is not None
        else load_account_recipient_map()
    )

    local_now = now or datetime.now(ZoneInfo(timezone_name))
    if local_now.tzinfo is None:
        raise ValueError("Reminder send time must be timezone-aware")
    manual_bookings_path = logs_dir.parent / MANUAL_BOOKINGS_FILENAME
    reminder_groups = group_confirmed_bookings(
        collect_confirmed_bookings(
            logs_dir,
            manual_bookings_path=manual_bookings_path,
        )
    )
    due_groups = tuple(
        reminder
        for reminder in reminder_groups
        if reminder_is_due(reminder, local_now, timezone_name)
    )
    if not due_groups:
        return 0

    missing_accounts = sorted(
        {
            reminder.account_label
            for reminder in due_groups
            if reminder.account_label not in effective_account_recipients
        }
    )
    if missing_accounts:
        raise ValueError(
            "No cancellation-reminder email is configured for: "
            + ", ".join(missing_accounts)
        )

    sent_reminders = load_sent_reminders(state_path)
    pending = [
        (reminder, effective_account_recipients[reminder.account_label])
        for reminder in due_groups
        if reminder_delivery_key(
            reminder,
            effective_account_recipients[reminder.account_label],
        )
        not in sent_reminders
    ]
    if not pending:
        return 0

    sent_count = 0
    failures: list[str] = []
    with _open_smtp(effective_settings) as server:
        server.login(effective_settings.sender, effective_settings.password)
        for reminder, recipient in pending:
            try:
                message = build_reminder_message(
                    effective_settings,
                    reminder,
                    recipient,
                    timezone_name,
                    generated_at=local_now,
                )
                server.send_message(message)
            except Exception as exc:  # noqa: BLE001 - isolate recipient delivery
                logger.exception(
                    "Failed to send cancellation reminder for %s %s to %s: %s",
                    reminder.booking_date.isoformat(),
                    reminder.start_time,
                    recipient,
                    exc,
                )
                failures.append(recipient)
                continue

            delivery_key = reminder_delivery_key(reminder, recipient)
            sent_reminders.add(delivery_key)
            save_sent_reminders(state_path, sent_reminders)
            sent_count += 1
            logger.info(
                "Cancellation reminder sent for %s %s %s (%s) to %s.",
                reminder.booking_date.isoformat(),
                reminder.start_time,
                reminder.account_label,
                _format_courts(reminder.court_numbers),
                recipient,
            )

    if failures:
        raise RuntimeError(
            "Cancellation reminder delivery failed for: " + ", ".join(failures)
        )
    return sent_count


def send_test_cancellation_invitation(
    logs_dir: Path,
    timezone_name: str,
    logger: logging.Logger,
    recipient: str,
    booking_date: date,
    start_time: str,
    account_label: str | None = None,
) -> None:
    settings = load_email_settings()
    normalized_recipient = parse_recipient_list(recipient)
    if len(normalized_recipient) != 1:
        raise ValueError("The test requires exactly one recipient")

    reminders = group_confirmed_bookings(
        collect_confirmed_bookings(
            logs_dir,
            manual_bookings_path=logs_dir.parent / MANUAL_BOOKINGS_FILENAME,
        )
    )
    matches = tuple(
        candidate
        for candidate in reminders
        if candidate.booking_date == booking_date
        and candidate.start_time == start_time
        and (account_label is None or candidate.account_label == account_label)
    )
    if not matches:
        raise ValueError(
            f"No confirmed booking exists for {booking_date.isoformat()} {start_time}"
        )
    if len(matches) != 1:
        raise ValueError(
            "More than one account booked that time; use --test-account to select one"
        )
    reminder = matches[0]

    target_recipient = normalized_recipient[0]
    message = build_reminder_message(
        settings,
        reminder,
        target_recipient,
        timezone_name,
        uid_namespace="test",
    )
    with _open_smtp(settings) as server:
        server.login(settings.sender, settings.password)
        server.send_message(message)
    logger.info(
        "Test cancellation invitation sent for %s %s to %s only.",
        booking_date.isoformat(),
        start_time,
        target_recipient,
    )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Send five-hour badminton cancellation reminders."
    )
    parser.add_argument("--test-recipient")
    parser.add_argument("--test-date")
    parser.add_argument("--test-time")
    parser.add_argument("--test-account", choices=tuple(ACCOUNT_EMAIL_ENV_NAMES))
    return parser


def _parse_test_arguments(
    args: argparse.Namespace,
) -> tuple[str, date, str, str | None] | None:
    values = (args.test_recipient, args.test_date, args.test_time)
    if not any(values):
        if args.test_account is not None:
            raise ValueError("--test-account requires the other test arguments")
        return None
    if not all(values):
        raise ValueError(
            "--test-recipient, --test-date, and --test-time must be used together"
        )
    booking_date = datetime.strptime(args.test_date, "%Y-%m-%d").date()
    start_time = datetime.strptime(args.test_time, "%H:%M").strftime("%H:%M")
    return args.test_recipient, booking_date, start_time, args.test_account


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("booking_cancellation_reminder")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False
    handler = logging.StreamHandler()
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s.%(msecs)03d | %(levelname)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    logger.addHandler(handler)
    return logger


def main(argv: Sequence[str] | None = None) -> int:
    load_dotenv(ENV_FILE, override=True)
    args = build_argument_parser().parse_args(argv)
    logger = setup_logging()
    timezone_name = os.getenv("TIMEZONE", DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE

    try:
        test_arguments = _parse_test_arguments(args)
        if test_arguments is not None:
            recipient, booking_date, start_time, account_label = test_arguments
            send_test_cancellation_invitation(
                LOGS_DIR,
                timezone_name,
                logger,
                recipient,
                booking_date,
                start_time,
                account_label,
            )
            return 0

        if not cancellation_reminders_enabled():
            return 0
        send_due_cancellation_reminders(
            LOGS_DIR,
            timezone_name,
            logger,
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - command boundary for cron logging
        logger.exception("Cancellation reminder run failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
