"""Entry point: poll configured news sources, translate new articles, post to Telegram."""

from __future__ import annotations

import asyncio
import logging
import signal

from .config import Config, ConfigError
from .models import Article
from .news_client import NewsClient
from .sources import SourceSpec, load_sources
from .storage import Storage
from .telegram_sender import TelegramSender
from .translator import Translator

logger = logging.getLogger("bot")

POST_DELAY_SECONDS = 2


async def _translate(
    translator: Translator, storage: Storage, article: Article
) -> None:
    if article.language == "en":
        # display_title/display_lead fall back to the original text.
        return
    cached = storage.get_translation(article.content_id)
    if cached:
        article.title_en, article.lead_en = cached
        return
    title_en, lead_en = await asyncio.to_thread(
        translator.translate, article.title, article.lead, article.language
    )
    article.title_en, article.lead_en = title_en, lead_en
    storage.save_translation(article.content_id, article.title, title_en, lead_en)


def _skip_backlog_for_new_sources(
    storage: Storage, new_articles: list[Article]
) -> list[Article]:
    """Mark articles of never-posted sources as seen without sending.

    Applies per source, so adding a source to a running deployment doesn't
    flood the channel with the feed's whole backlog.
    """
    fresh_sources = {
        source
        for source in {a.source for a in new_articles}
        if not storage.has_any_posts(source)
    }
    if not fresh_sources:
        return new_articles
    skipped = [a for a in new_articles if a.source in fresh_sources]
    logger.info(
        "First run for %s: marking %d articles as posted without sending "
        "(set SKIP_INITIAL_BACKLOG=false to post the backlog)",
        ", ".join(sorted(fresh_sources)),
        len(skipped),
    )
    for article in skipped:
        storage.mark_posted(article.content_id, article.title)
    return [a for a in new_articles if a.source not in fresh_sources]


async def run_cycle(
    config: Config,
    specs: list[SourceSpec],
    news: NewsClient,
    translator: Translator,
    sender: TelegramSender,
    storage: Storage,
    queue: str = "news",
) -> None:
    articles: list[Article] = []
    for spec in specs:
        try:
            fetched = await news.fetch_articles(spec)
        except Exception:
            logger.exception("Source %s: fetch failed", spec.name)
            continue
        if not fetched:
            logger.warning("Source %s: no articles this cycle", spec.name)
            continue
        articles.extend(fetched)
    if not articles:
        logger.warning("Queue %s: no articles fetched this cycle", queue)
        return

    # Merge sources chronologically, newest first.
    articles.sort(
        key=lambda a: a.published_at.timestamp() if a.published_at else 0.0,
        reverse=True,
    )

    if config.include_categories or config.skip_categories:
        kept = [a for a in articles if config.is_category_allowed(a.category)]
        if len(kept) < len(articles):
            logger.info(
                "Skipped %d articles by category filter (%s)",
                len(articles) - len(kept),
                f"include: {', '.join(config.include_categories)}"
                if config.include_categories
                else f"skip: {', '.join(config.skip_categories)}",
            )
        articles = kept

    new_articles = [a for a in articles if not storage.is_posted(a.content_id)]
    logger.info(
        "Queue %s: fetched %d articles, %d new", queue, len(articles), len(new_articles)
    )

    if new_articles and config.skip_initial_backlog:
        new_articles = _skip_backlog_for_new_sources(storage, new_articles)

    if len(new_articles) > config.max_posts_per_cycle:
        logger.info(
            "Queue %s: capping posts to %d this cycle (%d queued for later)",
            queue,
            config.max_posts_per_cycle,
            len(new_articles) - config.max_posts_per_cycle,
        )
        new_articles = new_articles[: config.max_posts_per_cycle]

    needs_deepl = any(a.language != "en" for a in new_articles)

    # Post oldest first so the channels read chronologically.
    for article in reversed(new_articles):
        try:
            await _translate(translator, storage, article)
        except Exception:
            logger.exception("Unexpected translation error for %s", article.content_id)
        chat_id = config.channel_for(article.channel)
        if chat_id is None:
            # Guarded at startup; only reachable if sources changed mid-run.
            logger.error(
                "No channel configured for key %r, skipping %s",
                article.channel,
                article.content_id,
            )
            continue
        if await sender.send_article(article, chat_id):
            storage.mark_posted(article.content_id, article.title)
            logger.info("Posted %s: %s", article.content_id, article.display_title)
        else:
            logger.warning(
                "Failed to post %s, will retry next cycle", article.content_id
            )
        await asyncio.sleep(POST_DELAY_SECONDS)

    if needs_deepl:
        await asyncio.to_thread(translator.log_usage)
    storage.cleanup_old(days=30)


