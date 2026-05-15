# Badminton Court Booker

Python Playwright automation for booking badminton courts on Southampton Sport Gladstone Go.

## Production Rules

- Target date = current `Europe/London` date + 8 days
- Preferred order:
  1. `19:00 Jubilee Court 1`
  2. `19:00 Jubilee Court 2`
  3. `19:00 Jubilee Court 3`
  4. `19:00 Jubilee Court 4`
  5. `18:00 Jubilee Court 1`
  6. `18:00 Jubilee Court 2`
  7. `18:00 Jubilee Court 3`
  8. `18:00 Jubilee Court 4`
- Stop after the first slot that reaches `Booking Confirmed!`
- If a slot does not confirm, continue to the next preferred slot
- `DRY_RUN=true` never clicks the final confirmation button

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
PREFERRED_TIMES=19:00,18:00
PREFERRED_COURTS=1,2,3,4
```

Keep credentials only in `.env`. Leave `TARGET_DATE_OVERRIDE` and `DEBUG_PAUSE_SECONDS` empty in production.

## Configuration Notes

- `TIMEZONE`: keep `Europe/London` in production
- `HEADLESS=true`: run without a visible browser
- `DRY_RUN=true`: safe test mode, never confirms a booking
- `PREFERRED_TIMES`: comma-separated times, e.g. `19:00,18:00`
- `PREFERRED_COURTS`: comma-separated court numbers, e.g. `1,2,3,4`

These two settings:

```dotenv
PREFERRED_TIMES=19:00,18:00
PREFERRED_COURTS=1,2,3,4
```

produce this order:

1. `19:00 Jubilee Court 1`
2. `19:00 Jubilee Court 2`
3. `19:00 Jubilee Court 3`
4. `19:00 Jubilee Court 4`
5. `18:00 Jubilee Court 1`
6. `18:00 Jubilee Court 2`
7. `18:00 Jubilee Court 3`
8. `18:00 Jubilee Court 4`

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
0 0 * * * cd /home/<YOUR_USERNAME>/badminton && /usr/bin/flock -n /tmp/badminton-booking.lock /home/<YOUR_USERNAME>/badminton/.venv/bin/python -u /home/<YOUR_USERNAME>/badminton/book_badminton.py >> /home/<YOUR_USERNAME>/badminton/logs/cron-run.log 2>&1
```

This runs every day at `00:00` London time and prevents overlapping runs.

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
