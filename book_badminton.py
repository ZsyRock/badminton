from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
from typing import Callable, Iterable, Sequence
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page, TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from booking_email import email_notifications_enabled, send_booking_report


DEFAULT_BOOKING_URL = "https://soton.gladstonego.cloud/account"
DEFAULT_LOGIN_PATH = "/auth/login"
DEFAULT_TIMEZONE = "Europe/London"
PROJECT_DIR = Path(__file__).resolve().parent
ENV_FILE = PROJECT_DIR / ".env"
LOGS_DIR = PROJECT_DIR / "logs"
SCREENSHOTS_DIR = PROJECT_DIR / "screenshots"
BLOCKER_PATTERNS = (
    "captcha",
    "forbidden",
    "access denied",
    "multi-factor",
    "two-factor",
    "rate limit",
    "too many requests",
    "verify you are human",
)
AUTHENTICATION_FAILURE_PATTERNS = (
    "invalid email or password",
    "incorrect email or password",
    "invalid username or password",
    "login failed",
    "credentials are incorrect",
    "account is locked",
)
MIDNIGHT_PREWARM_WINDOW_SECONDS = 120
MAX_ACCOUNT_SESSION_ATTEMPTS = 3
ACCOUNT_RETRY_DELAY_SECONDS = 2
ACCOUNT_A_KEY = "account_a"
ACCOUNT_B_KEY = "account_b"
ACCOUNT_C_KEY = "account_c"
ACCOUNT_A_LABEL = "账号A"
ACCOUNT_B_LABEL = "账号B"
ACCOUNT_C_LABEL = "账号C"
ACCOUNT_ATTEMPT_TIMES = {
    ACCOUNT_A_KEY: ("18:00", "16:00", "20:00"),
    ACCOUNT_B_KEY: ("18:00", "16:00", "20:00"),
    ACCOUNT_C_KEY: ("17:00", "19:00", "20:00"),
}
SUCCESSFUL_BOOKING_OUTCOMES = {"confirmed", "dry-run"}
BASKET_ITEM_ADDED_TEXT = "added to basket"
BASKET_SLOT_CONFLICT_TEXT = "you already have a booking for this slot in your basket"
LEASE_CREATION_ERROR_TEXT = "activity-calendar.errors.create-lease"


@dataclass(frozen=True)
class SlotPreference:
    start_time: str
    court_number: int

    @property
    def label(self) -> str:
        return f"{self.start_time} Jubilee Court {self.court_number}"


@dataclass(frozen=True)
class BookingAccountConfig:
    key: str
    label: str
    username: str
    password: str
    search_window_times: tuple[str, ...]
    court_priority: tuple[int, ...]


@dataclass(frozen=True)
class AppConfig:
    booking_url: str
    timezone_name: str
    headless: bool
    dry_run: bool
    debug_pause_seconds: int
    target_date_override: str | None
    accounts: tuple[BookingAccountConfig, ...]


@dataclass(frozen=True)
class BookingAttemptResult:
    slot: SlotPreference
    outcome: str


@dataclass
class BookingCoordinator:
    target_date: date
    release_at: datetime | None = None
    phase_selections: dict[tuple[str, str], SlotPreference | None] = field(
        default_factory=dict
    )
    phase_selection_ready: dict[tuple[str, str], asyncio.Event] = field(
        default_factory=dict
    )
    slot_claim_owners: dict[SlotPreference, str] = field(default_factory=dict)


@dataclass
class AccountRunProgress:
    completed_times: set[str] = field(default_factory=set)


class AccountLoggerAdapter(logging.LoggerAdapter):
    def process(self, msg: str, kwargs: dict[str, object]) -> tuple[str, dict[str, object]]:
        return f"[{self.extra['account_label']}] {msg}", kwargs


class BasketRecoveryError(RuntimeError):
    """Raised when a submitted slot is in the basket but cannot be safely confirmed."""


class AccessBlockerError(RuntimeError):
    """Raised for anti-automation or account-protection pages that must not be retried."""


class AuthenticationError(RuntimeError):
    """Raised when the site explicitly rejects an account login."""


class TargetDateValidationError(RuntimeError):
    """Raised when the calendar page cannot be tied to the requested date."""


def parse_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Unsupported boolean value: {value!r}")


def parse_csv_strings(value: str | None, default: Sequence[str]) -> tuple[str, ...]:
    if not value:
        return tuple(default)
    return tuple(part.strip() for part in value.split(",") if part.strip())


def parse_csv_ints(value: str | None, default: Sequence[int]) -> tuple[int, ...]:
    if not value:
        return tuple(default)
    return tuple(int(part.strip()) for part in value.split(",") if part.strip())


def parse_optional_int(value: str | None, default: int = 0) -> int:
    if value is None or not value.strip():
        return default
    parsed = int(value.strip())
    return max(parsed, 0)


def normalize_visible_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().lower()


def sort_clock_times(times: Sequence[str]) -> list[str]:
    return sorted(dict.fromkeys(time.strip() for time in times if time.strip()), key=lambda value: datetime.strptime(value, "%H:%M"))


def build_preferred_starting_from_labels(preferred_times: Sequence[str]) -> list[str]:
    return [normalize_visible_text(f"From {time_value}") for time_value in sort_clock_times(preferred_times)]


def build_preferred_starting_from_display_label(preferred_times: Sequence[str]) -> str:
    sorted_times = sort_clock_times(preferred_times)
    if sorted_times:
        return f"From {sorted_times[0]}"
    return "From 0:00"


def pick_starting_from_select_value(
    options: Sequence[tuple[str, str, bool]],
    preferred_times: Sequence[str] = (),
) -> str | None:
    first_enabled_value: str | None = None
    desired_labels = build_preferred_starting_from_labels(preferred_times)
    normalized_to_value: dict[str, str] = {}

    for value, label, disabled in options:
        if disabled:
            continue

        normalized_label = normalize_visible_text(label or value)
        if not normalized_label:
            continue
        if normalized_label in {"starting from", "select", "please select", "choose"}:
            continue

        if first_enabled_value is None:
            first_enabled_value = value

        normalized_to_value.setdefault(normalized_label, value)

    for desired_label in desired_labels:
        if desired_label in normalized_to_value:
            return normalized_to_value[desired_label]

    for zero_label in ("starting now", "from 0:00", "from 00:00", "0:00", "00:00"):
        if zero_label in normalized_to_value:
            return normalized_to_value[zero_label]

    return first_enabled_value


def compute_target_date(
    now: datetime | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> date:
    zone = ZoneInfo(timezone_name)
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    local_now = reference.astimezone(zone)
    return local_now.date() + timedelta(days=8)


def get_local_now(
    timezone_name: str = DEFAULT_TIMEZONE,
    now: datetime | None = None,
) -> datetime:
    zone = ZoneInfo(timezone_name)
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    return reference.astimezone(zone)


def seconds_until_next_local_midnight(
    timezone_name: str = DEFAULT_TIMEZONE,
    now: datetime | None = None,
) -> float:
    local_now = get_local_now(timezone_name, now=now)
    next_midnight = datetime.combine(
        local_now.date() + timedelta(days=1),
        datetime.min.time(),
        tzinfo=local_now.tzinfo,
    )
    return max((next_midnight - local_now).total_seconds(), 0.0)


def should_use_midnight_prewarm(
    timezone_name: str,
    target_date_override: str | None,
    now: datetime | None = None,
    prewarm_window_seconds: int = MIDNIGHT_PREWARM_WINDOW_SECONDS,
) -> bool:
    if target_date_override:
        return False
    seconds_until_midnight = seconds_until_next_local_midnight(timezone_name, now=now)
    return 0 < seconds_until_midnight <= prewarm_window_seconds


def compute_target_date_after_next_local_midnight(
    timezone_name: str = DEFAULT_TIMEZONE,
    now: datetime | None = None,
) -> date:
    local_now = get_local_now(timezone_name, now=now)
    next_midnight = datetime.combine(
        local_now.date() + timedelta(days=1),
        datetime.min.time(),
        tzinfo=local_now.tzinfo,
    )
    return compute_target_date(now=next_midnight, timezone_name=timezone_name)


def parse_target_date_override(value: str | None) -> date | None:
    if value is None or not value.strip():
        return None

    normalized = value.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(normalized, fmt).date()
        except ValueError:
            continue

    raise ValueError(
        "TARGET_DATE_OVERRIDE must be in YYYY-MM-DD or DD/MM/YYYY format"
    )


def resolve_target_date(
    timezone_name: str,
    target_date_override: str | None = None,
    now: datetime | None = None,
) -> tuple[date, bool]:
    override_date = parse_target_date_override(target_date_override)
    if override_date is not None:
        return override_date, True
    return compute_target_date(now=now, timezone_name=timezone_name), False


def format_date_for_site(target_date: date) -> str:
    return target_date.strftime("%d/%m/%Y")


def format_time_for_button_label(start_time: str) -> str:
    parsed = datetime.strptime(start_time, "%H:%M")
    return parsed.strftime("%I:%M %p").lstrip("0")


def format_slot_time_range(start_time: str) -> str:
    parsed = datetime.strptime(start_time, "%H:%M")
    end_time = parsed + timedelta(hours=1)
    return f"{parsed.strftime('%H:%M')} - {end_time.strftime('%H:%M')}"


def format_ordinal_day(day_number: int) -> str:
    if 10 <= day_number % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day_number % 10, "th")
    return f"{day_number}{suffix}"


