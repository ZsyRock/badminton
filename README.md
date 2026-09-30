# Badminton Court Booker

Python Playwright automation for booking badminton courts on Southampton Sport Gladstone Go.

## Production Rules

- Target date = current `Europe/London` date + 8 days
- Primary wave: `账号C` targets `17:00`, while `账号A` and `账号B` target
  separate Courts at `18:00`; this aims for one `17:00` slot plus two `18:00`
  slots
- `账号A` and `账号C` use Court order `1 → 2 → 3 → 4`; `账号B` uses
  `2 → 1 → 3 → 4` and skips the exact `18:00` Court already selected by A
- If the primary wave is incomplete, an unbooked account dynamically fills an
  adjacent time. The scheduler first preserves a consecutive two-hour block,
  then prefers a third `19:00` slot over `16:00`, and finally uses the remaining
  target hours down to `15:00`
- An isolated `15:00` slot remains an allowed last-resort booking
- All three accounts share time-and-Court claims so concurrent primary and
  fallback attempts do not race one another for the same slot
- Stop after the first slot that reaches `Booking Confirmed!`
- If the site explicitly rejects creation of its temporary slot reservation,
  release that internal slot claim, refresh and revalidate the target date, then
  continue to the next preferred Court
- `DRY_RUN=true` never clicks the final confirmation button
- A transient stuck session is closed and re-created with a fresh login, with no
  more than three total session attempts per account

The script does not bypass CAPTCHA, MFA, verification pages, forbidden pages, or rate limits.

## What A Git Clone Does Not Include

A downloaded clone does not include local secrets or machine setup:

- `.env`
- `.venv/`
- `logs/`
- `screenshots/`
- local `cron`
- local `systemd` installation

Each user must complete local setup on their own Ubuntu machine.

## Quick Start

Clone the repo:

```bash
git clone <YOUR_GITHUB_REPO_URL> badminton
cd badminton
```

