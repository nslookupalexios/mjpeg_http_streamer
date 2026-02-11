#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="mjpeg-server"
ENV_BASENAME="config.env"
ENV_BASE_TEMPLATE="config.env.base"
VENV_DIRNAME=".venv"

# ---------------------------
# Helpers
# ---------------------------
log() { echo "[INFO] $*"; }
warn() { echo "[WARN] $*" >&2; }
err() { echo "[ERROR] $*" >&2; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || { err "Missing command: $1"; exit 1; }
}

need_sudo() {
  if ! command -v sudo >/dev/null 2>&1; then
    err "sudo is required but not installed."
    exit 1
  fi
  if ! sudo -v; then
    err "sudo authentication failed."
    exit 1
  fi
}

# Replace or append KEY=VALUE in env file (simple, no quoting logic)
upsert_env_kv() {
  local file="$1"
  local key="$2"
  local value="$3"
  if grep -qE "^${key}=" "$file"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$file"
  else
    echo "${key}=${value}" >> "$file"
  fi
}

# Read KEY= from env file (first match), returns empty if missing
read_env_kv() {
  local file="$1"
  local key="$2"
  local val=""
  val="$(grep -E "^${key}=" "${file}" 2>/dev/null | head -n1 | cut -d= -f2- || true)"
  echo "${val}"
}

# ---------------------------
# Preconditions (must run as regular user)
# ---------------------------
if [[ "$(id -u)" -eq 0 ]]; then
  err "Do NOT run this script as root. Run it as a regular user: ./install_service.sh"
  exit 1
fi

need_cmd readlink
need_cmd dirname
need_cmd id
need_cmd getent
need_cmd python3

PROJECT_DIR="$(dirname "$(readlink -f "$0")")"
INVOKING_USER="$(id -un)"
INVOKING_HOME="$(getent passwd "$INVOKING_USER" | cut -d: -f6)"
if [[ -z "${INVOKING_HOME}" ]]; then
  err "Cannot determine home directory for user: ${INVOKING_USER}"
  exit 1
fi

log "Repository directory: ${PROJECT_DIR}"
log "Invoking user:        ${INVOKING_USER}"
log "User home:            ${INVOKING_HOME}"

# ---------------------------
# 1) System dependencies via apt (root-required)
# ---------------------------
log "Installing system dependencies (apt)..."
need_sudo
sudo apt-get update -y

sudo apt-get install -y \
  python3 \
  python3-venv \
  python3-pip \
  fonts-dejavu-core \
  libjpeg-turbo8 \
  zlib1g \
  libfreetype6

# ---------------------------
# 2) Prepare venv and python deps via pip (user-space)
# ---------------------------
VENV_DIR="${PROJECT_DIR}/${VENV_DIRNAME}"
PY_BIN="$(command -v python3)"

if [[ ! -d "${VENV_DIR}" ]]; then
  log "Creating virtualenv: ${VENV_DIR}"
  "${PY_BIN}" -m venv "${VENV_DIR}"
else
  log "Virtualenv exists: ${VENV_DIR}"
fi

UVICORN_BIN="${VENV_DIR}/bin/uvicorn"
PIP_BIN="${VENV_DIR}/bin/pip"
PY_VENV_BIN="${VENV_DIR}/bin/python"

log "Upgrading pip (venv)..."
"${PIP_BIN}" install --upgrade pip wheel setuptools

log "Installing Python dependencies (venv)..."
"${PIP_BIN}" install \
  fastapi \
  "uvicorn[standard]" \
  pillow \
  watchdog

# ---------------------------
# 3) Generate/merge env from base template (user-space)
# ---------------------------
ENV_FILE="${PROJECT_DIR}/${ENV_BASENAME}"

if [[ ! -f "${PROJECT_DIR}/${ENV_BASE_TEMPLATE}" ]]; then
  err "Missing ${ENV_BASE_TEMPLATE} in ${PROJECT_DIR}. Create it first."
  exit 1
fi

