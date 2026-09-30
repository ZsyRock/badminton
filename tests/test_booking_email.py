from datetime import date

from booking_email import (
    AccountRunResult,
    ConfirmedBooking,
    EmailSettings,
    apply_manual_bookings_to_run_results,
    build_email_message,
    build_report,
    collect_attempted_booking_dates,
    collect_confirmed_bookings,
    format_booking_result,
    load_email_settings,
    load_manual_bookings,
    parse_run_log,
)


def write_log(path, lines):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_parse_run_log_reads_confirmed_and_missing_results(tmp_path):
    log_path = tmp_path / "book-badminton-20260719-235931.log"
    write_log(
        log_path,
        [
            "2026-07-20 00:00:00.004 | INFO | [账号A] Target booking date is 28/07/2026",
            "2026-07-20 00:00:00.007 | INFO | [账号B] Target booking date is 28/07/2026",
            "2026-07-20 00:00:10.123 | INFO | [账号A] Success: confirmed booking for 19:00 Jubilee Court 4",
            "2026-07-20 00:00:10.456 | INFO | [账号B] No preferred slots were available to book.",
        ],
    )

    results = parse_run_log(log_path)

    assert results[0].booking == ConfirmedBooking(
        booking_date=date(2026, 7, 28),
        account_label="账号A",
        start_time="19:00",
        court_number=4,
    )
    assert results[1].booking is None
    assert results[1].detail == "no preferred slot was available"


def test_parse_run_log_includes_account_c_confirmation(tmp_path):
    log_path = tmp_path / "book-badminton-20260820-235930.log"
    write_log(
        log_path,
        [
            "2026-08-21 00:00:00 | INFO | Configured booking accounts: 账号A, 账号B, 账号C",
            "2026-08-21 00:00:00 | INFO | [账号C] Target booking date is 29/08/2026",
            "2026-08-21 00:00:04 | INFO | [账号C] Success: confirmed booking for 17:00 Jubilee Court 1",
        ],
    )

    results = parse_run_log(log_path)

    assert results == (
        AccountRunResult(
            account_label="账号C",
            target_date=date(2026, 8, 29),
            booking=ConfirmedBooking(
                booking_date=date(2026, 8, 29),
                account_label="账号C",
                start_time="17:00",
                court_number=1,
            ),
            detail="confirmed",
        ),
    )


def test_collect_confirmed_bookings_deduplicates_log_entries(tmp_path):
    lines = [
        "2026-07-20 00:00:00 | INFO | [账号A] Target booking date is 28/07/2026",
        "2026-07-20 00:00:10 | INFO | [账号A] Success: confirmed booking for 19:00 Jubilee Court 4",
    ]
    write_log(tmp_path / "book-badminton-20260719-235931.log", lines)
    write_log(tmp_path / "book-badminton-20260719-235932.log", lines)

    bookings = collect_confirmed_bookings(tmp_path)

    assert len(bookings) == 1


def test_collect_confirmed_bookings_keeps_only_latest_slot_per_account_and_date(
    tmp_path,
):
    write_log(
        tmp_path / "book-badminton-20260820-120000.log",
        [
            "2026-08-20 12:00:00 | INFO | Configured booking accounts: 账号A, 账号B, 账号C",
            "2026-08-20 12:00:00 | INFO | [账号A] Target booking date is 29/08/2026",
            "2026-08-20 12:00:05 | INFO | [账号A] Success: confirmed booking for 19:00 Jubilee Court 1",
        ],
    )
    write_log(
        tmp_path / "book-badminton-20260820-130000.log",
        [
            "2026-08-20 13:00:00 | INFO | Configured booking accounts: 账号A, 账号B, 账号C",
            "2026-08-20 13:00:00 | INFO | [账号A] Target booking date is 29/08/2026",
            "2026-08-20 13:00:00 | INFO | [账号B] Target booking date is 29/08/2026",
            "2026-08-20 13:00:00 | INFO | [账号C] Target booking date is 29/08/2026",
            "2026-08-20 13:00:05 | INFO | [账号A] Success: confirmed booking for 18:00 Jubilee Court 2",
            "2026-08-20 13:00:05 | INFO | [账号B] Success: confirmed booking for 16:00 Jubilee Court 1",
            "2026-08-20 13:00:05 | INFO | [账号C] Success: confirmed booking for 17:00 Jubilee Court 3",
        ],
    )

    bookings = collect_confirmed_bookings(tmp_path)

    assert len(bookings) == 3
    assert ConfirmedBooking(date(2026, 8, 29), "账号A", "19:00", 1) not in bookings
    assert ConfirmedBooking(date(2026, 8, 29), "账号A", "18:00", 2) in bookings
    assert {booking.account_label for booking in bookings} == {
        "账号A",
        "账号B",
        "账号C",
    }


