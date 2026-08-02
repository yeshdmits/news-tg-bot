# Contributing

## Development setup

Python 3.12+ and Docker (for the Postgres-backed tests) are the only
requirements.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev,translate]'
```

`ruff` is the linter/formatter (line length 100, configured in
`pyproject.toml`).

## Running tests

The suite has two tiers, split by the `pg` marker:

```bash
# Tier 1 — pure unit tests. No services, no network, sub-second.
pytest -m "not pg"

# Tier 2 — everything, against a throwaway Postgres.
docker compose up -d db
pytest
```

Notes:

- Any test that requests the `db` fixture is auto-marked `pg`
  (`tests/conftest.py`). The fixture creates `newsbot_test` if missing, runs
  `alembic upgrade head` once per session, and truncates all tables between
  tests.
- If Postgres is unreachable, `pg` tests **skip** locally but **fail** in CI
  — a green local run with skips is not the same as a green CI run.
- **No test touches the network.** Telegram is faked behind
  `httpx.MockTransport`, feeds are served from `tests/fixtures/`, and
  translation providers are counting fakes. Keep it that way in new tests.
- The compose file publishes Postgres on host port **5433** (avoiding a
  clash with any local server), which is the tests' default DSN — plain
  `pytest` just works. Set `TEST_DATABASE_URL` only to point the suite at a
  non-default database.

## Fixtures

`tests/fixtures/` holds one saved real XML response per source; they are the
parser-correctness baseline. Never regenerate them silently — if a feed
format changes, add a new capture alongside the old one and update
`tests/fixtures/README.md` with the capture date. `.gitattributes` marks them
binary so line endings are never normalized.

## What a good PR looks like

- **The PR title must be a Conventional Commit** (`feat: …`, `fix: …`,
  `docs: …`, …) — the `pr-title` status check enforces it. The repository
  squash-merges, so the title becomes the commit on `main` and directly
  decides the release: `fix:` cuts a patch release on merge, `feat:` a
  minor one, and `docs:`/`chore:`/`ci:`/`test:`/`refactor:` release
  nothing. There is no release PR — merging is releasing. See
  `docs/releasing.md`.
- **Breaking change?** Use `feat!:`/`fix!:` in the title, or add a
  `BREAKING CHANGE: <description>` footer to the squash commit *body* in
  the merge dialog. A footer that only exists in a PR comment or a squashed
  intermediate commit is silently lost and the version comes out too small.
  (While the version is 0.x, breaking changes bump minor, not major.)
- One concern per PR; behaviour changes come with tests that fail without
  the change.
- New spec fields belong in `feedspec/model.py` with validation, a loader
  test, and a row in `docs/configuration.md`.
- New error paths should be classified (see `feedspec/resolve.py`
  classification rules) and covered by a test asserting the resulting
  `error_events` row.
- `pytest` (both tiers) and `python -m compileall feedspec fetcher archive
  newsbot cli.py specsource.py` must pass — CI runs exactly these. CI also
  runs `./scripts/check-neutral.sh`, which rejects operator-specific values
  (real chat ids, personal registry/resource names) in tracked files — run
  it locally before pushing.
- Don't bump the spec `version` field unless the change is breaking; if it
  is, mark the PR as breaking (see above) and describe the migration in the
  PR description, which feeds the generated release notes.
