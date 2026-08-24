import asyncio
import logging
import re

import book_badminton
import pytest
from datetime import date, datetime

from book_badminton import (
    ACCOUNT_A_KEY,
    ACCOUNT_B_KEY,
    ACCOUNT_C_KEY,
    BookingAttemptResult,
    booking_confirmation_detected,
    build_search_window_times,
    load_config,
    SlotPreference,
    build_preferred_starting_from_display_label,
    build_slot_priority,
    compute_target_date_after_next_local_midnight,
    compute_target_date,
    build_available_spaces_button_pattern,
    build_slot_card_pattern,
    build_slot_button_pattern,
    format_date_for_site,
    format_slot_time_range,
    format_time_for_button_label,
    get_account_attempt_times,
    is_book_page_url,
    parse_target_date_override,
    pick_starting_from_select_value,
    post_login_success_detected,
    pick_best_available_slot,
    resolve_target_date,
    seconds_until_next_local_midnight,
    slot_card_text_matches,
    should_use_midnight_prewarm,
)


class StubLocator:
    def __init__(
        self,
        *,
        text="",
        count=1,
        visible=True,
        enabled=True,
        wait_error=None,
        on_click=None,
    ):
        self.text = text
        self.count_value = count
        self.visible = visible
        self.enabled = enabled
        self.wait_error = wait_error
        self.on_click = on_click
        self.click_count = 0

    @property
    def first(self):
        return self

    async def wait_for(self, **_kwargs):
        if self.wait_error is not None:
            raise self.wait_error

    async def click(self, **_kwargs):
        self.click_count += 1
        if self.on_click is not None:
            self.on_click()

    async def count(self):
        return self.count_value

    async def is_visible(self):
        return self.visible

    async def is_enabled(self):
        return self.enabled

    async def inner_text(self, **_kwargs):
        return self.text


class StubPage:
    def __init__(self, locators=None, url="https://example.test/book/basket"):
        self.locators = locators or {}
        self.url = url

    def locator(self, selector):
        return self.locators.get(selector, StubLocator(count=0, visible=False))

    async def wait_for_load_state(self, *_args, **_kwargs):
        return None

    async def wait_for_url(self, pattern, **_kwargs):
        if not pattern.search(self.url):
            raise book_badminton.PlaywrightTimeoutError(
                f"URL {self.url!r} did not match {pattern.pattern!r}"
            )


class AvailableSpacesTestPage:
    def __init__(self, url):
        self.url = url
        self.button = StubLocator()
        self.role_calls = []

    def get_by_role(self, role, name):
        self.role_calls.append((role, name))
        return self.button

    async def wait_for_load_state(self, *_args, **_kwargs):
        return None


class StatefulBasketPage:
    def __init__(self):
        self.state = "slot"
        self.url = "https://example.test/book/calendar"
        self.pending_route = None
        self.go_to_basket = StubLocator(on_click=self._open_basket)
        self.basket_item = StubLocator(
            text=(
                "Badminton\nTue, 11 August, 2026\nJubilee Court 1\n"
                "19:00 - 20:00\n£0.00"
            )
        )
        self.continue_to_payment = StubLocator(on_click=self._open_checkout)
        self.submit_no_price = StubLocator(on_click=self._submit_checkout)

    def _open_basket(self):
        self.pending_route = ("basket", "https://example.test/book/basket")

    def _open_checkout(self):
        self.pending_route = ("checkout", "https://example.test/book/checkout")

    def _submit_checkout(self):
        self.state = "submitted"

    def locator(self, selector):
        if selector == "body":
            body_by_state = {
                "slot": "Added to basket\nGo to your basket",
                "basket": (
                    "Your Basket\nBadminton\nTue, 11 August, 2026\nJubilee Court 1\n"
                    "19:00 - 20:00\n£0.00"
                ),
                "checkout": "Checkout\nTotal £0.00\nConfirm",
                "submitted": "Processing booking",
            }
            return StubLocator(text=body_by_state[self.state])
        if self.state == "slot" and selector == "#go-to-basket-btn":
            return self.go_to_basket
        if self.state == "basket" and selector == ".basket-item":
            return self.basket_item
        if self.state == "basket" and selector == "#continue-to-payment-btn":
            return self.continue_to_payment
        if self.state == "checkout" and selector == "#submit-no-price-btn":
            return self.submit_no_price
        return StubLocator(count=0, visible=False)

    async def wait_for_load_state(self, *_args, **_kwargs):
        return None

    async def wait_for_url(self, pattern, **_kwargs):
        if self.pending_route is not None:
            self.state, self.url = self.pending_route
            self.pending_route = None
        if not pattern.search(self.url):
            raise book_badminton.PlaywrightTimeoutError(
                f"URL {self.url!r} did not match {pattern.pattern!r}"
            )


def make_test_booking_context():
    account = book_badminton.BookingAccountConfig(
        key=ACCOUNT_A_KEY,
        label="账号A",
        username="account-a@example.com",
        password="password",
        search_window_times=("19:00", "21:00"),
        court_priority=(1, 2, 3, 4),
    )
    config = book_badminton.AppConfig(
        booking_url="https://example.test/account",
        timezone_name="Europe/London",
        headless=True,
        dry_run=False,
        debug_pause_seconds=0,
        target_date_override=None,
        accounts=(account,),
    )
    logger = logging.getLogger("booking-helper-test")
    logger.addHandler(logging.NullHandler())
    return config, account, logger


def clear_booking_env(monkeypatch):
    for key in (
        "BOOKING_URL",
        "GYM_USERNAME",
        "GYM_PASSWORD",
        "TIMEZONE",
        "HEADLESS",
        "DRY_RUN",
        "TARGET_DATE_OVERRIDE",
        "DEBUG_PAUSE_SECONDS",
        "PREFERRED_TIMES",
        "PREFERRED_COURTS",
        "SECONDARY_BOOKING_ENABLED",
        "SECONDARY_GYM_USERNAME",
        "SECONDARY_GYM_PASSWORD",
        "SECONDARY_PREFERRED_TIMES",
        "SECONDARY_PREFERRED_COURTS",
        "TERTIARY_BOOKING_ENABLED",
        "TERTIARY_GYM_USERNAME",
        "TERTIARY_GYM_PASSWORD",
        "TERTIARY_PREFERRED_TIMES",
        "TERTIARY_PREFERRED_COURTS",
    ):
        monkeypatch.delenv(key, raising=False)


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


