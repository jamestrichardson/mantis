#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run this installer as root (for example: sudo bash deploy/degobah/install.sh)" >&2
  exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_ROOT="${MANTIS_DEPLOY_ROOT:-/opt/mantis}"
BIN_PATH="${MANTIS_DEPLOY_BIN:-/usr/local/bin/mantis-deploy}"

install -d -m 0755 "${DEPLOY_ROOT}" "${DEPLOY_ROOT}/state"
install -m 0644 "${SCRIPT_DIR}/compose.yaml" "${DEPLOY_ROOT}/compose.yaml"
install -m 0755 "${SCRIPT_DIR}/mantis-deploy" "${BIN_PATH}"

if [[ ! -e "${DEPLOY_ROOT}/deploy.env" ]]; then
  install -m 0644 "${SCRIPT_DIR}/deploy.env.example" "${DEPLOY_ROOT}/deploy.env"
  echo "Created ${DEPLOY_ROOT}/deploy.env"
else
  echo "Preserved existing ${DEPLOY_ROOT}/deploy.env"
fi

if [[ ! -e "${DEPLOY_ROOT}/runtime.env" ]]; then
  install -m 0600 "${SCRIPT_DIR}/runtime.env.example" "${DEPLOY_ROOT}/runtime.env"
  echo "Created ${DEPLOY_ROOT}/runtime.env (edit secrets before running Mantis)"
else
  chmod 0600 "${DEPLOY_ROOT}/runtime.env"
  echo "Preserved existing ${DEPLOY_ROOT}/runtime.env"
fi

cat <<EOF
Installed degobah deployment files:
  ${DEPLOY_ROOT}/compose.yaml
  ${DEPLOY_ROOT}/deploy.env
  ${DEPLOY_ROOT}/runtime.env
  ${DEPLOY_ROOT}/state/
  ${BIN_PATH}

Next:
  1. Edit ${DEPLOY_ROOT}/runtime.env.
  2. Authenticate Docker to ghcr.io if the package requires it.
  3. Deploy an explicit tag, for example: ${BIN_PATH} pr-59
EOF
