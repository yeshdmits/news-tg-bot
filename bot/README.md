# Technical Documentation

User-facing setup instructions are in the [root README](../README.md). This document covers internals: architecture, configuration reference, failure behavior, and development notes.

## How It Works

The bot polls every news source defined in `sources.json` (see [News Sources](#news-sources)) on a fixed interval (default: every 30 minutes). Each cycle:

1. **Fetches** every source (one GET per configured query), validates the payload against the source's schema, maps it to articles via the source's field mapping, and merges all sources chronologically.
2. **Filters** out articles in skipped categories (`SKIP_CATEGORIES`) and articles already posted, using a local SQLite database.
3. **Translates** title and lead to English via DeepL (`EN-US`), using the source's language — sources already in English are not translated at all. Translations are cached in SQLite so a failed Telegram send never re-spends DeepL quota on retry.
4. **Posts** each new article to the channel via `sendPhoto` (falling back to `sendMessage` with a link preview if the image is missing or rejected), oldest first, with a 2-second delay between posts.

```
┌──────────────┐    fetch     ┌──────────────┐   translate   ┌─────────┐
│ news sources  │ ──────────► │   main loop   │ ────────────► │  DeepL  │
│ (json / xml)  │             │  (30 min tick)│ ◄──────────── └─────────┘
└──────────────┘              │               │
                 ┌──────────► │               │    sendPhoto  ┌──────────┐
                 │   dedup +  │               │ ────────────► │ Telegram │
         ┌───────┴─────┐      └──────────────┘               │ channel  │
         │   SQLite    │                                      └──────────┘
         │ (data/*.db) │
         └─────────────┘
```

## Module Overview

```
bot/
  main.py              Entry point: polling loop, cycle orchestration, graceful shutdown
  config.py            Env-var parsing into a frozen Config dataclass
  sources.py           SourceSpec dataclass + sources.json loader/validator
  sources.json         News source definitions — the only file to touch to add a source
  schemas/             XSD files referenced by xml sources
  models.py            Article dataclass
  news_client.py       Generic feed client: fetch, schema-validate, map to Articles
  translator.py        DeepL wrapper: quota guard, truncation, original-text fallback
  telegram_sender.py   Raw Telegram Bot API (sendPhoto/sendMessage), caption building
  storage.py           SQLite: dedup tracking + translation cache
tests/                 Pytest suite (parsers, storage)
```

## News Sources

Every news source is fully described in a JSON file — `bot/sources.json` by default, overridable with `SOURCES_PATH` (in Docker, mount your own file and point `SOURCES_PATH` at it; see the commented example in `docker-compose.yml`). **Adding a new source means adding an entry to this file (plus an XSD file for xml sources) — no code changes.** The file is validated at startup; the bot exits with a clear error on any problem.

Each source entry:

| Field | Required | Description |
|---|---|---|
| `name` | yes | Unique id, `[a-z0-9_-]+`. Becomes the prefix of stored article ids (`<name>:<feed id>`) — **don't rename a source later**, or its posted-history is orphaned and old articles may be re-posted |
| `type` | yes | `json` or `xml` |
| `url` | yes | Feed URL |
| `language` | yes | Source language (DeepL code, e.g. `de`, `fr`). **`en` disables translation entirely** for this source |
| `category` | yes | The `#hashtag` posted with this source's messages (normalized: lowercase, `/` and `-` → `_`). Used when the feed item carries no category of its own via `mapping.category`, and by the `INCLUDE_CATEGORIES`/`SKIP_CATEGORIES` filters |
| `schema` | json only | Inline JSON Schema the fetched payload must satisfy. Keep it simple — require only the shape the mapping needs |
| `schema_file` | xml only | Path to an XSD, relative to the sources file. Keep it lax (`processContents="lax"`/`skip`) — constrain only the elements the mapping reads |
| `mapping` | yes | Where Article fields live in the payload, see below |
| `queries` | no | List of query-param objects; the URL is fetched once per entry and results merged (useful for APIs that need several calls, e.g. one per time window). The string `"{limit}"` is replaced with `NEWS_FETCH_LIMIT` |
| `url_base` | no | Prefixed onto extracted URLs that start with `/` |
| `namespaces` | xml only | XML namespace prefixes used in mapping paths, e.g. `{"media": "http://search.yahoo.com/mrss/"}` |

The `mapping` object (`items`, `id`, `title`, `url` required; `lead`, `image`, `published`, `category`, `id_pattern` optional):

- **json**: dot-separated key paths (`image.variants.big.src`), `items` pointing at the article array. `image` may be a list of paths — first non-empty wins.
- **xml**: ElementTree paths — `items` relative to the document root (`channel/item`), the rest relative to an item. An `@attr` suffix reads an attribute (`media:thumbnail@url`).
- `id`: the field whose value makes a record **unique** — it is the dedup key (stored as `<name>:<value>`), so pick something stable per article: a content id, an RSS `guid`, or the article URL. Items sharing an id are posted once.
- `id_pattern`: optional regex applied to the extracted id value; the first capture group (or the whole match if there is no group) becomes the unique key. Useful to cut a stable token out of a long guid URL, e.g. `"id": "guid"` + `"id_pattern": "articles/([a-z0-9]+)"` turns `https://www.bbc.co.uk/news/articles/c36dnz1zez5o#0` into `c36dnz1zez5o`. If the regex doesn't match, the full value is used. Changing `id` or `id_pattern` on a live source changes its stored keys — recent articles may be re-posted once.
- `published_format`: `iso8601` (default) or `rfc822` (RSS `pubDate`).

Extracted categories are normalized to hashtag form (`sport/wm-2026` → `sport_wm_2026`). Items missing id, title, or url are skipped individually; a payload failing schema validation skips the **whole source** for that cycle (a schema mismatch means the feed changed shape — check the logs).

Article ids are stored namespaced by source name. `SKIP_INITIAL_BACKLOG` applies **per source**: adding a new source to a long-running deployment marks that source's current feed as seen instead of flooding the channel.

## Configuration Reference

All configuration is via environment variables (or `.env` for Docker Compose). The bot exits immediately with a clear error if a required variable is missing or a value is invalid.

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | *required* | Bot token from @BotFather |
| `TELEGRAM_CHANNEL_ID` | *required* | `@handle` or numeric `-100...` id; bot must be a channel admin |
| `DEEPL_API_KEY` | *required* | DeepL API authentication key |
| `POLL_INTERVAL_MINUTES` | `30` | Minutes between polling cycles |
| `NEWS_FETCH_LIMIT` | `10` | Max articles kept per source and per cycle; also substituted for `"{limit}"` in source query params |
| `SOURCES_PATH` | `bot/sources.json` | News source definitions file (see [News Sources](#news-sources)) |
| `DB_PATH` | `data/posted.db` | SQLite database location (directory is auto-created; `/app/data/posted.db` in Docker) |
| `TRANSLATE_LEAD` | `true` | Also translate the lead paragraph. `false` = titles only (~4× less DeepL usage) |
| `LEAD_MAX_CHARS` | `300` | Leads are truncated to this length *before* translation (video items carry very long transcript leads) |
| `MAX_POSTS_PER_CYCLE` | `5` | Cap on posts per cycle; excess articles are queued for the next cycle |
| `SKIP_INITIAL_BACKLOG` | `true` | On a source's first run (no posts from it yet), mark its current articles as seen instead of flooding the channel |
| `SKIP_CATEGORIES` | *(empty)* | Comma-separated category paths to exclude, matched by prefix: `sport` skips `sport` and every subcategory like `sport/wm-2026-in-usa`. Entries are normalized (lowercase, `/` and `-` → `_`), so URL-path and hashtag forms both work. Ignored when `INCLUDE_CATEGORIES` is set |
| `INCLUDE_CATEGORIES` | *(empty)* | Comma-separated category paths to post exclusively (same format and prefix matching). When set, only matching articles are posted and `SKIP_CATEGORIES` is ignored |
| `POST_STYLE` | `photo_full` | `photo_full` (image + title + lead), `photo_short` (image + title), `text_full` (title + lead, link preview disabled), `text_short` (title only) |

Boolean variables accept `1/true/yes/on` (case-insensitive); anything else is `false`.

With a `*_short` style, leads are never displayed, so the bot skips translating them regardless of `TRANSLATE_LEAD`. With a `text_*` style, the link preview is disabled so no image appears via the "Read more" link.

### Categories

Every posted message carries a `#hashtag`: the article's category from the feed via the `category` mapping path (normalized to hashtag form: `/` and `-` → `_`), falling back to the source's required `category` field when the feed item carries none. The easiest way to discover a source's categories is to watch its posts (or its feed data directly).

Category filters apply globally across all sources. Both lists match by prefix, so a top-level category covers all of its subcategories (`sport` also matches `sport_fussball`):

- Both lists empty → every category is posted.
- `INCLUDE_CATEGORIES` set → **only** matching articles are posted; `SKIP_CATEGORIES` is ignored (a warning is logged if both are set). Remember to include each source's `category` (or its feed categories), or that source goes silent.
- Only `SKIP_CATEGORIES` set → everything except matching articles is posted.

## Running Locally (without Docker)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHANNEL_ID=...
export DEEPL_API_KEY=...

python -m bot.main
```

Stop with `Ctrl+C` — the bot shuts down gracefully on `SIGINT`/`SIGTERM` (finishing the current cycle's cleanup and closing HTTP clients and the database).

In Docker, the SQLite database is persisted in the named volume `bot-data`, so restarts and image rebuilds do not cause duplicate posts. `docker compose down -v` wipes it (the next start is treated as a first run).

## Message Format

Messages are sent with HTML parse mode; all dynamic content is HTML-escaped. Captions are truncated to Telegram's 1024-character photo-caption limit (4096 for text messages).

> **Bold translated title**
>
> Translated lead paragraph (truncated to `LEAD_MAX_CHARS`, omitted in `*_short` styles).
>
> [Read more](https://example.com/article-url) | #category_hashtag

The hashtag is derived from the article's category path, e.g. `sport/wm-2026-in-usa` → `#sport_wm_2026_in_usa`.

## DeepL Quota Management

The free tier allows 500K characters/month. Built-in safeguards:

- **Lead truncation** to `LEAD_MAX_CHARS` happens before translation, bounding per-article cost.
- **Usage is checked before each translation**: a warning is logged at 80% of the monthly limit; at 95% the bot stops translating and posts the original text.
- **Translations are cached** in SQLite, so retried posts (e.g. after a Telegram outage) cost nothing.
- **English sources are never sent to DeepL** (`"language": "en"` in the source definition).
- `TRANSLATE_LEAD=false` or a `*_short` post style reduces usage to titles only.
- Current usage is logged after every cycle that posts something.

## Failure Behavior

| Failure | Behavior |
|---|---|
| Source unreachable / bad payload | Log a warning, skip that query (or source), other sources continue; retry next interval |
| Payload fails schema validation | Log an error, skip that source for the cycle (the feed likely changed shape) |
| Single article fails to parse | Skip that article, continue with the rest |
| DeepL error or quota exhausted | Post the original untranslated text |
| Telegram HTTP 429 (rate limit) | Sleep the server-provided `retry_after`, retry once |
| Telegram rejects the image | Fall back to a text message with a large link preview |
| Telegram send fails outright | Article is *not* marked as posted — retried next cycle (translation already cached) |
| Unexpected error in a cycle | Logged with traceback; the loop continues on the next interval |

Rows for posted articles older than 30 days are pruned from SQLite after each cycle.

## Development Notes

- No Telegram bot framework is used — the bot only needs two Bot API methods, called directly via httpx.
- The DeepL SDK is synchronous; calls are wrapped in `asyncio.to_thread()` to keep the event loop responsive.
- The SQLite schema is a single `articles` table with string ids namespaced by source name; `posted_at IS NULL` distinguishes "translated but not yet posted" from "posted".
- XML feeds are parsed with the stdlib `ElementTree`; `xmlschema` is used only for XSD validation, `jsonschema` for JSON payloads.
- CI (`.github/workflows/docker.yml`) compiles all modules, runs pytest (`tests/`), builds the Docker image on every push/PR, and pushes `yeshdmits/news-tg-bot:latest` + `:<sha>` to Docker Hub on pushes to `main`.

### Tests

```bash
.venv/bin/pip install pytest
.venv/bin/python -m pytest
```

### Smoke test (no credentials required)

All configured sources can be fetched, validated, and parsed against the live feeds without any API keys:

```bash
.venv/bin/python - <<'EOF'
import asyncio
from bot.news_client import NewsClient
from bot.sources import load_sources

async def main():
    client = NewsClient(fetch_limit=10)
    for spec in load_sources("bot/sources.json"):
        articles = await client.fetch_articles(spec)
        print(f"--- {spec.name}: {len(articles)} articles")
        for a in articles[:3]:
            print(a.content_id, a.title)
    await client.close()

asyncio.run(main())
EOF
```
