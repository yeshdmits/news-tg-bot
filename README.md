# Telegram News Bot

Watches the news sources defined in [`bot/sources.json`](bot/sources.json), translates non-English articles to English with DeepL, and posts them to your Telegram channel. Every source — a JSON API or an XML/RSS feed — is fully described by that config file (URL, payload schema, field mapping, language), so adding a new source means editing `sources.json` only, no code changes — see [bot/README.md](bot/README.md#news-sources).

## Quick Start

You need three things:

1. **Bot token** — create a bot with [@BotFather](https://t.me/BotFather) (`/newbot`)
2. **Channel** — add your bot to it as an **administrator** (with post permission)
3. **DeepL API key** — free at [deepl.com/pro-api](https://www.deepl.com/pro-api) (500K chars/month is plenty)

Then:

```bash
cp .env.example .env   # fill in the three required variables
docker compose up -d
docker compose logs -f
```

That's it. The bot checks the configured sources every 30 minutes and posts new articles.

> **No posts right away?** By default the first run marks current articles as seen without posting (to avoid flooding your channel). Set `SKIP_INITIAL_BACKLOG=false` in `.env` to post immediately.

## Configuration

Required (in `.env`):

| Variable | Description |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather |
| `TELEGRAM_CHANNEL_ID` | `@mychannel`, or numeric `-100...` for private channels (forward a channel message to [@userinfobot](https://t.me/userinfobot) to find it) |
| `DEEPL_API_KEY` | DeepL API key |

Most useful options (all optional, see `.env.example` for the full list):

| Variable | Default | Description |
|---|---|---|
| `POST_STYLE` | `photo_full` | `photo_full` (image + title + summary), `photo_short` (image + title), `text_full`, `text_short` |
| `SKIP_CATEGORIES` | *(empty)* | Categories to skip, comma-separated — e.g. `sport` skips all sport news. Categories come from each source's feed (the hashtag on posted messages), or from a source's `default_category` in `sources.json` |
| `INCLUDE_CATEGORIES` | *(empty)* | Post **only** these categories (same format). When set, `SKIP_CATEGORIES` is ignored |
| `POLL_INTERVAL_MINUTES` | `30` | How often to check for news |
| `MAX_POSTS_PER_CYCLE` | `5` | Max posts per check (extras are queued) |
| `SKIP_INITIAL_BACKLOG` | `true` | Don't post the existing backlog on first start |

After changing `.env`, apply with `docker compose up -d`.

## More

Technical documentation — architecture, failure handling, DeepL quota management, running without Docker — lives in [bot/README.md](bot/README.md).
