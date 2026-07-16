# Plan: 20min.ch Telegram News Bot

## Context

Build a Telegram bot that monitors https://www.20min.ch/ (Swiss German-language news) for new articles, translates them to English via DeepL, and posts rich messages (image + title + summary + link) to a Telegram channel every 30 minutes.

**Key discovery**: 20min.ch has a public JSON API — no scraping needed:
```
GET https://api.20min.ch/kaia/v1/most-consumed?tenantId=6&limit=10&timeFrame=6h
```
No "latest articles" endpoint exists, so the bot queries multiple time windows (1h, 6h, 24h) and deduplicates by `content_id`.

## Project Structure

```
tg-news/
  bot/
    __init__.py
    main.py              # entry point, async polling loop
    config.py            # env vars loaded into frozen dataclass
    models.py            # Article dataclass
    news_client.py       # 20min.ch API client (httpx)
    translator.py        # DeepL translation wrapper
    telegram_sender.py   # sendPhoto/sendMessage via raw Telegram Bot API
    storage.py           # SQLite dedup tracking
  requirements.txt       # httpx, deepl (only 2 deps)
  Dockerfile
  docker-compose.yml
  .env.example
  .gitignore
```

## Implementation Steps

### 1. Foundation (`config.py`, `models.py`, `__init__.py`)
- `Config` frozen dataclass with 3 required env vars: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHANNEL_ID`, `DEEPL_API_KEY`
- Optional overrides: `POLL_INTERVAL_MINUTES`, `NEWS_FETCH_LIMIT`, `DB_PATH`, `TRANSLATE_LEAD`
- `Article` dataclass with fields: `content_id`, `title`, `lead`, `url`, `image_url`, `published_at`, `category`, `title_en`, `lead_en`

### 2. Storage (`storage.py`)
- SQLite with single table `posted_articles (content_id INTEGER PK, title TEXT, posted_at TEXT)`
- Methods: `is_posted()`, `mark_posted()`, `cleanup_old(days=30)`
- Auto-creates `data/` directory and DB file

### 3. News Client (`news_client.py`)
- `httpx.AsyncClient` against `api.20min.ch/kaia/v1/most-consumed`
- `fetch_articles()` queries timeFrames `1h`, `6h`, `24h`, deduplicates by `content_id`, sorts newest-first
- Parses image URL from `image.variants.big.src`, prepends `https://www.20min.ch` to relative article URLs

### 4. Translator (`translator.py`)
- Uses `deepl` Python library, batches title + lead in one `translate_text()` call
- Falls back to original German text on failure
- Logs DeepL usage stats after each cycle
- Config flag `TRANSLATE_LEAD` (default true) — can disable to stay within free tier budget

### 5. Telegram Sender (`telegram_sender.py`)
- Raw Telegram Bot API via httpx (no telegram library — we only need sendPhoto + sendMessage)
- HTML parse mode, category as hashtag (e.g. `#schweiz`, `#sport_fussball`)
- Caption truncation to 1024 chars (Telegram limit)
- Rate limit handling: sleep `retry_after` on 429, retry once
- 2-second delay between posts

### 6. Main Loop (`main.py`)
- `while True` + `asyncio.sleep(interval)` — no scheduler library needed
- Each cycle: fetch → filter (dedup via SQLite) → translate → post
- DeepL sync calls wrapped in `asyncio.to_thread()`
- Graceful shutdown on KeyboardInterrupt/SIGINT

### 7. Deployment Files
- `Dockerfile`: python:3.12-slim, volume at `/app/data` for SQLite
- `docker-compose.yml`: `restart: unless-stopped`, named volume, env_file
- `.env.example`: documents all env vars
- `.gitignore`: `__pycache__/`, `.env`, `data/`, `*.db`, `.venv/`

## DeepL Free Tier Budget

Free tier = 500K chars/month. With both title+lead translated (~260 chars/article, ~3 new articles per 30-min cycle):
- Estimated: ~936K chars/month — **over budget by ~2x**

Mitigations built into the design:
- `TRANSLATE_LEAD=false` config flag reduces to ~216K chars/month (within budget)
- Usage logging after each cycle with warning at 80% of monthly limit
- Fallback to German text if quota exceeded

## Error Handling

| Failure | Behavior |
|---------|----------|
| 20min.ch API down | Log, skip cycle, retry next interval |
| DeepL failure/quota | Use original German text |
| Telegram 429 | Sleep retry_after, retry once |
| Telegram send failure | Skip (don't mark posted — retries next cycle) |
| Article parse error | Skip that article, continue others |

## Improvements Made During Implementation

- **Translation cache**: translations are stored in SQLite alongside dedup state, so a failed Telegram send never re-spends DeepL quota on retry.
- **Lead truncation** (`LEAD_MAX_CHARS`, default 300): video items carry full transcript leads (800+ chars); truncating before translation fixes the quota budget and keeps captions tidy.
- **DeepL quota guard**: usage is checked before translating — warning at 80%, automatic fallback to German at 95%.
- **First-run backlog suppression** (`SKIP_INITIAL_BACKLOG`, default true) and a per-cycle post cap (`MAX_POSTS_PER_CYCLE`, default 5) prevent channel flooding.
- **HTML escaping** of title/lead/URL for Telegram's HTML parse mode.
- **sendPhoto → sendMessage fallback** when the image is missing or rejected by Telegram.
- Files live at the repo root (`bot/`), not a nested `tg-news/` directory.

## Verification

1. Run `python -m bot.main` with real env vars — confirm articles are fetched, translated, and posted
2. Run a second time — confirm no duplicate posts (SQLite dedup works)
3. Check DeepL usage via `translator.get_usage()`
4. Test with `TRANSLATE_LEAD=false` to verify budget-safe mode
5. `docker compose up` to verify containerized deployment
