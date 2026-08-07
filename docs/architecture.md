# Architecture

One Python codebase, four packages with a strict dependency direction, one
CLI entrypoint, three runtime shapes.

## Components

| Package | Responsibility | May import |
|---|---|---|
| `feedspec/` | Parse and validate spec bytes; resolve effective config per (source, channel) pair. Pure — no I/O. | nothing internal |
| `specsource.py` | Spec acquisition: `SPEC_JSON` / `SPEC_URL` / `SPEC_PATH` precedence + HTTPS fetch; hands the bytes to `feedspec`. | `feedspec` |
| `fetcher/` | HTTP conditional GET, XML parsing, optional XSD validation, mapping-expression evaluation. **Never imports `archive`** — item-level problems surface through an `on_problem` callback the caller provides. | `feedspec` |
| `archive/` | The only writer to PostgreSQL: items, fetches, decisions, deliveries, error events, feedback reviews, migrations. | `feedspec` |
| `newsbot/` | Orchestration: the runner (fetch → store → route → post), Telegram client, formatter, translation, alert engine, ops-command bot, feedback bot, webhook app. | all of the above |
| `cli.py` | Argument parsing, settings from env, wiring. | all of the above |

```mermaid
graph LR
    cli[cli.py] --> newsbot
    newsbot --> archive
    newsbot --> fetcher
    newsbot --> feedspec
    fetcher --> feedspec
    archive --> feedspec
    archive --> pg[(PostgreSQL)]
    newsbot --> tg[Telegram Bot API]
    fetcher --> feeds[RSS/Atom feeds]
    newsbot --> deepl[DeepL API]
```

## Runtime shapes

The same Docker image runs as three units (locally via docker compose, in
production as Azure Container Apps — see `docs/deployment/azure.md`):

| Unit | Command | Trigger |
|---|---|---|
| fetch job | `python -m cli run --once` | cron, every 5 minutes |
| webhook app | `python -m cli serve` | HTTP, scale-to-zero |
| migrate job | `alembic upgrade head && python -m cli register-webhook` | manual / CI, before each rollout |

## One fetch execution

```mermaid
sequenceDiagram
    participant J as fetch job (run --once)
    participant PG as PostgreSQL
    participant F as feeds
    participant T as Telegram

    J->>PG: pg_try_advisory_lock('newsbot:fetch')
    Note over J: lock lost → exit 0, no work
    J->>T: flush alert outbox
    loop each due source
        J->>F: conditional GET (ETag/Last-Modified)
        F-->>J: 200 + XML (or 304 / error)
        J->>PG: record fetch, store items,<br/>dedupe, one routing decision<br/>per (item, bound channel)<br/>(archive-only source: none)
    end
    loop each enabled channel
        J->>PG: claim post slot (atomic UPSERT)
        J->>T: send up to max_posts_per_run posts
        J->>PG: record deliveries
    end
    loop each review chat
        J->>PG: claim oldest queued item (if no card showing)
        J->>T: send review card (title/lead/link + 2 buttons)
    end
    J->>PG: maybe digest, retention, purge seen_updates
    J->>T: (deadman ping to HEALTHCHECK_URL)
    J->>PG: advisory unlock
```

Key properties:

- **Advisory lock** makes overlapping executions safe: the loser logs
  `fetch_lock_held_elsewhere_skipping` and exits cleanly.
- **Store and decide are one transaction per source**; posting is a separate
  phase with per-post transactions, so a crash mid-posting loses nothing.
- **Dedupe** happens at store time: canonical-URL/content-hash exact match,
  then a simhash scan (Hamming distance ≤ 3) over the last 72 h; duplicates
  are stored anyway with `duplicate_of` set and get `duplicate` decisions.
- **Backlog, not loss**: an item routed while the channel's posting window is
  closed is recorded `rate_limited` with a `gated:` reason and reconsidered
  next window (`drain_all`) or dropped by policy (`drop_oldest`).
- **Storing and posting are separable**: a source with an empty `channels` list
  is [archive-only](configuration.md#archive-only-sources) — it runs the whole
  fetch/parse/dedupe/store path and stops before routing.
- **Reviewing is separable too**: a source with a
  [`feedback`](configuration.md#feedback-sourcesfeedback) chat queues each new
  item for an approve/reject label, independently of whether it posts anywhere.
- Failures are classified into error events and alerted per the spec's
  `errors` config; a failing source backs off exponentially (cap 60 min)
  without affecting other sources.

## The webhook path

```mermaid
sequenceDiagram
    participant T as Telegram
    participant W as webhook app (serve)
    participant PG as PostgreSQL

    T->>W: POST /telegram/webhook (secret header)
    W->>W: constant-time secret check → 401 if wrong
    W->>PG: mark update_id seen (idempotency gate)
    Note over W,PG: duplicate update → 200, no side effects
    alt message (ops command)
        W->>PG: read/write error events
        W->>T: reply in ops chat
    else callback_query (review button)
        W->>PG: write the approve/reject label
        W->>T: answer, stamp the card (edit, never delete)
        W->>PG: claim the next item, topping up from items if dry
        W->>T: send the next card
    end
```

`GET /healthz` answers without touching the database — a probe can never
wake Postgres or fail because of it.

The two update kinds diverge before any Bot API call: `getMe` only exists to
disambiguate `/cmd@suffix`, so a button press never pays for it on a cold
start. The label is written before any Telegram traffic, so a card that
cannot be deleted, or a next card that cannot be sent, never costs a
decision — the fetch job repairs the chat on its next pass.

## Spec-driven design

Everything the pipeline does per source/channel comes from the spec
(see `docs/configuration.md`), acquired at startup via `specsource.py` —
`SPEC_JSON` inline, `SPEC_URL` (an HTTPS fetch — what the cloud deployment
uses), or a `SPEC_PATH` file (local development). The spec is content-hashed; every
archived row references the spec version that produced it, so the archive
remains interpretable across config changes.
