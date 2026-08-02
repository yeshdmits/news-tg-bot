# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

While the version is `0.x`, the configuration format and the deployment
interface may change between minor releases.

The configuration format (`spec.json`) is versioned independently via its
`version` field; changes to it are called out in the entry for the release
that made them.

## 0.1.0 (2026-08-02)


### Continuous Integration

* single main-branch pipeline and automated releases ([#1](https://github.com/yeshdmits/news-tg-bot/issues/1)) ([4e83b65](https://github.com/yeshdmits/news-tg-bot/commit/4e83b6566fe23f65f02fee1febf251a151b364c1))

## [Unreleased]

## [0.1.0] — 2026-08-02

First public release.

### Added

- **Declarative configuration** (spec format version 2): a single JSON
  document defining sources, channels, per-binding filters, field mappings,
  defaults, and error-handling policy — validated by pydantic models that
  reject unknown keys. `python -m cli validate` checks a spec and prints every
  resolved source→channel binding; `python -m cli schema` emits a JSON Schema
  (`spec.schema.json`) so editors pointed at it via the example's `$schema`
  key get autocomplete. `spec.example.json` is a complete, runnable example on
  public official sources (ECB, Federal Reserve, SNB) with obviously fake
  channel ids (the `9999000` block).
- **Configuration loading through a precedence chain**, first source present
  wins, never falling through on failure: `SPEC_JSON` (inline, for
  secret-store injection) → `SPEC_URL` (HTTPS fetch) → `SPEC_PATH` (file).
  With no source configured the program exits, naming all three options.
- **Startup validation of required environment variables per command**,
  reporting every missing variable at once (exit 2); `serve` requires
  `DATABASE_URL` and `TELEGRAM_WEBHOOK_SECRET` at startup instead of failing
  lazily.
- **Fetch pipeline**: conditional GET (ETag/Last-Modified), optional XSD
  validation, XPath-based field mapping, and per-source failure backoff.
- **Permanent PostgreSQL archive** (alembic-managed): every fetched item is
  stored with its raw XML, a full routing and delivery audit trail, and spec
  version provenance, so history stays interpretable after configuration
  changes.
- **Deduplication** by canonical URL, content hash, and title simhash.
- **Telegram posting** with per-channel rate limits, queue policies, post
  styles, and optional DeepL lead translation with a quota guard.
- **Operator alerting**: fingerprinted error events, an escalation ladder,
  quiet hours, an alert budget, and ops-chat commands over a Telegram
  webhook.
- **Overlap safety**, all anchored in Postgres: a `newsbot:fetch` advisory
  lock so only one fetch/post cycle runs at a time, an atomic posting-slot
  claim per channel, and exactly-once webhook handling via recorded
  `update_id`s.
- **Local development**: `docker compose` stack with the database on host
  port 5433, matching the test suite's default DSN — a plain `pytest` works
  against the compose db.
- **Azure deployment**: a Terraform stack for Azure Container Apps
  (scale-to-zero webhook app, cron fetch job, manual migration job) with
  secrets in an Azure Key Vault, referenced through a user-assigned identity
  and written with write-only arguments so their values stay out of Terraform
  state; the application configuration is one of those secrets, injected as
  `SPEC_JSON`. The remote-state backend uses partial configuration
  (`backend.hcl`, gitignored; example tracked).
- **CI/CD** (GitHub Actions): neutrality check, tests against a Postgres
  service container, image build and push (GHCR by default with the built-in
  `GITHUB_TOKEN`; any registry via the `IMAGE_NAME` repository variable), and
  an OIDC deploy that runs migrations to completion before rolling the apps.
  The deploy job runs only when the `NAME_PREFIX` and `AZURE_RESOURCE_GROUP`
  repository variables are set — fresh forks build, test, and publish with
  zero configuration and skip the deploy with instructions.
- **Neutrality guard** (`scripts/check-neutral.sh`): CI fails if
  operator-specific patterns, real-looking Telegram ids outside the fake
  block, or live config files ever land in the tracked tree.
- **Documentation**: configuration reference, data model, deployment guides
  (compose, Terraform/Azure, and a provider contract for porting to other
  clouds), troubleshooting, architecture decision records, contribution and
  security policies. MIT license.

[Unreleased]: https://github.com/OWNER/REPO/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/OWNER/REPO/releases/tag/v0.1.0
