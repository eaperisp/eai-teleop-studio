#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
PROJECT_NAME="$(basename "${PROJECT_ROOT}")"
STAMP="$(date +%Y%m%d_%H%M%S)"
ARCHIVE="/tmp/${PROJECT_NAME}_${STAMP}.tar.gz"

usage() {
  cat <<EOF
Usage:
  bash scripts/deploy_project.sh
  bash scripts/deploy_project.sh robot@host:/data02/app

The script packages the current project without runtime data, logs, caches, or
git metadata. If a remote destination is provided, the archive is copied there.
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

cd "${PROJECT_ROOT}/.."
tar \
  --exclude="${PROJECT_NAME}/.git" \
  --exclude="${PROJECT_NAME}/data" \
  --exclude="${PROJECT_NAME}/logs" \
  --exclude="${PROJECT_NAME}/models" \
  --exclude="${PROJECT_NAME}/__pycache__" \
  --exclude="${PROJECT_NAME}/.pytest_cache" \
  --exclude="${PROJECT_NAME}/.mypy_cache" \
  --exclude="${PROJECT_NAME}/.ruff_cache" \
  --exclude="${PROJECT_NAME}/node_modules" \
  --exclude="${PROJECT_NAME}/hand_web/node_modules" \
  --exclude="${PROJECT_NAME}/teleop_web/static/__pycache__" \
  -czf "${ARCHIVE}" \
  "${PROJECT_NAME}"

echo "Created ${ARCHIVE}"

if [[ "$#" -gt 0 ]]; then
  scp "${ARCHIVE}" "$1/"
  remote_dir="${1##*:}"
  echo "Copied archive to $1"
  cat <<EOF

On the target host:
  cd ${remote_dir}
  tar -xzf $(basename "${ARCHIVE}")
  cd ${PROJECT_NAME}
  sudo bash scripts/install_autostart_services.sh
EOF
fi
