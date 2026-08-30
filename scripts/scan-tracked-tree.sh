#!/usr/bin/env bash
# MEYAR Slice 13 — reproducible secret/real-data scan over the tracked
# git tree (not the user's machine). Complements .githooks/pre-commit,
# which only inspects the staged diff at commit time; this script can be
# run on demand (e.g. before opening a PR) against everything already
# committed. Pattern-based, not a full secret/PII classifier — matches
# the same categories as the pre-commit guard. Never prints matched
# secret content, only the offending file path and pattern class. No new
# runtime dependency — git + grep only.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

blocked=0

report() {
  echo "FOUND: $1" >&2
  blocked=1
}

is_allowed_env_file() {
  case "$(basename "$1")" in
    .env.example | .env.sample | .env.template) return 0 ;;
    *) return 1 ;;
  esac
}

while IFS= read -r -d '' f; do
  [ -z "$f" ] && continue

  if printf '%s' "$f" | grep -Eiq '(^|/)\.env(\..+)?$'; then
    is_allowed_env_file "$f" || report "secret/env file tracked: $f"
  fi

  if printf '%s' "$f" | grep -Eiq '\.pem$|(^|/)id_rsa$|(^|/)id_ed25519$|(^|/)credentials\.json$|(^|/)\.ssh/'; then
    report "credential/private-key-shaped path tracked: $f"
  fi

  if printf '%s' "$f" | grep -Eiq '(^|/)(real_cvs?|customer_data|prod_data)(/|$)'; then
    report "real-candidate-data path tracked: $f (use fixtures/synthetic_cvs/)"
  fi

  if printf '%s' "$f" | grep -Eiq '(^|/)backend/var/'; then
    report "runtime storage path tracked: $f"
  fi

  if printf '%s' "$f" | grep -Eiq '(^|/)[^/]*dump[^/]*\.(sql(\.gz)?|dump)$|\.sqlite3?$'; then
    report "database dump/file tracked: $f"
  fi

  if printf '%s' "$f" | grep -Eiq '\.(gguf|safetensors|ggml)$'; then
    report "model weight artifact tracked: $f"
  fi
  if printf '%s' "$f" | grep -Eiq '(^|/)models?/.*\.(bin|pt|onnx)$'; then
    report "model weight artifact tracked: $f"
  fi
done < <(git ls-files -z)

# Content scan over the tracked tree (excludes binary/fixture PDFs+DOCX,
# which are intentionally synthetic and reviewed separately).
while IFS= read -r -d '' f; do
  [ -z "$f" ] && continue
  case "$f" in
    *.pdf | *.docx | *.png) continue ;;
  esac
  if grep -Eaq -- \
    '-----BEGIN (PRIVATE KEY|ENCRYPTED PRIVATE KEY|RSA PRIVATE KEY|EC PRIVATE KEY|DSA PRIVATE KEY|OPENSSH PRIVATE KEY)-----|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{36}|sk-[A-Za-z0-9]{20,}' \
    "$f" 2>/dev/null; then
    report "private-key or access-key-shaped content tracked: $f (secret material)"
  fi
done < <(git ls-files -z)

if [ "$blocked" = "1" ]; then
  echo "" >&2
  echo "Tracked-tree secret/real-data scan FAILED — see FOUND lines above." >&2
  exit 1
fi

echo "Tracked-tree secret/real-data scan: clean."
exit 0