def test_load_config_adds_secondary_and_tertiary_accounts_with_requested_defaults(
    monkeypatch,
):
    monkeypatch.setattr(book_badminton, "load_dotenv", lambda dotenv_path: None)
    clear_booking_env(monkeypatch)
    monkeypatch.setenv("GYM_USERNAME", "primary@example.com")
    monkeypatch.setenv("GYM_PASSWORD", "primary-password")
    monkeypatch.setenv("SECONDARY_BOOKING_ENABLED", "true")
    monkeypatch.setenv("SECONDARY_GYM_USERNAME", "secondary@example.com")
    monkeypatch.setenv("SECONDARY_GYM_PASSWORD", "secondary-password")
    monkeypatch.setenv("TERTIARY_BOOKING_ENABLED", "true")
    monkeypatch.setenv("TERTIARY_GYM_USERNAME", "tertiary@example.com")
    monkeypatch.setenv("TERTIARY_GYM_PASSWORD", "tertiary-password")

    config = load_config()

    assert [account.label for account in config.accounts] == [
        "账号A",
        "账号B",
        "账号C",
    ]
    assert config.accounts[0].key == ACCOUNT_A_KEY
    assert config.accounts[1].key == ACCOUNT_B_KEY
    assert config.accounts[2].key == ACCOUNT_C_KEY
    assert config.accounts[0].search_window_times == ("16:00", "18:00", "20:00")
    assert config.accounts[1].search_window_times == ("16:00", "18:00", "20:00")
    assert config.accounts[2].search_window_times == ("17:00", "19:00", "20:00")
    assert config.accounts[0].court_priority == (1, 2, 3, 4)
    assert config.accounts[1].court_priority == (2, 1, 3, 4)
    assert config.accounts[2].court_priority == (1, 2, 3, 4)


def test_load_config_disables_optional_accounts_when_flags_are_false(monkeypatch):
    monkeypatch.setattr(book_badminton, "load_dotenv", lambda dotenv_path: None)
    clear_booking_env(monkeypatch)
    monkeypatch.setenv("GYM_USERNAME", "primary@example.com")
    monkeypatch.setenv("GYM_PASSWORD", "primary-password")
    monkeypatch.setenv("SECONDARY_BOOKING_ENABLED", "false")
    monkeypatch.setenv("SECONDARY_GYM_USERNAME", "secondary@example.com")
    monkeypatch.setenv("SECONDARY_GYM_PASSWORD", "secondary-password")
    monkeypatch.setenv("TERTIARY_BOOKING_ENABLED", "false")
    monkeypatch.setenv("TERTIARY_GYM_USERNAME", "tertiary@example.com")
    monkeypatch.setenv("TERTIARY_GYM_PASSWORD", "tertiary-password")

    config = load_config()

    assert [account.label for account in config.accounts] == ["账号A"]


def test_account_attempt_times_are_fixed_for_every_target_weekday():
    assert get_account_attempt_times(ACCOUNT_A_KEY) == ("18:00", "16:00", "20:00")
    assert get_account_attempt_times(ACCOUNT_B_KEY) == ("18:00", "16:00", "20:00")
    assert get_account_attempt_times(ACCOUNT_C_KEY) == ("17:00", "19:00", "20:00")


def test_build_search_window_times_always_includes_required_fallback_hours():
    assert build_search_window_times(ACCOUNT_A_KEY, ("19:00",)) == (
        "16:00",
        "18:00",
        "19:00",
        "20:00",
    )
    assert build_search_window_times(ACCOUNT_B_KEY, ("21:00",)) == (
        "16:00",
        "18:00",
        "20:00",
        "21:00",
    )
    assert build_search_window_times(ACCOUNT_C_KEY, ("18:00",)) == (
        "17:00",
        "18:00",
        "19:00",
        "20:00",
    )


def test_account_b_phase_excludes_the_exact_court_claimed_by_account_a():
    account_b = book_badminton.BookingAccountConfig(
        key=ACCOUNT_B_KEY,
        label="账号B",
        username="b@example.com",
        password="password",
        search_window_times=("18:00", "16:00", "20:00"),
        court_priority=(2, 1, 3, 4),
    )
    coordinator = book_badminton.BookingCoordinator(
        target_date=date(2026, 8, 30)
    )
    assert book_badminton.claim_slot_for_account(
        coordinator,
        ACCOUNT_A_KEY,
        SlotPreference("18:00", 2),
    )

    assert book_badminton.build_phase_preferences(
        account_b,
        "18:00",
        coordinator,
    ) == [
        SlotPreference("18:00", 1),
        SlotPreference("18:00", 3),
        SlotPreference("18:00", 4),
    ]
    assert book_badminton.build_phase_preferences(
        account_b,
        "18:00",
    ) == [
        SlotPreference("18:00", 2),
        SlotPreference("18:00", 1),
        SlotPreference("18:00", 3),
        SlotPreference("18:00", 4),
    ]


def test_slot_claims_are_atomic_and_persist_across_account_retries():
    coordinator = book_badminton.BookingCoordinator(
        target_date=date(2026, 8, 30)
    )
    court_1 = SlotPreference("20:00", 1)
    court_2 = SlotPreference("20:00", 2)

    assert book_badminton.claim_slot_for_account(
        coordinator,
        ACCOUNT_A_KEY,
        court_1,
    )
    assert book_badminton.claim_slot_for_account(
        coordinator,
        ACCOUNT_A_KEY,
        court_1,
    )
    assert not book_badminton.claim_slot_for_account(
        coordinator,
        ACCOUNT_C_KEY,
        court_1,
    )
    assert book_badminton.claim_slot_for_account(
        coordinator,
        ACCOUNT_C_KEY,
        court_2,
    )
    assert not book_badminton.claim_slot_for_account(
        coordinator,
        ACCOUNT_A_KEY,
        court_2,
    )
    assert coordinator.slot_claim_owners == {
        court_1: ACCOUNT_A_KEY,
        court_2: ACCOUNT_C_KEY,
    }


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


def test_seconds_until_next_local_midnight_uses_london_clock():
    seconds = seconds_until_next_local_midnight(
        timezone_name="Europe/London",
        now=datetime.fromisoformat("2026-05-24T22:59:30+00:00"),
    )

    assert seconds == 30.0


def test_should_use_midnight_prewarm_only_in_short_window_before_midnight():
    assert should_use_midnight_prewarm(
        timezone_name="Europe/London",
        target_date_override=None,
        now=datetime.fromisoformat("2026-05-24T22:59:30+00:00"),
    )
    assert not should_use_midnight_prewarm(
        timezone_name="Europe/London",
        target_date_override=None,
        now=datetime.fromisoformat("2026-05-24T22:55:00+00:00"),
    )


