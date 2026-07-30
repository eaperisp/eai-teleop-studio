#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
SERVICE_NAME="robot-sync-tool.service"
SERVICE_SRC="${PROJECT_ROOT}/systemd/${SERVICE_NAME}"
SERVICE_DST="/etc/systemd/system/${SERVICE_NAME}"
ENV_FILE="${PROJECT_ROOT}/config/robot_sync.env"
ENV_EXAMPLE="${PROJECT_ROOT}/config/robot_sync.env.example"
DEFAULT_CONDA_PYTHON="/home/robot/miniconda3/envs/teleop/bin/python"

if [[ ! -f "${SERVICE_SRC}" ]]; then
  echo "ERROR: service file not found: ${SERVICE_SRC}" >&2
  exit 1
fi

mkdir -p "${PROJECT_ROOT}/config" "${PROJECT_ROOT}/data" "${PROJECT_ROOT}/logs"

if [[ ! -f "${ENV_FILE}" && -f "${ENV_EXAMPLE}" ]]; then
  cp "${ENV_EXAMPLE}" "${ENV_FILE}"
  chmod 600 "${ENV_FILE}"
  echo "Created ${ENV_FILE}. Edit it before starting if paths or password need changes."
fi

if [[ -n "${ROBOT_SYNC_PYTHON:-}" ]]; then
  PYTHON_BIN="${ROBOT_SYNC_PYTHON}"
elif [[ -x "${DEFAULT_CONDA_PYTHON}" ]]; then
  PYTHON_BIN="${DEFAULT_CONDA_PYTHON}"
else
  PYTHON_BIN="$(command -v python3)"
fi

if ! "${PYTHON_BIN}" -m pip --version >/dev/null 2>&1; then
  echo "ERROR: pip is not available for ${PYTHON_BIN}." >&2
  exit 1
fi

"${PYTHON_BIN}" -m pip install -r "${PROJECT_ROOT}/robot_sync_tool/requirements.txt"

if ! grep -q '^ROBOT_SYNC_PYTHON=' "${ENV_FILE}" 2>/dev/null; then
  printf '\nROBOT_SYNC_PYTHON=%s\n' "${PYTHON_BIN}" >> "${ENV_FILE}"
fi

sed "s#@PROJECT_ROOT@#${PROJECT_ROOT}#g" "${SERVICE_SRC}" > "${SERVICE_DST}"
systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}"

echo "Installed and started ${SERVICE_NAME}"
echo "Project: ${PROJECT_ROOT}"
echo "Python:  ${PYTHON_BIN}"
echo "Status:  systemctl status ${SERVICE_NAME}"
echo "Logs:    journalctl -u ${SERVICE_NAME} -f"
