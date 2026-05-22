#!/usr/bin/env bash
set -euo pipefail

VPS_HOST="${VPS_HOST:-root@77.238.234.181}"
REMOTE_DIR="${REMOTE_DIR:-/root/check-running-bot}"
SERVICE_NAME="${SERVICE_NAME:-check-running-bot.service}"

echo "Deploying to ${VPS_HOST}:${REMOTE_DIR}"

ssh "${VPS_HOST}" "mkdir -p '${REMOTE_DIR}'"
scp app.py requirements.txt "${VPS_HOST}:${REMOTE_DIR}/"

ssh "${VPS_HOST}" "bash -s" <<EOF
set -euo pipefail

REMOTE_DIR="${REMOTE_DIR}"
SERVICE_NAME="${SERVICE_NAME}"

if ! python3 -m venv --help >/dev/null 2>&1; then
  apt-get update -y
  apt-get install -y python3-venv
fi

python3 -m venv "\${REMOTE_DIR}/.venv"
"\${REMOTE_DIR}/.venv/bin/pip" install --upgrade pip >/dev/null
"\${REMOTE_DIR}/.venv/bin/pip" install -r "\${REMOTE_DIR}/requirements.txt" >/dev/null
"\${REMOTE_DIR}/.venv/bin/python" -m playwright install --with-deps chromium >/dev/null

if [ ! -f "\${REMOTE_DIR}/.env" ]; then
  cat > "\${REMOTE_DIR}/.env" <<'ENV'
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
RUNC_USERNAME=
RUNC_PASSWORD=
CHECK_URL=https://runc.run/check-in/217/
SOLD_OUT_MARKER="Свободные места на 42,2 км закончились"
POLL_INTERVAL_SECONDS=60
REQUEST_TIMEOUT_SECONDS=30
STATE_FILE=/root/check-running-bot/state.json
AUTO_BOOK_ENABLED=true
BOOKING_DISTANCE_LABELS=42,2 км|42.2 км|42 км
BOOKING_PRIMARY_PACE_LABELS=3:31-3:45|3:30-3:45|3:31–3:45
BOOKING_SECONDARY_USERNAME=
BOOKING_SECONDARY_PASSWORD=
BOOKING_SECONDARY_PACE_LABELS=3:56-4:05|3:55-4:05|3:56–4:05
CHECK_MAX_RETRIES=3
CHECK_RETRY_DELAY_SECONDS=2
BOOKING_RETRY_COOLDOWN_SECONDS=30
BROWSER_TIMEOUT_MS=30000
ENV
  chmod 600 "\${REMOTE_DIR}/.env"
  echo "Created \${REMOTE_DIR}/.env template. Fill in credentials and rerun deploy."
  exit 1
fi

cat > "/etc/systemd/system/\${SERVICE_NAME}" <<UNIT
[Unit]
Description=White Nights registration slot monitor bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=\${REMOTE_DIR}
EnvironmentFile=\${REMOTE_DIR}/.env
ExecStart=\${REMOTE_DIR}/.venv/bin/python \${REMOTE_DIR}/app.py
Restart=always
RestartSec=10
User=root
Group=root
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

systemctl daemon-reload
systemctl enable --now "\${SERVICE_NAME}"
systemctl restart "\${SERVICE_NAME}"
systemctl --no-pager --full status "\${SERVICE_NAME}"
EOF

echo "Deployment complete."
