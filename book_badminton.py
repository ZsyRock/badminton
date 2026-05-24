from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from time import perf_counter
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Locator, Page, TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright


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
MIDNIGHT_PREWARM_WINDOW_SECONDS = 120


@dataclass(frozen=True)
class SlotPreference:
    start_time: str
    court_number: int

    @property
    def label(self) -> str:
        return f"{self.start_time} Jubilee Court {self.court_number}"


@dataclass(frozen=True)
class AppConfig:
    booking_url: str
    username: str
    password: str
    timezone_name: str
    headless: bool
    dry_run: bool
    debug_pause_seconds: int
    target_date_override: str | None
    preferred_times: tuple[str, ...]
    preferred_courts: tuple[int, ...]

    @property
    def slot_priority(self) -> list[SlotPreference]:
        return build_slot_priority(self.preferred_times, self.preferred_courts)


@dataclass(frozen=True)
class BookingAttemptResult:
    slot: SlotPreference
    outcome: str


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


def pick_best_available_slot(
    available_slot_labels: Iterable[str],
    preferences: Sequence[SlotPreference],
) -> SlotPreference | None:
    available = {label.strip().lower() for label in available_slot_labels}
    for slot in preferences:
        if slot.label.lower() in available:
            return slot
    return None


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


def load_config() -> AppConfig:
    load_dotenv(dotenv_path=ENV_FILE)
    booking_url = os.getenv("BOOKING_URL", DEFAULT_BOOKING_URL).strip() or DEFAULT_BOOKING_URL
    username = os.getenv("GYM_USERNAME", "").strip()
    password = os.getenv("GYM_PASSWORD", "").strip()
    timezone_name = os.getenv("TIMEZONE", DEFAULT_TIMEZONE).strip() or DEFAULT_TIMEZONE
    headless = parse_bool(os.getenv("HEADLESS"), default=False)
    dry_run = parse_bool(os.getenv("DRY_RUN"), default=True)
    debug_pause_seconds = parse_optional_int(os.getenv("DEBUG_PAUSE_SECONDS"), default=0)
    target_date_override = os.getenv("TARGET_DATE_OVERRIDE", "").strip() or None
    preferred_times = parse_csv_strings(
        os.getenv("PREFERRED_TIMES"),
        default=("19:00", "18:00"),
    )
    preferred_courts = parse_csv_ints(
        os.getenv("PREFERRED_COURTS"),
        default=(1, 2, 3, 4),
    )

    if not username:
        raise ValueError("GYM_USERNAME is required in .env")
    if not password:
        raise ValueError("GYM_PASSWORD is required in .env")

    return AppConfig(
        booking_url=booking_url,
        username=username,
        password=password,
        timezone_name=timezone_name,
        headless=headless,
        dry_run=dry_run,
        debug_pause_seconds=debug_pause_seconds,
        target_date_override=target_date_override,
        preferred_times=preferred_times,
        preferred_courts=preferred_courts,
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
        "%(asctime)s | %(levelname)s | %(message)s",
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
            raise RuntimeError(
                f"Encountered blocker '{pattern}'. Stopping without attempting to bypass site protections."
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


async def login(page: Page, config: AppConfig, logger: logging.Logger) -> None:
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

    logger.info("Logging in as %s", config.username)
    username_input = page.get_by_placeholder("Enter your email")
    password_input = page.get_by_placeholder("Enter your password")

    await username_input.wait_for(state="visible")
    await username_input.fill(config.username)
    await password_input.fill(config.password)
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

    keywords = (
        "Jubilee Court",
        "Book now",
        "This slot is unavailable",
        "Available to book from",
        "18:00",
        "19:00",
        "6:00 PM",
        "7:00 PM",
    )
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
            "available-spaces",
            config.timezone_name,
            logger,
        )


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
    logger: logging.Logger,
) -> None:
    formatted_date = format_date_for_site(target_date)
    logger.info("Searching for Badminton on %s", formatted_date)

    await prepare_badminton_search_form(page, target_date, config, logger)
    await submit_badminton_search(page, config, logger)


async def prepare_badminton_search_form(
    page: Page,
    target_date: date,
    config: AppConfig,
    logger: logging.Logger,
) -> None:
    activity_field = get_activity_textbox(page)

    await activity_field.wait_for(state="visible")
    await activity_field.click()
    await activity_field.fill("badminton")
    await page.get_by_role("option", name="Select Badminton option").click()

    await set_target_date(page, target_date, logger)
    await try_select_starting_from(page, config.preferred_times, logger)


async def submit_badminton_search(
    page: Page,
    config: AppConfig,
    logger: logging.Logger,
) -> None:
    search_button = page.get_by_role("button", name="Search for activities")
    await search_button.click()
    await page.wait_for_load_state("networkidle")
    await check_for_access_blockers(page, config, logger)
    logger.info("Search submitted.")