def test_collect_attempted_booking_dates_keeps_no_slot_runs_and_skips_dry_runs(
    tmp_path,
):
    write_log(
        tmp_path / "book-badminton-20260908-235932.log",
        [
            "2026-09-08 23:59:32 | INFO | [账号A] HEADLESS=True DRY_RUN=False TIMEZONE=Europe/London",
            "2026-09-08 23:59:32 | INFO | [账号A] Target booking date is 17/09/2026",
            "2026-09-09 00:00:05 | INFO | [账号A] No preferred slots were available to book.",
        ],
    )
    write_log(
        tmp_path / "book-badminton-20260909-120000.log",
        [
            "2026-09-09 12:00:00 | INFO | [账号A] HEADLESS=True DRY_RUN=True TIMEZONE=Europe/London",
            "2026-09-09 12:00:00 | INFO | [账号A] Target booking date is 18/09/2026",
        ],
    )
    write_log(
        tmp_path / "book-badminton-broken.log",
        ["not a booking log"],
    )

    assert collect_attempted_booking_dates(tmp_path) == (date(2026, 9, 17),)


def test_manual_bookings_override_log_history_and_current_run(tmp_path):
    log_path = tmp_path / "book-badminton-20260820-235931.log"
    write_log(
        log_path,
        [
            "2026-08-21 00:00:00 | INFO | Configured booking accounts: 账号A, 账号B",
            "2026-08-21 00:00:00 | INFO | [账号A] Target booking date is 29/08/2026",
            "2026-08-21 00:00:00 | INFO | [账号B] Target booking date is 29/08/2026",
            "2026-08-21 00:00:19 | INFO | [账号A] Success: confirmed booking for 19:00 Jubilee Court 1",
            "2026-08-21 00:00:19 | INFO | [账号B] Success: confirmed booking for 20:00 Jubilee Court 1",
        ],
    )
    manual_path = tmp_path / "manual-bookings.json"
    manual_path.write_text(
        """[
          {"booking_date":"2026-08-29","account_label":"账号A","start_time":"17:00","court_number":4},
          {"booking_date":"2026-08-29","account_label":"账号B","start_time":"17:00","court_number":3}
        ]""",
        encoding="utf-8",
    )

    manual_bookings = load_manual_bookings(manual_path)
    collected = collect_confirmed_bookings(
        tmp_path,
        manual_bookings_path=manual_path,
    )
    corrected_results = apply_manual_bookings_to_run_results(
        parse_run_log(log_path),
        manual_bookings,
    )

    expected = {
        ConfirmedBooking(date(2026, 8, 29), "账号A", "17:00", 4),
        ConfirmedBooking(date(2026, 8, 29), "账号B", "17:00", 3),
    }
    assert set(collected) == expected
    assert {result.booking for result in corrected_results} == expected


def test_parse_run_log_supports_legacy_single_account_logs(tmp_path):
    log_path = tmp_path / "book-badminton-20260712-235931.log"
    write_log(
        log_path,
        [
            "2026-07-12 23:59:31 | INFO | Target booking date is 21/07/2026",
            "2026-07-13 00:00:07 | INFO | Success: confirmed booking for 19:00 Jubilee Court 1",
        ],
    )

    results = parse_run_log(log_path)

    assert results == (
        AccountRunResult(
            account_label="账号A",
            target_date=date(2026, 7, 21),
            booking=ConfirmedBooking(
                booking_date=date(2026, 7, 21),
                account_label="账号A",
                start_time="19:00",
                court_number=1,
            ),
            detail="confirmed",
        ),
    )


