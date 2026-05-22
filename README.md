# check-running-bot

Telegram bot service that monitors registration availability for the White Nights Marathon (42.2 km) and sends alerts to a Telegram channel.

## What it does

- Logs in to `runc.run` with your account.
- Opens `https://runc.run/check-in/217/` every 60 seconds.
- Checks if the sold-out text is present:
  - `Свободные места на 42,2 км закончились`
- Sends a Telegram alert to your channel when slots become available.
- Automatically attempts to submit registration when slots are available.
- Sends payment link to Telegram after successful booking.
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
- `CHECK_URL` (default: `https://runc.run/check-in/217/`)
- `SOLD_OUT_MARKER` (default: `Свободные места на 42,2 км закончились`)
- `POLL_INTERVAL_SECONDS` (default: `60`)
- `REQUEST_TIMEOUT_SECONDS` (default: `30`)
- `STATE_FILE` (default: `/root/check-running-bot/state.json`)
- `AUTO_BOOK_ENABLED` (default: `true`)
- `BOOKING_DISTANCE_LABELS` (default: `42,2 км|42.2 км|42 км`)
- `BOOKING_PRIMARY_PACE_LABELS` (example: `3:31-3:45|3:30-3:45`)
- `BOOKING_SECONDARY_USERNAME` (optional second account login)
- `BOOKING_SECONDARY_PASSWORD` (optional second account password)
- `BOOKING_SECONDARY_PACE_LABELS` (example: `3:56-4:05|3:55-4:05`)
- `BOOKING_RETRY_COOLDOWN_SECONDS` (default: `900`)
- `BROWSER_TIMEOUT_MS` (default: `30000`)

## Auto-booking behavior

- When slot is detected as available, bot sends availability notification first.
- Then bot attempts UI booking in headless Chromium (Playwright).
- Bot signs in, selects configured distance and pace, clicks `Зарегистрироваться`, and looks for payment URL.
- Supports booking for both primary and optional secondary account in one availability window.
- Payment link is sent to Telegram channel per account.
- Bot stores per-account booking status in state file and avoids duplicate booking attempts after success.

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
