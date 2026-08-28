#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
CONFIG_DIR="${PROJECT_ROOT}/config"
SERVER_IP=""
FORCE=0
DNS_NAMES=()

usage() {
  cat <<'EOF'
Usage:
  bash scripts/setup_hand_web_lan_tls.sh --ip <server-ip> [options]

Options:
  --ip <address>       Server IP used by customer browsers (required).
  --dns <name>         Additional DNS SAN. May be specified more than once.
  --force              Back up and replace existing hand Web TLS files.
  -h, --help           Show this help.

The script creates a private LAN CA, a server certificate with IP/DNS SANs,
and config/hand_web.env. Distribute only config/hand_web_ca.crt to clients.
Never distribute config/hand_web_ca.key or config/key.pem.
EOF
}

while [[ "$#" -gt 0 ]]; do
  case "$1" in
    --ip)
      SERVER_IP="${2:-}"
      shift 2
      ;;
    --dns)
      DNS_NAMES+=("${2:-}")
      shift 2
      ;;
    --force)
      FORCE=1
      shift
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
done

if [[ -z "${SERVER_IP}" ]]; then
  echo "ERROR: --ip is required." >&2
  exit 2
fi

if [[ "$(id -u)" -eq 0 ]]; then
  echo "ERROR: run this script as the service user (normally robot), without sudo." >&2
  exit 1
fi

python3 -c 'import ipaddress, sys; ipaddress.ip_address(sys.argv[1])' "${SERVER_IP}" \
  2>/dev/null || {
    echo "ERROR: invalid IP address: ${SERVER_IP}" >&2
    exit 2
  }

mkdir -p "${CONFIG_DIR}"
CA_CERT="${CONFIG_DIR}/hand_web_ca.crt"
CA_KEY="${CONFIG_DIR}/hand_web_ca.key"
SERVER_CERT="${CONFIG_DIR}/cert.pem"
SERVER_KEY="${CONFIG_DIR}/key.pem"
ENV_FILE="${CONFIG_DIR}/hand_web.env"
SERIAL_FILE="${CONFIG_DIR}/hand_web_ca.srl"
OUTPUTS=("${CA_CERT}" "${CA_KEY}" "${SERVER_CERT}" "${SERVER_KEY}" "${ENV_FILE}")

existing=()
for path in "${OUTPUTS[@]}"; do
  [[ -e "${path}" ]] && existing+=("${path}")
done

if [[ "${#existing[@]}" -gt 0 && "${FORCE}" -ne 1 ]]; then
  echo "ERROR: TLS files already exist. Re-run with --force to rotate them:" >&2
  printf '  %s\n' "${existing[@]}" >&2
  exit 1
fi

if [[ "${#existing[@]}" -gt 0 ]]; then
  backup_dir="${CONFIG_DIR}/tls-backup-$(date +%Y%m%d_%H%M%S)"
  mkdir -p "${backup_dir}"
  for path in "${OUTPUTS[@]}" "${SERIAL_FILE}"; do
    [[ -e "${path}" ]] && cp -a "${path}" "${backup_dir}/"
  done
  echo "Backed up existing TLS files to ${backup_dir}"
fi

rm -f "${CA_CERT}" "${CA_KEY}" "${SERVER_CERT}" "${SERVER_KEY}" "${SERIAL_FILE}"

tmp_dir="$(mktemp -d)"
trap 'rm -rf "${tmp_dir}"' EXIT
extensions="${tmp_dir}/server-extensions.cnf"
csr="${tmp_dir}/hand-web.csr"

{
  echo '[server_cert]'
  echo 'basicConstraints = critical,CA:FALSE'
  echo 'keyUsage = critical,digitalSignature,keyEncipherment'
  echo 'extendedKeyUsage = serverAuth'
  echo 'subjectKeyIdentifier = hash'
  echo 'authorityKeyIdentifier = keyid,issuer'
  echo 'subjectAltName = @alt_names'
  echo
  echo '[alt_names]'
  echo "IP.1 = ${SERVER_IP}"
  dns_index=1
  for dns_name in "${DNS_NAMES[@]}"; do
    if [[ -z "${dns_name}" ]]; then
      echo 'ERROR: --dns cannot be empty.' >&2
      exit 2
    fi
    echo "DNS.${dns_index} = ${dns_name}"
    dns_index=$((dns_index + 1))
  done
} > "${extensions}"

openssl req -x509 -new -nodes -newkey rsa:3072 -sha256 -days 3650 \
  -keyout "${CA_KEY}" \
  -out "${CA_CERT}" \
  -subj '/CN=EAI Teleop Studio LAN CA' \
  -addext 'basicConstraints=critical,CA:TRUE' \
  -addext 'keyUsage=critical,keyCertSign,cRLSign' \
  -addext 'subjectKeyIdentifier=hash'

openssl req -new -nodes -newkey rsa:2048 -sha256 \
  -keyout "${SERVER_KEY}" \
  -out "${csr}" \
  -subj "/CN=${SERVER_IP}"

openssl x509 -req -sha256 -days 825 \
  -in "${csr}" \
  -CA "${CA_CERT}" \
  -CAkey "${CA_KEY}" \
  -CAcreateserial \
  -out "${SERVER_CERT}" \
  -extfile "${extensions}" \
  -extensions server_cert

cat > "${ENV_FILE}" <<EOF
HAND_WEB_HOST=0.0.0.0
HAND_WEB_SCHEME=https
HAND_WEB_CERT=${SERVER_CERT}
HAND_WEB_KEY=${SERVER_KEY}
EOF

chmod 0600 "${CA_KEY}" "${SERVER_KEY}" "${ENV_FILE}"
chmod 0644 "${CA_CERT}" "${SERVER_CERT}"

openssl verify -CAfile "${CA_CERT}" "${SERVER_CERT}"
openssl x509 -in "${SERVER_CERT}" -noout -subject -dates -ext subjectAltName

cat <<EOF

LAN HTTPS files are ready.

1. Install config/hand_web_ca.crt on each customer computer.
2. Reinstall/restart hand-web.service:
     sudo bash scripts/install_autostart_services.sh hand-web.service
3. Open:
     https://${SERVER_IP}:18089/

The camera service also uses config/cert.pem and config/key.pem. Restart
teleimager-camera-capture.service if it should present the rotated certificate.
EOF
