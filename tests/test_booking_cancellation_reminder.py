from __future__ import annotations

import json
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

import booking_cancellation_reminder as reminder_module
from booking_cancellation_reminder import (
    REMINDER_SUMMARY,
    SlotReminder,
    build_calendar_invitation,
    build_reminder_message,
    cancellation_window,
    group_confirmed_bookings,
    load_sent_reminders,
    reminder_is_due,
    reminder_uid,
    send_due_cancellation_reminders,
)
from booking_email import ConfirmedBooking, EmailSettings


def make_booking(
    booking_date=date(2026, 10, 7),
    account_label="账号A",
    start_time="18:00",
    court_number=1,
):
    return ConfirmedBooking(
        booking_date=booking_date,
        account_label=account_label,
        start_time=start_time,
        court_number=court_number,
    )


def make_reminder(*bookings):
    if not bookings:
        bookings = (make_booking(),)
    return SlotReminder(
        booking_date=bookings[0].booking_date,
        start_time=bookings[0].start_time,
        bookings=tuple(bookings),
    )


def make_settings(*recipients):
    return EmailSettings(
        sender="sender@gmail.com",
        password="app-password",
        recipients=tuple(recipients or ("recipient@soton.ac.uk",)),
        smtp_host="smtp.example.test",
        smtp_port=465,
    )


def make_logger():
    logger = logging.getLogger("cancellation-reminder-test")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    return logger


def write_booking_log(path, booking_date, start_time="18:00", court_number=1):
    target_date = booking_date.strftime("%d/%m/%Y")
    path.write_text(
        "\n".join(
            (
                "2026-09-29 00:00:00 | INFO | Configured booking accounts: 账号A",
                f"2026-09-29 00:00:00 | INFO | [账号A] Target booking date is {target_date}",
                (
                    "2026-09-29 00:00:10 | INFO | [账号A] Success: confirmed "
                    f"booking for {start_time} Jubilee Court {court_number}"
                ),
            )
        )
        + "\n",
        encoding="utf-8",
    )


def write_two_account_booking_log(path, booking_date, start_time="18:00"):
    target_date = booking_date.strftime("%d/%m/%Y")
    path.write_text(
        "\n".join(
            (
                "2026-09-29 00:00:00 | INFO | Configured booking accounts: 账号A, 账号B",
                f"2026-09-29 00:00:00 | INFO | [账号A] Target booking date is {target_date}",
                f"2026-09-29 00:00:00 | INFO | [账号B] Target booking date is {target_date}",
                (
                    "2026-09-29 00:00:10 | INFO | [账号A] Success: confirmed "
                    f"booking for {start_time} Jubilee Court 1"
                ),
                (
                    "2026-09-29 00:00:11 | INFO | [账号B] Success: confirmed "
                    f"booking for {start_time} Jubilee Court 2"
                ),
            )
        )
        + "\n",
        encoding="utf-8",
    )


class FakeSMTP:
    def __init__(self):
        self.login_calls = []
        self.messages = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def login(self, sender, password):
        self.login_calls.append((sender, password))

    def send_message(self, message):
        self.messages.append(message)


def test_keeps_each_booking_account_as_a_separate_reminder():
    bookings = (
        make_booking(account_label="账号A", start_time="18:00", court_number=1),
        make_booking(account_label="账号B", start_time="18:00", court_number=2),
        make_booking(account_label="账号C", start_time="17:00", court_number=1),
    )

    groups = group_confirmed_bookings(bookings)

    assert [
        (group.start_time, group.account_label, group.court_numbers)
        for group in groups
    ] == [
        ("17:00", "账号C", (1,)),
        ("18:00", "账号A", (1,)),
        ("18:00", "账号B", (2,)),
    ]


def test_cancellation_window_is_exactly_five_to_four_hours_before_slot():
    reminder = make_reminder()

    reminder_at, deadline = cancellation_window(reminder, "Europe/London")

    assert reminder_at == datetime(2026, 10, 7, 13, 0, tzinfo=ZoneInfo("Europe/London"))
    assert deadline == datetime(2026, 10, 7, 14, 0, tzinfo=ZoneInfo("Europe/London"))


