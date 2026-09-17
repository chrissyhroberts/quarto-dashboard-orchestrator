#!/usr/bin/env bash
# Quarto Dashboard Delivery System 1.0.0
# Copyright (c) 2026 Chrissy h. Roberts — MIT License
set -uo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME_ENV="$BASE_DIR/config/runtime.env"
[[ -f "$RUNTIME_ENV" ]] && source "$RUNTIME_ENV"
DASHBOARDS_DIR="${DASHBOARDS_DIR:-${PROJECTS_DIR:-$BASE_DIR/dashboards}}"
SECRETS_FILE="${SECRETS_FILE:-$BASE_DIR/config/secrets.env}"
RCLONE_CONFIG="${RCLONE_CONFIG:-$BASE_DIR/config/rclone.conf}"
PYTHON="$BASE_DIR/.venv/bin/python"
CONTROLLER="$BASE_DIR/dashboard_controller.py"
LOG_DIR="$BASE_DIR/logs"

mkdir -p "$LOG_DIR"
umask 077

STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
WRAPPER_LOG="$LOG_DIR/wrapper_${STAMP}.log"

exec > >(tee -a "$WRAPPER_LOG") 2>&1

echo "=== Quarto Dashboard Delivery System 1.0.0 ==="
echo "Started: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "Dashboards: $DASHBOARDS_DIR"

finish() {
  rc=$?
  echo "Finished: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "Exit code: $rc"
}
trap finish EXIT

if [[ ! -x "$PYTHON" ]]; then
  echo "ERROR: .venv is missing. Run $BASE_DIR/setup.sh first." >&2
  exit 2
fi
if [[ ! -f "$CONTROLLER" ]]; then
  echo "ERROR: dashboard_controller.py is missing." >&2
  exit 2
fi

export RCLONE_CONFIG

args=( "$PYTHON" "$CONTROLLER"
       --projects-root "$DASHBOARDS_DIR"
       --logs-dir "$LOG_DIR" )

if [[ -f "$SECRETS_FILE" ]]; then
  args+=( --secrets "$SECRETS_FILE" )
fi

# No shell activation is needed: using .venv/bin/python directly is more reliable
# under cron and has exactly the same dependency isolation.
"${args[@]}"
