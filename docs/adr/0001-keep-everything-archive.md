# ADR 0001 — Keep everything in the archive, including raw XML

## Status

Accepted.

## Context

A news relay only strictly needs to remember which items it already posted.
Storing less would keep the database small and make retention trivial.

## Decision

Store every fetched item permanently: the raw XML fragment, all extracted
fields, every fetch record, every routing decision, every delivery attempt,
and every translation. Nothing content-related is ever deleted; only
operational error data has retention. Every row references the content-hash
of the exact spec version that produced it (`spec_versions`).

## Consequences

- The archive is an audit trail: "why did/didn't this item post three weeks
  ago" is answerable from `routing_decisions` + `deliveries`.
- Items are re-parseable after the fact — a test asserts that re-parsing a
  stored `raw_xml` blob with the same mapping reproduces the item — so
  mapping bugs can be corrected retroactively against stored data.
- `cli export` can produce datasets for analysis.
- Disk usage grows without bound; the operator watches `cli stats` and sizes
  Postgres storage accordingly (32 GB with auto-grow off in the bundled
  Terraform). This will look like a missing-pruning bug to newcomers; it is
  not. See `docs/data-model.md` and `docs/legal.md` for the retention and
  content implications.
