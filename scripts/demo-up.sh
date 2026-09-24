#!/usr/bin/env bash
# MEYAR local presentation demo — one-command bootstrap.
#
# LOCAL PRESENTATION CONVENIENCE ONLY. Not a production deployment script,
# not a Slice/issue-#35 deployment artifact. Wraps the already-accepted
# manual local-demo flow documented in docs/LOCAL_DEMO.md: Docker Compose
# PostgreSQL, `uv sync --locked`, Alembic migrations, `meyar seed-demo`,
# and a loopback-only Uvicorn server. See docs/LOCAL_DEMO.md for the full
# manual fallback and credential-handling notes.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"

BACKEND_DIR="$REPO_ROOT/backend"
HOST="127.0.0.1"
PORT="8000"
HEALTH_URL="http://$HOST:$PORT/api/v1/health"
LOGIN_URL="http://$HOST:$PORT/ui/login"

SKIP_SYNC=false
for arg in "$@"; do
  case "$arg" in
    --skip-sync)
      SKIP_SYNC=true
      ;;
    *)
      echo "Unknown argument: $arg" >&2
      echo "Usage: $0 [--skip-sync]" >&2
      exit 1
      ;;
  esac
done

log() { printf '%s\n' "$*"; }
fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

UVICORN_PID=""
CLEANUP_DONE=false
cleanup() {
  if [[ "$CLEANUP_DONE" == true ]]; then
    return
  fi
  CLEANUP_DONE=true
  if [[ -n "$UVICORN_PID" ]] && kill -0 "$UVICORN_PID" 2>/dev/null; then
    log ""
    log "Stopping MEYAR server (pid $UVICORN_PID)..."
    kill -TERM "$UVICORN_PID" 2>/dev/null || true
    wait "$UVICORN_PID" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

### 1. Prerequisite checks ###################################################

for cmd in git docker uv curl; do
  command -v "$cmd" >/dev/null 2>&1 \
    || fail "Required command not found on PATH: $cmd. Install it and re-run."
done

docker info >/dev/null 2>&1 \
  || fail "Docker daemon not reachable ('docker info' failed). Start Docker and re-run."

docker compose version >/dev/null 2>&1 \
  || fail "Docker Compose plugin not available ('docker compose version' failed)."

### 2. backend/.env handling ##################################################

ENV_FILE="$BACKEND_DIR/.env"
ENV_EXAMPLE="$BACKEND_DIR/.env.example"

if [[ -f "$ENV_FILE" ]]; then
  log "backend/.env already exists — leaving it untouched."
else
  [[ -f "$ENV_EXAMPLE" ]] || fail "Missing template: backend/.env.example"
  cp "$ENV_EXAMPLE" "$ENV_FILE"
  log "Created backend/.env from backend/.env.example (local non-production defaults)."
fi

### 3. Dependencies ###########################################################

if [[ "$SKIP_SYNC" == true ]]; then
  log "Skipping dependency sync (--skip-sync)."
else
  log "Syncing backend dependencies (uv sync --locked)..."
  (cd "$BACKEND_DIR" && uv sync --locked) || fail "uv sync --locked failed. Not continuing."
fi

### 4. PostgreSQL #############################################################

log "Starting PostgreSQL (docker compose up -d postgres)..."
docker compose up -d postgres || fail "docker compose up -d postgres failed."

log "Waiting for postgres to become healthy..."
# Overridable only for the test harness (scripts/tests) — real usage always
# gets the 60s default.
POSTGRES_TIMEOUT_SECONDS="${MEYAR_DEMO_POSTGRES_TIMEOUT_SECONDS:-60}"
elapsed=0
pg_health=""
while (( elapsed < POSTGRES_TIMEOUT_SECONDS )); do
  pg_health="$(docker compose ps --format '{{.Health}}' postgres 2>/dev/null || true)"
  if [[ "$pg_health" == "healthy" ]]; then
    break
  fi
  sleep 2
  elapsed=$((elapsed + 2))
done

if [[ "$pg_health" != "healthy" ]]; then
  docker compose ps
  fail "postgres did not become healthy within ${POSTGRES_TIMEOUT_SECONDS}s. Not migrating, seeding, or starting the server."
fi
log "postgres is healthy."

### 5. Database migrations ####################################################

log "Applying database migrations (alembic upgrade head)..."
(cd "$BACKEND_DIR" && uv run alembic upgrade head) \
  || fail "alembic upgrade head failed. Not seeding or starting the server."

heads_output="$(cd "$BACKEND_DIR" && uv run alembic heads)"
head_count="$(printf '%s\n' "$heads_output" | grep -c '(head)' || true)"
if [[ "$head_count" -ne 1 ]]; then
  printf '%s\n' "$heads_output" >&2
  fail "Expected exactly one Alembic head, found $head_count. Not seeding or starting the server."
fi
log "Alembic: exactly one head confirmed."

### 6. Demo credential seeding (authoritative: meyar seed-demo) ##############

log "Seeding synthetic demo dataset and rotating demo credentials (meyar seed-demo)..."
seed_status=0
seed_output="$(cd "$BACKEND_DIR" && uv run meyar seed-demo 2>&1)" || seed_status=$?

if [[ "$seed_status" -ne 0 ]]; then
  printf '%s\n' "$seed_output" >&2
  fail "meyar seed-demo failed (exit $seed_status). Not starting the server."
fi

# Best-effort, non-fragile extraction of just the human/UI credential for a
# concise final banner. The exact authoritative block always exists in
# seed_output (captured above); if extraction fails for any reason, fall
# back to printing that captured block verbatim rather than guessing.
human_username="$(printf '%s\n' "$seed_output" | sed -n 's/^Username: //p' | head -n1)"
human_password="$(printf '%s\n' "$seed_output" | awk '
  got_label { print; got_label=0; found=1 }
  /^Temporary password \(shown once, never persisted in plaintext\):$/ { got_label=1 }
  END { exit(found ? 0 : 1) }
')" || human_password=""

credentials_extracted=false
if [[ -n "$human_username" && -n "$human_password" ]]; then
  credentials_extracted=true
fi

### 7. Ollama status (optional, best-effort, read-only) #######################

ollama_status="NOT AVAILABLE"
if command -v ollama >/dev/null 2>&1 && ollama list >/dev/null 2>&1; then
  ollama_status="AVAILABLE"
fi

if [[ "$ollama_status" == "NOT AVAILABLE" ]]; then
  log ""
  log "Ollama unavailable."
  log "Login, candidate library, candidate detail, jobs, structured search,"
  log "pre-seeded deterministic evaluations/ranking still work."
  log "Natural-language / semantic / hybrid AI actions require local Ollama."
fi

### 8. Server start ############################################################

already_running=false

port_open() {
  (exec 3<>"/dev/tcp/$HOST/$PORT") 2>/dev/null
}

if port_open; then
  if curl -sf --max-time 3 "$HEALTH_URL" >/dev/null 2>&1; then
    log ""
    log "MEYAR is already running and healthy at http://$HOST:$PORT — reusing it (not starting a duplicate)."
    already_running=true
  else
    fail "Port $PORT is already in use by another process that is not MEYAR's health endpoint ($HEALTH_URL). Refusing to start a duplicate server or stop that process. Free the port and re-run."
  fi
fi

if [[ "$already_running" == false ]]; then
  log ""
  log "Starting MEYAR (uvicorn) on http://$HOST:$PORT ..."
  (cd "$BACKEND_DIR" && exec uv run uvicorn meyar.main:app --host "$HOST" --port "$PORT") &
  UVICORN_PID=$!

  # Overridable only for the test harness (scripts/tests) — real usage
  # always gets the 30s default.
  UVICORN_READY_TIMEOUT_SECONDS="${MEYAR_DEMO_UVICORN_TIMEOUT_SECONDS:-30}"
  ready=false
  for _ in $(seq 1 "$UVICORN_READY_TIMEOUT_SECONDS"); do
    if ! kill -0 "$UVICORN_PID" 2>/dev/null; then
      fail "MEYAR server process exited before becoming healthy. See output above."
    fi
    if curl -sf --max-time 3 "$HEALTH_URL" >/dev/null 2>&1; then
      ready=true
      break
    fi
    sleep 1
  done

  if [[ "$ready" != true ]]; then
    fail "MEYAR did not become healthy at $HEALTH_URL within ${UVICORN_READY_TIMEOUT_SECONDS}s."
  fi
fi

### 9. Ready banner #############################################################

git_sha="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"

log ""
log "MEYAR LOCAL DEMO READY"
log ""
log "Commit:"
log "  $git_sha"
log ""
log "Login:"
log "  $LOGIN_URL"
log ""
if [[ "$credentials_extracted" == true ]]; then
  log "Username:"
  log "  $human_username"
  log ""
  log "Temporary password (shown once):"
  log "  $human_password"
else
  log "--- seed-demo credential output (could not be summarized safely) ---"
  printf '%s\n' "$seed_output"
  log "----------------------------------------------------------------------"
  log "Username:"
  log "  demo.hr"
  log "Login:"
  log "  $LOGIN_URL"
fi
log ""
log "Candidate Library:"
log "  http://$HOST:$PORT/ui/library"
log ""
log "MEYAR AI:"
log "  http://$HOST:$PORT/ui/agent"
log ""
log "Jobs:"
log "  http://$HOST:$PORT/ui/jobs"
log ""
log "Swagger:"
log "  http://$HOST:$PORT/docs"
log ""
log "Ollama:"
log "  $ollama_status"
log ""
log "Stop:"
log "  Ctrl+C"
log ""

if [[ "$already_running" == false ]]; then
  wait "$UVICORN_PID"
fi