def test_should_use_midnight_prewarm_is_disabled_by_target_date_override():
    assert not should_use_midnight_prewarm(
        timezone_name="Europe/London",
        target_date_override="2026-06-01",
        now=datetime.fromisoformat("2026-05-24T22:59:30+00:00"),
    )


def test_compute_target_date_after_next_local_midnight_uses_post_midnight_day():
    resolved = compute_target_date_after_next_local_midnight(
        timezone_name="Europe/London",
        now=datetime.fromisoformat("2026-05-24T22:59:30+00:00"),
    )

    assert resolved == date(2026, 6, 2)


def test_format_date_for_site_returns_dd_mm_yyyy():
    target_date = datetime.fromisoformat("2026-05-22T12:00:00").date()

    assert format_date_for_site(target_date) == "22/05/2026"


def test_setup_logging_records_three_digit_milliseconds(tmp_path, monkeypatch):
    monkeypatch.setattr(book_badminton, "LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr(book_badminton, "SCREENSHOTS_DIR", tmp_path / "screenshots")

    logger, log_path = book_badminton.setup_logging("Europe/London")
    logger.info("millisecond-check")
    for handler in tuple(logger.handlers):
        handler.flush()
        handler.close()
        logger.removeHandler(handler)

    log_line = log_path.read_text(encoding="utf-8").strip()
    assert re.match(
        r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} \| "
        r"INFO \| millisecond-check$",
        log_line,
    )


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


def test_slot_card_text_matches_requires_exact_time_range_for_slot():
    slot = SlotPreference(start_time="19:00", court_number=1)

    assert slot_card_text_matches(
        "Jubilee Court 1 19:00 - 20:00 Thu 28th May This slot is unavailable",
        slot,
    )
    assert not slot_card_text_matches(
        "Jubilee Court 1 07:00 - 08:00 Thu 28th May Book now",
        slot,
    )


def test_build_slot_button_pattern_does_not_confuse_7pm_with_7am():
    slot = SlotPreference(start_time="19:00", court_number=1)
    pattern = build_slot_button_pattern(slot)

    assert not pattern.search("Book now: for Jubilee Court 1 at  7:00 AM Thursday, May 28, 2026")
    assert pattern.search("Book now: for Jubilee Court 1 at  7:00 PM Thursday, May 28, 2026")


def test_build_available_spaces_button_pattern_matches_target_day_not_hard_coded():
    target_date = datetime.fromisoformat("2026-05-22T12:00:00").date()
    pattern = build_available_spaces_button_pattern(target_date)

    assert pattern.search(
        "Badminton starts on Fri, 22nd May, at 07:00 AM: See available spaces"
    )
    assert not pattern.search(
        "Badminton starts on Thu, 21st May, at 07:00 AM: See available spaces"
    )


def test_calendar_activity_date_uses_london_date_at_bst_boundary():
    assert book_badminton.calendar_activity_date_from_url(
        "https://example.test/book/calendar/activity?"
        "activityDate=2026-08-30T23%3A30%3A00.000Z",
        "Europe/London",
    ) == date(2026, 8, 31)


def test_calendar_target_date_validation_is_fail_closed():
    matching_url = (
        "https://example.test/book/calendar/activity?"
        "activityDate=2026-08-31T15%3A00%3A00.000Z"
    )
    book_badminton.validate_calendar_target_date(
        matching_url,
        date(2026, 8, 31),
        "Europe/London",
    )

    with pytest.raises(book_badminton.TargetDateValidationError):
        book_badminton.validate_calendar_target_date(
            matching_url,
            date(2026, 8, 30),
            "Europe/London",
        )
    with pytest.raises(book_badminton.TargetDateValidationError):
        book_badminton.validate_calendar_target_date(
            "https://example.test/book/calendar/activity",
            date(2026, 8, 31),
            "Europe/London",
        )
    with pytest.raises(book_badminton.TargetDateValidationError):
        book_badminton.validate_calendar_target_date(
            "https://example.test/book/calendar/activity?activityDate=not-a-date",
            date(2026, 8, 31),
            "Europe/London",
        )


def test_open_available_spaces_clicks_only_exact_date_and_validates_url(monkeypatch):
    config, account, logger = make_test_booking_context()
    target_date = date(2026, 8, 31)
    page = AvailableSpacesTestPage(
        "https://example.test/book/calendar/activity?"
        "activityDate=2026-08-31T15%3A00%3A00.000Z"
    )

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(book_badminton, "check_for_access_blockers", no_op)
    monkeypatch.setattr(book_badminton, "wait_for_available_spaces_content", no_op)

    asyncio.run(
        book_badminton.open_available_spaces(
            page,
            target_date,
            config,
            account,
            logger,
        )
    )

    assert page.button.click_count == 1
    assert len(page.role_calls) == 1
    assert page.role_calls[0][0] == "button"
    assert page.role_calls[0][1].search(
        "Badminton starts on Mon, 31st August, at 06:00 PM: See available spaces"
    )


def test_open_available_spaces_stops_on_wrong_calendar_date(monkeypatch):
    config, account, logger = make_test_booking_context()
    page = AvailableSpacesTestPage(
        "https://example.test/book/calendar/activity?"
        "activityDate=2026-08-30T15%3A00%3A00.000Z"
    )

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(book_badminton, "check_for_access_blockers", no_op)
    monkeypatch.setattr(book_badminton, "wait_for_available_spaces_content", no_op)

    with pytest.raises(book_badminton.TargetDateValidationError):
        asyncio.run(
            book_badminton.open_available_spaces(
                page,
                date(2026, 8, 31),
                config,
                account,
                logger,
            )
        )

    assert page.button.click_count == 1
    assert len(page.role_calls) == 1


def test_open_available_spaces_never_falls_back_to_an_arbitrary_date(monkeypatch):
    config, account, logger = make_test_booking_context()
    page = AvailableSpacesTestPage(
        "https://example.test/book/search-results"
    )
    page.button.wait_error = book_badminton.PlaywrightTimeoutError(
        "exact target date was not present"
    )

    with pytest.raises(book_badminton.PlaywrightTimeoutError):
        asyncio.run(
            book_badminton.open_available_spaces(
                page,
                date(2026, 8, 31),
                config,
                account,
                logger,
            )
        )

    assert len(page.role_calls) == 1
    assert page.button.click_count == 0


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