def test_due_window_includes_start_and_excludes_four_hour_deadline():
    reminder = make_reminder()
    timezone_name = "Europe/London"

    assert reminder_is_due(
        reminder,
        datetime(2026, 10, 7, 13, 0, tzinfo=ZoneInfo(timezone_name)),
        timezone_name,
    )
    assert reminder_is_due(
        reminder,
        datetime(2026, 10, 7, 13, 59, 59, tzinfo=ZoneInfo(timezone_name)),
        timezone_name,
    )
    assert not reminder_is_due(
        reminder,
        datetime(2026, 10, 7, 12, 59, 59, tzinfo=ZoneInfo(timezone_name)),
        timezone_name,
    )
    assert not reminder_is_due(
        reminder,
        datetime(2026, 10, 7, 14, 0, tzinfo=ZoneInfo(timezone_name)),
        timezone_name,
    )


def test_calendar_invitation_uses_utc_and_outlook_friendly_request_fields():
    reminder = make_reminder(make_booking(account_label="账号A", court_number=1))

    calendar_body = build_calendar_invitation(
        reminder,
        "recipient@soton.ac.uk",
        "sender@gmail.com",
        "Europe/London",
        generated_at=datetime(2026, 9, 29, 12, 0, tzinfo=ZoneInfo("UTC")),
    )
    unfolded_calendar_body = calendar_body.replace("\r\n ", "")

    assert "METHOD:REQUEST\r\n" in calendar_body
    assert "DTSTART:20261007T120000Z\r\n" in calendar_body
    assert "DTEND:20261007T130000Z\r\n" in calendar_body
    assert f"SUMMARY:{REMINDER_SUMMARY}\r\n" in calendar_body
    assert "TRANSP:TRANSPARENT\r\n" in calendar_body
    assert "X-MICROSOFT-CDO-BUSYSTATUS:FREE\r\n" in calendar_body
    assert "Court 1" in unfolded_calendar_body
    assert "Court 2" not in unfolded_calendar_body
    assert "ATTENDEE;" in calendar_body
    assert "mailto:recipient@soton.ac.uk" in calendar_body


def test_winter_invitation_uses_gmt_without_manual_dst_rules():
    winter_booking = make_booking(
        booking_date=date(2026, 11, 5),
        start_time="17:00",
    )

    calendar_body = build_calendar_invitation(
        make_reminder(winter_booking),
        "recipient@soton.ac.uk",
        "sender@gmail.com",
        "Europe/London",
        generated_at=datetime(2026, 10, 1, 12, 0, tzinfo=ZoneInfo("UTC")),
    )

    assert "DTSTART:20261105T120000Z\r\n" in calendar_body
    assert "DTEND:20261105T130000Z\r\n" in calendar_body


def test_message_is_private_to_one_recipient_and_contains_calendar_alternative():
    message = build_reminder_message(
        make_settings("recipient@soton.ac.uk"),
        make_reminder(),
        "recipient@soton.ac.uk",
        "Europe/London",
        generated_at=datetime(2026, 9, 29, 12, 0, tzinfo=ZoneInfo("UTC")),
    )

    assert message["To"] == "recipient@soton.ac.uk"
    assert message["From"] == "Badminton Slots <sender@gmail.com>"
    assert str(message["Subject"]).startswith(REMINDER_SUMMARY)
    calendar_parts = [
        part for part in message.walk() if part.get_content_type() == "text/calendar"
    ]
    assert len(calendar_parts) == 1
    assert calendar_parts[0].get_param("method") == "REQUEST"
    assert calendar_parts[0]["Content-Class"] == "urn:content-classes:calendarmessage"
    assert REMINDER_SUMMARY in calendar_parts[0].get_content()


def test_test_and_production_invites_use_distinct_uids():
    reminder = make_reminder()

    assert reminder_uid(
        reminder,
        "recipient@soton.ac.uk",
        "test",
    ) != reminder_uid(
        reminder,
        "recipient@soton.ac.uk",
        "production",
    )


