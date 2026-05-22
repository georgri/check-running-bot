# check-running-bot

Telegram bot service that monitors registration availability for the White Nights Marathon (42.2 km) and sends alerts to a Telegram channel.

## What it does

- Logs in to `runc.run` with your account.
- Opens `https://runc.run/check-in/217/` every 60 seconds.
- Checks if the sold-out text is present:
  - `Свободные места на 42,2 км закончились`
- Sends a Telegram alert to your channel when slots become available.
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