def test_basket_state_detectors_match_added_and_conflict_messages():
    assert book_badminton.basket_item_added_detected(
        "Jubilee Court 1\nAdded to basket\nGo to your basket"
    )
    assert book_badminton.basket_slot_conflict_detected(
        "You already have a booking for this slot in your basket."
    )
    assert not book_badminton.basket_item_added_detected(
        "Booking Confirmed!"
    )


def test_basket_contains_expected_slot_requires_activity_date_court_and_time():
    slot = SlotPreference("19:00", 1)
    target_date = date(2026, 8, 11)

    assert book_badminton.basket_contains_expected_slot(
        (
            "Badminton\nTue, 11 August, 2026\nJubilee Court 1\n"
            "19:00 - 20:00\n£0.00"
        ),
        slot,
        target_date,
    )
    assert not book_badminton.basket_contains_expected_slot(
        (
            "Badminton\nTue, 11 August, 2026\nJubilee Court 2\n"
            "19:00 - 20:00\n£0.00"
        ),
        slot,
        target_date,
    )
    assert not book_badminton.basket_contains_expected_slot(
        (
            "Badminton\nTue, 11 August, 2026\nJubilee Court 1\n"
            "20:00 - 21:00\n£0.00"
        ),
        slot,
        target_date,
    )
    assert not book_badminton.basket_contains_expected_slot(
        (
            "Badminton\nWed, 12 August, 2026\nJubilee Court 1\n"
            "19:00 - 20:00\n£0.00"
        ),
        slot,
        target_date,
    )
    assert not book_badminton.basket_contains_expected_slot(
        (
            "Squash\nTue, 11 August, 2026\nJubilee Court 1\n"
            "19:00 - 20:00\n£0.00"
        ),
        slot,
        target_date,
    )


def test_recover_pending_basket_booking_completes_zero_price_checkout(monkeypatch):
    config, account, logger = make_test_booking_context()
    slot = SlotPreference("19:00", 1)
    page = StatefulBasketPage()

    async def no_access_blockers(*_args, **_kwargs):
        return None

    async def confirmation_detected(*_args, **_kwargs):
        return True

    monkeypatch.setattr(book_badminton, "check_for_access_blockers", no_access_blockers)
    monkeypatch.setattr(
        book_badminton,
        "wait_for_booking_confirmation_page",
        confirmation_detected,
    )

    recovered = asyncio.run(
        book_badminton.recover_pending_basket_booking(
            page,
            slot,
            date(2026, 8, 11),
            config,
            account,
            logger,
        )
    )

    assert recovered is True
    assert page.go_to_basket.click_count == 1
    assert page.continue_to_payment.click_count == 1
    assert page.submit_no_price.click_count == 1
    assert page.state == "submitted"


def test_recover_pending_basket_booking_rejects_mismatched_item(monkeypatch):
    config, account, logger = make_test_booking_context()
    page = StubPage(
        {
            "body": StubLocator(
                text=(
                    "Your Basket\nBadminton\nTue, 11 August, 2026\n"
                    "Jubilee Court 1\n19:00 - 20:00\n£0.00"
                )
            ),
            "#go-to-basket-btn": StubLocator(),
            ".basket-item": StubLocator(
                text=(
                    "Badminton\nTue, 11 August, 2026\nJubilee Court 4\n"
                    "19:00 - 20:00\n£0.00"
                ),
                count=1,
            ),
        }
    )

    async def no_access_blockers(*_args, **_kwargs):
        return None

    monkeypatch.setattr(book_badminton, "check_for_access_blockers", no_access_blockers)

    async def scenario():
        try:
            await book_badminton.recover_pending_basket_booking(
                page,
                SlotPreference("19:00", 1),
                date(2026, 8, 11),
                config,
                account,
                logger,
            )
        except book_badminton.BasketRecoveryError:
            return
        raise AssertionError("Expected mismatched basket item to stop recovery")

    asyncio.run(scenario())


def test_try_book_slot_recovers_when_final_button_is_missing_because_item_is_in_basket(
    monkeypatch,
):
    config, account, logger = make_test_booking_context()
    initial_button = StubLocator()
    final_button = StubLocator(
        wait_error=book_badminton.PlaywrightTimeoutError("final button missing")
    )
    page = StubPage()
    recovery_calls = []

    async def visible_slot(*_args, **_kwargs):
        return initial_button

    async def no_access_blockers(*_args, **_kwargs):
        return None

    async def find_final(*_args, **_kwargs):
        return final_button

    async def recover(*_args, **_kwargs):
        recovery_calls.append(True)
        return "confirmed"

    monkeypatch.setattr(book_badminton, "get_visible_slot_button", visible_slot)
    monkeypatch.setattr(book_badminton, "check_for_access_blockers", no_access_blockers)
    monkeypatch.setattr(book_badminton, "find_final_book_button", find_final)
    monkeypatch.setattr(book_badminton, "recover_basket_state_if_present", recover)

    outcome = asyncio.run(
        book_badminton.try_book_slot(
            page,
            "19:00",
            1,
            date(2026, 8, 11),
            config,
            account,
            logger,
        )
    )

    assert outcome == "confirmed"
    assert initial_button.click_count == 1
    assert recovery_calls == [True]


def test_try_book_slot_recovers_actual_post_click_added_to_basket_path(monkeypatch):
    config, account, logger = make_test_booking_context()
    page = StatefulBasketPage()
    initial_button = StubLocator()
    final_button = StubLocator()
    confirmation_checks = []

    async def visible_slot(*_args, **_kwargs):
        return initial_button

    async def no_access_blockers(*_args, **_kwargs):
        return None

    async def find_final(*_args, **_kwargs):
        return final_button

    async def confirmation_sequence(*_args, **_kwargs):
        confirmation_checks.append(True)
        return len(confirmation_checks) == 2

    monkeypatch.setattr(book_badminton, "get_visible_slot_button", visible_slot)
    monkeypatch.setattr(book_badminton, "check_for_access_blockers", no_access_blockers)
    monkeypatch.setattr(book_badminton, "find_final_book_button", find_final)
    monkeypatch.setattr(
        book_badminton,
        "wait_for_booking_confirmation_page",
        confirmation_sequence,
    )

    outcome = asyncio.run(
        book_badminton.try_book_slot(
            page,
            "19:00",
            1,
            date(2026, 8, 11),
            config,
            account,
            logger,
        )
    )

    assert outcome == "confirmed"
    assert final_button.click_count == 1
    assert page.go_to_basket.click_count == 1
    assert page.continue_to_payment.click_count == 1
    assert page.submit_no_price.click_count == 1
    assert len(confirmation_checks) == 2


