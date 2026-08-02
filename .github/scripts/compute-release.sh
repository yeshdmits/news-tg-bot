#!/usr/bin/env bash
# Decide whether HEAD warrants a release, and which version it gets.
#
# Reads the Conventional Commits since the last v* tag (commitizen does the
# parsing; its config lives in pyproject.toml under [tool.commitizen]) and
# writes two outputs to $GITHUB_OUTPUT (stdout when run locally):
#
#   released=true|false
#   version=X.Y.Z        (when released=false: the current version, as a
#                         placeholder so downstream expressions stay valid)
#
# Rules:
#   fix: -> patch, feat: -> minor, breaking (! or BREAKING CHANGE footer)
#   -> major — except while the version is 0.x, where breaking bumps minor
#   (--major-version-zero below). Reaching 1.0.0 is a deliberate act: use a
#   "Release-As: 1.0.0" footer. All other types release nothing.
#   Non-conventional commits release nothing and produce a warning.
#
# Requires: git history with tags (checkout with fetch-depth: 0), cz on PATH.
set -euo pipefail

OUT="${GITHUB_OUTPUT:-/dev/stdout}"

last_tag=$(git describe --tags --abbrev=0 --match 'v[0-9]*' 2>/dev/null || true)
range="${last_tag:+$last_tag..}HEAD"

# Non-conventional commits never trigger a release — say so instead of
# silently ignoring them.
for sha in $(git rev-list "$range"); do
  if ! cz check --message "$(git log -1 --format=%B "$sha")" >/dev/null 2>&1; then
    echo "::warning::commit ${sha:0:7} is not a Conventional Commit and does not count towards a release: $(git log -1 --format=%s "$sha")"
  fi
done

# A "Release-As: X.Y.Z" footer anywhere since the last tag forces that exact
# version; the newest occurrence wins.
release_as=$(git log "$range" --format=%B \
  | sed -n -E 's/^[Rr]elease-[Aa]s:[[:space:]]*v?([0-9]+\.[0-9]+\.[0-9]+)[[:space:]]*$/\1/p' \
  | head -n 1 || true)

version=""
if [ -n "$release_as" ]; then
  version="$release_as"
  echo "Release-As footer found: forcing version $version"
else
  flags=(--yes --get-next)
  # While the version is 0.x (or nothing is tagged yet), a breaking change
  # bumps minor, not major. Once a v1+ tag exists the flag drops away and
  # breaking changes bump major.
  case "${last_tag:-v0}" in
    v0.* | v0) flags+=(--major-version-zero) ;;
  esac
  set +e
  next=$(cz bump "${flags[@]}" 2>&1)
  rc=$?
  set -e
  if [ "$rc" -eq 0 ]; then
    version="$next"
  elif [ "$rc" -eq 3 ] || [ "$rc" -eq 21 ]; then
    # 3 = no commits since the tag, 21 = commits, but none release-worthy.
    echo "No release: ${next}"
  else
    echo "::error::cz bump failed (exit $rc): $next"
    exit "$rc"
  fi
fi

# Never overwrite an existing version tag: a re-run on an already-released
# commit (or a Release-As pointing at a used version) skips cleanly.
if [ -n "$version" ] && git rev-parse -q --verify "refs/tags/v$version" >/dev/null; then
  echo "::warning::tag v$version already exists — not releasing again"
  version=""
fi

if [ -n "$version" ]; then
  echo "Next version: $version (previous: ${last_tag:-none})"
  {
    echo "released=true"
    echo "version=$version"
  } >> "$OUT"
else
  current="${last_tag#v}"
  {
    echo "released=false"
    echo "version=${current:-0.0.0}"
  } >> "$OUT"
fi
