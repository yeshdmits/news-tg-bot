"""Entry point: poll 20min.ch, translate new articles, post to Telegram."""

from __future__ import annotations

import asyncio
import logging
import signal

from .config import Config, ConfigError
from .models import Article
from .news_client import NewsClient
from .storage import Storage
from .telegram_sender import TelegramSender
from .translator import Translator

logger = logging.getLogger("bot")

POST_DELAY_SECONDS = 2


async def _translate(
    translator: Translator, storage: Storage, article: Article
) -> None:
    cached = storage.get_translation(article.content_id)
    if cached:
        article.title_en, article.lead_en = cached
        return
    title_en, lead_en = await asyncio.to_thread(
        translator.translate, article.title, article.lead
    )
    article.title_en, article.lead_en = title_en, lead_en
    storage.save_translation(article.content_id, article.title, title_en, lead_en)


async def run_cycle(
    config: Config,
    news: NewsClient,
    translator: Translator,
    sender: TelegramSender,
    storage: Storage,
) -> None:
    articles = await news.fetch_articles()
    if not articles:
        logger.warning("No articles fetched this cycle")
        return

    new_articles = [a for a in articles if not storage.is_posted(a.content_id)]
    logger.info("Fetched %d articles, %d new", len(articles), len(new_articles))

    if new_articles and config.skip_initial_backlog and not storage.has_any_posts():
        logger.info(
            "First run: marking %d existing articles as posted without sending "
            "(set SKIP_INITIAL_BACKLOG=false to post the backlog)",
            len(new_articles),
        )
        for article in new_articles:
            storage.mark_posted(article.content_id, article.title)
        return

    if len(new_articles) > config.max_posts_per_cycle:
        logger.info(
            "Capping posts to %d this cycle (%d queued for later)",
            config.max_posts_per_cycle,
            len(new_articles) - config.max_posts_per_cycle,
        )
        new_articles = new_articles[: config.max_posts_per_cycle]

    # Post oldest first so the channel reads chronologically.
    for article in reversed(new_articles):
        try:
            await _translate(translator, storage, article)
        except Exception:
            logger.exception("Unexpected translation error for %d", article.content_id)
        if await sender.send_article(article):
            storage.mark_posted(article.content_id, article.title)
            logger.info("Posted %d: %s", article.content_id, article.display_title)
        else:
            logger.warning(
                "Failed to post %d, will retry next cycle", article.content_id
            )
        await asyncio.sleep(POST_DELAY_SECONDS)

    await asyncio.to_thread(translator.log_usage)
    storage.cleanup_old(days=30)


async def run(config: Config) -> None:
    storage = Storage(config.db_path)
    news = NewsClient(fetch_limit=config.news_fetch_limit)
    translator = Translator(
        config.deepl_api_key, config.translate_lead, config.lead_max_chars
    )
    sender = TelegramSender(config.telegram_bot_token, config.telegram_channel_id)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    logger.info(
        "Bot started: polling every %d min, translate_lead=%s, max %d posts/cycle",
        config.poll_interval_minutes,
        config.translate_lead,
        config.max_posts_per_cycle,
    )
    try:
        while not stop.is_set():
            try:
                await run_cycle(config, news, translator, sender, storage)
            except Exception:
                logger.exception("Cycle failed, retrying next interval")
            try:
                await asyncio.wait_for(
                    stop.wait(), timeout=config.poll_interval_minutes * 60
                )
            except asyncio.TimeoutError:
                pass
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
    except ConfigError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc
    asyncio.run(run(config))


if __name__ == "__main__":
    main()