def test_book_best_available_slot_stops_after_basket_recovery_confirms(monkeypatch):
    config, account, logger = make_test_booking_context()
    attempted_slots = []

    async def visible_slots(*_args, **_kwargs):
        return [
            "19:00 Jubilee Court 1",
            "19:00 Jubilee Court 2",
        ]

    async def confirmed_first_attempt(
        _page,
        start_time,
        court_number,
        *_args,
        **_kwargs,
    ):
        attempted_slots.append((start_time, court_number))
        return "confirmed"

    monkeypatch.setattr(book_badminton, "list_visible_bookable_slots", visible_slots)
    monkeypatch.setattr(book_badminton, "try_book_slot", confirmed_first_attempt)

    result = asyncio.run(
        book_badminton.book_best_available_slot(
            StubPage(),
            config,
            account,
            logger,
            target_date=date(2026, 8, 11),
            preferences=(
                SlotPreference("19:00", 1),
                SlotPreference("19:00", 2),
            ),
            phase_name="initial",
        )
    )

    assert result == BookingAttemptResult(
        slot=SlotPreference("19:00", 1),
        outcome="confirmed",
    )
    assert attempted_slots == [("19:00", 1)]


def test_book_best_available_slot_never_clicks_a_court_claimed_by_another_account(
    monkeypatch,
):
    config, account, logger = make_test_booking_context()
    config = book_badminton.AppConfig(
        booking_url=config.booking_url,
        timezone_name=config.timezone_name,
        headless=config.headless,
        dry_run=True,
        debug_pause_seconds=config.debug_pause_seconds,
        target_date_override=config.target_date_override,
        accounts=config.accounts,
    )
    court_1_button = StubLocator()
    court_2_button = StubLocator()
    claim_attempts = []

    async def visible_button(_page, slot):
        return {
            SlotPreference("19:00", 1): court_1_button,
            SlotPreference("19:00", 2): court_2_button,
        }.get(slot)

    async def no_access_blockers(*_args, **_kwargs):
        return None

    def claim_slot(slot):
        claim_attempts.append(slot)
        return slot.court_number == 2

    monkeypatch.setattr(book_badminton, "get_visible_slot_button", visible_button)
    monkeypatch.setattr(book_badminton, "check_for_access_blockers", no_access_blockers)

    result = asyncio.run(
        book_badminton.book_best_available_slot(
            StubPage(),
            config,
            account,
            logger,
            target_date=date(2026, 8, 11),
            preferences=(
                SlotPreference("19:00", 1),
                SlotPreference("19:00", 2),
            ),
            phase_name="19:00",
            claim_slot=claim_slot,
        )
    )

    assert result == BookingAttemptResult(
        SlotPreference("19:00", 2),
        "dry-run",
    )
    assert claim_attempts == [
        SlotPreference("19:00", 1),
        SlotPreference("19:00", 2),
    ]
    assert court_1_button.click_count == 0
    assert court_2_button.click_count == 1


def test_try_book_slot_treats_plain_unavailable_final_button_timeout_as_recoverable(
    monkeypatch,
):
    config, account, logger = make_test_booking_context()
    final_button = StubLocator(
        wait_error=book_badminton.PlaywrightTimeoutError("final button missing")
    )
    page = StubPage()
    close_calls = []

    async def visible_slot(*_args, **_kwargs):
        return StubLocator()

    async def no_access_blockers(*_args, **_kwargs):
        return None

    async def find_final(*_args, **_kwargs):
        return final_button

    async def no_basket_state(*_args, **_kwargs):
        return None

    async def slot_unavailable(*_args, **_kwargs):
        return True

    async def record_close(*_args, **_kwargs):
        close_calls.append(True)

    monkeypatch.setattr(book_badminton, "get_visible_slot_button", visible_slot)
    monkeypatch.setattr(book_badminton, "check_for_access_blockers", no_access_blockers)
    monkeypatch.setattr(book_badminton, "find_final_book_button", find_final)
    monkeypatch.setattr(
        book_badminton,
        "recover_basket_state_if_present",
        no_basket_state,
    )
    monkeypatch.setattr(book_badminton, "current_slot_is_unavailable", slot_unavailable)
    monkeypatch.setattr(book_badminton, "close_open_slot_panel", record_close)

    outcome = asyncio.run(
        book_badminton.try_book_slot(
            page,
            "19:00",
            1,
            date(2026, 8, 11),
            config,
            account,
            logger,
        )
    )

    assert outcome == "unconfirmed"
    assert close_calls == [True]


def test_try_book_slot_keeps_normal_direct_confirmation_path_unchanged(monkeypatch):
    config, account, logger = make_test_booking_context()
    initial_button = StubLocator()
    final_button = StubLocator()
    page = StubPage()

    async def visible_slot(*_args, **_kwargs):
        return initial_button

    async def no_access_blockers(*_args, **_kwargs):
        return None

    async def find_final(*_args, **_kwargs):
        return final_button

    async def confirmation_detected(*_args, **_kwargs):
        return True

    async def unexpected_recovery(*_args, **_kwargs):
        raise AssertionError("Normal confirmation must not enter basket recovery")

    monkeypatch.setattr(book_badminton, "get_visible_slot_button", visible_slot)
    monkeypatch.setattr(book_badminton, "check_for_access_blockers", no_access_blockers)
    monkeypatch.setattr(book_badminton, "find_final_book_button", find_final)
    monkeypatch.setattr(
        book_badminton,
        "wait_for_booking_confirmation_page",
        confirmation_detected,
    )
    monkeypatch.setattr(
        book_badminton,
        "recover_basket_state_if_present",
        unexpected_recovery,
    )

    outcome = asyncio.run(
        book_badminton.try_book_slot(
            page,
            "19:00",
            1,
            date(2026, 8, 11),
            config,
            account,
            logger,
        )
    )

    assert outcome == "confirmed"
    assert initial_button.click_count == 1
    assert final_button.click_count == 1


