#!/usr/bin/env bash
# MEYAR local presentation demo — conservative cleanup.
#
# LOCAL PRESENTATION CONVENIENCE ONLY. Deliberately narrow scope: stops the
# checked-in PostgreSQL container only. It never deletes Docker volumes,
# resets the database, deletes the demo tenant, deletes backend/.env, or
# kills processes by name/pattern — demo-up.sh owns its foreground Uvicorn
# child directly (Ctrl+C stops exactly that process); this script cannot
# safely identify or kill an unrelated process on port 8000, so it doesn't
# try. See docs/LOCAL_DEMO.md.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

HOST="127.0.0.1"
PORT="8000"
HEALTH_URL="http://$HOST:$PORT/api/v1/health"

log() { printf '%s\n' "$*"; }

command -v docker >/dev/null 2>&1 || {
  printf 'ERROR: docker not found on PATH.\n' >&2
  exit 1
}

log "Stopping PostgreSQL (docker compose stop postgres)..."
docker compose stop postgres

port_open() {
  (exec 3<>"/dev/tcp/$HOST/$PORT") 2>/dev/null
}

if command -v curl >/dev/null 2>&1 && port_open; then
  if curl -sf --max-time 3 "$HEALTH_URL" >/dev/null 2>&1; then
    log ""
    log "MEYAR process may still be running; stop the terminal/session that"
    log "launched demo-up.sh."
  fi
fi

log ""
log "Done. Data and the meyar_pg_data volume were left intact."
