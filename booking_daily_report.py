from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Sequence
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

from booking_email import (
    email_notifications_enabled,
    parse_run_log,
    send_booking_report,
)


DEFAULT_TIMEZONE = "Europe/London"
PROJECT_DIR = Path(__file__).resolve().parent
ENV_FILE = PROJECT_DIR / ".env"
LOGS_DIR = PROJECT_DIR / "logs"
STATE_FILE = PROJECT_DIR / "daily_report_state.json"
STATE_VERSION = 1
BOOKING_LOG_PATTERN = re.compile(
    r"book-badminton-(?P<timestamp>\d{8}-\d{6})\.log"
)
RUN_MODE_PATTERN = re.compile(r"\bDRY_RUN\s*=\s*(True|False)\b", re.IGNORECASE)
RUN_COMPLETION_PATTERN = re.compile(r"Booking run completed; exit_code=\d+")
LEGACY_COMPLETION_MARKER = "Browser closed."
BOOKING_RELEASE_DAYS = 8


def _booking_log_timestamp(log_path: Path) -> datetime | None:
    match = BOOKING_LOG_PATTERN.fullmatch(log_path.name)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group("timestamp"), "%Y%m%d-%H%M%S")
    except ValueError:
        return None


def is_completed_production_log(
    log_path: Path,
    expected_target_date: date,
) -> bool:
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False

    run_modes = {value.casefold() for value in RUN_MODE_PATTERN.findall(text)}
    if run_modes != {"false"}:
        return False
    if not (
        RUN_COMPLETION_PATTERN.search(text)
        or LEGACY_COMPLETION_MARKER in text
    ):
        return False
    try:
        run_results = parse_run_log(log_path)
    except (OSError, ValueError):
        return False
    target_dates = {
        result.target_date
        for result in run_results
        if result.target_date is not None
    }
    return target_dates == {expected_target_date}


def select_daily_production_log(logs_dir: Path, report_date: date) -> Path:
    """Select only the booking run started around midnight for report_date."""

    window_start = datetime.combine(report_date - timedelta(days=1), time(23, 50))
    window_end = datetime.combine(report_date, time(10, 0))
    expected_target_date = report_date + timedelta(days=BOOKING_RELEASE_DAYS)
    dated_logs = (
        (timestamp, log_path)
        for log_path in logs_dir.glob("book-badminton-*.log")
        if (timestamp := _booking_log_timestamp(log_path)) is not None
        and window_start <= timestamp <= window_end
    )
    for _, log_path in sorted(dated_logs, reverse=True):
        if is_completed_production_log(log_path, expected_target_date):
            return log_path
    raise FileNotFoundError(
        "No completed non-dry-run booking log was found for the overnight "
        f"booking run associated with {report_date.isoformat()}"
    )


def _delivery_key(report_date: date, log_path: Path) -> str:
    return f"{report_date.isoformat()}|{log_path.name}"


def load_sent_delivery_keys(state_file: Path) -> set[str]:
    if not state_file.exists():
        return set()
    try:
        payload = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read daily-report state: {state_file}") from exc
    if payload.get("version") != STATE_VERSION or not isinstance(
        payload.get("sent"), list
    ):
        raise ValueError(f"Unsupported daily-report state format: {state_file}")
    if not all(isinstance(value, str) for value in payload["sent"]):
        raise ValueError(f"Invalid daily-report state entries: {state_file}")
    return set(payload["sent"])


def save_sent_delivery_keys(state_file: Path, sent: set[str]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = state_file.with_suffix(state_file.suffix + ".tmp")
    payload = {
        "version": STATE_VERSION,
        "sent": sorted(sent),
    }
    temporary_file.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_file.replace(state_file)


def resolve_test_recipient(explicit_recipient: str | None = None) -> str:
    configured = (
        explicit_recipient
        if explicit_recipient is not None
        else os.getenv("BOOKING_EMAIL_TEST_TO", "")
    )
    recipients = tuple(
        address.strip()
        for address in re.split(r"[,;]", configured)
        if address.strip()
    )
    if len(recipients) != 1:
        raise ValueError(
            "Test report delivery requires exactly one recipient from "
            "--test-recipient or BOOKING_EMAIL_TEST_TO"
        )
    recipient = recipients[0]
    if (
        "\r" in recipient
        or "\n" in recipient
        or "@" not in recipient
        or any(character.isspace() for character in recipient)
    ):
        raise ValueError(f"Invalid test booking-report recipient: {recipient!r}")
    return recipient


def send_daily_report(
    *,
    logs_dir: Path = LOGS_DIR,
    timezone_name: str | None = None,
    test_mode: bool = False,
    explicit_test_recipient: str | None = None,
    logger: logging.Logger | None = None,
    now: datetime | None = None,
    state_file: Path = STATE_FILE,
) -> bool:
    effective_logger = logger or logging.getLogger("booking_daily_report")
    effective_timezone = (
        timezone_name
        or os.getenv("TIMEZONE", DEFAULT_TIMEZONE).strip()
        or DEFAULT_TIMEZONE
    )

    if explicit_test_recipient is not None and not test_mode:
        raise ValueError("--test-recipient may only be used together with --test")

    if now is not None and now.tzinfo is None:
        raise ValueError("Daily-report evaluation time must be timezone-aware")
    local_now = (
        datetime.now(ZoneInfo(effective_timezone))
        if now is None
        else now.astimezone(ZoneInfo(effective_timezone))
    )
    report_date = local_now.date()

    recipients_override: tuple[str, ...] | None = None
    if test_mode:
        recipients_override = (resolve_test_recipient(explicit_test_recipient),)
    elif not email_notifications_enabled():
        effective_logger.info(
            "Skipping production booking report because BOOKING_EMAIL_ENABLED is false."
        )
        return False

    current_log_path = select_daily_production_log(logs_dir, report_date)
    delivery_key = _delivery_key(report_date, current_log_path)
    sent_delivery_keys: set[str] | None = None
    if not test_mode:
        sent_delivery_keys = load_sent_delivery_keys(state_file)
        if delivery_key in sent_delivery_keys:
            effective_logger.info(
                "Skipping booking report already sent for %s from %s.",
                report_date.isoformat(),
                current_log_path.name,
            )
            return False

    send_booking_report(
        current_log_path,
        logs_dir,
        effective_timezone,
        effective_logger,
        recipients_override=recipients_override,
    )
    if sent_delivery_keys is not None:
        sent_delivery_keys.add(delivery_key)
        save_sent_delivery_keys(state_file, sent_delivery_keys)
    return True


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Send the latest completed booking-run report."
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="send only to the single configured test recipient",
    )
    parser.add_argument(
        "--test-recipient",
        help="single-use test recipient; requires --test",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    if args.test_recipient is not None and not args.test:
        parser.error("--test-recipient requires --test")

    load_dotenv(dotenv_path=ENV_FILE)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
        stream=sys.stdout,
    )
    logger = logging.getLogger("booking_daily_report")
    try:
        send_daily_report(
            test_mode=args.test,
            explicit_test_recipient=args.test_recipient,
            logger=logger,
        )
    except Exception as exc:  # noqa: BLE001 - cron must receive a non-zero exit
        logger.exception("Failed to send booking report: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
