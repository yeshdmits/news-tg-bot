# ADR 0008 — Overlap safety: advisory lock, atomic post slot, idempotent webhook

## Status

Accepted.

## Context

On serverless infrastructure, overlapping and interrupted executions are
routine: a slow fetch pass can still be running when the next cron fires;
Telegram redelivers webhook updates on any non-200; containers get killed
mid-transaction. The failure that matters most is double-posting into a
public channel — it is user-visible and cannot be retracted quietly.

## Decision

Three mechanisms, all anchored in Postgres:

- **One pipeline at a time.** `run --once` takes
  `pg_try_advisory_lock(hashtext('newsbot:fetch'))`; the loser logs
  `fetch_lock_held_elsewhere_skipping` and exits 0. Release is in a
  `finally`, and the role-level timeouts from migration 0003 bound a stray
  holder.
- **Atomic posting window.** A channel's posting slot is claimed by a single
  conditional UPSERT on `channel_state` — a read-then-write would let two
  executions both see "window open" and double-post.
- **Exactly-once webhook handling.** Every Telegram `update_id` is recorded
  in `seen_updates` before its side effects run; a redelivered update is
  answered 200 without re-acking/re-replying. Failures *before* the mark
  answer non-200 so Telegram redelivers; a monotonic offset (the getUpdates
  model) would be wrong here because redeliveries can arrive out of order.

## Consequences

- Overlap is a non-event; no external lock service or queue is needed.
- The advisory-lock skip means a run can legitimately do nothing — operators
  must read that log line as "healthy, yielded" (see
  `docs/troubleshooting.md`).
- Idempotency state carries small ongoing costs: `seen_updates` is purged on
  a 24 h window every execution.
- Everything hinges on Postgres being the single coordination point — which
  this design already assumes (ADR 0001).
