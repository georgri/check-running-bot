# check-running-bot

Telegram bot service that monitors registration availability for White Nights race slots and sends alerts to a Telegram channel.

## What it does

- Logs in to `runc.run` with your account.
- Opens both race pages every 60 seconds by default:
  - full marathon: `https://runc.run/check-in/217/`
  - half marathon: `https://runc.run/check-in/228/`
- Checks race-specific sold-out markers:
  - full: `Свободные места на 42,2 км закончились`
  - half: `Свободные места на 21,1 км закончились`
- Sends a Telegram alert to your channel when slots become available.
- Automatically attempts to submit registration when slots are available.
- Sends one booking message with payment link when page enters booked state.
- Detects paid state (redirect to `/races/`) and stops slot checks for that race/account pair.
- Sends diagnostic messages on:
  - service start
  - service shutdown
  - unrecoverable errors

## Project files

- `app.py` - monitoring service.
- `requirements.txt` - Python dependencies.
- `deploy.sh` - deploy/redeploy script for VPS + systemd.

## Prerequisites

Local machine:

- `bash`
- `ssh`/`scp` access to VPS

VPS:

- Ubuntu/Debian with `systemd`
- `python3` and `python3-venv` (deploy script can install if missing)

## Configuration

The service uses environment variables from `.env` on VPS:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `RUNC_USERNAME`
- `RUNC_PASSWORD`
- `POLL_INTERVAL_SECONDS` (default: `60`)
- `REQUEST_TIMEOUT_SECONDS` (default: `30`)
- `STATE_FILE` (default: `/root/check-running-bot/state.json`)
- `AUTO_BOOK_ENABLED` (default: `true`)
- `FULL_CHECK_URL` (default: `https://runc.run/check-in/217/`)
- `FULL_SOLD_OUT_MARKER` (default: `Свободные места на 42,2 км закончились`)
- `FULL_BOOKING_DISTANCE_LABELS` (default: `42,2 км|42.2 км|42 км`)
- `HALF_CHECK_URL` (default: `https://runc.run/check-in/228/`)
- `HALF_SOLD_OUT_MARKER` (default: `Свободные места на 21,1 км закончились`)
- `HALF_BOOKING_DISTANCE_LABELS` (default: `21,1 км|21.1 км|21 км`)
- `BOOKING_PRIMARY_MARATHON_PACE_LABEL` (default: `3:31-3:45`) - marathon pace used for auto-inference
- `BOOKING_PRIMARY_FULL_PACE_LABELS` (optional; defaults to marathon pace)
- `BOOKING_PRIMARY_HALF_PACE_LABELS` (optional; default inferred slightly faster from marathon pace)
- `BOOKING_SECONDARY_USERNAME` (optional second account login)
- `BOOKING_SECONDARY_PASSWORD` (optional second account password)
- `BOOKING_SECONDARY_MARATHON_PACE_LABEL` (default: `3:56-4:05`)
- `BOOKING_SECONDARY_FULL_PACE_LABELS` (optional; defaults to marathon pace)
- `BOOKING_SECONDARY_HALF_PACE_LABELS` (optional; default inferred slightly faster from marathon pace)
- `PACE_INFERENCE_SECONDS_FASTER` (default: `10`) - how much faster than marathon pace to choose for 21.1
- `CHECK_MAX_RETRIES` (default: `3`) - retries before reporting check error
- `CHECK_RETRY_DELAY_SECONDS` (default: `2`) - delay between check retries
- `BOOKING_RETRY_COOLDOWN_SECONDS` (default: `30`)
- `BROWSER_TIMEOUT_MS` (default: `30000`)

## Auto-booking behavior

- When slot is detected as available, bot sends availability notification first.
- Then bot attempts UI booking in headless Chromium (Playwright).
- Bot signs in, selects configured distance and pace, clicks `Зарегистрироваться`, and looks for payment URL.
- Supports booking for both primary and optional secondary account for both targets (full + half).
- Bot stores per-target/per-account booking state (`register`, `booked`, `paid`) in state file.
- While a race/account is in `booked` state, bot does not send repeated booking notifications (booking message is sent only once).
- If booking expires and form appears again, bot resumes slot checks and booking attempts.
- If race/account reaches `paid` state, bot sends one paid message and no longer checks slots for that race/account.
- Bot reports recoverable check error only after check retries are exhausted.

## Telegram status command

- Tag the bot in channel with `status` (example: `@your_bot_username status`).
- Bot replies with the latest check attempt results including target id and timestamps.

## Service health check

Run:

```bash
chmod +x service-status.sh
./service-status.sh
```

## Deploy

Run from project root:

```bash
chmod +x deploy.sh
./deploy.sh
```

By default, script deploys to:

- host: `root@77.238.234.181`
- path: `/root/check-running-bot`
- service: `check-running-bot.service`

You can override:

```bash
VPS_HOST=root@1.2.3.4 REMOTE_DIR=/root/check-running-bot ./deploy.sh
```

## Operations

On VPS:

```bash
systemctl status check-running-bot.service
journalctl -u check-running-bot.service -f
systemctl restart check-running-bot.service
```

## Security notes

- Do not commit `.env` with real secrets.
- Store bot token and site credentials only on VPS with strict permissions (`chmod 600`).