Create the environment and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install --with-deps chromium
```

If Ubuntu says `venv` is missing:

```bash
sudo apt-get update
sudo apt-get install -y python3-venv
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install --with-deps chromium
```

Create `.env`:

```bash
cp .env.example .env
```

Recommended production `.env`:

```dotenv
BOOKING_URL=https://soton.gladstonego.cloud/account
GYM_USERNAME=your-email@example.com
GYM_PASSWORD=your-password
TIMEZONE=Europe/London
HEADLESS=true
DRY_RUN=false
TARGET_DATE_OVERRIDE=
DEBUG_PAUSE_SECONDS=
PREFERRED_TIMES=15:00,16:00,17:00,18:00,19:00
PREFERRED_COURTS=1,2,3,4
SECONDARY_BOOKING_ENABLED=false
SECONDARY_GYM_USERNAME=
SECONDARY_GYM_PASSWORD=
SECONDARY_PREFERRED_TIMES=15:00,16:00,17:00,18:00,19:00
SECONDARY_PREFERRED_COURTS=2,1,3,4
TERTIARY_BOOKING_ENABLED=false
TERTIARY_GYM_USERNAME=
TERTIARY_GYM_PASSWORD=
TERTIARY_PREFERRED_TIMES=15:00,16:00,17:00,18:00,19:00
TERTIARY_PREFERRED_COURTS=1,2,3,4
BOOKING_EMAIL_ENABLED=false
BOOKING_EMAIL_REFERENCE_SCRIPT=/home/<YOUR_USERNAME>/send_ip_email.py
```

Keep credentials only in `.env`. Leave `TARGET_DATE_OVERRIDE` and `DEBUG_PAUSE_SECONDS` empty in production.

## Configuration Notes

- `TIMEZONE`: keep `Europe/London` in production
- `HEADLESS=true`: run without a visible browser
- `DRY_RUN=true`: safe test mode, never confirms a booking
- `PREFERRED_TIMES`: comma-separated search window times for `账号A`
- `PREFERRED_COURTS`: comma-separated court priority for `账号A`
- `SECONDARY_BOOKING_ENABLED=true`: enable an optional second booking account in parallel
- `SECONDARY_GYM_USERNAME` / `SECONDARY_GYM_PASSWORD`: credentials for the optional second account
- `SECONDARY_PREFERRED_TIMES`: comma-separated search window times for `账号B`
- `SECONDARY_PREFERRED_COURTS`: comma-separated court priority for `账号B`
- `TERTIARY_BOOKING_ENABLED=true`: enable `账号C` in parallel
- `TERTIARY_GYM_USERNAME` / `TERTIARY_GYM_PASSWORD`: credentials for `账号C`
- `TERTIARY_PREFERRED_TIMES`: comma-separated search window times for `账号C`
- `TERTIARY_PREFERRED_COURTS`: comma-separated court priority for `账号C`
- `BOOKING_EMAIL_ENABLED=true`: enable the HTML booking report sent by the separate daily 10:00 London-time job
- `BOOKING_EMAIL_REFERENCE_SCRIPT`: optional path to the existing IP email script; its sender, Gmail app password, and recipient are read without executing the script
- `BOOKING_EMAIL_FROM`, `BOOKING_EMAIL_APP_PASSWORD`, `BOOKING_EMAIL_APP_PASSWORD_FILE`, `BOOKING_EMAIL_TO`: optional explicit SMTP settings that take precedence over the reference script; the password-file option keeps the secret outside `.env`, and multiple recipients are comma-separated
- `BOOKING_EMAIL_TEST_TO`: exactly one private recipient for manual report tests; test mode fails closed if this is empty or contains multiple addresses and never falls back to `BOOKING_EMAIL_TO`
- `BOOKING_CANCELLATION_REMINDER_ENABLED=true`: enable calendar invitations during the final one-hour free-cancellation window

The daily email is sent at `10:00 Europe/London`, not at the end of the midnight
booking run. Its opening section lists the slots that can be played on the report
date, while its table shows the
schedule from the current date through the latest attempted or confirmed booking
date reconstructed from `logs/`. Its table covers 15:00 through 19:00 only; consecutive
booked cells are light green and isolated booked cells are light yellow. A target
date where no account secured a slot remains visible, with an em dash in every empty
time cell.
Keep booking logs if you want historical attempts and bookings to remain in the
table.

Optional local corrections can be stored in `manual_bookings.json`. This runtime
data file is intentionally excluded from Git so personal booking history is not
published with the source code.

Each booked slot is displayed as plain `Court N` text. The report does not add a
calendar link, hidden event metadata, or calendar attachment to booked cells.

`booking_cancellation_reminder.py` scans confirmed bookings once per minute. At
five hours before a booked slot, it sends each recipient a private RFC 5545 meeting
invitation titled `Badminton Cancellation DDL (1h left)`. The transparent event
runs until four hours before the slot, so it represents the final one-hour
cancellation window without marking the recipient as busy. Each booking is sent
only to the email address used by that booking account (`GYM_USERNAME`,
`SECONDARY_GYM_USERNAME`, or `TERTIARY_GYM_USERNAME`). Even when two accounts book
different Courts at the same hour, each receives a separate invitation containing
only its own Court. The daily report recipient list is never used for cancellation
reminders. A local state file prevents duplicates. If the computer was offline at
the five-hour point, the script catches up only while the four-hour deadline has
not passed.

The same rules apply to every target weekday. In the primary wave C targets
`17:00`, while A gets first choice at `18:00` and B targets a different `18:00`
Court. B waits only for A's Court selection, not for A's full confirmation page.
If that ideal shape cannot be completed, unbooked accounts dynamically choose
from `15:00` through `19:00` to preserve a consecutive pair. Once there is only
one Court in each hour of a consecutive pair, `19:00` is preferred over `16:00`
for the third booking. An isolated `15:00` is still accepted as a final fallback.
Shared claims prevent two accounts from selecting the same time-and-Court slot.

The available-spaces result must match the requested date twice: the script only
clicks the exact dated result and then verifies the calendar URL's `activityDate`
in `Europe/London` before inspecting or clicking any slot. Logs include
millisecond timestamps. Full-page button/text diagnostics run after all preferred
times are exhausted (or during `DRY_RUN`), not before a successful production
booking attempt.

The preferred-time settings control the search window used before opening available
spaces. Configure all three accounts with the complete `15:00` through `19:00`
window; the coordinator, rather than the order of that comma-separated value,
chooses each account's primary and fallback attempts.

If the site adds a selected slot to the basket but does not reach the normal success
page, the script preserves the original Court choice and completes the site's
zero-price Basket → Checkout → Confirm flow. It verifies that the basket contains
exactly one item matching the attempted Court and time before confirming. If the
basket state is ambiguous, that account stops safely instead of risking a duplicate
booking or blindly moving to another Court.

If Gladstone explicitly returns `ACTIVITY-CALENDAR.ERRORS.CREATE-LEASE` only after
the current final click, and a second state check finds neither a confirmation nor
an expected basket item, the script treats that submission as rejected. It releases
only that account's exact-slot claim, refreshes the calendar, revalidates the target
date, and continues in the configured Court and fallback-time order. A stale error
left by an earlier SPA action is not treated as a new rejection.

For page timeouts or other transient browser failures before an ambiguous final
submission, the failed page and browser context are closed and the account logs in
again. Each account gets at most three total sessions. CAPTCHA, rate limiting,
explicit login rejection, and uncertain post-submit states stop immediately rather
than creating repeated login or duplicate-booking risk.

## Manual Runs

Dry run:

```bash
source .venv/bin/activate
DRY_RUN=true HEADLESS=false python book_badminton.py
```

Real run:

```bash
source .venv/bin/activate
HEADLESS=true DRY_RUN=false python book_badminton.py
```

## Verification

```bash
source .venv/bin/activate
python -m pytest -q
python -m py_compile book_badminton.py booking_daily_report.py booking_cancellation_reminder.py
```

## Ubuntu Scheduling

Recommended: `cron`

```bash
crontab -e
```

Add:

```cron
CRON_TZ=Europe/London
59 23 * * * /usr/bin/flock -n /tmp/badminton-booking.lock /bin/bash -lc 'sleep 30; cd /home/<YOUR_USERNAME>/badminton && /home/<YOUR_USERNAME>/badminton/.venv/bin/python -u /home/<YOUR_USERNAME>/badminton/book_badminton.py >> /home/<YOUR_USERNAME>/badminton/logs/cron-run.log 2>&1'
0 10 * * * /usr/bin/flock -n /tmp/badminton-booking-report.lock /bin/bash -lc 'cd /home/<YOUR_USERNAME>/badminton && /home/<YOUR_USERNAME>/badminton/.venv/bin/python -u /home/<YOUR_USERNAME>/badminton/booking_daily_report.py >> /home/<YOUR_USERNAME>/badminton/logs/booking-report.log 2>&1'
* * * * * /usr/bin/flock -n /tmp/badminton-cancellation-reminder.lock /bin/bash -lc 'cd /home/<YOUR_USERNAME>/badminton && /home/<YOUR_USERNAME>/badminton/.venv/bin/python -u /home/<YOUR_USERNAME>/badminton/booking_cancellation_reminder.py >> /home/<YOUR_USERNAME>/badminton/logs/cancellation-reminder.log 2>&1'
```

This starts every day at `23:59:30` London time, prewarms the three account sessions,
and submits the searches at midnight. The separate report job sends the summary at
`10:00` London time. Each job has its own lock to prevent overlapping runs.

To send a manual report test only to the single address in
`BOOKING_EMAIL_TEST_TO`, run:

```bash
.venv/bin/python booking_daily_report.py --test
```

Check it:

```bash
crontab -l
```

Alternative: `systemd`

Templates are in `deploy/`.

```bash
sed -i "s|<YOUR_USERNAME>|$USER|g" deploy/badminton-booking.service
sudo cp deploy/badminton-booking.service /etc/systemd/system/
sudo cp deploy/badminton-booking.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now badminton-booking.timer
```

The timer runs every day at `00:00:00 Europe/London`.

## Logs

- Logs are written under `logs/`
- Screenshots are written under `screenshots/`
- The browser is closed at the end of every run

Useful commands:

```bash
tail -f logs/cron-run.log
ls -lt logs | head
```

If using `systemd`:

```bash
journalctl -u badminton-booking.service -n 200
journalctl -u badminton-booking.service -f
```

## For Any AI Assistant

This repo does not depend on any specific AI tool. A local assistant such as Claude, Codex, or another AI tool should be able to deploy it by following this order:

1. inspect the repo
2. create `.venv`
3. install `requirements.txt`
4. run `python -m playwright install --with-deps chromium`
5. create `.env` from `.env.example`
6. verify with `pytest` and `py_compile`
7. configure either `cron` or `systemd`

## Files

- [book_badminton.py](book_badminton.py): main script
- [booking_daily_report.py](booking_daily_report.py): 10:00 daily report sender and isolated test-email entry point
- [booking_cancellation_reminder.py](booking_cancellation_reminder.py): five-hour cancellation reminder sender
- [.env.example](.env.example): environment template
- [requirements.txt](requirements.txt): Python dependencies
- [tests/test_booking_helpers.py](tests/test_booking_helpers.py): unit tests
- [tests/test_booking_daily_report.py](tests/test_booking_daily_report.py): report scheduling and test-recipient isolation tests
- [tests/test_booking_cancellation_reminder.py](tests/test_booking_cancellation_reminder.py): reminder timing and calendar-invite tests
- [deploy/badminton-booking.service](deploy/badminton-booking.service): optional `systemd` service template
- [deploy/badminton-booking.timer](deploy/badminton-booking.timer): optional `systemd` timer template

## Selector Maintenance

Do not execute `recorded_flow.py` in production. Use it only as selector reference.

If selectors drift, capture a fresh Playwright recording:

```bash
source .venv/bin/activate
python -m playwright codegen https://soton.gladstonego.cloud/account
```

Then compare the new flow against the helper functions in [book_badminton.py](book_badminton.py).
