from datetime import date, datetime

from book_badminton import (
    booking_confirmation_detected,
    SlotPreference,
    build_slot_priority,
    compute_target_date,
    build_available_spaces_button_pattern,
    build_slot_card_pattern,
    build_slot_button_pattern,
    format_date_for_site,
    format_slot_time_range,
    format_time_for_button_label,
    is_book_page_url,
    parse_target_date_override,
    post_login_success_detected,
    pick_best_available_slot,
    resolve_target_date,
)


def test_compute_target_date_uses_europe_london_day_boundary():
    now = datetime.fromisoformat("2026-05-13T23:30:00+00:00")

    assert compute_target_date(now=now).isoformat() == "2026-05-22"


def test_parse_target_date_override_accepts_iso_format():
    assert parse_target_date_override("2026-05-21") == date(2026, 5, 21)


def test_parse_target_date_override_accepts_uk_format():
    assert parse_target_date_override("21/05/2026") == date(2026, 5, 21)


def test_parse_target_date_override_returns_none_for_empty_value():
    assert parse_target_date_override("") is None
    assert parse_target_date_override(None) is None


def test_parse_target_date_override_rejects_other_formats():
    try:
        parse_target_date_override("05-21-2026")
    except ValueError as exc:
        assert "TARGET_DATE_OVERRIDE" in str(exc)
    else:
        raise AssertionError("Expected ValueError for unsupported date override format")


def test_resolve_target_date_prefers_override_when_present():
    resolved, using_override = resolve_target_date(
        timezone_name="Europe/London",
        target_date_override="2026-05-21",
        now=datetime.fromisoformat("2026-05-13T23:30:00+00:00"),
    )

    assert resolved == date(2026, 5, 21)
    assert using_override is True


def test_resolve_target_date_uses_default_rule_when_override_missing():
    resolved, using_override = resolve_target_date(
        timezone_name="Europe/London",
        target_date_override=None,
        now=datetime.fromisoformat("2026-05-13T23:30:00+00:00"),
    )

    assert resolved == date(2026, 5, 22)
    assert using_override is False


def test_format_date_for_site_returns_dd_mm_yyyy():
    target_date = datetime.fromisoformat("2026-05-22T12:00:00").date()

    assert format_date_for_site(target_date) == "22/05/2026"


def test_build_slot_priority_respects_time_then_court_order():
    actual = build_slot_priority(
        preferred_times=["19:00", "18:00"],
        preferred_courts=[1, 2, 3, 4],
    )

    assert actual == [
        SlotPreference(start_time="19:00", court_number=1),
        SlotPreference(start_time="19:00", court_number=2),
        SlotPreference(start_time="19:00", court_number=3),
        SlotPreference(start_time="19:00", court_number=4),
        SlotPreference(start_time="18:00", court_number=1),
        SlotPreference(start_time="18:00", court_number=2),
        SlotPreference(start_time="18:00", court_number=3),
        SlotPreference(start_time="18:00", court_number=4),
    ]


def test_pick_best_available_slot_returns_first_preferred_match():
    preferences = build_slot_priority(
        preferred_times=["19:00", "18:00"],
        preferred_courts=[1, 2, 3, 4],
    )
    available = {
        "18:00 Jubilee Court 4",
        "18:00 Jubilee Court 2",
        "19:00 Jubilee Court 3",
    }

    assert pick_best_available_slot(available, preferences) == SlotPreference(
        start_time="19:00",
        court_number=3,
    )


def test_pick_best_available_slot_returns_none_when_no_preferences_match():
    preferences = build_slot_priority(
        preferred_times=["19:00", "18:00"],
        preferred_courts=[1, 2],
    )
    available = {"20:00 Jubilee Court 1"}

    assert pick_best_available_slot(available, preferences) is None


