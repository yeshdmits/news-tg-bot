# Technical Documentation

User-facing setup instructions are in the [root README](../README.md). This document covers internals: architecture, configuration reference, failure behavior, and development notes.

## How It Works

The bot polls the public 20min.ch content API on a fixed interval (default: every 30 minutes):

```
GET https://api.20min.ch/kaia/v1/most-consumed?tenantId=6&limit=10&timeFrame=6h
```

Since the API exposes no "latest articles" endpoint, each cycle queries three time windows (`1h`, `6h`, `24h`) and deduplicates the results by `contentId`. Each cycle then:

1. **Filters** out articles in skipped categories (`SKIP_CATEGORIES`) and articles already posted, using a local SQLite database.
2. **Translates** title and lead from German to English via DeepL (`EN-US`). Translations are cached in SQLite so a failed Telegram send never re-spends DeepL quota on retry.
3. **Posts** each new article to the channel via `sendPhoto` (falling back to `sendMessage` with a link preview if the image is missing or rejected), oldest first, with a 2-second delay between posts.

```
┌─────────────┐    fetch     ┌──────────────┐   translate   ┌─────────┐
│ 20min.ch API │ ──────────► │   main loop   │ ────────────► │  DeepL  │
└─────────────┘              │  (30 min tick)│ ◄──────────── └─────────┘
                             │               │
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
  models.py            Article dataclass
  news_client.py       20min.ch API client (httpx, async)
  translator.py        DeepL wrapper: quota guard, truncation, German fallback
  telegram_sender.py   Raw Telegram Bot API (sendPhoto/sendMessage), caption building
  storage.py           SQLite: dedup tracking + translation cache
```

## Configuration Reference

All configuration is via environment variables (or `.env` for Docker Compose). The bot exits immediately with a clear error if a required variable is missing or a value is invalid.

| Variable | Default | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | *required* | Bot token from @BotFather |
| `TELEGRAM_CHANNEL_ID` | *required* | `@handle` or numeric `-100...` id; bot must be a channel admin |
| `DEEPL_API_KEY` | *required* | DeepL API authentication key |
| `POLL_INTERVAL_MINUTES` | `30` | Minutes between polling cycles |
| `NEWS_FETCH_LIMIT` | `10` | Articles requested per API time window (×3 windows per cycle) |
| `DB_PATH` | `data/posted.db` | SQLite database location (directory is auto-created; `/app/data/posted.db` in Docker) |
| `TRANSLATE_LEAD` | `true` | Also translate the lead paragraph. `false` = titles only (~4× less DeepL usage) |
| `LEAD_MAX_CHARS` | `300` | Leads are truncated to this length *before* translation (video items carry very long transcript leads) |
| `MAX_POSTS_PER_CYCLE` | `5` | Cap on posts per cycle; excess articles are queued for the next cycle |
| `SKIP_INITIAL_BACKLOG` | `true` | On the very first run (empty database), mark current articles as seen instead of flooding the channel |
| `SKIP_CATEGORIES` | *(empty)* | Comma-separated category paths to exclude, matched by prefix: `sport` skips `sport` and every subcategory like `sport/wm-2026-in-usa`. Entries are normalized (lowercase, `/` and `-` → `_`), so URL-path and hashtag forms both work. Ignored when `INCLUDE_CATEGORIES` is set |
| `INCLUDE_CATEGORIES` | *(empty)* | Comma-separated category paths to post exclusively (same format and prefix matching). When set, only matching articles are posted and `SKIP_CATEGORIES` is ignored |
| `POST_STYLE` | `photo_full` | `photo_full` (image + title + lead), `photo_short` (image + title), `text_full` (title + lead, link preview disabled), `text_short` (title only) |

Boolean variables accept `1/true/yes/on` (case-insensitive); anything else is `false`.

With a `*_short` style, leads are never displayed, so the bot skips translating them regardless of `TRANSLATE_LEAD`. With a `text_*` style, the link preview is disabled so no image appears via the "Read more" link.

### Categories

The API has no endpoint listing all categories — an article's category is its section path on 20min.ch (the API's `mainCategoryFullUrlPath` field, which is also the first part of the article URL, e.g. `www.20min.ch/sport/fussball/...` → `sport/fussball`).

Filtering rules (both lists match by prefix, so a top-level category covers all of its subcategories):