def test_due_sender_records_success_and_does_not_send_duplicate(
    tmp_path,
    monkeypatch,
):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    write_booking_log(
        logs_dir / "book-badminton-20260928-235931.log",
        date(2026, 10, 7),
    )
    state_path = tmp_path / "state.json"
    smtp = FakeSMTP()
    monkeypatch.setattr(reminder_module, "_open_smtp", lambda _settings: smtp)
    now = datetime(2026, 10, 7, 13, 0, tzinfo=ZoneInfo("Europe/London"))

    first_count = send_due_cancellation_reminders(
        logs_dir,
        "Europe/London",
        make_logger(),
        now=now,
        state_path=state_path,
        settings=make_settings("recipient@soton.ac.uk"),
        account_recipients={"账号A": "recipient@soton.ac.uk"},
    )
    second_count = send_due_cancellation_reminders(
        logs_dir,
        "Europe/London",
        make_logger(),
        now=now,
        state_path=state_path,
        settings=make_settings("recipient@soton.ac.uk"),
        account_recipients={"账号A": "recipient@soton.ac.uk"},
    )

    assert first_count == 1
    assert second_count == 0
    assert len(smtp.messages) == 1
    assert len(load_sent_reminders(state_path)) == 1
    assert json.loads(state_path.read_text(encoding="utf-8"))["version"] == 2


def test_same_hour_bookings_are_routed_only_to_their_own_account_emails(
    tmp_path,
    monkeypatch,
):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    write_two_account_booking_log(
        logs_dir / "book-badminton-20260928-235931.log",
        date(2026, 10, 7),
    )
    smtp = FakeSMTP()
    monkeypatch.setattr(reminder_module, "_open_smtp", lambda _settings: smtp)

    sent_count = send_due_cancellation_reminders(
        logs_dir,
        "Europe/London",
        make_logger(),
        now=datetime(2026, 10, 7, 13, 0, tzinfo=ZoneInfo("Europe/London")),
        state_path=tmp_path / "state.json",
        settings=make_settings("unused@example.test"),
        account_recipients={
            "账号A": "account-a@soton.ac.uk",
            "账号B": "account-b@soton.ac.uk",
        },
    )

    assert sent_count == 2
    assert [message["To"] for message in smtp.messages] == [
        "account-a@soton.ac.uk",
        "account-b@soton.ac.uk",
    ]
    message_bodies = [message.get_body(preferencelist=("plain",)).get_content() for message in smtp.messages]
    assert "Court 1" in message_bodies[0]
    assert "Court 2" not in message_bodies[0]
    assert "Court 2" in message_bodies[1]
    assert "Court 1" not in message_bodies[1]


def test_due_sender_does_not_connect_before_window_or_after_deadline(
    tmp_path,
    monkeypatch,
):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    write_booking_log(
        logs_dir / "book-badminton-20260928-235931.log",
        date(2026, 10, 7),
    )

    def unexpected_smtp(_settings):
        raise AssertionError("SMTP must not open when no reminder is due")

    monkeypatch.setattr(reminder_module, "_open_smtp", unexpected_smtp)
    common = {
        "logs_dir": logs_dir,
        "timezone_name": "Europe/London",
        "logger": make_logger(),
        "state_path": tmp_path / "state.json",
        "settings": make_settings("recipient@soton.ac.uk"),
        "account_recipients": {"账号A": "recipient@soton.ac.uk"},
    }

    assert send_due_cancellation_reminders(
        **common,
        now=datetime(2026, 10, 7, 12, 59, tzinfo=ZoneInfo("Europe/London")),
    ) == 0
    assert send_due_cancellation_reminders(
        **common,
        now=datetime(2026, 10, 7, 14, 0, tzinfo=ZoneInfo("Europe/London")),
    ) == 0


def test_corrupt_state_fails_closed_before_sending(tmp_path, monkeypatch):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    write_booking_log(
        logs_dir / "book-badminton-20260928-235931.log",
        date(2026, 10, 7),
    )
    state_path = tmp_path / "state.json"
    state_path.write_text("not-json", encoding="utf-8")

    def unexpected_smtp(_settings):
        raise AssertionError("SMTP must not open with corrupt state")

    monkeypatch.setattr(reminder_module, "_open_smtp", unexpected_smtp)

    with pytest.raises(ValueError, match="Could not read reminder state safely"):
        send_due_cancellation_reminders(
            logs_dir,
            "Europe/London",
            make_logger(),
            now=datetime(2026, 10, 7, 13, 0, tzinfo=ZoneInfo("Europe/London")),
            state_path=state_path,
            settings=make_settings("recipient@soton.ac.uk"),
            account_recipients={"账号A": "recipient@soton.ac.uk"},
        )