def test_format_time_for_button_label_converts_to_12_hour_clock():
    assert format_time_for_button_label("19:00") == "7:00 PM"
    assert format_time_for_button_label("18:00") == "6:00 PM"
    assert format_time_for_button_label("00:00") == "12:00 AM"


def test_format_slot_time_range_builds_one_hour_window():
    assert format_slot_time_range("14:00") == "14:00 - 15:00"
    assert format_slot_time_range("19:00") == "19:00 - 20:00"


def test_build_slot_button_pattern_matches_dynamic_court_and_time_label():
    slot = SlotPreference(start_time="19:00", court_number=2)
    pattern = build_slot_button_pattern(slot)

    assert pattern.search("Book now: for Jubilee Court 2 at 7:00 PM Thursday, May 21,")
    assert not pattern.search("Book now: for Jubilee Court 4 at 2:00 PM Thursday, May 21,")


def test_build_slot_card_pattern_matches_court_and_time_range_text():
    slot = SlotPreference(start_time="14:00", court_number=4)
    pattern = build_slot_card_pattern(slot)

    assert pattern.search("Jubilee Court 4 14:00 - 15:00 Thu 21st May Book now")
    assert not pattern.search("Jubilee Court 3 14:00 - 15:00 Thu 21st May Book now")


def test_build_available_spaces_button_pattern_matches_target_day_not_hard_coded():
    target_date = datetime.fromisoformat("2026-05-22T12:00:00").date()
    pattern = build_available_spaces_button_pattern(target_date)

    assert pattern.search(
        "Badminton starts on Fri, 22nd May, at 07:00 AM: See available spaces"
    )
    assert not pattern.search(
        "Badminton starts on Thu, 21st May, at 07:00 AM: See available spaces"
    )


def test_is_book_page_url_accepts_book_route():
    assert is_book_page_url("https://soton.gladstonego.cloud/book")
    assert is_book_page_url("https://soton.gladstonego.cloud/book?foo=bar")
    assert not is_book_page_url("https://soton.gladstonego.cloud/auth/login")


def test_post_login_success_detected_accepts_multiple_success_signals():
    assert post_login_success_detected(
        current_url="https://soton.gladstonego.cloud/book",
        activity_form_visible=False,
        make_booking_visible=False,
        book_nav_visible=False,
    )
    assert post_login_success_detected(
        current_url="https://soton.gladstonego.cloud/",
        activity_form_visible=True,
        make_booking_visible=False,
        book_nav_visible=False,
    )
    assert post_login_success_detected(
        current_url="https://soton.gladstonego.cloud/",
        activity_form_visible=False,
        make_booking_visible=True,
        book_nav_visible=False,
    )
    assert post_login_success_detected(
        current_url="https://soton.gladstonego.cloud/",
        activity_form_visible=False,
        make_booking_visible=False,
        book_nav_visible=True,
    )
    assert not post_login_success_detected(
        current_url="https://soton.gladstonego.cloud/auth/login",
        activity_form_visible=False,
        make_booking_visible=False,
        book_nav_visible=False,
    )


def test_booking_confirmation_detected_accepts_confirmation_screen_text():
    body_text = """
    Booking Confirmed!
    Badminton
    Thursday 21st May 2026 09:00
    Booking ref: 10955735
    Your booking confirmation and receipt has been sent to player@example.com.
    Make another booking
    """

    assert booking_confirmation_detected(
        current_url="https://soton.gladstonego.cloud/book/confirmation",
        body_text=body_text,
    )


def test_booking_confirmation_detected_rejects_calendar_unavailable_text():
    body_text = """
    Activity Calendar - Book
    Jubilee Court 1 07:00 - 08:00 This slot is unavailable
    Jubilee Court 2 07:00 - 08:00 Book now
    """

    assert not booking_confirmation_detected(
        current_url="https://soton.gladstonego.cloud/book/calendar/HIFCASBADM1?activityDate=2026-05-21T06:00:00.000Z",
        body_text=body_text,
    )