def test_try_book_slot_does_not_retry_unknown_state_after_final_click(monkeypatch):
    config, account, logger = make_test_booking_context()
    initial_button = StubLocator()
    final_button = StubLocator()
    page = StubPage()
    recovery_calls = []

    async def visible_slot(*_args, **_kwargs):
        return initial_button

    async def no_access_blockers(*_args, **_kwargs):
        return None

    async def find_final(*_args, **_kwargs):
        return final_button

    async def broken_confirmation_poll(*_args, **_kwargs):
        raise book_badminton.PlaywrightError("confirmation page closed")

    async def no_recoverable_state(*_args, **_kwargs):
        recovery_calls.append(True)
        return None

    monkeypatch.setattr(book_badminton, "get_visible_slot_button", visible_slot)
    monkeypatch.setattr(book_badminton, "check_for_access_blockers", no_access_blockers)
    monkeypatch.setattr(book_badminton, "find_final_book_button", find_final)
    monkeypatch.setattr(
        book_badminton,
        "wait_for_booking_confirmation_page",
        broken_confirmation_poll,
    )
    monkeypatch.setattr(
        book_badminton,
        "recover_basket_state_if_present",
        no_recoverable_state,
    )

    async def scenario():
        try:
            await book_badminton.try_book_slot(
                page,
                "19:00",
                1,
                date(2026, 8, 11),
                config,
                account,
                logger,
            )
        except book_badminton.BasketRecoveryError:
            return
        raise AssertionError("Expected an unknown post-click state to stop safely")

    asyncio.run(scenario())

    assert final_button.click_count == 1
    assert recovery_calls == [True]


def test_pick_starting_from_select_value_prefers_starting_now_when_present():
    options = [
        ("0", "Starting now", False),
        ("1", "From 01:00", False),
        ("2", "From 02:00", False),
    ]

    assert pick_starting_from_select_value(options) == "0"


def test_build_preferred_starting_from_display_label_uses_earliest_preferred_time():
    assert build_preferred_starting_from_display_label(["19:00", "18:00"]) == "From 18:00"


def test_pick_starting_from_select_value_prefers_configured_evening_filter_when_available():
    options = [
        ("0", "Starting now", False),
        ("18", "From 18:00", False),
        ("19", "From 19:00", False),
    ]

    assert pick_starting_from_select_value(options, preferred_times=["19:00", "18:00"]) == "18"


def test_pick_starting_from_select_value_prefers_midnight_style_label_when_present():
    options = [
        ("", "Starting from", False),
        ("00", "From 00:00", False),
        ("01", "From 01:00", False),
    ]

    assert pick_starting_from_select_value(options) == "00"


def test_pick_starting_from_select_value_falls_back_to_first_enabled_option():
    options = [
        ("", "Starting from", False),
        ("0", "Any time", True),
        ("1", "From 01:00", False),
        ("2", "From 02:00", False),
    ]

    assert pick_starting_from_select_value(options) == "1"


class RetryTestPage:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class RetryTestContext:
    def __init__(self):
        self.page = RetryTestPage()
        self.closed = False

    async def new_page(self):
        return self.page

    async def close(self):
        self.closed = True


class RetryTestBrowser:
    def __init__(self):
        self.contexts = []

    async def new_context(self, **_kwargs):
        context = RetryTestContext()
        self.contexts.append(context)
        return context


def make_retry_test_context(account_key=ACCOUNT_C_KEY):
    labels = {
        ACCOUNT_A_KEY: "账号A",
        ACCOUNT_B_KEY: "账号B",
        ACCOUNT_C_KEY: "账号C",
    }
    courts = (2, 1, 3, 4) if account_key == ACCOUNT_B_KEY else (1, 2, 3, 4)
    account = book_badminton.BookingAccountConfig(
        key=account_key,
        label=labels[account_key],
        username=f"{account_key}@example.com",
        password="password",
        search_window_times=tuple(
            sorted(get_account_attempt_times(account_key))
        ),
        court_priority=courts,
    )
    config = book_badminton.AppConfig(
        booking_url="https://example.test/account",
        timezone_name="Europe/London",
        headless=True,
        dry_run=False,
        debug_pause_seconds=0,
        target_date_override=None,
        accounts=(account,),
    )
    coordinator = book_badminton.BookingCoordinator(target_date=date(2026, 8, 29))
    logger = logging.getLogger(f"retry-test-{account_key}")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    return config, account, coordinator, logger


def test_transient_failure_uses_fresh_login_and_resumes_incomplete_phase(monkeypatch):
    config, account, coordinator, logger = make_retry_test_context()
    browser = RetryTestBrowser()
    login_calls = []
    phase_calls = []

    async def successful_login(*_args, **_kwargs):
        login_calls.append(True)

    async def no_op(*_args, **_kwargs):
        return None

    async def no_screenshot(*_args, **_kwargs):
        return None

    async def phase_result(*_args, **kwargs):
        phase_name = kwargs["phase_name"]
        preferences = kwargs["preferences"]
        phase_calls.append((phase_name, tuple(preferences)))
        if len(phase_calls) == 1:
            return None
        if len(phase_calls) == 2:
            raise book_badminton.PlaywrightTimeoutError("page stopped responding")
        return BookingAttemptResult(
            slot=SlotPreference("19:00", 1),
            outcome="confirmed",
        )

    monkeypatch.setattr(book_badminton, "login", successful_login)
    monkeypatch.setattr(book_badminton, "open_booking_search", no_op)
    monkeypatch.setattr(book_badminton, "search_badminton", no_op)
    monkeypatch.setattr(book_badminton, "open_available_spaces", no_op)
    monkeypatch.setattr(book_badminton, "book_best_available_slot", phase_result)
    monkeypatch.setattr(book_badminton, "save_failure_screenshot", no_screenshot)
    monkeypatch.setattr(book_badminton, "ACCOUNT_RETRY_DELAY_SECONDS", 0)

    exit_code = asyncio.run(
        book_badminton.run_account_booking(
            browser,
            config,
            account,
            logger,
            coordinator,
        )
    )

    assert exit_code == 0
    assert len(login_calls) == 2
    assert len(browser.contexts) == 2
    assert all(context.closed and context.page.closed for context in browser.contexts)
    assert [phase_name for phase_name, _ in phase_calls] == [
        "17:00",
        "19:00",
        "19:00",
    ]
    assert [slot.court_number for slot in phase_calls[-1][1]] == [1, 2, 3, 4]