async def open_available_spaces(
    page: Page,
    target_date: date,
    config: AppConfig,
    logger: logging.Logger,
) -> None:
    logger.info("Opening available spaces for %s", format_date_for_site(target_date))

    see_spaces = page.get_by_role(
        "button",
        name=build_available_spaces_button_pattern(target_date),
    ).first
    if not await see_spaces.count():
        see_spaces = page.get_by_role(
            "button",
            name=re.compile(r"Badminton starts on.*See available spaces", re.IGNORECASE),
        ).first

    await see_spaces.wait_for(state="visible", timeout=20_000)
    await see_spaces.click()
    await page.wait_for_load_state("networkidle")
    await check_for_access_blockers(page, config, logger)
    await wait_for_available_spaces_content(page, config, logger)
    logger.info("Available spaces page opened.")
    await log_available_spaces_diagnostics(page, config, logger)


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
    logger: logging.Logger,
    timeout_ms: int = 20_000,
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

        await page.wait_for_timeout(250)

    logger.warning("Timed out waiting for booking confirmation page for %s", slot.label)
    await log_post_confirmation_state(page, slot, logger)
    await save_named_screenshot(
        page,
        f"missing-confirmation-{slot.start_time}-court-{slot.court_number}",
        config.timezone_name,
        logger,
    )
    return False


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
    config: AppConfig,
    logger: logging.Logger,
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

    await initial_button.click()
    logger.info("Opened details panel for %s", slot.label)
    await check_for_access_blockers(page, config, logger)

    if config.dry_run:
        logger.info("DRY_RUN=true so booking stops before final confirmation for %s", slot.label)
        return "dry-run"

    final_button = await find_final_book_button(page)
    await final_button.wait_for(state="visible", timeout=15_000)
    await final_button.click()
    logger.info("Clicked final Book Badminton confirmation for %s", slot.label)
    try:
        await page.wait_for_load_state("networkidle", timeout=5_000)
    except PlaywrightTimeoutError:
        logger.info("Network did not reach idle promptly after confirming %s; continuing to wait for confirmation page.", slot.label)
    confirmation_detected = await wait_for_booking_confirmation_page(page, slot, config, logger)
    if confirmation_detected:
        return "confirmed"

    await close_open_slot_panel(page, slot, logger)
    return "unconfirmed"


async def book_best_available_slot(
    page: Page,
    config: AppConfig,
    logger: logging.Logger,
    booking_flow_started_at: float | None = None,
) -> BookingAttemptResult | None:
    logger.info("Checking preferred slots in priority order.")
    visible_slots = await list_visible_bookable_slots(page, config.slot_priority)
    if booking_flow_started_at is not None:
        logger.info(
            "Timing | login page open -> preferred slot availability resolved: %.2fs",
            perf_counter() - booking_flow_started_at,
        )
    if visible_slots:
        logger.info("Currently bookable preferred slots: %s", ", ".join(visible_slots))
    else:
        logger.info("No preferred slots appear bookable from the current page state.")

    planned_slot = pick_best_available_slot(visible_slots, config.slot_priority)
    if planned_slot is not None:
        logger.info("Best currently available preferred slot: %s", planned_slot.label)

    last_unconfirmed_slot: SlotPreference | None = None
    for slot in config.slot_priority:
        outcome = await try_book_slot(page, slot.start_time, slot.court_number, config, logger)
        if outcome == "unconfirmed":
            last_unconfirmed_slot = slot
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