if [[ ! -f "${ENV_FILE}" ]]; then
  log "Creating ${ENV_FILE} from ${ENV_BASE_TEMPLATE}"
  cp "${PROJECT_DIR}/${ENV_BASE_TEMPLATE}" "${ENV_FILE}"
else
  log "${ENV_FILE} already exists; will enrich/update detected fields."
fi

# Enrich with target-specific values
upsert_env_kv "${ENV_FILE}" "INVOKING_USER" "${INVOKING_USER}"
upsert_env_kv "${ENV_FILE}" "MJPEG_HTTP_STREAMER_DIR" "${PROJECT_DIR}"

# Keep vars used by systemd unit
upsert_env_kv "${ENV_FILE}" "PROJECT_DIR" "${PROJECT_DIR}"
upsert_env_kv "${ENV_FILE}" "VENV_DIR" "${VENV_DIR}"
upsert_env_kv "${ENV_FILE}" "PYTHON_BIN" "${PY_VENV_BIN}"
upsert_env_kv "${ENV_FILE}" "UVICORN_BIN" "${UVICORN_BIN}"

# Defaults for frames directories if not set
if grep -qE "^FRAMES_OJBECT_DIR_ABS=$" "${ENV_FILE}"; then
  upsert_env_kv "${ENV_FILE}" "FRAMES_OJBECT_DIR_ABS" "${PROJECT_DIR}/images1"
fi
if grep -qE "^FRAMES_ROCK_DIR_ABS=$" "${ENV_FILE}"; then
  upsert_env_kv "${ENV_FILE}" "FRAMES_ROCK_DIR_ABS" "${PROJECT_DIR}/images2"
fi

# Ensure frames directories exist (user-space)
FRAMES_DIR1="$(read_env_kv "${ENV_FILE}" "FRAMES_OJBECT_DIR_ABS")"
FRAMES_DIR2="$(read_env_kv "${ENV_FILE}" "FRAMES_ROCK_DIR_ABS")"

if [[ -n "${FRAMES_DIR1}" ]]; then
  mkdir -p "${FRAMES_DIR1}" || true
fi
if [[ -n "${FRAMES_DIR2}" ]]; then
  mkdir -p "${FRAMES_DIR2}" || true
fi

# ---------------------------
# 4) Install systemd service using EnvironmentFile (root-required)
# ---------------------------
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

log "Installing systemd unit: ${SERVICE_PATH}"
need_sudo

APP_MODULE="$(read_env_kv "${ENV_FILE}" "APP_MODULE")"
HOST="$(read_env_kv "${ENV_FILE}" "HOST")"
PORT="$(read_env_kv "${ENV_FILE}" "PORT")"

if [[ -z "${APP_MODULE}" ]]; then APP_MODULE="mjpeg_server:app"; fi
if [[ -z "${HOST}" ]]; then HOST="0.0.0.0"; fi
if [[ -z "${PORT}" ]]; then PORT="8000"; fi

sudo tee "${SERVICE_PATH}" >/dev/null <<EOF
[Unit]
Description=MJPEG FastAPI/Uvicorn streamer (env-driven, dual source)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=${ENV_FILE}
User=${INVOKING_USER}
Group=${INVOKING_USER}

WorkingDirectory=${PROJECT_DIR}
ExecStart=${UVICORN_BIN} ${APP_MODULE} --host ${HOST} --port ${PORT}

Restart=on-failure
RestartSec=2
TimeoutStopSec=10

NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF

# ---------------------------
# 5) Enable & start (root-required)
# ---------------------------
log "Reloading systemd..."
sudo systemctl daemon-reload

log "Enabling service..."
sudo systemctl enable "${SERVICE_NAME}.service"

log "Starting service..."
sudo systemctl restart "${SERVICE_NAME}.service"

log "Done. Service status:"
sudo systemctl --no-pager status "${SERVICE_NAME}.service" || true

log "Logs (tail):"
sudo journalctl -u "${SERVICE_NAME}.service" --no-pager -n 50 || true