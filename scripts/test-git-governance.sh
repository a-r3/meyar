#!/usr/bin/env bash
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
hook="$repo_root/.githooks/pre-commit"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
git -C "$tmp" init -q
git -C "$tmp" config user.email test@example.invalid
git -C "$tmp" config user.name governance-test
git -C "$tmp" config core.hooksPath "$repo_root/.githooks"
printf 'seed\n' > "$tmp/seed.txt"
git -C "$tmp" add seed.txt && git -C "$tmp" commit -qm seed

expect_pass() { git -C "$tmp" reset -q; rm -rf "$tmp/work"; (cd "$tmp" && eval "$1"); git -C "$tmp" add -A -f; (cd "$tmp" && "$hook"); }
expect_block() { git -C "$tmp" reset -q; rm -rf "$tmp/work"; (cd "$tmp" && eval "$1"); git -C "$tmp" add -A -f; if (cd "$tmp" && "$hook") >/dev/null 2>&1; then echo "expected BLOCKED: $2" >&2; exit 1; fi; }

expect_pass "printf 'x\n' > source.py"; expect_pass "printf '# docs\n' > README.md"
if ! printf 'refs/heads/chore/git-governance abc refs/heads/chore/git-governance def\n' | (cd "$tmp" && "$repo_root/.githooks/pre-push"); then
  echo 'expected task-branch push to pass' >&2
  exit 1
fi
expect_pass "printf 'SAFE=1\n' > .env.example"
mkdir -p "$tmp/fixtures/synthetic_cvs"; printf 'synthetic CV\n' > "$tmp/fixtures/synthetic_cvs/example.txt"; git -C "$tmp" add -A -f; (cd "$tmp" && "$hook")
expect_block "printf 'x\n' > .env" direct-env
git -C "$tmp" reset -q; rm -f "$tmp/.env"; printf 'SAFE=1\n' > "$tmp/.env.example"; git -C "$tmp" add -f .env.example; git -C "$tmp" commit -qm template; git -C "$tmp" mv .env.example .env; expect_block ":" rename-env
expect_block "mkdir -p backups; printf 'dump\n' > backups/prod.dump" database-dump
expect_block "printf 'x\n' > model.gguf" model-artifact
expect_block "printf '%s\\n' \"-----BEGIN \"\"PRIVATE KEY-----\" > key.txt" pkcs8
expect_block "printf '%s\\n' \"-----BEGIN \"\"RSA PRIVATE KEY-----\" > key.txt" rsa
expect_block "printf '%s\\n' \"-----BEGIN \"\"OPENSSH PRIVATE KEY-----\" > key.txt" openssh
echo 'governance hook regression simulations: PASS'