def test_transient_failure_stops_after_three_fresh_sessions(monkeypatch):
    config, account, coordinator, logger = make_retry_test_context()
    browser = RetryTestBrowser()
    login_calls = []

    async def failed_login(*_args, **_kwargs):
        login_calls.append(True)
        raise book_badminton.PlaywrightTimeoutError("login page stopped responding")

    async def no_screenshot(*_args, **_kwargs):
        return None

    monkeypatch.setattr(book_badminton, "login", failed_login)
    monkeypatch.setattr(book_badminton, "save_failure_screenshot", no_screenshot)
    monkeypatch.setattr(book_badminton, "ACCOUNT_RETRY_DELAY_SECONDS", 0)

    exit_code = asyncio.run(
        book_badminton.run_account_booking(
            browser,
            config,
            account,
            logger,
            coordinator,
        )
    )

    assert exit_code == 1
    assert len(login_calls) == 3
    assert len(browser.contexts) == 3
    assert all(context.closed and context.page.closed for context in browser.contexts)


def test_nonretryable_failure_stops_once_and_releases_account_b_waiters(monkeypatch):
    config, account, coordinator, logger = make_retry_test_context(ACCOUNT_A_KEY)
    browser = RetryTestBrowser()
    login_calls = []

    async def blocked_login(*_args, **_kwargs):
        login_calls.append(True)
        raise book_badminton.AccessBlockerError("rate limit")

    async def no_screenshot(*_args, **_kwargs):
        return None

    monkeypatch.setattr(book_badminton, "login", blocked_login)
    monkeypatch.setattr(book_badminton, "save_failure_screenshot", no_screenshot)

    exit_code = asyncio.run(
        book_badminton.run_account_booking(
            browser,
            config,
            account,
            logger,
            coordinator,
        )
    )

    assert exit_code == 1
    assert len(login_calls) == 1
    assert len(browser.contexts) == 1
    assert all(
        book_badminton.get_phase_selection_event(
            coordinator,
            ACCOUNT_A_KEY,
            start_time,
        ).is_set()
        for start_time in get_account_attempt_times(ACCOUNT_A_KEY)
    )


def test_three_accounts_complete_primary_phases_without_priority_deadlock(monkeypatch):
    account_a = make_retry_test_context(ACCOUNT_A_KEY)[1]
    account_b = make_retry_test_context(ACCOUNT_B_KEY)[1]
    account_c = make_retry_test_context(ACCOUNT_C_KEY)[1]
    config = book_badminton.AppConfig(
        booking_url="https://example.test/account",
        timezone_name="Europe/London",
        headless=True,
        dry_run=False,
        debug_pause_seconds=0,
        target_date_override=None,
        accounts=(account_a, account_b, account_c),
    )
    coordinator = book_badminton.BookingCoordinator(target_date=date(2026, 8, 30))
    browser = RetryTestBrowser()
    logger = logging.getLogger("three-account-priority-test")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    phase_calls = {}

    async def no_op(*_args, **_kwargs):
        return None

    async def confirmed_primary(*_args, **kwargs):
        account = _args[2]
        preferences = tuple(kwargs["preferences"])
        phase_calls[account.key] = preferences
        return BookingAttemptResult(slot=preferences[0], outcome="confirmed")

    monkeypatch.setattr(book_badminton, "login", no_op)
    monkeypatch.setattr(book_badminton, "open_booking_search", no_op)
    monkeypatch.setattr(book_badminton, "search_badminton", no_op)
    monkeypatch.setattr(book_badminton, "open_available_spaces", no_op)
    monkeypatch.setattr(book_badminton, "book_best_available_slot", confirmed_primary)

    async def scenario():
        return await asyncio.wait_for(
            asyncio.gather(
                *(
                    book_badminton.run_account_booking(
                        browser,
                        config,
                        account,
                        logger,
                        coordinator,
                    )
                    for account in config.accounts
                )
            ),
            timeout=1,
        )

    exit_codes = asyncio.run(scenario())

    assert exit_codes == [0, 0, 0]
    assert phase_calls[ACCOUNT_A_KEY][0] == SlotPreference("18:00", 1)
    assert phase_calls[ACCOUNT_C_KEY][0] == SlotPreference("17:00", 1)
    assert phase_calls[ACCOUNT_B_KEY] == (
        SlotPreference("18:00", 2),
        SlotPreference("18:00", 3),
        SlotPreference("18:00", 4),
    )
    assert len(browser.contexts) == 3
    assert all(context.closed and context.page.closed for context in browser.contexts)


def test_account_b_starts_after_a_claim_before_a_confirmation(monkeypatch):
    account_a = make_retry_test_context(ACCOUNT_A_KEY)[1]
    account_b = make_retry_test_context(ACCOUNT_B_KEY)[1]
    config = book_badminton.AppConfig(
        booking_url="https://example.test/account",
        timezone_name="Europe/London",
        headless=True,
        dry_run=False,
        debug_pause_seconds=0,
        target_date_override=None,
        accounts=(account_a, account_b),
    )
    browser = RetryTestBrowser()
    logger = logging.getLogger("early-account-b-test")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    account_b_preferences = []

    async def no_op(*_args, **_kwargs):
        return None

    monkeypatch.setattr(book_badminton, "login", no_op)
    monkeypatch.setattr(book_badminton, "open_booking_search", no_op)
    monkeypatch.setattr(book_badminton, "search_badminton", no_op)
    monkeypatch.setattr(book_badminton, "open_available_spaces", no_op)

    async def scenario():
        coordinator = book_badminton.BookingCoordinator(
            target_date=date(2026, 8, 30)
        )
        account_a_confirmation = asyncio.Event()
        account_b_started = asyncio.Event()

        async def controlled_confirmation(*args, **kwargs):
            account = args[2]
            preferences = tuple(kwargs["preferences"])
            claim_slot = kwargs["claim_slot"]
            if account.key == ACCOUNT_A_KEY:
                selected = preferences[0]
                assert claim_slot(selected)
                await account_a_confirmation.wait()
                return BookingAttemptResult(selected, "confirmed")

            account_b_preferences.extend(preferences)
            selected = next(slot for slot in preferences if claim_slot(slot))
            account_b_started.set()
            return BookingAttemptResult(selected, "confirmed")

        monkeypatch.setattr(
            book_badminton,
            "book_best_available_slot",
            controlled_confirmation,
        )
        tasks = [
            asyncio.create_task(
                book_badminton.run_account_booking(
                    browser,
                    config,
                    account,
                    logger,
                    coordinator,
                )
            )
            for account in config.accounts
        ]
        await asyncio.wait_for(account_b_started.wait(), timeout=1)
        assert not account_a_confirmation.is_set()
        account_a_confirmation.set()
        return await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)

    exit_codes = asyncio.run(scenario())

    assert exit_codes == [0, 0]
    assert SlotPreference("18:00", 1) not in account_b_preferences
    assert account_b_preferences[0] == SlotPreference("18:00", 2)


