#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
UNITREE_SDK_ROOT="${UNITREE_SDK_ROOT:-${HOME}/unitree_sdk2}"
DFX_ROOT="${DFX_ROOT:-${HOME}/DFX_inspire_service}"
PATCH_FILE="${PROJECT_ROOT}/patches/inspire_dfx_ubuntu22_fmt.patch"
INSTALL_DEPS=1
FORCE_SDK_BUILD=0
MASK_BRLTTY=1

run_native() {
  env \
    -u CONDA_PREFIX \
    -u CONDA_DEFAULT_ENV \
    -u CMAKE_PREFIX_PATH \
    -u CMAKE_INCLUDE_PATH \
    -u CMAKE_LIBRARY_PATH \
    -u LD_LIBRARY_PATH \
    -u PKG_CONFIG_PATH \
    PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    "$@"
}

usage() {
  cat <<EOF
Usage: bash scripts/setup_inspire_dfx.sh [options]

Prepare and build Inspire DFX service on Ubuntu 22.04.

Options:
  --skip-deps        Do not run apt-get.
  --force-sdk-build  Rebuild and reinstall the C++ Unitree SDK.
  --keep-brltty      Keep Ubuntu Braille services enabled (may claim CH340).
  -h, --help         Show this help.

Environment overrides:
  UNITREE_SDK_ROOT   Default: ${HOME}/unitree_sdk2
  DFX_ROOT           Default: ${HOME}/DFX_inspire_service
EOF
}

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --skip-deps)
      INSTALL_DEPS=0
      ;;
    --force-sdk-build)
      FORCE_SDK_BUILD=1
      ;;
    --keep-brltty)
      MASK_BRLTTY=0
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

if [[ "$(id -u)" -eq 0 ]]; then
  SUDO=()
else
  command -v sudo >/dev/null || {
    echo "ERROR: sudo is required to install packages and libraries." >&2
    exit 1
  }
  SUDO=(sudo)
fi

if [[ "${MASK_BRLTTY}" -eq 1 ]] \
  && command -v systemctl >/dev/null \
  && systemctl list-unit-files brltty.service brltty-udev.service >/dev/null 2>&1; then
  echo "Masking brltty services so CH340 remains bound to the ch341 driver."
  "${SUDO[@]}" systemctl mask --now brltty.service brltty-udev.service
fi

if [[ "${INSTALL_DEPS}" -eq 1 ]]; then
  "${SUDO[@]}" apt-get update
  "${SUDO[@]}" apt-get install -y \
    build-essential cmake git \
    libboost-all-dev libeigen3-dev libfmt-dev libspdlog-dev libyaml-cpp-dev
fi

if [[ ! -f "${PATCH_FILE}" ]]; then
  echo "ERROR: patch file not found: ${PATCH_FILE}" >&2
  exit 1
fi

unitree_installed=0
UNITREE_SYSTEM_LIBRARY="$(find /usr/local/lib /usr/lib -maxdepth 4 -type f \
  \( -name 'libunitree_sdk2.a' -o -name 'libunitree_sdk2.so' \) -print -quit 2>/dev/null || true)"
if [[ -n "${UNITREE_SYSTEM_LIBRARY}" ]]; then
  unitree_installed=1
fi

if [[ "${FORCE_SDK_BUILD}" -eq 1 || "${unitree_installed}" -eq 0 ]]; then
  if [[ ! -f "${UNITREE_SDK_ROOT}/CMakeLists.txt" ]]; then
    if [[ -e "${UNITREE_SDK_ROOT}" ]]; then
      echo "ERROR: ${UNITREE_SDK_ROOT} exists but is not a Unitree SDK source tree." >&2
      exit 1
    fi
    git clone https://github.com/unitreerobotics/unitree_sdk2.git "${UNITREE_SDK_ROOT}"
  fi
  run_native /usr/bin/cmake \
    -S "${UNITREE_SDK_ROOT}" \
    -B "${UNITREE_SDK_ROOT}/build-system" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_C_COMPILER=/usr/bin/gcc \
    -DCMAKE_CXX_COMPILER=/usr/bin/g++ \
    -DBoost_NO_BOOST_CMAKE=ON \
    -DBOOST_ROOT=/usr
  run_native /usr/bin/cmake --build "${UNITREE_SDK_ROOT}/build-system" --parallel
  "${SUDO[@]}" /usr/bin/cmake --install "${UNITREE_SDK_ROOT}/build-system"
  "${SUDO[@]}" ldconfig
fi

if [[ ! -f "${DFX_ROOT}/CMakeLists.txt" ]]; then
  if [[ -e "${DFX_ROOT}" ]]; then
    echo "ERROR: ${DFX_ROOT} exists but is not a DFX source tree." >&2
    exit 1
  fi
  git clone https://github.com/unitreerobotics/DFX_inspire_service.git "${DFX_ROOT}"
fi

if ! grep -q 'spdlog::spdlog fmt::fmt' "${DFX_ROOT}/CMakeLists.txt"; then
  if git -C "${DFX_ROOT}" apply --check "${PATCH_FILE}"; then
    git -C "${DFX_ROOT}" apply "${PATCH_FILE}"
  else
    echo "ERROR: DFX fmt patch cannot be applied cleanly." >&2
    echo "Check local changes in ${DFX_ROOT}/CMakeLists.txt." >&2
    exit 1
  fi
fi

MULTIARCH="$(/usr/bin/gcc -print-multiarch)"
FMT_CMAKE_DIR="/usr/lib/${MULTIARCH}/cmake/fmt"
if [[ ! -d "${FMT_CMAKE_DIR}" ]]; then
  echo "ERROR: system fmt CMake package not found: ${FMT_CMAKE_DIR}" >&2
  echo "Install libfmt-dev from the Ubuntu repository." >&2
  exit 1
fi

run_native /usr/bin/cmake \
  '-UBoost_*' \
  -Ufmt_DIR \
  -Uspdlog_DIR \
  -S "${DFX_ROOT}" \
  -B "${DFX_ROOT}/build" \
  -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_COMPILER=/usr/bin/gcc \
  -DCMAKE_CXX_COMPILER=/usr/bin/g++ \
  -DBoost_NO_BOOST_CMAKE=ON \
  -DBOOST_ROOT=/usr \
  -Dfmt_DIR="${FMT_CMAKE_DIR}"

CONFIGURED_FMT_DIR="$(sed -n 's/^fmt_DIR:[^=]*=//p' "${DFX_ROOT}/build/CMakeCache.txt" | tail -n 1)"
if [[ "${CONFIGURED_FMT_DIR}" != "${FMT_CMAKE_DIR}" ]]; then
  echo "ERROR: CMake selected unexpected fmt_DIR: ${CONFIGURED_FMT_DIR}" >&2
  exit 1
fi

run_native /usr/bin/cmake --build "${DFX_ROOT}/build" --target inspire_h1 --parallel

BINARY="${DFX_ROOT}/build/inspire_h1"
if [[ ! -x "${BINARY}" ]]; then
  echo "ERROR: inspire_h1 was not produced: ${BINARY}" >&2
  exit 1
fi
if [[ -n "$(ldd "${BINARY}" | grep -E 'lib(fmt|spdlog)' | grep '/miniconda' || true)" ]]; then
  echo "ERROR: inspire_h1 links against Conda fmt/spdlog libraries." >&2
  exit 1
fi

echo "DFX build completed successfully."
echo "  fmt_DIR: ${CONFIGURED_FMT_DIR}"
echo "  binary:  ${BINARY}"