def test_build_report_colours_complete_pair_green_and_other_days_yellow():
    report_date = date(2026, 7, 20)
    bookings = (
        ConfirmedBooking(report_date, "账号A", "17:00", 4),
        ConfirmedBooking(report_date, "账号B", "18:00", 4),
        ConfirmedBooking(report_date.replace(day=21), "账号A", "15:00", 3),
    )
    run_results = (
        AccountRunResult("账号A", report_date, bookings[0], "confirmed"),
        AccountRunResult("账号B", report_date, bookings[1], "confirmed"),
    )

    subject, plain_body, html_body = build_report(
        run_results,
        bookings,
        report_date,
    )

    assert subject == "[Badminton Booking Report] Mon 20 Jul - Tue 21 Jul"
    assert (
        "Today's playable slots are:\n"
        "17:00 and 18:00 at Court 4."
    ) in plain_body
    assert "Account A" not in plain_body
    assert "Account B" not in html_body
    assert "Playable Slots Summary" in plain_body
    assert "Playable Slots Summary" in html_body
    assert "Light green indicates" not in plain_body
    assert "Light green indicates" not in html_body
    assert 'background:#e2f0d9' in html_body
    assert 'background:#fff2cc' in html_body
    assert (
        "Today&#x27;s playable slots are:<br>"
        '<span style="background:#e2f0d9;padding:3px 6px">'
        "17:00 and 18:00 at Court 4</span>."
    ) in html_body
    assert '<p style="font-size:20px;line-height:1.6">' in html_body
    assert '<tr style="background:#e2f0d9">' not in html_body
    assert "Court 3" in html_body


def test_format_booking_result_keeps_nonconsecutive_or_different_courts_separate():
    bookings = (
        ConfirmedBooking(date(2026, 7, 28), "账号A", "19:00", 4),
        ConfirmedBooking(date(2026, 7, 28), "账号B", "21:00", 1),
    )

    assert format_booking_result(bookings) == (
        "19:00 at Court 4 and 21:00 at Court 1"
    )


def test_build_report_marks_any_consecutive_booked_cells_green_only():
    report_date = date(2026, 7, 20)
    bookings = (
        ConfirmedBooking(report_date, "账号A", "18:00", 3),
        ConfirmedBooking(report_date, "账号B", "19:00", 4),
        ConfirmedBooking(report_date, "账号C", "16:00", 1),
    )

    _, _, html_body = build_report((), bookings, report_date)

    assert 'background:#e2f0d9' in html_body
    assert 'background:#fff2cc' in html_body
    assert "Court 3" in html_body
    assert "Court 4" in html_body
    assert "Court 1" in html_body
    assert '<tr style="background:#e2f0d9">' not in html_body
    assert '<tr style="background:#fff2cc">' not in html_body


def test_build_report_table_runs_from_15_to_19_only():
    report_date = date(2026, 7, 20)

    _, plain_body, html_body = build_report((), (), report_date)

    for start_time in ("15:00", "16:00", "17:00", "18:00", "19:00"):
        assert f">{start_time}</th>" in html_body
        assert f"{start_time}: —" in plain_body
    assert ">14:00</th>" not in html_body
    assert "14:00:" not in plain_body
    assert ">20:00</th>" not in html_body
    assert "20:00:" not in plain_body
    assert ">21:00</th>" not in html_body


def test_build_report_omits_historical_slots_outside_target_hours_from_table():
    report_date = date(2026, 7, 20)
    bookings = (
        ConfirmedBooking(report_date, "账号A", "15:00", 1),
        ConfirmedBooking(report_date, "账号B", "20:00", 2),
    )

    _, plain_body, html_body = build_report((), bookings, report_date)

    assert "15:00: Court 1" in plain_body
    assert ">Court 1</td>" in html_body
    assert "20:00:" not in plain_body
    assert ">20:00</th>" not in html_body
    table_html = html_body.split("<h2>Playable Slots Summary</h2>", 1)[1]
    assert "Court 2" not in table_html


