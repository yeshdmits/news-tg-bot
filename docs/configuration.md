# Configuration

The bot is configured in two layers:

1. **Environment variables** — where things are (database, token, ports).
   Parsed once at startup by `Settings.from_env()` in `cli.py`. The
   application reads environment variables and nothing else: it has no idea
   whether a value came from a `.env` file, a CI variable, or a cloud
   secret store — injecting from those is the deployment layer's job.
2. **The spec** — what the bot does: sources, channels, routing, error
   handling. Parsed and validated by `feedspec/loader.py` against the pydantic
   models in `feedspec/model.py`. `spec.example.json` is a complete working
   example; your live copy (conventionally `spec.json`) is gitignored.

Everything on this page is enforced by that code; run
`python -m cli validate --spec your-spec.json` at any time to check a spec
offline — it needs no database, network, or credentials.

## Where the spec comes from

Three sources, checked in this order — the first one present wins, and a
failure is fatal (the chain **never falls through** to a lower source, so a
network blip cannot silently start the bot on the wrong configuration):

| Order | Variable | Use it for |
|---|---|---|
| 1 | `SPEC_JSON` | The full spec as an inline string, for injecting the config straight from a secret store — no filesystem needed. Container platforms cap secret/env sizes, so mind your platform's ceiling: Azure Container Apps handles the ~10 KB reference spec fine. |
| 2 | `SPEC_URL` | An `https://` URL fetched once at startup (10 s timeout, redirects followed, no retry, no cache). Non-200, transport errors and unparsable bodies all abort startup. Use it to update the config without redeploying, and for specs too large to sit in an env var. Plain `http://` is refused. **What the reference Azure deployment uses** — see [Azure](deployment/azure.md). |
| 3 | `SPEC_PATH` | A file path — local development, compose (which mounts your file at `/app/spec.json`), and tests. |

An explicit `--spec PATH` argument (on `validate` and `fetch`) beats all
three. With no source configured at all, startup fails listing the three
options and pointing at `spec.example.json`.

The precedence is worth remembering when switching between sources: setting
`SPEC_URL` while `SPEC_JSON` is still set anywhere in the environment leaves
the inline copy live, silently and without an error. Remove the old variable,
don't just add the new one.

`SPEC_URL` moves the spec off your infrastructure, which is the point — but
it also makes whatever serves it a startup dependency, and the fetch does not
retry. The fetch job recovers on its next scheduled run; a unit that loads
the spec as part of a deploy does not. Validating against the live URL before
rolling anything out is the cheap way to avoid this:

```bash
SPEC_URL="https://<host>/<path>/spec.json" python -m cli validate
```

Whichever source is used, the loader logs one `spec_loaded` line at info
level with the source kind and a SHA-256 of the exact bytes — never the
content, which names private chat ids. The same hash keys the
`spec_versions` archive table, so the provenance of every archived item
survives no matter how the spec was delivered.

If your spec references XSD files (`schema_file`), their relative paths
resolve against the spec file's own directory for `SPEC_PATH`, and against
`SPEC_BASE_DIR` (default: the working directory, `/app` in the image) for
inline/URL specs.

## Editor support

`spec.schema.json` is a JSON Schema generated from the pydantic models
(regenerate with `python -m cli schema > spec.schema.json`; a test fails if
it drifts). Point your editor at it — the example does so via its
`"$schema": "./spec.schema.json"` key — and you get autocomplete and inline
errors while typing. `python -m cli validate` runs the same schema first
(reporting every structural problem at once) and the full loader second;
the loader remains authoritative, since cross-checks like duplicate channel
names are beyond JSON Schema.

## Environment variables

Required variables are validated **per command, at startup, all at once**:
a bare `run` names both `DATABASE_URL` and the missing spec source in a
single message before exiting with status 2.

