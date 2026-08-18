#!/usr/bin/env bash
# PreToolUse guard for MEYAR: blocks secret exposure, destructive git ops,
# and accidental real-CV/customer-data commits. Reads the standard Claude
# Code hook JSON payload on stdin. Exit 2 blocks the tool call.
set -euo pipefail

payload="$(cat)"
tool_name="$(printf '%s' "$payload" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("tool_name",""))' 2>/dev/null || echo "")"

get_field() {
  printf '%s' "$payload" | python3 -c "
import json,sys
d=json.load(sys.stdin)
ti=d.get('tool_input',{}) or {}
print(ti.get('$1',''))
" 2>/dev/null || echo ""
}

block() {
  echo "BLOCKED by MEYAR guard hook: $1" >&2
  exit 2
}

case "$tool_name" in
  Bash)
    cmd="$(get_field command)"
    if printf '%s' "$cmd" | grep -Eiq '(^|[^A-Za-z0-9_-])git\s+push\s+.*--force|git\s+push\s+-f(\s|$)'; then
      block "force-push is not allowed automatically; ask the user to run it manually."
    fi
    if printf '%s' "$cmd" | grep -Eiq 'rm\s+-rf\s+(/($|\s)|~($|\s)|\.\.($|/))'; then
      block "destructive rm -rf on a root/home/parent path."
    fi
    if printf '%s' "$cmd" | grep -Eiq 'git\s+(reset\s+--hard|clean\s+-f|checkout\s+\.|branch\s+-D)'; then
      block "destructive git operation requires explicit user instruction; confirm with the user first."
    fi
    if printf '%s' "$cmd" | grep -Eiq '\bsecurity\s+find-generic-password|\bsecurity\s+dump-keychain|keychain'; then
      block "macOS Keychain access is not allowed from automated commands."
    fi
    if printf '%s' "$cmd" | grep -Eiq '\b(vercel|scp|rsync).*(deploy|--prod)|ssh\s+.*(deploy|production)'; then
      block "remote deploy/upload commands require explicit user confirmation."
    fi
    ;;
  Write|Edit)
    fp="$(get_field file_path)"
    if printf '%s' "$fp" | grep -Eiq '(^|/)\.env(\..*)?$|(^|/)\.ssh(/|$)|id_rsa|id_ed25519|credentials\.json|\.pem$'; then
      block "writing to a secret/credential path ($fp) is not allowed automatically."
    fi
    if printf '%s' "$fp" | grep -Eiq '(^|/)(real_cvs?|customer_data|prod_data)(/|$)'; then
      block "writing under a real-candidate-data path ($fp) is not allowed — use fixtures/synthetic_cvs/."
    fi
    ;;
esac

exit 0
