#!/usr/bin/env bash
# Neutrality guard: fail if the tracked tree re-acquires operator-specific
# values. Runs in CI on every push (see .github/workflows/pipeline.yml) and
# locally via ./scripts/check-neutral.sh. Patterns live in
# scripts/neutral-patterns.txt; the rules below are structural.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

fail=0

say() { printf '%s\n' "$*" >&2; }

# --- 1. Operator-identifier patterns ---------------------------------------
# LICENSE is exempt: the copyright notice legitimately names the author.
patterns=$(grep -vE '^\s*(#|$)' scripts/neutral-patterns.txt)
while IFS= read -r pattern; do
  hits=$(git grep -nIE -- "$pattern" -- ':(exclude)LICENSE' || true)
  if [ -n "$hits" ]; then
    say "NEUTRALITY: pattern '$pattern' found in tracked tree:"
    say "$hits"
    fail=1
  fi
done <<< "$patterns"

# --- 2. Real-looking Telegram ids outside the designated fake block --------
# Every example/test chat id must contain the token 9999000 (see
# tests/conftest.py). Anything shaped like a real chat id (a quoted or bare
# negative run of 9+ digits) without that token fails.
id_hits=$(git grep -nIoE -- '-[0-9]{9,}' \
  | grep -v '9999000' \
  || true)
if [ -n "$id_hits" ]; then
  say "NEUTRALITY: real-looking Telegram ids (negative, 9+ digits, missing the 9999000 fake-block token):"
  say "$id_hits"
  fail=1
fi

# --- 3. Files that must never be tracked ------------------------------------
forbidden=$(git ls-files -- \
  'spec.json' \
  '.env' \
  '**/.env' \
  'deploy/terraform/**/backend.hcl' \
  'deploy/terraform/**/spec.local.json' \
  | grep -v '\.example' || true)
# *.tfvars anywhere, except *.tfvars.example
tfvars=$(git ls-files -- '*.tfvars' || true)
forbidden="$forbidden$tfvars"
if [ -n "${forbidden//[$'\n ']/}" ]; then
  say "NEUTRALITY: live configuration files are tracked (only .example variants may be):"
  say "$forbidden"
  fail=1
fi

if [ "$fail" -ne 0 ]; then
  say ""
  say "See NEUTRALISATION.md for what belongs where (env var, repository"
  say "variable/secret, tfvars, Key Vault) instead of the tracked tree."
  exit 1
fi

echo "neutrality check passed"