def test_all_accounts_use_unique_claims_when_they_fall_back_to_20(monkeypatch):
    account_a = make_retry_test_context(ACCOUNT_A_KEY)[1]
    account_b = make_retry_test_context(ACCOUNT_B_KEY)[1]
    account_c = make_retry_test_context(ACCOUNT_C_KEY)[1]
    config = book_badminton.AppConfig(
        booking_url="https://example.test/account",
        timezone_name="Europe/London",
        headless=True,
        dry_run=False,
        debug_pause_seconds=0,
        target_date_override=None,
        accounts=(account_a, account_b, account_c),
    )
    coordinator = book_badminton.BookingCoordinator(
        target_date=date(2026, 8, 30)
    )
    browser = RetryTestBrowser()
    logger = logging.getLogger("shared-20-claim-test")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    selected_slots = {}

    async def no_op(*_args, **_kwargs):
        return None

    async def fall_back_to_20(*args, **kwargs):
        account = args[2]
        if kwargs["phase_name"] != "20:00":
            return None
        claim_slot = kwargs["claim_slot"]
        for slot in kwargs["preferences"]:
            if claim_slot(slot):
                selected_slots[account.key] = slot
                return BookingAttemptResult(slot, "confirmed")
        return None

    monkeypatch.setattr(book_badminton, "login", no_op)
    monkeypatch.setattr(book_badminton, "open_booking_search", no_op)
    monkeypatch.setattr(book_badminton, "search_badminton", no_op)
    monkeypatch.setattr(book_badminton, "open_available_spaces", no_op)
    monkeypatch.setattr(
        book_badminton,
        "book_best_available_slot",
        fall_back_to_20,
    )

    async def scenario():
        return await asyncio.wait_for(
            asyncio.gather(
                *(
                    book_badminton.run_account_booking(
                        browser,
                        config,
                        account,
                        logger,
                        coordinator,
                    )
                    for account in config.accounts
                )
            ),
            timeout=1,
        )

    exit_codes = asyncio.run(scenario())

    assert exit_codes == [0, 0, 0]
    assert set(selected_slots) == {
        ACCOUNT_A_KEY,
        ACCOUNT_B_KEY,
        ACCOUNT_C_KEY,
    }
    assert {slot.start_time for slot in selected_slots.values()} == {"20:00"}
    assert len(set(selected_slots.values())) == 3
    assert {slot.court_number for slot in selected_slots.values()} == {1, 2, 3}


def test_full_page_diagnostics_run_once_only_after_all_phases_exhaust(monkeypatch):
    config, account, coordinator, logger = make_retry_test_context(ACCOUNT_C_KEY)
    browser = RetryTestBrowser()
    phase_calls = []
    diagnostic_calls = []

    async def no_op(*_args, **_kwargs):
        return None

    async def no_slots(*_args, **kwargs):
        phase_calls.append(kwargs["phase_name"])
        return None

    async def record_diagnostics(*_args, **_kwargs):
        diagnostic_calls.append(True)

    monkeypatch.setattr(book_badminton, "login", no_op)
    monkeypatch.setattr(book_badminton, "open_booking_search", no_op)
    monkeypatch.setattr(book_badminton, "search_badminton", no_op)
    monkeypatch.setattr(book_badminton, "open_available_spaces", no_op)
    monkeypatch.setattr(book_badminton, "book_best_available_slot", no_slots)
    monkeypatch.setattr(
        book_badminton,
        "log_available_spaces_diagnostics",
        record_diagnostics,
    )

    exit_code = asyncio.run(
        book_badminton.run_account_booking(
            browser,
            config,
            account,
            logger,
            coordinator,
        )
    )

    assert exit_code == 0
    assert phase_calls == ["17:00", "19:00", "20:00"]
    assert diagnostic_calls == [True]
    assert len(browser.contexts) == 1


def test_success_path_skips_full_page_diagnostics(monkeypatch):
    config, account, coordinator, logger = make_retry_test_context(ACCOUNT_C_KEY)
    browser = RetryTestBrowser()
    diagnostic_calls = []

    async def no_op(*_args, **_kwargs):
        return None

    async def confirmed(*_args, **kwargs):
        selected = kwargs["preferences"][0]
        assert kwargs["claim_slot"](selected)
        return BookingAttemptResult(selected, "confirmed")

    async def record_diagnostics(*_args, **_kwargs):
        diagnostic_calls.append(True)

    monkeypatch.setattr(book_badminton, "login", no_op)
    monkeypatch.setattr(book_badminton, "open_booking_search", no_op)
    monkeypatch.setattr(book_badminton, "search_badminton", no_op)
    monkeypatch.setattr(book_badminton, "open_available_spaces", no_op)
    monkeypatch.setattr(book_badminton, "book_best_available_slot", confirmed)
    monkeypatch.setattr(
        book_badminton,
        "log_available_spaces_diagnostics",
        record_diagnostics,
    )

    exit_code = asyncio.run(
        book_badminton.run_account_booking(
            browser,
            config,
            account,
            logger,
            coordinator,
        )
    )

    assert exit_code == 0
    assert diagnostic_calls == []


def test_diagnostic_failure_does_not_trigger_a_fresh_booking_session(monkeypatch):
    config, account, coordinator, logger = make_retry_test_context(ACCOUNT_C_KEY)
    browser = RetryTestBrowser()

    async def no_op(*_args, **_kwargs):
        return None

    async def no_slots(*_args, **_kwargs):
        return None

    async def broken_diagnostics(*_args, **_kwargs):
        raise book_badminton.PlaywrightError("diagnostic page disappeared")

    monkeypatch.setattr(book_badminton, "login", no_op)
    monkeypatch.setattr(book_badminton, "open_booking_search", no_op)
    monkeypatch.setattr(book_badminton, "search_badminton", no_op)
    monkeypatch.setattr(book_badminton, "open_available_spaces", no_op)
    monkeypatch.setattr(book_badminton, "book_best_available_slot", no_slots)
    monkeypatch.setattr(
        book_badminton,
        "log_available_spaces_diagnostics",
        broken_diagnostics,
    )

    exit_code = asyncio.run(
        book_badminton.run_account_booking(
            browser,
            config,
            account,
            logger,
            coordinator,
        )
    )

    assert exit_code == 0
    assert len(browser.contexts) == 1
