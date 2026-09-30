import logging
from datetime import date, datetime, timedelta, timezone

import pytest

import booking_daily_report
import booking_email
from booking_email import EmailSettings


REPORT_NOW = datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc)


def write_booking_log(
    path,
    *,
    dry_run: bool = False,
    completed: bool = True,
    target_date: date = date(2026, 10, 8),
):
    lines = [
        "2026-09-30 00:00:00 | INFO | Configured booking accounts: 账号A",
        (
            "2026-09-30 00:00:00 | INFO | [账号A] "
            f"HEADLESS=True DRY_RUN={dry_run} TIMEZONE=Europe/London"
        ),
        f"2026-09-30 00:00:00 | INFO | [账号A] Target booking date is {target_date:%d/%m/%Y}",
    ]
    if completed:
        lines.append(
            "2026-09-30 00:00:10 | INFO | Booking run completed; exit_code=0"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_select_daily_production_log_skips_dry_run_and_incomplete(
    tmp_path,
):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    stale = logs_dir / "book-badminton-20260928-235931.log"
    write_booking_log(stale)
    expected = logs_dir / "book-badminton-20260929-235000.log"
    write_booking_log(expected)
    write_booking_log(
        logs_dir / "book-badminton-20260929-235930.log",
        dry_run=True,
    )
    write_booking_log(
        logs_dir / "book-badminton-20260929-235931.log",
        completed=False,
    )
    (logs_dir / "book-badminton-invalid.log").write_text(
        "DRY_RUN=False\nBrowser closed.\n",
        encoding="utf-8",
    )

    selected = booking_daily_report.select_daily_production_log(
        logs_dir,
        date(2026, 9, 30),
    )

    assert selected == expected


def test_select_daily_production_log_never_falls_back_to_an_older_day(tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    write_booking_log(logs_dir / "book-badminton-20260928-235931.log")
    write_booking_log(
        logs_dir / "book-badminton-20260929-235931.log",
        completed=False,
    )

    with pytest.raises(FileNotFoundError, match="2026-09-30"):
        booking_daily_report.select_daily_production_log(
            logs_dir,
            date(2026, 9, 30),
        )


def test_select_daily_production_log_rejects_wrong_target_date(tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    write_booking_log(
        logs_dir / "book-badminton-20260929-235931.log",
        target_date=date(2026, 10, 7),
    )

    with pytest.raises(FileNotFoundError, match="2026-09-30"):
        booking_daily_report.select_daily_production_log(
            logs_dir,
            date(2026, 9, 30),
        )


def test_test_mode_uses_only_dedicated_test_recipient(
    tmp_path,
    monkeypatch,
):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    log_path = logs_dir / "book-badminton-20260929-235931.log"
    write_booking_log(log_path)
    monkeypatch.setenv(
        "BOOKING_EMAIL_TO",
        "first@example.com,second@example.com",
    )
    monkeypatch.setenv("BOOKING_EMAIL_TEST_TO", "owner@example.com")
    calls = []

    def fake_send_booking_report(*args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(
        booking_daily_report,
        "send_booking_report",
        fake_send_booking_report,
    )

    sent = booking_daily_report.send_daily_report(
        logs_dir=logs_dir,
        test_mode=True,
        logger=logging.getLogger("test-daily-report"),
        now=REPORT_NOW,
    )

    assert sent is True
    assert len(calls) == 1
    assert calls[0][0][0] == log_path
    assert calls[0][1]["recipients_override"] == ("owner@example.com",)


def test_explicit_test_recipient_takes_precedence_over_environment(
    tmp_path,
    monkeypatch,
):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    write_booking_log(logs_dir / "book-badminton-20260929-235931.log")
    monkeypatch.setenv("BOOKING_EMAIL_TEST_TO", "environment@example.com")
    calls = []
    monkeypatch.setattr(
        booking_daily_report,
        "send_booking_report",
        lambda *args, **kwargs: calls.append(kwargs),
    )

    booking_daily_report.send_daily_report(
        logs_dir=logs_dir,
        test_mode=True,
        explicit_test_recipient="explicit@example.com",
        now=REPORT_NOW,
    )

    assert calls[0]["recipients_override"] == ("explicit@example.com",)


@pytest.mark.parametrize(
    "configured",
    ("", "first@example.com,second@example.com"),
)
def test_test_mode_fails_closed_without_exactly_one_recipient(
    tmp_path,
    monkeypatch,
    configured,
):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    write_booking_log(logs_dir / "book-badminton-20260929-235931.log")
    monkeypatch.setenv("BOOKING_EMAIL_TEST_TO", configured)
    monkeypatch.setenv("BOOKING_EMAIL_TO", "production-list@example.com")
    monkeypatch.setattr(
        booking_daily_report,
        "send_booking_report",
        lambda *args, **kwargs: pytest.fail("email must not be sent"),
    )

    with pytest.raises(ValueError, match="exactly one recipient"):
        booking_daily_report.send_daily_report(
            logs_dir=logs_dir,
            test_mode=True,
            now=REPORT_NOW,
        )


def test_production_mode_honours_enabled_flag_and_does_not_override_recipients(
    tmp_path,
    monkeypatch,
):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    write_booking_log(logs_dir / "book-badminton-20260929-235931.log")
    calls = []
    monkeypatch.setattr(
        booking_daily_report,
        "send_booking_report",
        lambda *args, **kwargs: calls.append(kwargs),
    )

    monkeypatch.setenv("BOOKING_EMAIL_ENABLED", "false")
    state_file = tmp_path / "daily-report-state.json"
    assert booking_daily_report.send_daily_report(
        logs_dir=logs_dir,
        now=REPORT_NOW,
        state_file=state_file,
    ) is False
    assert calls == []

    monkeypatch.setenv("BOOKING_EMAIL_ENABLED", "true")
    assert booking_daily_report.send_daily_report(
        logs_dir=logs_dir,
        now=REPORT_NOW,
        state_file=state_file,
    ) is True
    assert calls[0]["recipients_override"] is None


def test_production_report_is_sent_only_once_for_the_same_daily_log(
    tmp_path,
    monkeypatch,
):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    write_booking_log(logs_dir / "book-badminton-20260929-235931.log")
    state_file = tmp_path / "daily-report-state.json"
    calls = []
    monkeypatch.setenv("BOOKING_EMAIL_ENABLED", "true")
    monkeypatch.setattr(
        booking_daily_report,
        "send_booking_report",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert booking_daily_report.send_daily_report(
        logs_dir=logs_dir,
        now=REPORT_NOW,
        state_file=state_file,
    ) is True
    assert booking_daily_report.send_daily_report(
        logs_dir=logs_dir,
        now=REPORT_NOW,
        state_file=state_file,
    ) is False

    assert len(calls) == 1
    assert booking_daily_report.load_sent_delivery_keys(state_file) == {
        "2026-09-30|book-badminton-20260929-235931.log"
    }


def test_send_booking_report_recipient_override_replaces_production_list(
    tmp_path,
    monkeypatch,
):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    target_date = date.today() + timedelta(days=8)
    log_path = logs_dir / "book-badminton-20260929-235931.log"
    log_path.write_text(
        "\n".join(
            (
                "Configured booking accounts: 账号A",
                f"[账号A] Target booking date is {target_date:%d/%m/%Y}",
                "[账号A] Success: confirmed booking for 18:00 Jubilee Court 1",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    production_settings = EmailSettings(
        sender="sender@example.com",
        password="secret",
        recipients=("first@example.com", "second@example.com"),
        smtp_host="smtp.example.com",
        smtp_port=465,
    )
    monkeypatch.setattr(
        booking_email,
        "load_email_settings",
        lambda: production_settings,
    )
    sent_messages = []

    class FakeSmtp:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def login(self, sender, password):
            assert sender == production_settings.sender
            assert password == production_settings.password

        def send_message(self, message):
            sent_messages.append(message)

    monkeypatch.setattr(booking_email.smtplib, "SMTP_SSL", FakeSmtp)

    booking_email.send_booking_report(
        log_path,
        logs_dir,
        "Europe/London",
        logging.getLogger("test-recipient-override"),
        recipients_override=("owner@example.com",),
    )

    assert len(sent_messages) == 1
    assert sent_messages[0]["To"] == "owner@example.com"
    assert "first@example.com" not in sent_messages[0]["To"]
    assert "second@example.com" not in sent_messages[0]["To"]