def test_build_report_merges_multiple_courts_in_the_same_time_cell():
    report_date = date(2026, 8, 29)
    bookings = (
        ConfirmedBooking(report_date, "账号A", "18:00", 1),
        ConfirmedBooking(report_date, "账号B", "18:00", 2),
        ConfirmedBooking(report_date, "账号C", "17:00", 4),
    )

    _, plain_body, html_body = build_report((), bookings, report_date)

    assert "18:00: Court 1 + 2" in plain_body
    assert ">Court 1 + 2</td>" in html_body
    assert ">Court 4</td>" in html_body
    assert "Court 1<br>Court 2" not in html_body


def test_build_report_lists_todays_same_time_courts_in_playable_summary():
    report_date = date(2026, 8, 21)
    target_date = date(2026, 8, 30)
    todays_bookings = (
        ConfirmedBooking(report_date, "账号A", "17:00", 1),
        ConfirmedBooking(report_date, "账号B", "18:00", 1),
        ConfirmedBooking(report_date, "账号C", "18:00", 2),
    )
    latest_run_bookings = (
        ConfirmedBooking(target_date, "账号A", "15:00", 4),
        ConfirmedBooking(target_date, "账号B", "16:00", 3),
        ConfirmedBooking(target_date, "账号C", "17:00", 2),
    )
    run_results = tuple(
        AccountRunResult(booking.account_label, target_date, booking, "confirmed")
        for booking in latest_run_bookings
    )

    subject, plain_body, html_body = build_report(
        run_results,
        todays_bookings + latest_run_bookings,
        report_date,
    )

    assert subject == "[Badminton Booking Report] Fri 21 Aug - Sun 30 Aug"
    assert (
        "Today's playable slots are:\n"
        "17:00 at Court 1,\n"
        "18:00 at Court 1 and 2."
    ) in plain_body
    assert (
        "Today&#x27;s playable slots are:<br>"
        '<span style="background:#e2f0d9;padding:3px 6px">'
        "17:00 at Court 1</span>,<br>"
        '<span style="background:#e2f0d9;padding:3px 6px">'
        "18:00 at Court 1 and 2</span>."
    ) in html_body
    assert "18:00: Court 1 + 2" in plain_body
    assert ">Court 1 + 2</td>" in html_body
    assert "17:00 and 18:00 at Court 1" not in plain_body
    assert "15:00 at Court 4" not in plain_body.split("Playable Slots Summary", 1)[0]


def test_build_report_says_when_no_slots_are_booked_for_today():
    report_date = date(2026, 8, 21)
    target_date = date(2026, 8, 30)
    future_booking = ConfirmedBooking(target_date, "账号A", "18:00", 1)

    _, plain_body, html_body = build_report(
        (
            AccountRunResult(
                "账号A",
                target_date,
                future_booking,
                "confirmed",
            ),
        ),
        (future_booking,),
        report_date,
    )

    assert (
        "Today's playable slots are:\n"
        "No slots are booked for today."
    ) in plain_body
    assert (
        "Today&#x27;s playable slots are:<br>"
        '<span style="background:#fff2cc;padding:3px 6px">'
        "No slots are booked for today.</span>"
    ) in html_body
    assert ">Court 1</td>" in html_body


def test_build_report_caps_each_date_to_one_slot_per_account():
    report_date = date(2026, 8, 21)
    target_date = date(2026, 8, 29)
    bookings = (
        ConfirmedBooking(target_date, "账号A", "19:00", 4),
        ConfirmedBooking(target_date, "账号B", "20:00", 4),
        ConfirmedBooking(target_date, "账号C", "17:00", 3),
        ConfirmedBooking(target_date, "账号A", "18:00", 2),
        ConfirmedBooking(target_date, "账号B", "18:00", 1),
    )

    _, _, html_body = build_report((), bookings, report_date)

    assert html_body.count("Court ") == 2
    assert "Court 1 + 2" in html_body
    assert "Court 3" in html_body
    assert "Court 4" not in html_body


