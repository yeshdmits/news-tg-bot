# ADR 0006 — One image, three serverless units, scale-to-zero

## Status

Accepted.

## Context

The program has three jobs with different lifecycles: a pipeline pass every
few minutes, an HTTP endpoint that is idle almost always, and schema
migrations at deploy time. The classic shape — one always-on container
running a scheduler loop (`cli run` without `--once` still exists for local
compose) — bills 24/7 for a workload that is active a few percent of the
time.

## Decision

Build a single Docker image (spec and schemas baked in — Container Apps has
no persistent filesystem, and a config change is just a rebuild) and run it
as three Azure Container Apps units differing only in command:

| Unit | Command | Shape |
|---|---|---|
| fetch job (`<name_prefix>-fetch`) | `cli run --once` | cron job, `*/5`, timeout 280 s, no retry |
| webhook app (`<name_prefix>-bot`) | `cli serve` | app, min replicas **0**, max 2 |
| migrate job (`<name_prefix>-migrate`) | `alembic upgrade head && cli register-webhook` | manual job |

The cron period (5 min) matches the tightest `fetch_interval_min` in the
spec; the 280 s replica timeout keeps an execution from outliving its cron
slot.

## Consequences

- Compute cost approaches zero at idle; `min_replicas = 0` on the webhook
  app roughly halves the bill versus keeping one replica warm — at the price
  of cold-start latency on the first webhook call after idling.
- Overlap and interruption become normal events, which forces the sturdier
  execution model of ADR 0008 (advisory lock, idempotent webhook, atomic
  post-slot claim).
- Telegram webhook registration cannot happen at container start (a
  scale-from-zero app would re-register on every cold start and hit rate
  limits) — it lives in the migrate job instead.
- One image means the fetch job carries starlette/uvicorn and the webhook
  app carries lxml; image size is traded for a single build and one tag to
  reason about.
