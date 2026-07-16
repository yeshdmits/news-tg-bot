# 20min.ch Telegram News Bot

A bot that monitors [20min.ch](https://www.20min.ch/) (Swiss German-language news) for trending articles, translates them to English via DeepL, and posts them to a Telegram channel as rich messages (image + title + summary + link).

## How It Works

The bot polls the public 20min.ch content API on a fixed interval (default: every 30 minutes):

```
GET https://api.20min.ch/kaia/v1/most-consumed?tenantId=6&limit=10&timeFrame=6h
```

Since the API exposes no "latest articles" endpoint, each cycle queries three time windows (`1h`, `6h`, `24h`) and deduplicates the results by `contentId`. Each cycle then:

1. **Filters** out articles already posted, using a local SQLite database.
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

## Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.11+ (3.12 recommended) | Or Docker — no local Python needed then |
| Telegram bot token | Create a bot via [@BotFather](https://t.me/BotFather) (`/newbot`) |
| Telegram channel | The bot must be added to the channel as an **administrator** with permission to post messages |
| DeepL API key | Free tier (500K chars/month) is sufficient — sign up at [deepl.com/pro-api](https://www.deepl.com/pro-api) |

To find a private channel's numeric ID (format `-100xxxxxxxxxx`), forward one of its messages to [@userinfobot](https://t.me/userinfobot), or use the channel's public `@handle` directly.

## Configuration

All configuration is via environment variables (or an `.env` file for Docker Compose). Copy the template first:

```bash
cp .env.example .env
```

### Required

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather, e.g. `123456:ABC-DEF...` |
| `TELEGRAM_CHANNEL_ID` | Target channel: `@mychannel` or numeric `-1001234567890` |
| `DEEPL_API_KEY` | DeepL API authentication key |

The bot exits immediately with a clear error message if any of these is missing.

### Optional

| Variable | Default | Description |
|---|---|---|
| `POLL_INTERVAL_MINUTES` | `30` | Minutes between polling cycles |
| `NEWS_FETCH_LIMIT` | `10` | Articles requested per API time window (×3 windows per cycle) |
| `DB_PATH` | `data/posted.db` | SQLite database location (directory is auto-created) |
| `TRANSLATE_LEAD` | `true` | Also translate the lead paragraph. Set to `false` to translate titles only (~4× less DeepL usage) |
| `LEAD_MAX_CHARS` | `300` | Leads are truncated to this length *before* translation (video items carry very long transcript leads) |
| `MAX_POSTS_PER_CYCLE` | `5` | Cap on posts per cycle; excess articles are queued for the next cycle |
| `SKIP_INITIAL_BACKLOG` | `true` | On the very first run (empty database), mark current articles as seen instead of flooding the channel with them |

Boolean variables accept `1/true/yes/on` (case-insensitive); anything else is `false`.

## Running

### Docker Compose (recommended)

```bash
cp .env.example .env   # then fill in the three required variables
docker compose up -d
docker compose logs -f
```

The SQLite database is persisted in the named volume `bot-data`, so restarts and image rebuilds do not cause duplicate posts.

### Locally

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

### First run behavior

With `SKIP_INITIAL_BACKLOG=true` (the default), the first cycle marks all currently-trending articles as already seen **without posting them**. Posting begins with articles that appear after startup. Set it to `false` if you want the current backlog posted immediately (capped at `MAX_POSTS_PER_CYCLE`).

## Message Format

Messages are sent with HTML parse mode; all dynamic content is HTML-escaped. Captions are truncated to Telegram's 1024-character photo-caption limit.

> **Bold translated title**
>
> Translated lead paragraph (truncated to `LEAD_MAX_CHARS`).
>
> [Read more](https://www.20min.ch/...) | #category_hashtag

The hashtag is derived from the article's category path, e.g. `sport/wm-2026-in-usa` → `#sport_wm_2026_in_usa`.

## DeepL Quota Management

The free tier allows 500K characters/month. Built-in safeguards:

- **Lead truncation** to `LEAD_MAX_CHARS` happens before translation, bounding per-article cost.
- **Usage is checked before each translation**: a warning is logged at 80% of the monthly limit; at 95% the bot stops translating and posts the original German text.
- **Translations are cached** in SQLite, so retried posts (e.g. after a Telegram outage) cost nothing.
- `TRANSLATE_LEAD=false` reduces usage to titles only if you need a bigger margin.
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

## Project Structure

```
bot/
  main.py              Entry point: polling loop, cycle orchestration, graceful shutdown
  config.py            Env-var parsing into a frozen Config dataclass
  models.py            Article dataclass
  news_client.py       20min.ch API client (httpx, async)
  translator.py        DeepL wrapper: quota guard, truncation, German fallback
  telegram_sender.py   Raw Telegram Bot API (sendPhoto/sendMessage), caption building
  storage.py           SQLite: dedup tracking + translation cache
requirements.txt       httpx, deepl (the only runtime dependencies)
Dockerfile             python:3.12-slim, data volume at /app/data
docker-compose.yml     restart policy, named volume, env_file wiring
.env.example           Documented configuration template
PLAN.md                Original design document
```

## Development Notes

- No Telegram bot framework is used — the bot only needs two Bot API methods, called directly via httpx.
- The DeepL SDK is synchronous; calls are wrapped in `asyncio.to_thread()` to keep the event loop responsive.
- The SQLite schema is a single `articles` table; `posted_at IS NULL` distinguishes "translated but not yet posted" from "posted".

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
