# ADR 0002 — Telegram ops group + error_events instead of a logging stack

## Status

Accepted.

## Context

The obvious Azure answer to observability is a Log Analytics workspace wired
to the Container Apps environment. Log Analytics bills per GB ingested; a
chatty container can out-cost the compute of this deliberately tiny stack.
The operators already live in Telegram.

## Decision

No Log Analytics (`logs_destination` unset in Terraform). Observability is:

- **`error_events`** in Postgres: every failure fingerprinted, grouped,
  counted, and kept (occurrences 30 days, events 365 by default);
- **the ops Telegram group**: alerts with an escalation ladder, cooldowns,
  budgets, quiet hours, digests, and `/ack`-style commands over a webhook;
- **a deadman ping** (`HEALTHCHECK_URL`) after each successful fetch pass,
  so an external monitor catches "the bot stopped running entirely" — the
  failure mode self-reporting cannot catch;
- **live console logs** via `az containerapp logs show` while a replica
  exists.

## Consequences

- Near-zero standing observability cost; alerting quality is high because
  errors are classified and deduplicated at the source.
- No long-term console-log retention and no metrics dashboards. Post-hoc
  debugging relies on `error_events.detail` (stack traces, HTTP bodies)
  rather than log search.
- If the database itself is down, alerting degrades to the alert outbox
  (flushed on recovery) and the deadman monitor.