| Variable | Type | Default | Required by | Notes |
|---|---|---|---|---|
| `SPEC_JSON` / `SPEC_URL` / `SPEC_PATH` | str | *(empty)* | every command that reads the spec (`run`, `serve`, `validate`, `fetch`, `register-webhook`) — one of the three | See [Where the spec comes from](#where-the-spec-comes-from). |
| `SPEC_BASE_DIR` | str | *(empty)* | — | XSD resolution dir for inline/URL specs; empty means the working directory. |
| `DATABASE_URL` | str | *(empty)* | `run`, `serve`, `stats`, `export`, alembic | Postgres DSN. |
| `TELEGRAM_BOT_TOKEN` | str | *(empty)* | `register-webhook` | Empty elsewhere disables all Telegram sending — errors are still recorded in the database, silently. |
| `DRY_RUN` | bool | `true` | — | Truthy values: `1`, `true`, `yes` (case- and whitespace-insensitive). See [Dry run](#dry-run). |
| `LOG_LEVEL` | str | `info` | — | Standard Python levels. An unknown value silently falls back to `info`. |
| `TRANSLATE_PROVIDER` | str | `none` | — | `none` or `deepl`. Any other value degrades to a no-op provider with a warning. |
| `TRANSLATE_API_KEY` | str | *(empty)* | — | DeepL key. `deepl` provider without a key also degrades to a no-op with a warning — posts go out untranslated. |
| `HEALTHCHECK_URL` | str | *(empty)* | — | Deadman-switch ping (GET, 10 s timeout) fired after every successful fetch pass. Empty disables. |
| `TELEGRAM_WEBHOOK_SECRET` | str | *(empty)* | `serve`, `register-webhook` | Compared against `X-Telegram-Bot-Api-Secret-Token` on every webhook request. **Empty rejects all webhook calls** (fail closed). |
| `WEBHOOK_URL` | str | *(empty)* | `register-webhook` | Public HTTPS endpoint passed to Telegram's `setWebhook`. |
| `PORT` | int | `8000` | — | `cli serve` listen port. A non-numeric value is reported as `CONFIG: PORT: not an integer` at startup. |
| `SEEN_UPDATES_TTL_HOURS` | int | `24` | — | Webhook idempotency window. Purged every execution. Must be positive — a zero or negative value would delete the whole table on the next pass and is rejected at startup. |
| `FETCHES_TTL_DAYS` | int | `90` | — | Fetch audit-row retention. **Inert until migration 0006**: `items.fetch_id` references `fetches` until then, so the purge would fail on rows still referenced; it returns 0 rather than raising. Must be positive. |

The error-table windows (`error_occurrences`, `alert_outbox`, `error_events`)
live in the spec under [`errors.retention`](#errors), not here — they are
policy rather than deployment. Every table's window is listed in one place:
[docs/data-model.md](data-model.md#retention).

Deployment-only variables — consumed by docker compose, CI, or Terraform,
never by the application: `SPEC_FILE`, `IMAGE`, `POSTGRES_PASSWORD`,
`TEST_DATABASE_URL` (see `.env.example`), the CI repository variables
(`IMAGE_NAME`, `NAME_PREFIX`, `AZURE_RESOURCE_GROUP`) and everything in
`terraform.tfvars` (see `docs/deployment/`).

## The spec

Top level (all models are frozen and reject unknown keys):

```json
{
  "version": 2,
  "defaults": { },
  "channels": [ ],
  "sources": [ ],
  "errors": { }
}
```

- `version` — **must be the literal `2`**. Anything else:
  `unknown spec version …; this loader implements version 2`. This is the
  spec *format* version, independent of the application version: it advances
  only when the format changes incompatibly, and such changes are called out
  in the changelog entry for the release that made them.
- `defaults` — optional overrides of the documented defaults below
  (see [Precedence](#precedence)).
- `channels` — the channel registry.
- `sources` — the feeds.
- `errors` — optional; enables operator alerting and ops commands.

### Channels (`channels[]`)

A channel is a Telegram chat plus its posting knobs.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `name` | str | **required** | Referenced by source bindings. Must be unique. |
| `id` | str | **required** | Telegram chat id (e.g. `"-100999900001"`). Must be unique. |
| `language` | str | `"en"` | Target language for posts; drives translation. |
| `post_interval_min` | int | `30` | Minimum minutes between posting batches to this chat. |
| `max_posts_per_run` | int | `5` | Batch cap per posting window, across **all** sources bound to the chat. |
| `max_age_min` | int \| null | `null` | Items older than this at posting time become `too_old`. `null` = no age limit. |
| `queue_policy` | `"drop_oldest"` \| `"drain_all"` | `"drop_oldest"` | What happens to backlog beyond the cap: dropped permanently, or deferred to the next window. |
| `post_style` | `"link_preview"` \| `"link_preview_title_only"` \| `"photo_full"` \| `"text_only"` | `"link_preview"` | How posts render — see below. |
| `enabled` | bool | `true` | Disabled channels record `channel_disabled` decisions and post nothing. |

### Sources (`sources[]`)

| Field | Type | Default | Meaning |
|---|---|---|---|
| `name` | str | **required** | Unique source key; also the archive key and hashtag base. |
| `url` | str | **required** | Feed URL. |
| `language` | str | **required** | Feed language; compared to channel language to decide translation. |
| `channels` | list of bindings | **required** (may be empty) | See [Bindings](#bindings-sourceschannels). An empty list makes the source [archive-only](#archive-only-sources). The key itself is mandatory — a dropped one is an error, not a silent mute. |
| `mapping` | object | **required** | See [Mapping](#mapping-sourcesmapping). |
| `type` | `"xml"` | `"xml"` | Only XML feeds are implemented. |
| `kind` | `"general_news"` \| `"central_bank"` \| `"statistics"` \| `"press_wire"` \| `"exchange"` | `"general_news"` | Taxonomy used by `when.source_kinds` predicates. |
| `region` | str \| null | `null` | Free-form region tag, rendered as a hashtag (e.g. `#CH`). |
| `fetch_limit` | int | `10` | Max items taken from the top of the feed per fetch. |
| `fetch_interval_min` | int | `15` | Fetch cadence. The Azure cron job runs every 5 minutes; a source is fetched when its interval has elapsed. |
| `cold_start_policy` | str | `"skip_all"` | `"skip_all"` or `"post_newest:N"` (N ≥ 1). First-ever fetch either archives everything without posting, or posts only the N newest. Anything else: `unsupported cold_start_policy …`. |
| `enabled` | bool | `true` | Disabled sources are never fetched. |
| `url_verified` | str \| null | `null` | Date you last confirmed the feed URL. `null` produces a load warning, not an error. |
| `namespaces` | dict | `{}` | XML prefix → namespace URI map for mapping expressions. |
| `schema_file` | str \| null | `null` | XSD path, resolved relative to the spec file's directory. When set, feeds that fail validation raise `SchemaValidationFailed` instead of being parsed loosely. |
| `notes` | str \| null | `null` | Free-form operator notes. |

### Bindings (`sources[].channels[]`)

A binding attaches a source to one channel with per-pair filtering.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `name` | str | **required** | A channel `name` from the registry. Unknown or duplicate names are load errors. |
| `include_categories` | list | `[]` | Only items with at least one of these categories are routed. **An empty list means "no filter", not "include nothing".** |
| `exclude_categories` | list | `[]` | Items with any of these categories are filtered. |
| `lead_max_length` | int | `300` | Lead truncation budget for this pair. |
| `translate_lead` | bool | `false` | Translate title+lead into the channel language when languages differ. |
| `post_style` | style \| null | `null` | Override; `null` inherits from the channel. |
| `when` | object \| null | `null` | Content predicate, see below. |

### Post styles

Every post carries the title, the "Read more" link and the source hashtags.
The styles differ in what else goes out:

| Style | Lead | Telegram link preview | Image |
|---|---|---|---|
| `link_preview` | yes | yes | — |
| `link_preview_title_only` | **no** | yes | — |
| `photo_full` | yes | no (the image replaces it) | posted as a photo with the text as its caption |
| `text_only` | yes | **no** | — |

`link_preview_title_only` posts the headline and lets Telegram's preview carry
the context. It is the right choice for feeds whose lead merely restates the
title (a `lead_fallback: "title"` source, for instance) and anywhere you want
to republish less of the publisher's text — see
[legal.md](legal.md#configuration-that-changes-exposure). The lead is dropped
before translation, so it costs no DeepL quota even with `translate_lead: true`.

`photo_full` falls back to a text post when the item has no image, and that
fallback behaves like `link_preview`, not like `text_only`.

**Channel-scoped keys are rejected on bindings.** `id`,
`post_interval_min`, `max_posts_per_run`, and `max_age_min` are properties of
the chat, not of one source's relationship to it — a binding declaring one
fails at load with a message naming the source, channel, and key (see
[Rejected examples](#rejected-examples)).

### Archive-only sources

`"channels": []` is valid and means **fetch it, keep it, never post it**. The
source is polled on its interval, parsed, deduped, and stored in `items` with
its raw XML like any other — it simply produces no routing decisions and no
deliveries. Use it to evaluate a feed before wiring it to a chat, to build a
corpus for `cli export`, or for a signal you never intend to republish.

```json
{
  "name": "example-wire",
  "url": "https://wire.example.org/feed.xml",
  "language": "en",
  "cold_start_policy": "skip_all",
  "mapping": { "items": "//item", "id": "guid", "title": "title", "url": "link" },
  "channels": []
}
```

Three things to know:

- The `channels` key stays **required**. Archive-only must be declared with an
  explicit `[]`; omitting the key is still a load error, so a typo can never
  quietly stop a source from posting.
- The effective `cold_start_policy` must be `skip_all`. `post_newest:N` only
  controls posting, so on an archive-only source it provably does nothing, and
  the loader refuses it rather than let it read as if the first fetch would
  publish something. This is checked *after* `defaults` are applied — under a
  spec-wide `defaults.cold_start_policy: "post_newest:N"` you must set
  `"cold_start_policy": "skip_all"` on the archive-only source itself.
- **Binding a channel later routes only new items.** Routing decisions are
  written when an item is first stored, so items archived while the source was
  unbound stay unposted forever. That is deliberate — it stops a backlog of
  months-old items flooding a chat the moment you wire it up.

Going the other way — emptying the `channels` list of a source that was posting —
is safe too: items already routed but not yet sent are retired with a
`channel_disabled` decision (reason `binding removed from spec`) on the next run
rather than posted.

`cli validate` names these sources explicitly:

```
example-wire (general_news, en, every 15 min, limit 10, cold_start=skip_all)
    -> (archive only — stored, never posted)

OK: 5 sources (1 archive-only), 2 channels, 5 bindings
```

### `when` predicates

All groups are optional; empty groups are no-ops; all present groups must
pass.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `keywords_any` | list | `[]` | At least one keyword must appear. |
| `keywords_all` | list | `[]` | Every keyword must appear. |
| `keywords_none` | list | `[]` | No keyword may appear. |
| `categories_any` | list | `[]` | At least one item category must match. |
| `source_kinds` | list | `[]` | Item's source `kind` must be one of these. |
| `match_fields` | list of `"title"`/`"lead"` | `["title", "lead"]` | Where keywords are searched. |
| `case_sensitive` | bool | `false` | Keyword matching case sensitivity. |

### Mapping (`sources[].mapping`)

Expressions are a strict subset of XPath 1.0 with one extension: a trailing
`@attr` reads an attribute of the matched element
(`media:content[@width='700']@url` means `media:content[@width='700']/@url`).
`items` is evaluated against the document; every other expression is
evaluated relative to each matched item element. Extracted text is
whitespace-collapsed; empty results are treated as missing.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `items` | str | **required** | Expression selecting the item elements, e.g. `//item`. |
| `id` | str | **required** | Stable per-item id (e.g. `guid`). Duplicate protection keys on `(source, id)`. |
| `id_pattern` | str \| null | `null` | Regex applied to the raw id; first capture group wins (whole match if no groups). A non-match makes the item unidentifiable and it is skipped with a problem report. |
| `title` | str | **required** | Item title. |
| `url` | str | **required** | Item link. |
| `published` | str \| null | `null` | Publication date expression. |
| `published_format` | `"rfc822"` \| `"iso8601"` \| null | `null` | Date format; unset parses as RFC 822. Unparseable dates raise `PublishedDateUnparseable` and the fetch time is used. |
| `lead` | str \| list \| null | `null` | Teaser text. A list is an **ordered fallback**: the first expression yielding text wins. |
| `allow_empty_lead` | bool | `false` | When false, an item with no lead is a problem unless `lead_fallback` fills it. |
| `lead_fallback` | `"title"` \| null | `null` | Use the title as the lead when the lead is empty. |
| `lead_html` | bool | `false` | Strip HTML tags from the extracted lead. |
| `lead_remove` | list of regex | `[]` | Patterns deleted from the lead (boilerplate like "Read more…"). Each pattern is compiled at validation time — a bad regex fails the load. |
| `image` | str \| list \| null | `null` | Image URL expression(s), ordered fallback like `lead`. |
| `category` | str \| null | `null` | Category expression (may match multiple elements). |
| `category_from` | str \| null | `null` | Derive a category from the item URL. Only `url_path_segment:N` (1-based, non-empty segments) is supported; anything else: `unsupported category_from …`. |
| `copyright` | str \| null | `null` | Copyright notice expression, stored with the item. |

### Errors (`errors`)

Optional. Without it there is no ops chat, no alerting, and
`register-webhook` refuses to register commands.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `ops_chat_id` | str | **required** | The operator group chat. **Must not equal any news channel id** — the loader refuses to alert into a news channel. |
| `ops_topic_id` | int \| null | `null` | Forum topic for alerts. |
| `digest_topic_id` | int \| null | `null` | Forum topic for digests. |
| `authorization.read_roles` | list | `["member"]` | Who may run read commands. |
| `authorization.write_roles` | list | `["admin"]` | Who may run destructive commands (/ack, /mute…). |
| `authorization.write_allowlist` | list of int | `[]` | User ids always allowed to write. |
| `authorization.admin_cache_min` | int | `15` | Minutes to cache `getChatAdministrators`. |
| `alerting.enabled` | bool | `true` | Master switch for sending (events are recorded regardless). |
| `alerting.escalation_ladder` | list of int | `[1, 10, 50, 200]` | Occurrence counts at which an alert (re-)notifies. |
| `alerting.cooldown_min` | int | `30` | Minimum minutes between notifying sends per event. Severity escalation bypasses it. |
| `alerting.max_alerts_per_hour` | int | `10` | Alert budget; beyond it a single suppression notice is posted. Critical alerts bypass the budget. |
| `alerting.digest_interval_min` | int | `360` | Cadence of the unacked-events digest. |
| `alerting.silent_below` | severity | `"error"` | Severities below this send with `disable_notification`. |
| `alerting.edit_on_escalation` | bool | `true` | Escalations edit the existing alert message instead of posting a new one. |
| `alerting.pin_critical` | bool | `true` | Pin critical alerts in the ops chat. |
| `alerting.quiet_hours` | object \| null | `null` | `{"from": "HH:MM", "to": "HH:MM", "timezone": "...", "suppress_below": severity}`. Malformed times: `quiet_hours time … must be HH:MM`. |
| `classification` | dict | `{}` | Per-error-class `{severity, escalate_after, escalates_to}` overrides. |
| `source_overrides` | dict | `{}` | Per-source classification overrides. Unknown source names are a load error. |
| `retention.occurrences_days` | int | `30` | Error occurrences older than this are purged. |
| `retention.events_days` | int | `365` | Resolved events older than this (with no surviving occurrences) are purged. |

Severity is one of `critical`, `error`, `warning`, `info`. Classification
precedence per error class:
**source override < spec `classification` < built-in default < unknown-class
rule** (`error`, escalate after 3). The built-in defaults live in
`DEFAULT_CLASSIFICATION` in `feedspec/model.py`.

## Precedence

Every overridable field resolves through the same chain — later wins:

```
documented default  <  spec "defaults"  <  channel registry entry  <  binding
```

- Lists **replace**, they never concatenate. An explicit
  `include_categories: []` on a binding survives resolution as "no filter".
- The implementation distinguishes "explicitly set" from "defaulted" via
  pydantic's `model_fields_set`, so setting a field to its default value
  still counts as explicit.
- `defaults` may only contain keys that are overridable somewhere:
  binding keys (`include_categories`, `exclude_categories`,
  `lead_max_length`, `translate_lead`, `post_style`, `when`), source keys
  (`type`, `kind`, `region`, `fetch_limit`, `fetch_interval_min`,
  `cold_start_policy`, `enabled`, `namespaces`, `schema_file`), and channel
  keys (`language`, `post_interval_min`, `max_posts_per_run`, `max_age_min`,
  `queue_policy`). Anything else: `unknown key(s) in defaults: […]`.
- `post_style` has its own fallback: binding → channel (explicit) →
  `defaults` → channel documented default.

## Dry run

`DRY_RUN=true` (the default) suppresses **channel posting only**: posts are
formatted, selected, and recorded in the archive as `skipped`/`dry_run`, but
nothing is sent. Operator alerts and ops-chat replies still go out if a bot
token is set — dry run rehearses the pipeline, not the alerting.

## Worked example

A complete, valid spec (placeholder ids — substitute your own chats):

```json
{
  "version": 2,
  "defaults": {
    "fetch_interval_min": 15,
    "post_style": "link_preview"
  },
  "channels": [
    {
      "name": "world-news",
      "id": "-100999900001",
      "language": "en",
      "post_interval_min": 30,
      "max_posts_per_run": 5,
      "max_age_min": 720,
      "queue_policy": "drop_oldest"
    }
  ],
  "sources": [
    {
      "name": "example-world",
      "url": "https://example.org/feeds/world.rss",
      "language": "en",
      "kind": "general_news",
      "region": "EU",
      "url_verified": "2026-08-02",
      "mapping": {
        "items": "//item",
        "id": "guid",
        "title": "title",
        "url": "link",
        "published": "pubDate",
        "published_format": "rfc822",
        "lead": ["description", "title"],
        "lead_html": true,
        "category": "category"
      },
      "channels": [
        {
          "name": "world-news",
          "exclude_categories": ["sport"],
          "lead_max_length": 300
        }
      ]
    }
  ],
  "errors": {
    "ops_chat_id": "-4999900004",
    "alerting": {
      "quiet_hours": {
        "from": "23:00",
        "to": "07:00",
        "timezone": "UTC",
        "suppress_below": "critical"
      }
    }
  }
}
```

## Rejected examples

Real output from `python -m cli validate` (exit code 1 in every case).

A channel-scoped key on a binding:

```json
"channels": [{ "name": "world-news", "post_interval_min": 10 }]
```

```
INVALID: source 'example-feed' binding to channel 'world-news': key(s)
['post_interval_min'] are channel-scoped and must be set in the channel
registry, not on a binding
```

The ops chat pointed at a news channel:

```json
"errors": { "ops_chat_id": "-100999900001" }
```

```
INVALID: errors.ops_chat_id '-100999900001' is also the chat id of news
channel 'world-news' — refusing to alert into a news channel
```

A typo in any field name (unknown keys are always rejected):

```json
{ "name": "world-news", "id": "-100999900001", "post_stile": "text_only" }
```

```
INVALID: spec failed validation:
1 validation error for Spec
channels.0.post_stile
  Extra inputs are not permitted [type=extra_forbidden, ...]
```

A posting-only cold start on a source that never posts:

```json
{ "name": "example-wire", "channels": [], "cold_start_policy": "post_newest:2" }
```

```
INVALID: source 'example-wire' has no channel bindings (archive-only) but
cold_start_policy is 'post_newest:2', which only affects posting; set it to
'skip_all'
```

Other load-time errors you can hit: duplicate channel names / chat ids /
source names, a binding to an unknown channel, the same channel bound twice
by one source, a source with no `channels` key at all (an empty list is how
you declare [archive-only](#archive-only-sources)),
`errors.source_overrides` naming an unknown source, a spec that is not valid
JSON, and a root that is not a JSON object.