def test_booked_slot_is_plain_court_text_without_calendar_link():
    booking_date = date(2026, 7, 24)
    booking = ConfirmedBooking(booking_date, "账号A", "18:00", 1)

    _, _, html_body = build_report((), (booking,), booking_date)

    assert "Court 1" in html_body
    assert "Badminton 1h" not in html_body
    assert "outlook-event-" not in html_body
    assert "x-apple-data-detectors" not in html_body
    assert 'class="ms-outlook-mobile-availability-container"' not in html_body
    assert 'itemtype="http://schema.org/Event"' not in html_body
    assert 'itemprop="startDate"' not in html_body
    assert 'itemprop="endDate"' not in html_body
    assert 'href="cid:' not in html_body
    assert "https://outlook.office.com" not in html_body
    assert "ms-outlook:" not in html_body


def test_consecutive_cells_remain_plain_court_text():
    report_date = date(2026, 7, 24)
    bookings = (
        ConfirmedBooking(report_date, "账号A", "17:00", 1),
        ConfirmedBooking(report_date, "账号B", "18:00", 2),
    )

    _, _, html_body = build_report((), bookings, report_date)

    assert "Court 1" in html_body
    assert "Court 2" in html_body
    assert "Badminton 2h" not in html_body
    assert "outlook-event-" not in html_body
    assert "x-apple-data-detectors" not in html_body
    assert 'class="ms-outlook-mobile-availability-container"' not in html_body
    assert 'itemtype="http://schema.org/Event"' not in html_body
    assert 'href="cid:' not in html_body
    assert "https://outlook.office.com" not in html_body


def test_plain_slot_does_not_add_hidden_schedule_text_in_winter():
    booking_date = date(2026, 1, 16)
    booking = ConfirmedBooking(booking_date, "账号A", "18:00", 1)

    _, _, html_body = build_report((), (booking,), booking_date)

    assert "Court 1" in html_body
    assert "Friday, 16 January 2026 from 18:00 to 19:00 GMT" not in html_body
    assert "x-apple-data-detectors" not in html_body


def test_email_message_uses_configured_recipient_without_calendar_parts():
    report_date = date(2026, 7, 24)
    bookings = (
        ConfirmedBooking(report_date, "账号A", "17:00", 1),
        ConfirmedBooking(report_date, "账号B", "18:00", 2),
    )
    subject, plain_body, html_body = build_report(
        (),
        bookings,
        report_date,
    )
    settings = EmailSettings(
        sender="sender@example.com",
        password="secret",
        recipients=("recipient@example.com",),
        smtp_host="smtp.example.com",
        smtp_port=465,
    )

    message = build_email_message(
        settings,
        subject,
        plain_body,
        html_body,
    )

    assert message["To"] == "recipient@example.com"
    html_part = next(
        part for part in message.walk() if part.get_content_type() == "text/html"
    )
    assert all(
        part.get_content_type() != "text/calendar"
        for part in message.walk()
    )
    assert 'href="cid:' not in html_part.get_content()
    assert "https://outlook.office.com" not in html_part.get_content()
    assert "x-apple-data-detectors" not in html_part.get_content()


def test_build_report_runs_through_latest_confirmed_booking_date():
    report_date = date(2026, 7, 20)
    latest_date = date(2026, 7, 28)
    booking = ConfirmedBooking(latest_date, "账号A", "19:00", 4)

    _, plain_body, _ = build_report(
        (
            AccountRunResult("账号A", latest_date, booking, "confirmed"),
        ),
        (booking,),
        report_date,
    )

    assert "Mon 20 Jul" in plain_body
    assert "Tue 28 Jul" in plain_body


def test_build_report_keeps_all_failed_target_date_with_dash_cells():
    report_date = date(2026, 9, 10)
    failed_target_date = date(2026, 9, 18)
    run_results = tuple(
        AccountRunResult(
            account_label,
            failed_target_date,
            None,
            "no preferred slot was available",
        )
        for account_label in ("账号A", "账号B", "账号C")
    )

    subject, plain_body, html_body = build_report(
        run_results,
        (),
        report_date,
        attempted_booking_dates=(failed_target_date,),
    )

    assert subject == "[Badminton Booking Report] Thu 10 Sep - Fri 18 Sep"
    assert "Fri 18 Sep: 15:00: —" in plain_body
    failed_row = html_body.split(">Fri 18 Sep</td>", 1)[1].split("</tr>", 1)[0]
    assert failed_row.count("</td>") == 5
    assert "Court" not in failed_row
    assert failed_row.count(">—</td>") == 5