def build_available_spaces_button_pattern(target_date: date) -> re.Pattern[str]:
    weekday = target_date.strftime("%a")
    ordinal_day = format_ordinal_day(target_date.day)
    month_name = target_date.strftime("%B")
    return re.compile(
        rf"Badminton starts on\s*{weekday}\s*,?\s*{ordinal_day}\s+{month_name},?\s+at\s+"
        rf"\d{{1,2}}:\d{{2}}\s+[AP]M:\s*See available spaces",
        re.IGNORECASE,
    )


def calendar_activity_date_from_url(
    calendar_url: str,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> date:
    activity_dates = parse_qs(urlsplit(calendar_url).query).get("activityDate", [])
    if len(activity_dates) != 1 or not activity_dates[0].strip():
        raise TargetDateValidationError(
            "The available-spaces URL did not contain exactly one activityDate."
        )

    raw_value = activity_dates[0].strip()
    if raw_value.endswith(("Z", "z")):
        raw_value = f"{raw_value[:-1]}+00:00"
    try:
        activity_datetime = datetime.fromisoformat(raw_value)
    except ValueError as exc:
        raise TargetDateValidationError(
            f"The calendar activityDate was not a valid ISO timestamp: {activity_dates[0]!r}."
        ) from exc
    if activity_datetime.tzinfo is None:
        raise TargetDateValidationError(
            "The calendar activityDate did not include a timezone offset."
        )
    return activity_datetime.astimezone(ZoneInfo(timezone_name)).date()


def validate_calendar_target_date(
    calendar_url: str,
    target_date: date,
    timezone_name: str = DEFAULT_TIMEZONE,
) -> None:
    actual_date = calendar_activity_date_from_url(calendar_url, timezone_name)
    if actual_date != target_date:
        raise TargetDateValidationError(
            "The available-spaces calendar date did not match the requested date: "
            f"expected {target_date.isoformat()}, found {actual_date.isoformat()}."
        )


def build_slot_button_pattern(slot: SlotPreference) -> re.Pattern[str]:
    button_time = format_time_for_button_label(slot.start_time)
    return re.compile(
        rf"Book now:\s*for\s+Jubilee Court {slot.court_number}\s+at\s+{re.escape(button_time)}\b",
        re.IGNORECASE,
    )


def build_slot_card_pattern(slot: SlotPreference) -> re.Pattern[str]:
    time_range = format_slot_time_range(slot.start_time)
    return re.compile(
        rf"Jubilee Court {slot.court_number}.*{re.escape(time_range)}|"
        rf"{re.escape(time_range)}.*Jubilee Court {slot.court_number}",
        re.IGNORECASE | re.DOTALL,
    )


def slot_card_text_matches(card_text: str, slot: SlotPreference) -> bool:
    normalized = normalize_visible_text(card_text)
    if not normalized:
        return False

    court_label = normalize_visible_text(f"Jubilee Court {slot.court_number}")
    time_range = normalize_visible_text(format_slot_time_range(slot.start_time))
    return court_label in normalized and time_range in normalized


def build_final_confirmation_pattern() -> re.Pattern[str]:
    return re.compile(r"^Book Badminton for £0\.00 at", re.IGNORECASE)


def derive_login_url(booking_url: str) -> str:
    if "/account" in booking_url:
        return booking_url.replace("/account", DEFAULT_LOGIN_PATH)
    return booking_url.rstrip("/") + DEFAULT_LOGIN_PATH


def is_book_page_url(url: str) -> bool:
    return "/book" in url.lower()


def post_login_success_detected(
    current_url: str,
    activity_form_visible: bool,
    make_booking_visible: bool,
    book_nav_visible: bool,
) -> bool:
    return any(
        (
            is_book_page_url(current_url),
            activity_form_visible,
            make_booking_visible,
            book_nav_visible,
        )
    )


def build_slot_priority(
    preferred_times: Iterable[str],
    preferred_courts: Iterable[int],
) -> list[SlotPreference]:
    slots: list[SlotPreference] = []
    seen: set[tuple[str, int]] = set()
    for start_time in preferred_times:
        normalized_time = start_time.strip()
        for court_number in preferred_courts:
            key = (normalized_time, int(court_number))
            if key in seen:
                continue
            seen.add(key)
            slots.append(SlotPreference(*key))
    return slots


def build_account_slot_priority(
    account: BookingAccountConfig,
    preferred_times: Sequence[str],
) -> list[SlotPreference]:
    return build_slot_priority(preferred_times, account.court_priority)


def pick_best_available_slot(
    available_slot_labels: Iterable[str],
    preferences: Sequence[SlotPreference],
) -> SlotPreference | None:
    available = {label.strip().lower() for label in available_slot_labels}
    for slot in preferences:
        if slot.label.lower() in available:
            return slot
    return None


def booking_attempt_succeeded(result: BookingAttemptResult | None) -> bool:
    return result is not None and result.outcome in SUCCESSFUL_BOOKING_OUTCOMES


def get_account_attempt_times(account_key: str) -> tuple[str, ...]:
    try:
        return ACCOUNT_ATTEMPT_TIMES[account_key]
    except KeyError as exc:
        raise ValueError(f"Unsupported account key: {account_key}") from exc


def get_required_search_window_times(account_key: str) -> tuple[str, ...]:
    return get_account_attempt_times(account_key)


def build_search_window_times(
    account_key: str,
    configured_times: Sequence[str],
) -> tuple[str, ...]:
    merged_times = list(configured_times) + list(get_required_search_window_times(account_key))
    return tuple(sort_clock_times(merged_times))


def booking_confirmation_detected(current_url: str, body_text: str) -> bool:
    normalized_url = current_url.lower()
    normalized_text = " ".join(body_text.split()).lower()

    has_heading = "booking confirmed" in normalized_text
    has_reference = "booking ref:" in normalized_text or "booking reference" in normalized_text
    has_receipt_message = "booking confirmation and receipt has been sent" in normalized_text
    has_follow_up_action = "make another booking" in normalized_text
    has_confirmation_url = any(
        fragment in normalized_url
        for fragment in ("/confirmation", "/receipt", "/complete", "/success")
    )

    if has_heading and (has_reference or has_receipt_message or has_follow_up_action):
        return True

    if has_confirmation_url and (has_reference or has_receipt_message):
        return True

    return False


def basket_item_added_detected(body_text: str) -> bool:
    return BASKET_ITEM_ADDED_TEXT in " ".join(body_text.split()).lower()


def basket_slot_conflict_detected(body_text: str) -> bool:
    return BASKET_SLOT_CONFLICT_TEXT in " ".join(body_text.split()).lower()


def lease_creation_error_count(body_text: str) -> int:
    """Count the site's explicit temporary-reservation failure marker."""

    return " ".join(body_text.split()).lower().count(LEASE_CREATION_ERROR_TEXT)


def basket_contains_expected_slot(
    basket_item_text: str,
    slot: SlotPreference,
    target_date: date,
) -> bool:
    normalized_text = re.sub(
        r"[^a-z0-9]+",
        " ",
        normalize_visible_text(basket_item_text).lower(),
    ).strip()
    court_label = f"jubilee court {slot.court_number}"
    time_range = re.sub(
        r"[^a-z0-9]+",
        " ",
        format_slot_time_range(slot.start_time).lower(),
    ).strip()
    date_label = (
        f"{target_date:%a} {target_date.day} {target_date:%B} {target_date.year}"
    ).lower()
    return all(
        expected in normalized_text
        for expected in ("badminton", court_label, time_range, date_label)
    )


def build_account_config(
    *,
    key: str,
    label: str,
    username: str,
    password: str,
    search_window_times: Sequence[str],
    court_priority: Sequence[int],
) -> BookingAccountConfig:
    normalized_username = username.strip()
    normalized_password = password.strip()
    if not normalized_username:
        raise ValueError(f"{label} username is required in .env")
    if not normalized_password:
        raise ValueError(f"{label} password is required in .env")

    return BookingAccountConfig(
        key=key,
        label=label,
        username=normalized_username,
        password=normalized_password,
        search_window_times=build_search_window_times(key, search_window_times),
        court_priority=tuple(court_priority),
    )


def load_config() -> AppConfig:
    load_dotenv(dotenv_path=ENV_FILE)
    booking_url = os.getenv("BOOKING_URL", DEFAULT_BOOKING_URL).strip() or DEFAULT_BOOKING_URL
    timezone_name = os.getenv("TIMEZONE", DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE
    headless = parse_bool(os.getenv("HEADLESS"), default=False)
    dry_run = parse_bool(os.getenv("DRY_RUN"), default=True)
    debug_pause_seconds = parse_optional_int(os.getenv("DEBUG_PAUSE_SECONDS"), default=0)
    target_date_override = os.getenv("TARGET_DATE_OVERRIDE", "").strip() or None
    primary_search_window_times = parse_csv_strings(
        os.getenv("PREFERRED_TIMES"),
        default=ACCOUNT_ATTEMPT_TIMES[ACCOUNT_A_KEY],
    )
    primary_court_priority = parse_csv_ints(
        os.getenv("PREFERRED_COURTS"),
        default=(1, 2, 3, 4),
    )
    accounts = [
        build_account_config(
            key=ACCOUNT_A_KEY,
            label=ACCOUNT_A_LABEL,
            username=os.getenv("GYM_USERNAME", ""),
            password=os.getenv("GYM_PASSWORD", ""),
            search_window_times=primary_search_window_times,
            court_priority=primary_court_priority,
        )
    ]

    secondary_username = os.getenv("SECONDARY_GYM_USERNAME", "")
    secondary_password = os.getenv("SECONDARY_GYM_PASSWORD", "")
    secondary_enabled_default = bool(
        secondary_username.strip() or secondary_password.strip()
    )
    secondary_enabled = parse_bool(
        os.getenv("SECONDARY_BOOKING_ENABLED"),
        default=secondary_enabled_default,
    )
    if secondary_enabled:
        secondary_search_window_times = parse_csv_strings(
            os.getenv("SECONDARY_PREFERRED_TIMES"),
            default=ACCOUNT_ATTEMPT_TIMES[ACCOUNT_B_KEY],
        )
        secondary_court_priority = parse_csv_ints(
            os.getenv("SECONDARY_PREFERRED_COURTS"),
            default=(2, 1, 3, 4),
        )
        accounts.append(
            build_account_config(
                key=ACCOUNT_B_KEY,
                label=ACCOUNT_B_LABEL,
                username=secondary_username,
                password=secondary_password,
                search_window_times=secondary_search_window_times,
                court_priority=secondary_court_priority,
            )
        )

    tertiary_username = os.getenv("TERTIARY_GYM_USERNAME", "")
    tertiary_password = os.getenv("TERTIARY_GYM_PASSWORD", "")
    tertiary_enabled_default = bool(
        tertiary_username.strip() or tertiary_password.strip()
    )
    tertiary_enabled = parse_bool(
        os.getenv("TERTIARY_BOOKING_ENABLED"),
        default=tertiary_enabled_default,
    )
    if tertiary_enabled:
        tertiary_search_window_times = parse_csv_strings(
            os.getenv("TERTIARY_PREFERRED_TIMES"),
            default=ACCOUNT_ATTEMPT_TIMES[ACCOUNT_C_KEY],
        )
        tertiary_court_priority = parse_csv_ints(
            os.getenv("TERTIARY_PREFERRED_COURTS"),
            default=(1, 2, 3, 4),
        )
        accounts.append(
            build_account_config(
                key=ACCOUNT_C_KEY,
                label=ACCOUNT_C_LABEL,
                username=tertiary_username,
                password=tertiary_password,
                search_window_times=tertiary_search_window_times,
                court_priority=tertiary_court_priority,
            )
        )

    return AppConfig(
        booking_url=booking_url,
        timezone_name=timezone_name,
        headless=headless,
        dry_run=dry_run,
        debug_pause_seconds=debug_pause_seconds,
        target_date_override=target_date_override,
        accounts=tuple(accounts),
    )


def setup_logging(timezone_name: str) -> tuple[logging.Logger, Path]:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)

    run_stamp = datetime.now(ZoneInfo(timezone_name)).strftime("%Y%m%d-%H%M%S")
    log_path = LOGS_DIR / f"book-badminton-{run_stamp}.log"

    logger = logging.getLogger("book_badminton")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.propagate = False

    formatter = logging.Formatter(
        "%(asctime)s.%(msecs)03d | %(levelname)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger, log_path


def sanitize_filename(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip("-") or "screenshot"


async def save_failure_screenshot(
    page: Page | None,
    reason: str,
    timezone_name: str,
    logger: logging.Logger,
) -> Path | None:
    if page is None:
        return None

    timestamp = datetime.now(ZoneInfo(timezone_name)).strftime("%Y%m%d-%H%M%S")
    screenshot_path = SCREENSHOTS_DIR / f"{timestamp}-{sanitize_filename(reason)}.png"
    try:
        await page.screenshot(path=str(screenshot_path), full_page=True)
        logger.info("Saved failure screenshot to %s", screenshot_path)
        return screenshot_path
    except PlaywrightError as exc:
        logger.warning("Failed to save screenshot for %s: %s", reason, exc)
        return None


async def save_named_screenshot(
    page: Page | None,
    prefix: str,
    timezone_name: str,
    logger: logging.Logger,
) -> Path | None:
    if page is None:
        return None

    timestamp = datetime.now(ZoneInfo(timezone_name)).strftime("%Y%m%d-%H%M%S")
    screenshot_path = SCREENSHOTS_DIR / f"{sanitize_filename(prefix)}-{timestamp}.png"
    try:
        await page.screenshot(path=str(screenshot_path), full_page=True)
        logger.info("Saved screenshot to %s", screenshot_path)
        return screenshot_path
    except PlaywrightError as exc:
        logger.warning("Failed to save screenshot for %s: %s", prefix, exc)
        return None


async def check_for_access_blockers(
    page: Page,
    config: AppConfig,
    logger: logging.Logger,
) -> None:
    content = (await page.locator("body").inner_text(timeout=5_000)).lower()
    current_url = page.url.lower()
    for pattern in BLOCKER_PATTERNS:
        if pattern in content or pattern in current_url:
            await save_failure_screenshot(
                page,
                f"blocked-{pattern}",
                config.timezone_name,
                logger,
            )
            raise AccessBlockerError(
                f"Encountered blocker '{pattern}'. Stopping without attempting to bypass site protections."
            )


async def check_for_authentication_failure(page: Page) -> None:
    try:
        content = normalize_visible_text(
            await page.locator("body").inner_text(timeout=2_000)
        )
    except PlaywrightError:
        return

    for pattern in AUTHENTICATION_FAILURE_PATTERNS:
        if pattern in content:
            raise AuthenticationError(
                "The site explicitly rejected the login. Stopping without repeated "
                "password attempts."
            )


async def is_visible(locator: Locator) -> bool:
    if not await locator.count():
        return False
    try:
        return await locator.first.is_visible()
    except PlaywrightError:
        return False


async def get_locator_name(locator: Locator) -> str:
    try:
        aria_label = await locator.get_attribute("aria-label")
        if aria_label and aria_label.strip():
            return aria_label.strip()
        text = await locator.inner_text()
        if text and text.strip():
            return " ".join(text.split())
    except PlaywrightError:
        return ""
    return ""


def get_activity_textbox(page: Page) -> Locator:
    return page.get_by_role("textbox", name="What are you looking to do")


def get_date_input(page: Page) -> Locator:
    return page.locator(
        "input[name='date'], input[id='activityDate'], input[data-qa-id='activityDate']"
    ).first


def get_make_booking_button(page: Page) -> Locator:
    return page.get_by_role("button", name="Make a booking", exact=True)


def get_book_nav_locator(page: Page) -> Locator:
    return page.get_by_role("link", name=re.compile(r"^book$", re.IGNORECASE)).first


async def wait_for_booking_entrypoint(
    page: Page,
    config: AppConfig,
    logger: logging.Logger,
    timeout_ms: int = 20_000,
) -> str:
    deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000)

    while asyncio.get_running_loop().time() < deadline:
        await check_for_access_blockers(page, config, logger)
        await check_for_authentication_failure(page)
        activity_form_visible = await is_visible(get_activity_textbox(page))
        make_booking_visible = await is_visible(get_make_booking_button(page))
        book_nav_visible = await is_visible(get_book_nav_locator(page))
        current_url = page.url

        if post_login_success_detected(
            current_url=current_url,
            activity_form_visible=activity_form_visible,
            make_booking_visible=make_booking_visible,
            book_nav_visible=book_nav_visible,
        ):
            if is_book_page_url(current_url) or activity_form_visible:
                logger.info("Detected booking search page after login: %s", current_url)
                return "book-form"
            if make_booking_visible:
                logger.info("Detected dashboard booking entrypoint after login: %s", current_url)
                return "make-booking"
            logger.info("Detected top navigation Book entrypoint after login: %s", current_url)
            return "book-nav"

        await page.wait_for_timeout(250)

    raise PlaywrightTimeoutError("Timed out waiting for a valid post-login booking entrypoint.")


async def login(
    page: Page,
    config: AppConfig,
    account: BookingAccountConfig,
    logger: logging.Logger,
) -> None:
    login_url = derive_login_url(config.booking_url)
    logger.info("Opening login page: %s", login_url)
    await page.goto(login_url, wait_until="domcontentloaded")
    await check_for_access_blockers(page, config, logger)

    if await is_visible(get_activity_textbox(page)):
        logger.info("Already on the booking search page.")
        return

    if await is_visible(get_make_booking_button(page)) or await is_visible(get_book_nav_locator(page)):
        logger.info("Already logged in and at a booking entrypoint.")
        return

    logger.info("Logging in as %s", account.username)
    username_input = page.get_by_placeholder("Enter your email")
    password_input = page.get_by_placeholder("Enter your password")

    await username_input.wait_for(state="visible")
    await username_input.fill(account.username)
    await password_input.fill(account.password)
    submit_button = page.get_by_role("button", name="Login", exact=True)
    await submit_button.click()
    await page.wait_for_load_state("networkidle")
    entrypoint = await wait_for_booking_entrypoint(page, config, logger)
    logger.info("Login successful via %s state.", entrypoint)
    logger.info("Login successful.")


async def open_booking_search(page: Page, config: AppConfig, logger: logging.Logger) -> None:
    logger.info("Opening the booking search page.")

    activity_field = get_activity_textbox(page)
    if await is_visible(activity_field) and is_book_page_url(page.url):
        logger.info("Already on the booking search page; no navigation needed.")
        return

    make_booking_button = get_make_booking_button(page)
    if await is_visible(make_booking_button):
        await make_booking_button.click()
    else:
        book_nav = get_book_nav_locator(page)
        if await is_visible(book_nav):
            await book_nav.click()
        else:
            raise RuntimeError("Could not find booking entry controls after login.")

    await page.wait_for_load_state("networkidle")
    await check_for_access_blockers(page, config, logger)
    await activity_field.wait_for(state="visible", timeout=20_000)
    logger.info("Booking search page opened.")


async def try_select_starting_from(
    page: Page,
    preferred_times: Sequence[str],
    logger: logging.Logger,
) -> None:
    starting_from = page.get_by_label(re.compile(r"starting from", re.I)).first
    await starting_from.wait_for(state="visible")
    preferred_labels = build_preferred_starting_from_labels(preferred_times)
    target_label = build_preferred_starting_from_display_label(preferred_times)

    tag_name = (await starting_from.evaluate("(el) => el.tagName")).lower()
    if tag_name == "select":
        raw_options = await starting_from.evaluate(
            """
            (el) => Array.from(el.options).map((option) => ({
                value: option.value,
                label: (option.label || option.textContent || "").trim(),
                disabled: option.disabled,
            }))
            """
        )
        option_tuples = [
            (
                str(option.get("value", "")),
                str(option.get("label", "")),
                bool(option.get("disabled", False)),
            )
            for option in raw_options
        ]
        selected_value = pick_starting_from_select_value(option_tuples, preferred_times=preferred_times)
        if selected_value is None:
            raise RuntimeError("Could not determine a usable 'Starting from' select option.")

        await starting_from.select_option(value=selected_value)
        logger.info(
            "Selected 'Starting from' using native select value %r for preferred times %s.",
            selected_value,
            ", ".join(preferred_times),
        )
        return

    try:
        await starting_from.select_option(label=target_label)
        logger.info("Selected '%s' using select_option.", target_label)
        return
    except PlaywrightError:
        pass

    await starting_from.click()
    option_patterns = [re.compile(re.escape(label), re.IGNORECASE) for label in preferred_labels]
    option_patterns.extend(
        [
            re.compile(r"starting now", re.IGNORECASE),
            re.compile(r"from 0:00", re.IGNORECASE),
            re.compile(r"from 00:00", re.IGNORECASE),
        ]
    )
    for pattern in option_patterns:
        option = page.get_by_role("option", name=pattern).first
        if await option.count():
            await option.click()
            logger.info("Selected 'Starting from' option using option click pattern %r.", pattern.pattern)
            return

    is_text_entry = await starting_from.evaluate(
        "(el) => ['INPUT', 'TEXTAREA'].includes(el.tagName) || el.isContentEditable"
    )
    if is_text_entry:
        await starting_from.fill(target_label)
        logger.info("Filled 'Starting from' as text fallback with %s.", target_label)
        return

    raise RuntimeError("Could not set the 'Starting from' control using any supported strategy.")


async def close_calendar_overlay(page: Page, logger: logging.Logger) -> None:
    overlay_backdrop = page.locator(".cdk-overlay-backdrop")
    overlay_table = page.locator(".mat-calendar-table")

    if not await overlay_backdrop.count() and not await overlay_table.count():
        return

    try:
        await overlay_backdrop.first.wait_for(state="hidden", timeout=1_500)
        return
    except PlaywrightTimeoutError:
        pass

    await page.keyboard.press("Escape")
    try:
        await overlay_backdrop.first.wait_for(state="hidden", timeout=2_000)
        logger.info("Closed calendar overlay with Escape.")
        return
    except PlaywrightTimeoutError:
        logger.warning("Calendar overlay remained open after Escape; continuing with best effort.")


async def select_target_date_from_calendar(
    page: Page,
    target_date: date,
    logger: logging.Logger,
) -> None:
    logger.info("Selecting date from calendar for %s", format_date_for_site(target_date))
    await page.get_by_role("button", name="Open calendar").click()

    month_year_pattern = re.compile(
        rf"{target_date.strftime('%B')}\s+{target_date.year}",
        re.IGNORECASE,
    )
    next_month_button = page.get_by_role(
        "button",
        name=re.compile(r"(next month|go to next month|next)", re.IGNORECASE),
    ).first

    for _ in range(12):
        month_header = page.get_by_text(month_year_pattern).first
        if await month_header.count():
            break
        if not await next_month_button.count():
            break
        await next_month_button.click()
    else:
        logger.warning("Could not confirm the target month header in the calendar.")

    day_with_month_patterns = [
        re.compile(
            rf"{format_ordinal_day(target_date.day)}\s+{target_date.strftime('%B')}\s+{target_date.year}",
            re.IGNORECASE,
        ),
        re.compile(
            rf"{target_date.strftime('%B')}\s+{target_date.day}(?:st|nd|rd|th)?(?:,)?\s+{target_date.year}",
            re.IGNORECASE,
        ),
    ]
    for pattern in day_with_month_patterns:
        button = page.get_by_role("button", name=pattern).first
        if await button.count():
            await button.click()
            await close_calendar_overlay(page, logger)
            logger.info("Selected calendar day using accessible date label.")
            return

    exact_day_text = page.get_by_text(str(target_date.day), exact=True)
    count = await exact_day_text.count()
    for index in range(count):
        candidate = exact_day_text.nth(index)
        if await candidate.is_visible():
            await candidate.click()
            await close_calendar_overlay(page, logger)
            logger.info("Selected calendar day using visible exact day text.")
            return

    raise RuntimeError(f"Could not select target date {format_date_for_site(target_date)} in the calendar.")


async def set_target_date(
    page: Page,
    target_date: date,
    logger: logging.Logger,
) -> None:
    formatted_date = format_date_for_site(target_date)
    date_input = get_date_input(page)

    if await is_visible(date_input):
        try:
            await date_input.click()
            await date_input.fill(formatted_date)
            await date_input.press("Tab")
            current_value = await date_input.input_value()
            if current_value.strip() == formatted_date:
                logger.info("Set target date by filling the date input directly.")
                return
            logger.warning(
                "Date input value after fill was %r instead of %r; falling back to calendar.",
                current_value,
                formatted_date,
            )
        except PlaywrightError as exc:
            logger.warning("Direct date input fill failed (%s); falling back to calendar.", exc)

    await select_target_date_from_calendar(page, target_date, logger)


async def log_available_spaces_diagnostics(
    page: Page,
    config: AppConfig,
    account: BookingAccountConfig,
    logger: logging.Logger,
) -> None:
    logger.info("Available spaces page URL: %s", page.url)
    try:
        logger.info("Available spaces page title: %s", await page.title())
    except PlaywrightError as exc:
        logger.warning("Could not read page title: %s", exc)

    visible_button_names: list[str] = []
    buttons = page.get_by_role("button")
    button_count = await buttons.count()
    for index in range(button_count):
        button = buttons.nth(index)
        if not await is_visible(button):
            continue
        name = await get_locator_name(button)
        if name:
            visible_button_names.append(name)

    if visible_button_names:
        logger.info("Visible button accessible names on available spaces page:")
        for name in visible_button_names:
            logger.info("BUTTON | %s", name)
    else:
        logger.info("No visible buttons were detected on the available spaces page.")

    keywords = [
        "Jubilee Court",
        "Book now",
        "This slot is unavailable",
        "Available to book from",
    ]
    for preferred_time in account.search_window_times:
        keywords.append(preferred_time)
        keywords.append(format_time_for_button_label(preferred_time))
        keywords.append(format_slot_time_range(preferred_time))
    try:
        body_text = await page.locator("body").inner_text()
    except PlaywrightError as exc:
        logger.warning("Could not read page text for diagnostics: %s", exc)
        body_text = ""

    matching_lines: list[str] = []
    for line in body_text.splitlines():
        normalized = " ".join(line.split())
        if not normalized:
            continue
        if any(keyword.lower() in normalized.lower() for keyword in keywords):
            matching_lines.append(normalized)

    if matching_lines:
        logger.info("Visible text snippets matching slot diagnostics:")
        for line in matching_lines:
            logger.info("TEXT | %s", line)
    else:
        logger.info("No matching visible text snippets were found for slot diagnostics.")

    if config.dry_run:
        await save_named_screenshot(
            page,
            f"{account.label}-available-spaces",
            config.timezone_name,
            logger,
        )


async def log_available_spaces_diagnostics_best_effort(
    page: Page,
    config: AppConfig,
    account: BookingAccountConfig,
    logger: logging.Logger,
) -> None:
    try:
        await log_available_spaces_diagnostics(page, config, account, logger)
    except Exception as exc:  # noqa: BLE001 - diagnostics must never disrupt booking
        logger.warning("Could not collect available-spaces diagnostics: %s", exc)


async def wait_for_available_spaces_content(
    page: Page,
    config: AppConfig,
    logger: logging.Logger,
    timeout_ms: int = 30_000,
) -> None:
    logger.info("Waiting for available spaces content to finish loading.")
    deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000)

    spinner_locators = [
        page.locator(".loading-spinner, .spinner, .mat-progress-spinner"),
        page.locator("svg[role='progressbar'], [role='progressbar']"),
    ]
    content_patterns = (
        re.compile(r"Jubilee Court", re.IGNORECASE),
        re.compile(r"Book now", re.IGNORECASE),
        re.compile(r"This slot is unavailable", re.IGNORECASE),
        re.compile(r"Available to book from", re.IGNORECASE),
    )

    while asyncio.get_running_loop().time() < deadline:
        await check_for_access_blockers(page, config, logger)

        body_text = ""
        try:
            body_text = await page.locator("body").inner_text(timeout=2_000)
        except PlaywrightError:
            pass

        if any(pattern.search(body_text) for pattern in content_patterns):
            logger.info("Detected available spaces content in page text.")
            return

        spinner_visible = False
        for locator in spinner_locators:
            if await is_visible(locator):
                spinner_visible = True
                break

        if not spinner_visible:
            slot_card_candidates = page.locator("article, section, li, div").filter(
                has_text=re.compile(r"Jubilee Court", re.IGNORECASE)
            )
            if await slot_card_candidates.count():
                logger.info("Detected slot card candidates after spinner disappeared.")
                return

        await page.wait_for_timeout(500)

    logger.warning(
        "Timed out waiting for available spaces content. Proceeding with diagnostics on current page state."
    )


async def find_slot_card(page: Page, slot: SlotPreference) -> Locator | None:
    candidate_sets = (
        page.locator(".activity-calendar-timetable-slot"),
        page.locator("[data-qa-id^='slot-']"),
        page.locator("article, section, li"),
    )

    best_candidate: Locator | None = None
    best_text_length: int | None = None

    for candidates in candidate_sets:
        count = await candidates.count()
        for index in range(count):
            candidate = candidates.nth(index)
            if not await is_visible(candidate):
                continue

            try:
                candidate_text = await candidate.inner_text()
            except PlaywrightError:
                continue

            if not slot_card_text_matches(candidate_text, slot):
                continue

            candidate_text_length = len(normalize_visible_text(candidate_text))
            if best_text_length is None or candidate_text_length < best_text_length:
                best_candidate = candidate
                best_text_length = candidate_text_length

        if best_candidate is not None:
            return best_candidate

    return None


async def search_badminton(
    page: Page,
    target_date: date,
    config: AppConfig,
    account: BookingAccountConfig,
    logger: logging.Logger,
) -> None:
    formatted_date = format_date_for_site(target_date)
    logger.info("Searching for Badminton on %s", formatted_date)

    await prepare_badminton_search_form(page, target_date, config, account, logger)
    await submit_badminton_search(page, config, logger)


async def prepare_badminton_search_form(
    page: Page,
    target_date: date,
    config: AppConfig,
    account: BookingAccountConfig,
    logger: logging.Logger,
) -> None:
    activity_field = get_activity_textbox(page)

    await activity_field.wait_for(state="visible")
    await activity_field.click()
    await activity_field.fill("badminton")
    await page.get_by_role("option", name="Select Badminton option").click()

    await set_target_date(page, target_date, logger)
    await try_select_starting_from(page, account.search_window_times, logger)


async def submit_badminton_search(
    page: Page,
    config: AppConfig,
    logger: logging.Logger,
) -> None:
    search_button = page.get_by_role("button", name="Search for activities")
    logger.info("Timing milestone | dispatching booking search click.")
    await search_button.click()
    await page.wait_for_load_state("networkidle")
    await check_for_access_blockers(page, config, logger)
    logger.info("Search submitted.")


async def open_available_spaces(
    page: Page,
    target_date: date,
    config: AppConfig,
    account: BookingAccountConfig,
    logger: logging.Logger,
) -> None:
    logger.info("Opening available spaces for %s", format_date_for_site(target_date))

    see_spaces = page.get_by_role(
        "button",
        name=build_available_spaces_button_pattern(target_date),
    ).first
    await see_spaces.wait_for(state="visible", timeout=20_000)
    logger.info(
        "Timing milestone | dispatching exact-date available-spaces click for %s.",
        format_date_for_site(target_date),
    )
    await see_spaces.click()
    await page.wait_for_load_state("networkidle")
    await check_for_access_blockers(page, config, logger)
    await wait_for_available_spaces_content(page, config, logger)
    validate_calendar_target_date(
        page.url,
        target_date,
        config.timezone_name,
    )
    logger.info(
        "Verified available-spaces calendar date: %s",
        format_date_for_site(target_date),
    )
    logger.info("Available spaces page opened.")
    if config.dry_run:
        await log_available_spaces_diagnostics_best_effort(
            page,
            config,
            account,
            logger,
        )


async def get_visible_slot_button(page: Page, slot: SlotPreference) -> Locator | None:
    explicit_button = page.get_by_role("button", name=build_slot_button_pattern(slot)).first
    if await explicit_button.count() and await explicit_button.is_visible():
        return explicit_button

    card = await find_slot_card(page, slot)
    if card is None:
        return None

    plain_book_button = card.get_by_role("button", name=re.compile(r"^Book now$", re.IGNORECASE)).first
    if await plain_book_button.count() and await plain_book_button.is_visible():
        return plain_book_button

    generic_book_button = card.get_by_role("button", name=re.compile(r"book now", re.IGNORECASE)).first
    if await generic_book_button.count() and await generic_book_button.is_visible():
        return generic_book_button

    return None


async def list_visible_bookable_slots(
    page: Page,
    preferences: Sequence[SlotPreference],
) -> list[str]:
    available: list[str] = []
    for slot in preferences:
        button = await get_visible_slot_button(page, slot)
        if button is not None:
            available.append(slot.label)
    return available


async def find_final_book_button(page: Page) -> Locator:
    return page.get_by_role("button", name=build_final_confirmation_pattern()).first


async def log_post_confirmation_state(
    page: Page,
    slot: SlotPreference,
    logger: logging.Logger,
    body_text: str | None = None,
) -> bool:
    logger.info("Post-confirmation page URL: %s", page.url)

    try:
        logger.info("Post-confirmation page title: %s", await page.title())
    except PlaywrightError as exc:
        logger.warning("Could not read post-confirmation page title for %s: %s", slot.label, exc)

    success_keywords = (
        "you are booked",
        "booking confirmed",
        "your booking is confirmed",
        "booking reference",
        "my bookings",
        "reservation",
        "booked activities",
    )
    diagnostic_keywords = success_keywords + (
        "confirmed",
        "basket",
        "unable",
        "unavailable",
        "error",
    )
    if body_text is None:
        try:
            body_text = await page.locator("body").inner_text(timeout=5_000)
        except PlaywrightError as exc:
            logger.warning("Could not read post-confirmation page text for %s: %s", slot.label, exc)
            return False

    success_detected = booking_confirmation_detected(page.url, body_text)
    matching_lines: list[str] = []
    for line in body_text.splitlines():
        normalized = " ".join(line.split())
        if not normalized:
            continue
        if any(keyword.lower() in normalized.lower() for keyword in diagnostic_keywords):
            matching_lines.append(normalized)

    if matching_lines:
        logger.info("Post-confirmation text snippets for %s:", slot.label)
        for line in matching_lines[:20]:
            logger.info("CONFIRM | %s", line)

    if success_detected:
        logger.info("Detected explicit booking confirmation text for %s", slot.label)

    return success_detected


async def wait_for_booking_confirmation_page(
    page: Page,
    slot: SlotPreference,
    config: AppConfig,
    account: BookingAccountConfig,
    logger: logging.Logger,
    timeout_ms: int = 20_000,
    lease_error_baseline: int | None = None,
) -> bool:
    logger.info("Waiting for booking confirmation page for %s", slot.label)
    deadline = asyncio.get_running_loop().time() + (timeout_ms / 1000)

    while asyncio.get_running_loop().time() < deadline:
        await check_for_access_blockers(page, config, logger)
        try:
            body_text = await page.locator("body").inner_text(timeout=1_000)
        except PlaywrightError:
            body_text = ""

        if booking_confirmation_detected(page.url, body_text):
            logger.info("Detected booking confirmation page for %s", slot.label)
            await log_post_confirmation_state(page, slot, logger, body_text=body_text)
            return True

        if (
            lease_error_baseline is not None
            and lease_creation_error_count(body_text) > lease_error_baseline
        ):
            logger.warning(
                "The site explicitly rejected temporary reservation creation for %s; "
                "stopping the confirmation wait early.",
                slot.label,
            )
            await log_post_confirmation_state(page, slot, logger, body_text=body_text)
            await save_named_screenshot(
                page,
                f"{account.label}-lease-rejected-{slot.start_time}-court-{slot.court_number}",
                config.timezone_name,
                logger,
            )
            return False

        await page.wait_for_timeout(250)

    logger.warning("Timed out waiting for booking confirmation page for %s", slot.label)
    await log_post_confirmation_state(page, slot, logger)
    await save_named_screenshot(
        page,
        f"{account.label}-missing-confirmation-{slot.start_time}-court-{slot.court_number}",
        config.timezone_name,
        logger,
    )
    return False


async def read_page_body_text(page: Page, timeout_ms: int = 5_000) -> str:
    try:
        return await page.locator("body").inner_text(timeout=timeout_ms)
    except PlaywrightError:
        return ""


async def read_lease_creation_error_count(
    page: Page,
    timeout_ms: int = 2_000,
) -> int | None:
    """Return the current lease-error count, or None when the page is unreadable."""

    try:
        body_text = await page.locator("body").inner_text(timeout=timeout_ms)
    except PlaywrightError:
        return None
    return lease_creation_error_count(body_text)


async def wait_for_network_idle_best_effort(
    page: Page,
    logger: logging.Logger,
    description: str,
    timeout_ms: int = 5_000,
) -> None:
    try:
        await page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        logger.info(
            "Network did not reach idle after %s; continuing with visible page-state checks.",
            description,
        )


async def refresh_available_spaces_after_rejection(
    page: Page,
    target_date: date,
    config: AppConfig,
    logger: logging.Logger,
) -> None:
    """Clear stale SPA errors and revalidate the booking date before continuing."""

    logger.info(
        "Refreshing available spaces after an explicit lease rejection for %s.",
        format_date_for_site(target_date),
    )
    await page.reload()
    await wait_for_network_idle_best_effort(
        page,
        logger,
        "refreshing after an explicit lease rejection",
    )
    await check_for_access_blockers(page, config, logger)
    await wait_for_available_spaces_content(page, config, logger)
    validate_calendar_target_date(page.url, target_date, config.timezone_name)
    logger.info(
        "Revalidated available-spaces calendar date after lease rejection: %s",
        format_date_for_site(target_date),
    )


async def recover_pending_basket_booking(
    page: Page,
    slot: SlotPreference,
    target_date: date,
    config: AppConfig,
    account: BookingAccountConfig,
    logger: logging.Logger,
) -> bool:
    """Complete the site's zero-price checkout for exactly one known basket item."""

    logger.warning(
        "%s was added to the basket without reaching confirmation; attempting the "
        "site's zero-price basket checkout.",
        slot.label,
    )

    try:
        go_to_basket = page.locator("#go-to-basket-btn").first
        await go_to_basket.wait_for(state="visible", timeout=5_000)
        await go_to_basket.click()
        await page.wait_for_url(
            re.compile(r"/book/(?:basket|success)(?:[/?#]|$)", re.IGNORECASE),
            timeout=10_000,
        )
        await wait_for_network_idle_best_effort(
            page,
            logger,
            "opening the basket",
        )
        await check_for_access_blockers(page, config, logger)

        arrival_body = await read_page_body_text(page)
        if booking_confirmation_detected(page.url, arrival_body):
            logger.info(
                "Booking confirmation appeared while opening the basket for %s.",
                slot.label,
            )
            return True

        if "/book/basket" not in page.url.lower():
            raise BasketRecoveryError(
                f"The basket link for {slot.label} did not open the expected basket page."
            )

        basket_item = page.locator(".basket-item").first
        await basket_item.wait_for(state="visible", timeout=10_000)
        basket_item_count = await page.locator(".basket-item").count()
        if basket_item_count != 1:
            raise BasketRecoveryError(
                f"Expected exactly one basket item for {slot.label}, but found "
                f"{basket_item_count}. Stopping to avoid confirming an unrelated item."
            )
        basket_item_text = await basket_item.inner_text(timeout=5_000)
        if not basket_contains_expected_slot(basket_item_text, slot, target_date):
            raise BasketRecoveryError(
                f"The basket item did not match {slot.label}. Stopping to avoid "
                "confirming an unrelated item."
            )
        if "£0.00" not in basket_item_text:
            raise BasketRecoveryError(
                f"The basket for {slot.label} was not explicitly shown as £0.00. "
                "Stopping instead of entering a paid checkout."
            )

        continue_to_payment = page.locator("#continue-to-payment-btn").first
        await continue_to_payment.wait_for(state="visible", timeout=10_000)
        await continue_to_payment.click(timeout=10_000)
        await page.wait_for_url(
            re.compile(r"/book/(?:checkout|success)(?:[/?#]|$)", re.IGNORECASE),
            timeout=10_000,
        )
        logger.info("Opened zero-price checkout for %s from the basket.", slot.label)
        await wait_for_network_idle_best_effort(
            page,
            logger,
            "opening zero-price checkout",
        )
        await check_for_access_blockers(page, config, logger)

        checkout_body = await read_page_body_text(page)
        if booking_confirmation_detected(page.url, checkout_body):
            logger.info(
                "Booking confirmation appeared while opening checkout for %s.",
                slot.label,
            )
            return True

        if "/book/checkout" not in page.url.lower():
            raise BasketRecoveryError(
                f"The basket checkout for {slot.label} did not open the expected "
                "checkout page."
            )

        submit_no_price = page.locator("#submit-no-price-btn").first
        await submit_no_price.wait_for(state="visible", timeout=10_000)
        await submit_no_price.click(timeout=10_000)
        logger.info("Submitted zero-price basket confirmation for %s.", slot.label)
        await wait_for_network_idle_best_effort(
            page,
            logger,
            "submitting zero-price basket confirmation",
        )
    except BasketRecoveryError:
        raise
    except PlaywrightError as exc:
        raise BasketRecoveryError(
            f"Could not complete the basket recovery controls for {slot.label}: {exc}"
        ) from exc

    if await wait_for_booking_confirmation_page(
        page,
        slot,
        config,
        account,
        logger,
    ):
        logger.info("Recovered and confirmed %s through the basket.", slot.label)
        return True

    raise BasketRecoveryError(
        f"Basket checkout for {slot.label} was submitted, but no explicit booking "
        "confirmation was detected. Stopping to avoid a duplicate booking."
    )


async def recover_basket_state_if_present(
    page: Page,
    slot: SlotPreference,
    target_date: date,
    config: AppConfig,
    account: BookingAccountConfig,
    logger: logging.Logger,
) -> str | None:
    try:
        body_text = await page.locator("body").inner_text(timeout=5_000)
    except PlaywrightError as exc:
        raise BasketRecoveryError(
            f"Could not inspect the page after the unconfirmed attempt for "
            f"{slot.label}. Stopping to avoid a duplicate booking."
        ) from exc
    if not body_text.strip():
        raise BasketRecoveryError(
            f"The page was blank after the unconfirmed attempt for {slot.label}. "
            "Stopping to avoid a duplicate booking."
        )

    if booking_confirmation_detected(page.url, body_text):
        logger.info(
            "Recovered an explicit booking confirmation for %s while checking the "
            "post-submit page state.",
            slot.label,
        )
        return "confirmed"

    if basket_item_added_detected(body_text):
        recovered = await recover_pending_basket_booking(
            page,
            slot,
            target_date,
            config,
            account,
            logger,
        )
        return "confirmed" if recovered else None

    if basket_slot_conflict_detected(body_text):
        raise BasketRecoveryError(
            f"The site reports that the {slot.start_time} slot is already in the "
            "basket, but it was not added by this recoverable attempt. Stopping to "
            "avoid confirming or creating a duplicate booking."
        )

    return None


async def recover_or_stop_after_unknown_final_action(
    page: Page,
    slot: SlotPreference,
    target_date: date,
    config: AppConfig,
    account: BookingAccountConfig,
    logger: logging.Logger,
    cause: Exception,
    lease_error_baseline: int | None = None,
) -> str:
    try:
        recovered_outcome = await recover_basket_state_if_present(
            page,
            slot,
            target_date,
            config,
            account,
            logger,
        )
    except (AccessBlockerError, BasketRecoveryError):
        raise
    except Exception as recovery_exc:  # noqa: BLE001 - final-submit safety boundary
        raise BasketRecoveryError(
            f"The final booking action for {slot.label} ended in an unknown state, "
            "and the page could not be inspected safely. Stopping to avoid a "
            "duplicate booking."
        ) from recovery_exc

    if recovered_outcome is not None:
        return recovered_outcome

    post_submit_lease_error_count = await read_lease_creation_error_count(page)
    if (
        lease_error_baseline is not None
        and post_submit_lease_error_count is not None
        and post_submit_lease_error_count > lease_error_baseline
    ):
        logger.warning(
            "The final click for %s raised an error, but the site explicitly rejected "
            "temporary reservation creation and no confirmation or basket item exists; "
            "continuing safely.",
            slot.label,
        )
        await close_open_slot_panel(page, slot, logger)
        return "lease-rejected"

    raise BasketRecoveryError(
        f"The final booking action for {slot.label} ended in an unknown state. "
        "Stopping to avoid a duplicate booking."
    ) from cause


async def current_slot_is_unavailable(page: Page, slot: SlotPreference) -> bool:
    card = await find_slot_card(page, slot)
    if card is None:
        return False
    try:
        card_text = " ".join((await card.inner_text()).split()).lower()
    except PlaywrightError:
        return False
    return any(
        message in card_text
        for message in (
            "this slot is unavailable",
            "no additional spaces available",
        )
    )


async def close_open_slot_panel(page: Page, slot: SlotPreference, logger: logging.Logger) -> None:
    close_patterns = (
        re.compile(r"cancel.*close activity slot form", re.IGNORECASE),
        re.compile(r"close activity slot", re.IGNORECASE),
    )
    for pattern in close_patterns:
        button = page.get_by_role("button", name=pattern).first
        if not await button.count():
            continue
        try:
            if not await button.is_visible():
                continue
            await button.click()
            logger.info("Closed open slot panel after unconfirmed attempt for %s", slot.label)
            await page.wait_for_timeout(500)
            return
        except PlaywrightError as exc:
            logger.warning("Failed to close slot panel for %s using %s: %s", slot.label, pattern.pattern, exc)

    logger.warning("Could not find a close button for the open slot panel after unconfirmed attempt for %s", slot.label)


async def try_book_slot(
    page: Page,
    start_time: str,
    court_number: int,
    target_date: date,
    config: AppConfig,
    account: BookingAccountConfig,
    logger: logging.Logger,
    claim_slot: Callable[[SlotPreference], bool] | None = None,
) -> str | None:
    slot = SlotPreference(start_time=start_time, court_number=court_number)
    logger.info("Trying preferred slot: %s", slot.label)

    initial_button = await get_visible_slot_button(page, slot)
    if initial_button is None:
        logger.info(
            "Slot button not found for %s using 12-hour label %s",
            slot.label,
            format_time_for_button_label(slot.start_time),
        )
        return None

    if claim_slot is not None:
        if not claim_slot(slot):
            logger.info(
                "Skipping %s because another account already claimed that exact slot.",
                slot.label,
            )
            return None
        logger.info(
            "Selected and claimed %s; coordinated accounts may now choose another court.",
            slot.label,
        )

    await initial_button.click()
    logger.info("Opened details panel for %s", slot.label)
    await check_for_access_blockers(page, config, logger)

    if config.dry_run:
        logger.info("DRY_RUN=true so booking stops before final confirmation for %s", slot.label)
        return "dry-run"

    final_button = await find_final_book_button(page)
    try:
        await final_button.wait_for(state="visible", timeout=15_000)
    except PlaywrightTimeoutError:
        recovered_outcome = await recover_basket_state_if_present(
            page,
            slot,
            target_date,
            config,
            account,
            logger,
        )
        if recovered_outcome is not None:
            return recovered_outcome

        if await current_slot_is_unavailable(page, slot):
            logger.info(
                "The final booking button disappeared because %s became unavailable; "
                "continuing to the next preference.",
                slot.label,
            )
            await close_open_slot_panel(page, slot, logger)
            return "unconfirmed"
        raise

    lease_error_baseline = await read_lease_creation_error_count(page)
    if lease_error_baseline is None:
        logger.warning(
            "Could not establish the pre-submit lease-error state for %s; any "
            "unconfirmed result will retain the existing duplicate-booking safety stop.",
            slot.label,
        )

    try:
        await final_button.click()
    except Exception as exc:  # noqa: BLE001 - click may already have reached the site
        return await recover_or_stop_after_unknown_final_action(
            page,
            slot,
            target_date,
            config,
            account,
            logger,
            exc,
            lease_error_baseline=lease_error_baseline,
        )
    logger.info("Clicked final Book Badminton confirmation for %s", slot.label)
    try:
        try:
            await page.wait_for_load_state("networkidle", timeout=5_000)
        except PlaywrightTimeoutError:
            logger.info(
                "Network did not reach idle promptly after confirming %s; "
                "continuing to wait for confirmation page.",
                slot.label,
            )
        confirmation_detected = await wait_for_booking_confirmation_page(
            page,
            slot,
            config,
            account,
            logger,
            lease_error_baseline=lease_error_baseline,
        )
        if confirmation_detected:
            return "confirmed"

        recovered_outcome = await recover_basket_state_if_present(
            page,
            slot,
            target_date,
            config,
            account,
            logger,
        )
        if recovered_outcome is not None:
            return recovered_outcome

        post_submit_lease_error_count = await read_lease_creation_error_count(page)
        if (
            lease_error_baseline is not None
            and post_submit_lease_error_count is not None
            and post_submit_lease_error_count > lease_error_baseline
        ):
            logger.warning(
                "The site explicitly rejected temporary reservation creation for %s, "
                "and no confirmation or basket item exists; continuing safely.",
                slot.label,
            )
            await close_open_slot_panel(page, slot, logger)
            return "lease-rejected"

        if await current_slot_is_unavailable(page, slot):
            logger.info(
                "%s became unavailable without a confirmation or basket item; "
                "continuing to the next preference.",
                slot.label,
            )
            await close_open_slot_panel(page, slot, logger)
            return "unconfirmed"

        raise BasketRecoveryError(
            f"The final booking action for {slot.label} produced neither a "
            "confirmation nor a recoverable basket item. Stopping to avoid a "
            "duplicate booking."
        )
    except (AccessBlockerError, BasketRecoveryError):
        raise
    except Exception as exc:  # noqa: BLE001 - submission has already been sent
        return await recover_or_stop_after_unknown_final_action(
            page,
            slot,
            target_date,
            config,
            account,
            logger,
            exc,
            lease_error_baseline=lease_error_baseline,
        )


async def book_best_available_slot(
    page: Page,
    config: AppConfig,
    account: BookingAccountConfig,
    logger: logging.Logger,
    target_date: date,
    preferences: Sequence[SlotPreference],
    phase_name: str,
    booking_flow_started_at: float | None = None,
    claim_slot: Callable[[SlotPreference], bool] | None = None,
    release_slot: Callable[[SlotPreference], bool] | None = None,
) -> BookingAttemptResult | None:
    if not preferences:
        logger.info("No slot preferences were configured for the %s phase.", phase_name)
        return None

    logger.info("Checking %s slots in priority order.", phase_name)
    visible_slots = await list_visible_bookable_slots(page, preferences)
    if booking_flow_started_at is not None:
        logger.info(
            "Timing | login page open -> preferred slot availability resolved: %.3fs",
            perf_counter() - booking_flow_started_at,
        )
    if visible_slots:
        logger.info("Currently bookable preferred slots: %s", ", ".join(visible_slots))
    else:
        logger.info("No preferred slots appear bookable from the current page state.")

    planned_slot = pick_best_available_slot(visible_slots, preferences)
    if planned_slot is not None:
        logger.info("Best currently available %s slot: %s", phase_name, planned_slot.label)

    last_unconfirmed_slot: SlotPreference | None = None
    for slot in preferences:
        outcome = await try_book_slot(
            page,
            slot.start_time,
            slot.court_number,
            target_date,
            config,
            account,
            logger,
            claim_slot,
        )
        if outcome in {"unconfirmed", "lease-rejected"}:
            if release_slot is not None and release_slot(slot):
                logger.info(
                    "Released the coordinated claim for %s after a definitive failed attempt.",
                    slot.label,
                )
            last_unconfirmed_slot = slot
            if outcome == "lease-rejected":
                logger.warning(
                    "Temporary reservation creation was explicitly rejected for %s; "
                    "refreshing and continuing to the next preferred slot.",
                    slot.label,
                )
                await refresh_available_spaces_after_rejection(
                    page,
                    target_date,
                    config,
                    logger,
                )
            else:
                logger.warning(
                    "No booking confirmation page was detected for %s, so continuing to the next preferred slot.",
                    slot.label,
                )
            continue
        if outcome is not None:
            return BookingAttemptResult(slot=slot, outcome=outcome)
    if last_unconfirmed_slot is not None:
        return BookingAttemptResult(slot=last_unconfirmed_slot, outcome="unconfirmed")
    return None


def get_account_logger(
    base_logger: logging.Logger,
    account: BookingAccountConfig,
) -> AccountLoggerAdapter:
    return AccountLoggerAdapter(base_logger, {"account_label": account.label})


def _phase_selection_key(account_key: str, start_time: str) -> tuple[str, str]:
    return account_key, start_time


def get_phase_selection_event(
    coordinator: BookingCoordinator,
    account_key: str,
    start_time: str,
) -> asyncio.Event:
    key = _phase_selection_key(account_key, start_time)
    return coordinator.phase_selection_ready.setdefault(key, asyncio.Event())


def publish_phase_selection(
    coordinator: BookingCoordinator,
    account_key: str,
    start_time: str,
    selection: SlotPreference | None,
) -> None:
    key = _phase_selection_key(account_key, start_time)
    ready_event = get_phase_selection_event(coordinator, account_key, start_time)

    if ready_event.is_set():
        return
    coordinator.phase_selections[key] = selection
    ready_event.set()


def release_remaining_account_phases(
    coordinator: BookingCoordinator,
    account_key: str,
) -> None:
    for start_time in get_account_attempt_times(account_key):
        publish_phase_selection(coordinator, account_key, start_time, None)


async def wait_for_phase_selection(
    coordinator: BookingCoordinator,
    account_key: str,
    start_time: str,
    logger: logging.Logger,
) -> SlotPreference | None:
    ready_event = get_phase_selection_event(coordinator, account_key, start_time)

    logger.info(
        "Waiting for Account A to select a %s court before Account B starts the same time.",
        start_time,
    )
    await ready_event.wait()
    return coordinator.phase_selections.get(
        _phase_selection_key(account_key, start_time)
    )


def claim_slot_for_account(
    coordinator: BookingCoordinator,
    account_key: str,
    slot: SlotPreference,
) -> bool:
    owner = coordinator.slot_claim_owners.get(slot)
    if owner is not None and owner != account_key:
        return False
    coordinator.slot_claim_owners.setdefault(slot, account_key)
    publish_phase_selection(
        coordinator,
        account_key,
        slot.start_time,
        slot,
    )
    return True


def release_slot_for_account(
    coordinator: BookingCoordinator,
    account_key: str,
    slot: SlotPreference,
) -> bool:
    """Release an exact-slot claim only when it is owned by this account."""

    if coordinator.slot_claim_owners.get(slot) != account_key:
        return False
    del coordinator.slot_claim_owners[slot]
    return True


def _account_is_configured(config: AppConfig, account_key: str) -> bool:
    return any(account.key == account_key for account in config.accounts)


def build_phase_preferences(
    account: BookingAccountConfig,
    start_time: str,
    coordinator: BookingCoordinator | None = None,
) -> list[SlotPreference]:
    preferences = build_account_slot_priority(account, (start_time,))
    if coordinator is not None:
        preferences = [
            slot
            for slot in preferences
            if coordinator.slot_claim_owners.get(slot) in (None, account.key)
        ]
    return preferences


async def run_account_booking_session(
    browser,
    config: AppConfig,
    account: BookingAccountConfig,
    logger: logging.Logger,
    coordinator: BookingCoordinator,
    progress: AccountRunProgress,
    attempt_number: int,
) -> BookingAttemptResult | None:
    context = None
    page = None
    target_date = coordinator.target_date
    try:
        context = await browser.new_context(
            locale="en-GB",
            timezone_id=config.timezone_name,
        )
        page = await context.new_page()
        booking_flow_started_at = perf_counter()

        await login(page, config, account, logger)
        logger.info(
            "Timing | session %s login page open -> login complete: %.3fs",
            attempt_number,
            perf_counter() - booking_flow_started_at,
        )
        await open_booking_search(page, config, logger)

        local_now = get_local_now(config.timezone_name)
        if coordinator.release_at is not None and local_now < coordinator.release_at:
            logger.info(
                "Prewarming the booking search form for %s before midnight.",
                format_date_for_site(target_date),
            )
            await prepare_badminton_search_form(
                page,
                target_date,
                config,
                account,
                logger,
            )
            wait_seconds = max(
                (coordinator.release_at - get_local_now(config.timezone_name)).total_seconds(),
                0.0,
            )
            if wait_seconds > 0:
                logger.info(
                    "Midnight prewarm mode is waiting %.3fs before submitting the search.",
                    wait_seconds,
                )
                await page.wait_for_timeout(int(wait_seconds * 1000))
            await submit_badminton_search(page, config, logger)
        else:
            await search_badminton(page, target_date, config, account, logger)

        logger.info(
            "Timing | session %s login page open -> search submitted: %.3fs",
            attempt_number,
            perf_counter() - booking_flow_started_at,
        )
        await open_available_spaces(page, target_date, config, account, logger)
        logger.info(
            "Timing | session %s login page open -> available spaces page opened: %.3fs",
            attempt_number,
            perf_counter() - booking_flow_started_at,
        )

        last_result: BookingAttemptResult | None = None
        for start_time in get_account_attempt_times(account.key):
            if start_time in progress.completed_times:
                logger.info(
                    "Skipping already completed %s phase after session recovery.",
                    start_time,
                )
                continue

            if (
                account.key == ACCOUNT_B_KEY
                and _account_is_configured(config, ACCOUNT_A_KEY)
            ):
                account_a_selection = await wait_for_phase_selection(
                    coordinator,
                    ACCOUNT_A_KEY,
                    start_time,
                    logger,
                )
                if account_a_selection is not None:
                    logger.info(
                        "Account A selected %s, so Account B will skip that exact slot without waiting for confirmation.",
                        account_a_selection.label,
                    )

            preferences = build_phase_preferences(
                account,
                start_time,
                coordinator,
            )

            logger.info(
                "%s booking phase targets: %s",
                start_time,
                ", ".join(slot.label for slot in preferences),
            )
            phase_result = await book_best_available_slot(
                page,
                config,
                account,
                logger,
                target_date=target_date,
                preferences=preferences,
                phase_name=start_time,
                booking_flow_started_at=(
                    booking_flow_started_at
                    if not progress.completed_times
                    else None
                ),
                claim_slot=lambda slot: claim_slot_for_account(
                    coordinator,
                    account.key,
                    slot,
                ),
                release_slot=lambda slot: release_slot_for_account(
                    coordinator,
                    account.key,
                    slot,
                ),
            )
            progress.completed_times.add(start_time)
            if phase_result is not None:
                if booking_attempt_succeeded(phase_result):
                    if not claim_slot_for_account(
                        coordinator,
                        account.key,
                        phase_result.slot,
                    ):
                        raise AssertionError(
                            f"{account.label} returned a successful result for "
                            f"{phase_result.slot.label} without owning the coordinated "
                            "slot claim."
                        )
                last_result = phase_result
            else:
                publish_phase_selection(
                    coordinator,
                    account.key,
                    start_time,
                    None,
                )
            if booking_attempt_succeeded(phase_result):
                break

        if not config.dry_run and not booking_attempt_succeeded(last_result):
            await log_available_spaces_diagnostics_best_effort(
                page,
                config,
                account,
                logger,
            )

        if config.debug_pause_seconds > 0:
            logger.info(
                "DEBUG_PAUSE_SECONDS=%s so pausing on the available spaces page.",
                config.debug_pause_seconds,
            )
            await page.wait_for_timeout(config.debug_pause_seconds * 1000)
        return last_result
    except Exception:
        if page is not None:
            await save_failure_screenshot(
                page,
                f"{account.label}-session-{attempt_number}-failure",
                config.timezone_name,
                logger,
            )
        raise
    finally:
        if page is not None:
            try:
                await page.close()
                logger.info("Page closed for session attempt %s.", attempt_number)
            except PlaywrightError as exc:
                logger.warning("Failed to close page cleanly: %s", exc)
        if context is not None:
            try:
                await context.close()
                logger.info("Browser context closed for session attempt %s.", attempt_number)
            except PlaywrightError as exc:
                logger.warning("Failed to close browser context cleanly: %s", exc)


async def run_account_booking(
    browser,
    config: AppConfig,
    account: BookingAccountConfig,
    base_logger: logging.Logger,
    coordinator: BookingCoordinator,
) -> int:
    logger = get_account_logger(base_logger, account)
    progress = AccountRunProgress()
    logger.info(
        "HEADLESS=%s DRY_RUN=%s TIMEZONE=%s",
        config.headless,
        config.dry_run,
        config.timezone_name,
    )
    logger.info("Target booking date is %s", format_date_for_site(coordinator.target_date))
    logger.info(
        "Booking time order is %s; court order is %s.",
        " -> ".join(get_account_attempt_times(account.key)),
        " -> ".join(str(court) for court in account.court_priority),
    )
    if coordinator.release_at is not None:
        logger.info(
            "Midnight prewarm mode is active. Retries will keep the fixed target date %s.",
            format_date_for_site(coordinator.target_date),
        )
    if config.target_date_override:
        logger.warning(
            "TARGET_DATE_OVERRIDE is active. Using override date %s instead of Europe/London today + 8 days.",
            format_date_for_site(coordinator.target_date),
        )

    try:
        for attempt_number in range(1, MAX_ACCOUNT_SESSION_ATTEMPTS + 1):
            logger.info(
                "Starting browser session attempt %s/%s.",
                attempt_number,
                MAX_ACCOUNT_SESSION_ATTEMPTS,
            )
            try:
                booking_result = await run_account_booking_session(
                    browser,
                    config,
                    account,
                    logger,
                    coordinator,
                    progress,
                    attempt_number,
                )
            except (
                AccessBlockerError,
                AuthenticationError,
                BasketRecoveryError,
                ValueError,
                TypeError,
                AssertionError,
                KeyError,
            ) as exc:
                logger.exception("Booking run failed: %s", exc)
                return 1
            except Exception as exc:  # noqa: BLE001 - bounded recovery boundary
                if attempt_number >= MAX_ACCOUNT_SESSION_ATTEMPTS:
                    logger.exception(
                        "Booking run failed: exhausted %s browser session attempts: %s",
                        MAX_ACCOUNT_SESSION_ATTEMPTS,
                        exc,
                    )
                    return 1
                logger.exception(
                    "Browser session attempt %s/%s failed: %s",
                    attempt_number,
                    MAX_ACCOUNT_SESSION_ATTEMPTS,
                    exc,
                )
                logger.info(
                    "Closing the failed session and retrying with a fresh login in %ss.",
                    ACCOUNT_RETRY_DELAY_SECONDS,
                )
                await asyncio.sleep(ACCOUNT_RETRY_DELAY_SECONDS)
                continue

            if booking_result is None:
                logger.info("No preferred slots were available to book.")
            elif booking_result.outcome == "dry-run":
                logger.info("Success: would book %s", booking_result.slot.label)
            elif booking_result.outcome == "confirmed":
                logger.info("Success: confirmed booking for %s", booking_result.slot.label)
            elif booking_result.outcome == "unconfirmed":
                logger.warning(
                    "Tried all preferred slots in order, but none reached the booking confirmation page. Last unconfirmed attempt: %s",
                    booking_result.slot.label,
                )
            else:
                logger.warning(
                    "Booking was submitted for %s, but no explicit confirmation signal was detected. Verify it in the site.",
                    booking_result.slot.label,
                )
            return 0

        logger.error("Booking run failed: no browser session attempt was executed.")
        return 1
    finally:
        release_remaining_account_phases(coordinator, account.key)


async def run_booking() -> int:
    config = load_config()
    logger, log_path = setup_logging(config.timezone_name)
    logger.info("Log file: %s", log_path)
    logger.info(
        "Configured booking accounts: %s",
        ", ".join(account.label for account in config.accounts),
    )

    browser = None
    run_now = datetime.now(timezone.utc)
    prewarm_mode = should_use_midnight_prewarm(
        timezone_name=config.timezone_name,
        target_date_override=config.target_date_override,
        now=run_now,
    )
    release_at: datetime | None = None
    if prewarm_mode:
        local_now = get_local_now(config.timezone_name, now=run_now)
        release_at = datetime.combine(
            local_now.date() + timedelta(days=1),
            datetime.min.time(),
            tzinfo=local_now.tzinfo,
        )
        coordinator_target_date = compute_target_date_after_next_local_midnight(
            config.timezone_name,
            now=run_now,
        )
    else:
        coordinator_target_date, _ = resolve_target_date(
            timezone_name=config.timezone_name,
            target_date_override=config.target_date_override,
            now=run_now,
        )
    coordinator = BookingCoordinator(
        target_date=coordinator_target_date,
        release_at=release_at,
    )
    exit_code = 1
    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=config.headless)
            results = await asyncio.gather(
                *[
                    run_account_booking(browser, config, account, logger, coordinator)
                    for account in config.accounts
                ]
            )
            if any(result != 0 for result in results):
                exit_code = 1
            else:
                exit_code = 0
        finally:
            if browser is not None:
                try:
                    await browser.close()
                    logger.info("Browser closed.")
                except PlaywrightError as exc:
                    logger.warning("Failed to close browser cleanly: %s", exc)

    if email_notifications_enabled() and not config.dry_run:
        try:
            await asyncio.to_thread(
                send_booking_report,
                log_path,
                LOGS_DIR,
                config.timezone_name,
                logger,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to send booking report email: %s", exc)
    elif config.dry_run:
        logger.info("Skipping booking report email because DRY_RUN=true.")

    return exit_code


def main() -> int:
    return asyncio.run(run_booking())


if __name__ == "__main__":
    raise SystemExit(main())
