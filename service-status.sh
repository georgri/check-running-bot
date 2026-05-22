#!/usr/bin/env bash
set -euo pipefail

VPS_HOST="${VPS_HOST:-root@77.238.234.181}"
SERVICE_NAME="${SERVICE_NAME:-check-running-bot.service}"

echo "Service active state on ${VPS_HOST}:"
ssh "${VPS_HOST}" "systemctl is-active ${SERVICE_NAME}"

echo
echo "Service status:"
ssh "${VPS_HOST}" "systemctl status ${SERVICE_NAME} --no-pager"

echo
echo "Last 10 log lines:"
ssh "${VPS_HOST}" "journalctl -u ${SERVICE_NAME} -n 10 --no-pager"
