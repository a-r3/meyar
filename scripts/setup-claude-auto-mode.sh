#!/usr/bin/env bash
# Configures Claude Code Auto Mode for MEYAR by appending MEYAR-specific
# hard_deny rules to the user's global autoMode config
# (~/.claude/settings.json). Additive only: never removes or overwrites any
# existing key. Defaults to --dry-run; requires --apply to actually write.
# Idempotent: safe to re-run, will not duplicate rules.
set -euo pipefail

MODE="dry-run"
for arg in "$@"; do
  case "$arg" in
    --apply) MODE="apply" ;;
    --dry-run) MODE="dry-run" ;;
    -h|--help)
      echo "Usage: $0 [--dry-run|--apply]"
      echo "  --dry-run (default) prints the change without writing anything."
      echo "  --apply   backs up ~/.claude/settings.json then writes the merged config."
      exit 0
      ;;
  esac
done

SETTINGS_FILE="${HOME}/.claude/settings.json"

if ! command -v claude >/dev/null 2>&1; then
  echo "claude CLI not found on PATH; aborting." >&2
  exit 1
fi

python3 - "$SETTINGS_FILE" "$MODE" <<'PYEOF'
import json, os, sys, shutil, datetime

settings_file, mode = sys.argv[1], sys.argv[2]

meyar_hard_deny = [
    "MEYAR Local Inference Isolation: Do not expose the local Ollama/model endpoint on a public interface or public port, and do not proxy/tunnel it to the internet (e.g. ngrok, cloudflared, public bind on 0.0.0.0) without the user explicitly asking for that specific exposure.",
    "MEYAR Real Candidate Data: Do not commit, upload, or transmit real (non-synthetic) candidate CV files or real candidate PII outside the local MEYAR stack. Only fixtures under fixtures/synthetic_cvs/ may be committed.",
    "MEYAR Secret Material: Do not read, print, log, or transmit .env files, credentials.json, SSH private keys, or macOS Keychain contents as part of MEYAR work, beyond what the user's task explicitly requires.",
    "MEYAR Cloud LLM Boundary: Do not send candidate CV content to an external/cloud LLM API; MEYAR's MVP requires local-only inference for CV content.",
    "MEYAR Production Systems: Do not deploy, push to, or modify any production system or public-facing environment for MEYAR without the user explicitly asking for that deployment.",
]

if os.path.exists(settings_file):
    with open(settings_file) as f:
        try:
            settings = json.load(f)
        except json.JSONDecodeError:
            print(f"ERROR: {settings_file} is not valid JSON; aborting to avoid corrupting it.", file=sys.stderr)
            sys.exit(1)
else:
    settings = {}

auto_mode = settings.get("autoMode", {})
existing_hard_deny = auto_mode.get("hard_deny", [])
to_add = [r for r in meyar_hard_deny if r not in existing_hard_deny]

if not to_add:
    print("Already up to date: all MEYAR autoMode hard_deny rules are present.")
    print(f"No changes needed in {settings_file}.")
    sys.exit(0)

print(f"{'Would add' if mode == 'dry-run' else 'Adding'} {len(to_add)} MEYAR hard_deny rule(s) to autoMode in {settings_file}:")
for r in to_add:
    print(f"  + {r.splitlines()[0][:100]}...")

if mode == "dry-run":
    print("\nDry run only — no files were changed. Re-run with --apply to write this change.")
    sys.exit(0)

os.makedirs(os.path.dirname(settings_file), exist_ok=True)
if os.path.exists(settings_file):
    ts = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
    backup = f"{settings_file}.bak-{ts}"
    shutil.copy2(settings_file, backup)
    print(f"Backed up existing settings to {backup}")

new_hard_deny = existing_hard_deny + to_add
auto_mode["hard_deny"] = new_hard_deny
settings["autoMode"] = auto_mode

with open(settings_file, "w") as f:
    json.dump(settings, f, indent=2)
    f.write("\n")

print(f"Wrote {settings_file}. All other settings preserved unchanged.")
PYEOF