def test_build_report_preserves_historical_failed_date_after_a_later_run():
    report_date = date(2026, 9, 10)
    current_target_date = date(2026, 9, 17)
    historical_failed_date = date(2026, 9, 18)
    run_results = (
        AccountRunResult(
            "账号A",
            current_target_date,
            ConfirmedBooking(current_target_date, "账号A", "18:00", 1),
            "confirmed",
        ),
    )

    subject, plain_body, html_body = build_report(
        run_results,
        (run_results[0].booking,),
        report_date,
        attempted_booking_dates=(historical_failed_date,),
    )

    assert subject.endswith("Fri 18 Sep")
    assert "Thu 17 Sep: 15:00: —" in plain_body
    assert ">Court 1</td>" in html_body
    assert ">Fri 18 Sep</td>" in html_body


def test_load_email_settings_reads_legacy_constants_without_executing_script(
    tmp_path,
    monkeypatch,
):
    reference_script = tmp_path / "send_ip_email.py"
    reference_script.write_text(
        '\n'.join(
            (
                'SENDER_EMAIL = "sender@example.com"',
                'APP_PASSWORD = "app-password"',
                'RECEIVER_EMAIL = "recipient@example.com"',
                'raise RuntimeError("must not execute")',
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BOOKING_EMAIL_REFERENCE_SCRIPT", str(reference_script))
    monkeypatch.delenv("BOOKING_EMAIL_FROM", raising=False)
    monkeypatch.delenv("BOOKING_EMAIL_APP_PASSWORD", raising=False)
    monkeypatch.delenv("BOOKING_EMAIL_TO", raising=False)

    settings = load_email_settings()

    assert settings.sender == "sender@example.com"
    assert settings.password == "app-password"
    assert settings.recipients == ("recipient@example.com",)


def test_load_email_settings_supports_legacy_getenv_defaults(tmp_path, monkeypatch):
    reference_script = tmp_path / "send_ip_email.py"
    reference_script.write_text(
        '\n'.join(
            (
                'import os',
                'SENDER_EMAIL = os.getenv("IP_SENDER", "sender@example.com")',
                'APP_PASSWORD = os.getenv("IP_PASSWORD", "default-password")',
                'RECEIVER_EMAIL = os.getenv("IP_RECIPIENT", "recipient@example.com")',
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("BOOKING_EMAIL_REFERENCE_SCRIPT", str(reference_script))
    monkeypatch.setenv("IP_PASSWORD", "environment-password")
    monkeypatch.delenv("BOOKING_EMAIL_FROM", raising=False)
    monkeypatch.delenv("BOOKING_EMAIL_APP_PASSWORD", raising=False)
    monkeypatch.delenv("BOOKING_EMAIL_TO", raising=False)

    settings = load_email_settings()

    assert settings.sender == "sender@example.com"
    assert settings.password == "environment-password"
    assert settings.recipients == ("recipient@example.com",)


def test_load_email_settings_reads_explicit_app_password_file(tmp_path, monkeypatch):
    password_file = tmp_path / "app_password"
    password_file.write_text("file-app-password\n", encoding="utf-8")
    monkeypatch.setenv(
        "BOOKING_EMAIL_REFERENCE_SCRIPT",
        str(tmp_path / "missing-reference.py"),
    )
    monkeypatch.setenv("BOOKING_EMAIL_FROM", "sender@example.com")
    monkeypatch.delenv("BOOKING_EMAIL_APP_PASSWORD", raising=False)
    monkeypatch.setenv("BOOKING_EMAIL_APP_PASSWORD_FILE", str(password_file))
    monkeypatch.setenv("BOOKING_EMAIL_TO", "recipient@example.com")

    settings = load_email_settings()

    assert settings.password == "file-app-password"
    assert settings.recipients == ("recipient@example.com",)