- Both lists empty → every category is posted.
- `INCLUDE_CATEGORIES` set → **only** matching articles are posted; `SKIP_CATEGORIES` is ignored (a warning is logged if both are set). Articles without a category are excluded.
- Only `SKIP_CATEGORIES` set → everything except matching articles is posted.

Top-level categories observed in trending articles:

| Category | Content | Subcategories seen |
|---|---|---|
| `schweiz` | Swiss national news | — |
| `ausland` | International news | `ukraine`, `donald-trump`, … |
| `wirtschaft` | Business & economy | — |
| `sport` | Sports | `fussball`, `tennis`, `wm-2026-in-usa`, … |
| `people` | Celebrity news | `festivals`, … |
| `lifestyle` | Lifestyle | `beauty`, `fashion`, `reisen`, `eatanddrink`, `living`, `bodyandsoul`, … |
| `wissen` | Science | — |
| `wetter` | Weather | `hitzewelle`, … |
| `regionen` | Regional news | `zuerich`, `bern`, `basel`, `ostschweiz`, `zentralschweiz`, … |
| `community` | Reader community | — |
| `faktenchecks` | Fact checks | — |

20min also runs temporary topic/campaign categories (e.g. `iran-nahost`, `sommer-deines-lebens`), so this list is not exhaustive. To see which categories are currently trending:

```bash
for tf in 1h 6h 24h; do
  curl -s "https://api.20min.ch/kaia/v1/most-consumed?tenantId=6&limit=100&timeFrame=$tf"; echo
done | python3 -c "
import json, sys
cats = set()
for line in sys.stdin:
    if line.strip():
        cats.update(i['mainCategoryFullUrlPath'] for i in json.loads(line)['items']
                    if i.get('mainCategoryFullUrlPath'))
print('\n'.join(sorted(cats)))"
```

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
> [Read more](https://www.20min.ch/...) | #category_hashtag

The hashtag is derived from the article's category path, e.g. `sport/wm-2026-in-usa` → `#sport_wm_2026_in_usa`.

## DeepL Quota Management

The free tier allows 500K characters/month. Built-in safeguards:

- **Lead truncation** to `LEAD_MAX_CHARS` happens before translation, bounding per-article cost.
- **Usage is checked before each translation**: a warning is logged at 80% of the monthly limit; at 95% the bot stops translating and posts the original German text.
- **Translations are cached** in SQLite, so retried posts (e.g. after a Telegram outage) cost nothing.
- `TRANSLATE_LEAD=false` or a `*_short` post style reduces usage to titles only.
- Current usage is logged after every cycle that posts something.

## Failure Behavior

| Failure | Behavior |
|---|---|
| 20min.ch API unreachable / bad JSON | Log a warning, skip that time window (or cycle), retry next interval |
| Single article fails to parse | Skip that article, continue with the rest |
| DeepL error or quota exhausted | Post the original German text |
| Telegram HTTP 429 (rate limit) | Sleep the server-provided `retry_after`, retry once |
| Telegram rejects the image | Fall back to a text message with a large link preview |
| Telegram send fails outright | Article is *not* marked as posted — retried next cycle (translation already cached) |
| Unexpected error in a cycle | Logged with traceback; the loop continues on the next interval |

Rows for posted articles older than 30 days are pruned from SQLite after each cycle.

## Development Notes

- No Telegram bot framework is used — the bot only needs two Bot API methods, called directly via httpx.
- The DeepL SDK is synchronous; calls are wrapped in `asyncio.to_thread()` to keep the event loop responsive.
- The SQLite schema is a single `articles` table; `posted_at IS NULL` distinguishes "translated but not yet posted" from "posted".
- CI (`.github/workflows/docker.yml`) compiles all modules, runs pytest if a `tests/` directory exists, builds the Docker image on every push/PR, and pushes `yeshdmits/news-tg-bot:latest` + `:<sha>` to Docker Hub on pushes to `main`.

### Smoke test (no credentials required)

The news client, caption builder, and storage layer can be exercised against the live API without any API keys:

```bash
.venv/bin/python - <<'EOF'
import asyncio
from bot.news_client import NewsClient

async def main():
    client = NewsClient(fetch_limit=10)
    articles = await client.fetch_articles()
    await client.close()
    for a in articles[:5]:
        print(a.content_id, a.title)

asyncio.run(main())
EOF
```
