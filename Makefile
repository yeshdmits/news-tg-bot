# Local development and verification.
#
# These targets drive docker-compose.yml — Postgres with pg_cron, plus Azurite
# as a Blob emulator. See CONTRIBUTING.md for the test tiers and
# docs/archive-format.md for what the archive targets produce.
#
# Everything is safe to run repeatedly. `local-reset` is the only destructive
# target and it says so.

SHELL := /bin/bash
.DEFAULT_GOAL := help

COMPOSE ?= docker compose

# Prefer a checkout-local virtualenv when there is one, so these targets work
# without an activated shell. Override with `make PY=python3.12 ...`.
PY ?= $(shell test -x .venv/bin/python && echo .venv/bin/python || echo python)

# Matches docker-compose.yml's published port and tests/conftest.py's
# default, so `make test` needs no environment at all.
DATABASE_URL ?= postgresql://newsbot:newsbot@localhost:5433/newsbot

# Azurite's well-known development credentials. Not a secret: this account
# exists only inside the emulator (see NEUTRALISATION.md — nothing here is
# operator-specific).
AZURITE_CONNECTION_STRING ?= DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==;BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;
ARCHIVE_CONTAINER ?= archive

# local-seed knobs. N is the item count; DAYS spreads them backwards from now
# so retention and archive windows can be exercised without waiting.
N     ?= 100000
DAYS  ?= 10
JOBS  ?= 0

export DATABASE_URL
export ARCHIVE_BLOB_CONNECTION_STRING = $(AZURITE_CONNECTION_STRING)
export ARCHIVE_CONTAINER

.PHONY: help local-up local-down local-reset local-migrate local-seed \
        local-archive local-verify local-loadtest test test-offline

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# Only the backing services: naming them keeps `migrate` and `bot` (which are
# in the default profile, and build the app image) out of the way. Running the
# bot is `docker compose up`, which is a different intent from testing.
SERVICES ?= db azurite

local-up: ## Start Postgres + Azurite and wait for both to be healthy
	$(COMPOSE) up -d --wait $(SERVICES)

local-down: ## Stop the stack, keep the data
	$(COMPOSE) down

local-reset: ## DESTRUCTIVE: drop volumes, recreate the stack, run all migrations
	$(COMPOSE) down -v
	$(COMPOSE) up -d --wait $(SERVICES)
	$(MAKE) local-migrate

local-migrate: ## Run alembic to head against the local database
	$(PY) -m alembic upgrade head

local-seed: ## Generate N synthetic items across ~50 sources (N=, DAYS=, JOBS=)
	$(PY) -m tools.seed --items $(N) --days $(DAYS) --jobs $(JOBS)

local-archive: ## Run the archive+purge job once against Azurite
	@$(PY) -m cli archive --help >/dev/null 2>&1 \
	  || { echo "make: 'cli archive' does not exist yet — it lands in Phase 5 of the scaling work."; exit 1; }
	$(PY) -m cli archive --once

local-verify: ## Assert the storage budget and archive integrity
	@$(PY) -m cli watchdog --help >/dev/null 2>&1 \
	  || { echo "make: 'cli watchdog' does not exist yet — it lands in Phase 5 of the scaling work."; exit 1; }
	$(PY) -m cli watchdog --verify-archive

local-loadtest: ## Sustained-ingest load test against the local stack
	@test -f tools/loadtest.py \
	  || { echo "make: tools/loadtest.py does not exist yet — it lands in Phase 6 of the scaling work."; exit 1; }
	$(PY) -m tools.loadtest

test: ## Full test suite (needs the local stack up)
	$(PY) -m pytest

test-offline: ## Tier 1 only: no services, no network
	$(PY) -m pytest -m "not pg"

# Deliberately no `lint` target: `ruff check .` reports 76 pre-existing
# findings and `ruff format --check` wants to reformat 35 files, and CI gates
# on neither (see .github/workflows/pipeline.yml). A target that fails on a
# clean checkout teaches people to ignore it. Run ruff directly on what you
# touched — CONTRIBUTING.md has the settings.
