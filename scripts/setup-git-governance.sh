#!/usr/bin/env bash
# Activates MEYAR's repo-local Git hooks (.githooks/) for this clone only.
# Idempotent, safe to re-run. Does not touch global Git config.
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
cd "$repo_root"

chmod +x .githooks/pre-commit .githooks/pre-push
git config core.hooksPath .githooks

echo "MEYAR git governance hooks active for $repo_root"
echo "core.hooksPath = $(git config --get core.hooksPath)"
