#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
SERVICES=(
  "xr-teleop-web.service"
  "hand-web.service"
  "teleimager-camera-capture.service"
  "robot-sync-tool.service"
  "h2-switch-flip-api.service"
)
LOGROTATE_SRC="${PROJECT_ROOT}/config/eai-teleop-studio.logrotate"
LOGROTATE_DST="/etc/logrotate.d/eai-teleop-studio"

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  cat <<EOF
Usage: bash scripts/install_autostart_services.sh [service ...]

Install and enable project systemd services. If no service is provided, all
services are installed:
  ${SERVICES[*]}

Project root is detected as:
  ${PROJECT_ROOT}
EOF
  exit 0
fi

if [[ "$(id -u)" -ne 0 ]]; then
  echo "ERROR: run this script with sudo." >&2
  exit 1
fi

if [[ "$#" -gt 0 ]]; then
  SERVICES=("$@")
fi

install -d -o robot -g robot -m 775 \
  "${PROJECT_ROOT}/logs/app" \
  "${PROJECT_ROOT}/logs/system" \
  "${PROJECT_ROOT}/logs/tasks" \
  "${PROJECT_ROOT}/config"

for service in "${SERVICES[@]}"; do
  src="${PROJECT_ROOT}/systemd/${service}"
  dst="/etc/systemd/system/${service}"
  if [[ ! -f "${src}" ]]; then
    echo "ERROR: service file not found: ${src}" >&2
    exit 1
  fi
  sed "s#@PROJECT_ROOT@#${PROJECT_ROOT}#g" "${src}" > "${dst}"
  chmod 0644 "${dst}"
  echo "Installed ${dst}"
done

if [[ -f "${LOGROTATE_SRC}" ]]; then
  sed "s#@PROJECT_ROOT@#${PROJECT_ROOT}#g" "${LOGROTATE_SRC}" > "${LOGROTATE_DST}"
  chmod 0644 "${LOGROTATE_DST}"
  echo "Installed ${LOGROTATE_DST}"
else
  echo "WARN: logrotate template not found: ${LOGROTATE_SRC}" >&2
fi

systemctl daemon-reload
for service in "${SERVICES[@]}"; do
  systemctl enable --now "${service}"
  systemctl --no-pager --lines=0 status "${service}" || true
done

echo "Done. View logs with:"
echo "  journalctl -u <service> -f"
echo "  tail -f ${PROJECT_ROOT}/logs/app/<service>.log"
echo "Log rotation:"
echo "  logrotate -d ${LOGROTATE_DST}"