async def run_booking() -> int:
    config = load_config()
    logger, log_path = setup_logging(config.timezone_name)
    logger.info("Log file: %s", log_path)
    logger.info("HEADLESS=%s DRY_RUN=%s TIMEZONE=%s", config.headless, config.dry_run, config.timezone_name)
    prewarm_mode = should_use_midnight_prewarm(
        timezone_name=config.timezone_name,
        target_date_override=config.target_date_override,
    )
    target_date: date | None = None
    using_override = False
    planned_target_date: date | None = None
    if prewarm_mode:
        planned_target_date = compute_target_date_after_next_local_midnight(config.timezone_name)
        logger.info(
            "Midnight prewarm mode is active. The script will log in before midnight and submit the search just after the London date rolls over."
        )
        logger.info(
            "Planned post-midnight target booking date is %s",
            format_date_for_site(planned_target_date),
        )
    else:
        target_date, using_override = resolve_target_date(
            timezone_name=config.timezone_name,
            target_date_override=config.target_date_override,
        )
        if using_override:
            logger.warning(
                "TARGET_DATE_OVERRIDE is active. Using override date %s instead of Europe/London today + 8 days.",
                format_date_for_site(target_date),
            )
        logger.info("Target booking date is %s", format_date_for_site(target_date))
    logger.info(
        "Slot priority: %s",
        ", ".join(slot.label for slot in config.slot_priority),
    )

    browser = None
    context = None
    page = None

    async with async_playwright() as playwright:
        try:
            browser = await playwright.chromium.launch(headless=config.headless)
            context = await browser.new_context(
                locale="en-GB",
                timezone_id=config.timezone_name,
            )
            page = await context.new_page()

            booking_flow_started_at = perf_counter()
            await login(page, config, logger)
            logger.info(
                "Timing | login page open -> login complete: %.2fs",
                perf_counter() - booking_flow_started_at,
            )
            await open_booking_search(page, config, logger)
            if prewarm_mode:
                assert planned_target_date is not None
                logger.info(
                    "Prewarming the booking search form for %s before midnight.",
                    format_date_for_site(planned_target_date),
                )
                await prepare_badminton_search_form(page, planned_target_date, config, logger)
                seconds_until_midnight = seconds_until_next_local_midnight(config.timezone_name)
                if seconds_until_midnight > 0:
                    logger.info(
                        "Midnight prewarm mode is waiting %.2fs before submitting the search.",
                        seconds_until_midnight,
                    )
                    wait_started_at = perf_counter()
                    await page.wait_for_timeout(int(seconds_until_midnight * 1000))
                    logger.info(
                        "Timing | midnight prewarm wait before search: %.2fs",
                        perf_counter() - wait_started_at,
                    )
                target_date, using_override = resolve_target_date(
                    timezone_name=config.timezone_name,
                    target_date_override=config.target_date_override,
                )
                if using_override:
                    logger.warning(
                        "TARGET_DATE_OVERRIDE is active. Using override date %s instead of Europe/London today + 8 days.",
                        format_date_for_site(target_date),
                    )
                logger.info("Target booking date is %s", format_date_for_site(target_date))
                if target_date != planned_target_date:
                    logger.warning(
                        "Post-midnight target date changed from prewarmed %s to %s. Updating the search form before submitting.",
                        format_date_for_site(planned_target_date),
                        format_date_for_site(target_date),
                    )
                    await prepare_badminton_search_form(page, target_date, config, logger)
                await submit_badminton_search(page, config, logger)
            else:
                assert target_date is not None
                await search_badminton(page, target_date, config, logger)
            logger.info(
                "Timing | login page open -> search submitted: %.2fs",
                perf_counter() - booking_flow_started_at,
            )
            assert target_date is not None
            await open_available_spaces(page, target_date, config, logger)
            logger.info(
                "Timing | login page open -> available spaces page opened: %.2fs",
                perf_counter() - booking_flow_started_at,
            )
            booking_result = await book_best_available_slot(
                page,
                config,
                logger,
                booking_flow_started_at=booking_flow_started_at,
            )

            if booking_result:
                if booking_result.outcome == "dry-run":
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
                if config.debug_pause_seconds > 0:
                    logger.info(
                        "DEBUG_PAUSE_SECONDS=%s so pausing on the available spaces page.",
                        config.debug_pause_seconds,
                    )
                    await page.wait_for_timeout(config.debug_pause_seconds * 1000)
                return 0

            logger.info("No preferred slots were available to book.")
            if config.debug_pause_seconds > 0:
                logger.info(
                    "DEBUG_PAUSE_SECONDS=%s so pausing on the available spaces page.",
                    config.debug_pause_seconds,
                )
                await page.wait_for_timeout(config.debug_pause_seconds * 1000)
            return 0
        except (PlaywrightError, PlaywrightTimeoutError, RuntimeError, ValueError) as exc:
            logger.exception("Booking run failed: %s", exc)
            if page is not None:
                await save_failure_screenshot(
                    page,
                    "booking-failure",
                    config.timezone_name,
                    logger,
                )
            return 1
        finally:
            if page is not None:
                try:
                    await page.close()
                    logger.info("Page closed.")
                except PlaywrightError as exc:
                    logger.warning("Failed to close page cleanly: %s", exc)
            if context is not None:
                try:
                    await context.close()
                    logger.info("Browser context closed.")
                except PlaywrightError as exc:
                    logger.warning("Failed to close browser context cleanly: %s", exc)
            if browser is not None:
                try:
                    await browser.close()
                    logger.info("Browser closed.")
                except PlaywrightError as exc:
                    logger.warning("Failed to close browser cleanly: %s", exc)


def main() -> int:
    return asyncio.run(run_booking())


if __name__ == "__main__":
    raise SystemExit(main())
