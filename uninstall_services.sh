#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="mjpeg-server"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"

# If you want to also delete local project artifacts (venv/config/images), set to "1"
PURGE_LOCAL_ARTIFACTS="${PURGE_LOCAL_ARTIFACTS:-0}"

log() { echo "[INFO] $*"; }
warn() { echo "[WARN] $*" >&2; }
err() { echo "[ERROR] $*" >&2; }

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || { err "Missing command: $1"; exit 1; }
}

need_sudo() {
  need_cmd sudo
  if ! sudo -v; then
    err "sudo authentication failed."
    exit 1
  fi
}

# ---------------------------
# Preconditions
# ---------------------------
need_cmd systemctl
need_sudo

# Best-effort: stop/disable even if unit is broken or missing
log "Stopping service (best-effort)..."
sudo systemctl stop "${SERVICE_NAME}.service" >/dev/null 2>&1 || true

log "Disabling service (best-effort)..."
sudo systemctl disable "${SERVICE_NAME}.service" >/dev/null 2>&1 || true

log "Resetting failed state (best-effort)..."
sudo systemctl reset-failed "${SERVICE_NAME}.service" >/dev/null 2>&1 || true

# Remove unit file
if [[ -f "${UNIT_PATH}" ]]; then
  log "Removing unit file: ${UNIT_PATH}"
  sudo rm -f "${UNIT_PATH}"
else
  warn "Unit file not found: ${UNIT_PATH}"
fi

log "Reloading systemd..."
sudo systemctl daemon-reload

# ---------------------------
# Optional: purge local artifacts in repo
# ---------------------------
if [[ "${PURGE_LOCAL_ARTIFACTS}" == "1" ]]; then
  # Project dir is assumed to be where this uninstall script resides
  PROJECT_DIR="$(dirname "$(readlink -f "$0")")"
  log "Purging local artifacts under: ${PROJECT_DIR}"

  # These are created by the installer / runtime. Remove only if present.
  rm -rf "${PROJECT_DIR}/.venv" || true
  rm -f  "${PROJECT_DIR}/config.env" || true

  # Be conservative: remove only the default images directory inside repo
  if [[ -d "${PROJECT_DIR}/images" ]]; then
    rm -rf "${PROJECT_DIR}/images" || true
  fi
else
  log "Local artifacts purge disabled. To purge, run: PURGE_LOCAL_ARTIFACTS=1 ./uninstall_service.sh"
fi

log "Uninstall completed."
log "You can verify: systemctl status ${SERVICE_NAME}.service (should show unit not found)."