async def _run_queue(
    config: Config,
    queue: str,
    specs: list[SourceSpec],
    interval_minutes: int,
    news: NewsClient,
    translator: Translator,
    sender: TelegramSender,
    storage: Storage,
    stop: asyncio.Event,
) -> None:
    logger.info(
        "Queue %s: %s — posting every %d min",
        queue,
        ", ".join(spec.name for spec in specs),
        interval_minutes,
    )
    while not stop.is_set():
        try:
            await run_cycle(config, specs, news, translator, sender, storage, queue)
        except Exception:
            logger.exception("Queue %s: cycle failed, retrying next interval", queue)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval_minutes * 60)
        except asyncio.TimeoutError:
            pass


async def run(config: Config, specs: list[SourceSpec]) -> None:
    storage = Storage(config.db_path)
    news = NewsClient(fetch_limit=config.news_fetch_limit)
    translator = Translator(
        config.deepl_api_key,
        # Leads hidden by the post style are never displayed — don't translate them.
        config.translate_lead and config.post_full_text,
        config.lead_max_chars,
    )
    sender = TelegramSender(
        config.telegram_bot_token,
        with_image=config.post_with_image,
        full_text=config.post_full_text,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    if config.include_categories:
        category_filter = "include only " + ",".join(config.include_categories)
        if config.skip_categories:
            logger.warning(
                "Both INCLUDE_CATEGORIES and SKIP_CATEGORIES are set; "
                "SKIP_CATEGORIES is ignored"
            )
    elif config.skip_categories:
        category_filter = "skip " + ",".join(config.skip_categories)
    else:
        category_filter = "all categories"
    logger.info(
        "Bot started: sources: %s, translate_lead=%s, max %d posts/cycle, "
        "style=%s, categories: %s",
        ", ".join(spec.name for spec in specs),
        config.translate_lead,
        config.max_posts_per_cycle,
        config.post_style,
        category_filter,
    )

    en_specs = [s for s in specs if s.language == "en"]
    translated_specs = [s for s in specs if s.language != "en"]
    queues = []
    if en_specs:
        queues.append(("english", en_specs, config.en_post_interval_minutes))
    if translated_specs:
        queues.append(
            ("translated", translated_specs, config.translated_post_interval_minutes)
        )
    try:
        await asyncio.gather(
            *(
                _run_queue(
                    config, queue, queue_specs, interval,
                    news, translator, sender, storage, stop,
                )
                for queue, queue_specs, interval in queues
            )
        )
    finally:
        logger.info("Shutting down")
        await news.close()
        await sender.close()
        storage.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = Config.from_env()
        specs = load_sources(config.sources_path)
        missing = sorted(
            {s.channel for s in specs if config.channel_for(s.channel) is None}
        )
        if missing:
            raise ConfigError(
                "No Telegram channel configured for channel key(s): "
                + ", ".join(missing)
                + " — set "
                + " / ".join(f"{key.upper()}_TELEGRAM_CHANNEL_ID" for key in missing)
                + " (or TELEGRAM_CHANNEL_ID as a fallback for all channels)"
            )
    except ConfigError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc
    asyncio.run(run(config, specs))


if __name__ == "__main__":
    main()
