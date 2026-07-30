#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="robot-sync-tool.service"
SERVICE_DST="/etc/systemd/system/${SERVICE_NAME}"

systemctl disable --now "${SERVICE_NAME}" || true
rm -f "${SERVICE_DST}"
systemctl daemon-reload

echo "Stopped and removed ${SERVICE_NAME}"

