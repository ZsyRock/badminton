# Badminton Court Booker

Python Playwright automation for booking badminton courts on Southampton Sport Gladstone Go.

## Production Rules

- Target date = current `Europe/London` date + 8 days
- `账号A`: `18:00 → 16:00 → 20:00`, Court `1 → 2 → 3 → 4`
- `账号B`: `18:00 → 16:00 → 20:00`, Court `2 → 1 → 3 → 4`
- `账号C`: `17:00 → 19:00 → 20:00`, Court `1 → 2 → 3 → 4`
- For each shared A/B time, B waits only until A selects and claims a Court,
  then immediately proceeds while skipping that exact slot
- All three accounts use a shared exact-slot claim registry, including the
  common `20:00` fallback, so they do not race one another for the same Court
- Stop after the first slot that reaches `Booking Confirmed!`
- If a slot does not confirm, continue to the next preferred slot
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
PREFERRED_TIMES=18:00,16:00,20:00
PREFERRED_COURTS=1,2,3,4
SECONDARY_BOOKING_ENABLED=false
SECONDARY_GYM_USERNAME=
SECONDARY_GYM_PASSWORD=
SECONDARY_PREFERRED_TIMES=18:00,16:00,20:00
SECONDARY_PREFERRED_COURTS=2,1,3,4
TERTIARY_BOOKING_ENABLED=false
TERTIARY_GYM_USERNAME=
TERTIARY_GYM_PASSWORD=
TERTIARY_PREFERRED_TIMES=17:00,19:00,20:00
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
- `BOOKING_EMAIL_ENABLED=true`: send an HTML report after a real (non-dry-run) booking run
- `BOOKING_EMAIL_REFERENCE_SCRIPT`: optional path to the existing IP email script; its sender, Gmail app password, and recipient are read without executing the script
- `BOOKING_EMAIL_FROM`, `BOOKING_EMAIL_APP_PASSWORD`, `BOOKING_EMAIL_APP_PASSWORD_FILE`, `BOOKING_EMAIL_TO`: optional explicit SMTP settings that take precedence over the reference script; the password-file option keeps the secret outside `.env`, and multiple recipients are comma-separated

The daily email contains a one-sentence English summary of the current run and a
schedule from the current date through the latest confirmed booking date reconstructed
from `logs/`. Its table covers 16:00 through 20:00; consecutive booked cells are
light green and isolated booked cells are light yellow. Keep booking logs if you want
historical bookings to remain in the table.

Optional local corrections can be stored in `manual_bookings.json`. This runtime
data file is intentionally excluded from Git so personal booking history is not
published with the source code.

Each booked slot is displayed as plain `Court N` text. The report does not add a
calendar link, hidden event metadata, or calendar attachment to booked cells.

The same rules apply to every target weekday. Each account moves to its next time
only if it has not secured a booking. A and C use Court order `1, 2, 3, 4`; B uses
`2, 1, 3, 4`. B waits for A's Court selection at each shared time (`18:00`,
`16:00`, `20:00`) but no longer waits for the full confirmation page. Exact-slot
claims are shared by A, B, and C, so the common `20:00` fallback also avoids
same-Court races.

The available-spaces result must match the requested date twice: the script only
clicks the exact dated result and then verifies the calendar URL's `activityDate`
in `Europe/London` before inspecting or clicking any slot. Logs include
millisecond timestamps. Full-page button/text diagnostics run after all preferred
times are exhausted (or during `DRY_RUN`), not before a successful production
booking attempt.

The preferred-time settings control the search window used before opening available
spaces. The code automatically adds every time required by the fixed account plan.

If the site adds a selected slot to the basket but does not reach the normal success
page, the script preserves the original Court choice and completes the site's
zero-price Basket → Checkout → Confirm flow. It verifies that the basket contains
exactly one item matching the attempted Court and time before confirming. If the
basket state is ambiguous, that account stops safely instead of risking a duplicate
booking or blindly moving to another Court.

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
python -m py_compile book_badminton.py
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
```

This starts every day at `23:59:30` London time, prewarms the three account sessions,
submits the searches at midnight, and prevents overlapping runs.

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
- [.env.example](.env.example): environment template
- [requirements.txt](requirements.txt): Python dependencies
- [tests/test_booking_helpers.py](tests/test_booking_helpers.py): unit tests
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
