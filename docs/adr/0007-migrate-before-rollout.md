# ADR 0007 — Migrations must succeed before any unit gets the new image

## Status

Accepted.

## Context

A deploy ships code and, sometimes, a schema change the code depends on.
If the fetch job or webhook app picks up new code before the migration has
run, it can fail against the old schema — or worse, half-work.

## Decision

The CI deploy job is strictly sequential:

1. point the migrate job at the new sha and start it (migrations run on the
   **new** image);
2. poll until the execution reports `Succeeded` — `Failed`, `Stopped`,
   `Degraded`, or a 10-minute timeout aborts the deploy;
3. only then update the fetch job and the webhook app.

The workflow comment marks the ordering as load-bearing; the steps must not
be parallelised.

## Consequences

- A failed migration leaves the previous image running everywhere — the
  stack degrades to "no deploy" rather than "broken schema".
- Deploys take a couple of minutes longer than a blind rollout.
- The guarantee is forward-only: rolling *back* past a migration requires a
  manual `alembic downgrade` first (documented in
  `docs/deployment/azure.md`).
- Old code briefly runs against the new schema (between steps 2 and 3),
  so migrations are expected to be backward-compatible for one deploy — the
  usual expand/contract discipline.
