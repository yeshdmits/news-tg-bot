"""Posting to a Telegram channel via the raw Bot API (sendPhoto/sendMessage)."""

from __future__ import annotations

import asyncio
import html
import logging

import httpx

from .models import Article

logger = logging.getLogger(__name__)

CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096


def build_caption(article: Article, limit: int = CAPTION_LIMIT) -> str:
    title = html.escape(article.display_title)
    lead = html.escape(article.display_lead)
    footer = f'\n\n<a href="{html.escape(article.url, quote=True)}">Read more</a>'
    if article.category:
        footer += f" | #{article.category}"

    caption = f"<b>{title}</b>"
    budget = limit - len(caption) - len(footer)
    if lead and budget > 20:
        if len(lead) + 2 > budget:
            lead = lead[: budget - 3].rsplit(" ", 1)[0] + "…"
        caption += f"\n\n{lead}"
    return caption + footer


class TelegramSender:
    def __init__(self, bot_token: str, channel_id: str) -> None:
        self._api_base = f"https://api.telegram.org/bot{bot_token}"
        self._channel_id = channel_id
        self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, payload: dict) -> bool:
        """Call a Bot API method; on 429 sleep retry_after and retry once."""
        for attempt in (1, 2):
            try:
                response = await self._client.post(
                    f"{self._api_base}/{method}", json=payload
                )
            except httpx.HTTPError as exc:
                logger.warning("Telegram %s network error: %s", method, exc)
                return False

            if response.status_code == 429 and attempt == 1:
                try:
                    retry_after = response.json()["parameters"]["retry_after"]
                except (ValueError, KeyError):
                    retry_after = 5
                logger.warning("Telegram rate limit, sleeping %ss", retry_after)
                await asyncio.sleep(retry_after + 1)
                continue

            if response.status_code == 200:
                return True

            logger.warning(
                "Telegram %s failed (%d): %s",
                method,
                response.status_code,
                response.text[:300],
            )
            return False
        return False

    async def send_article(self, article: Article) -> bool:
        caption = build_caption(article)
        if article.image_url:
            ok = await self._call(
                "sendPhoto",
                {
                    "chat_id": self._channel_id,
                    "photo": article.image_url,
                    "caption": caption,
                    "parse_mode": "HTML",
                },
            )
            if ok:
                return True
            logger.info(
                "sendPhoto failed for %d, falling back to text message",
                article.content_id,
            )
        return await self._call(
            "sendMessage",
            {
                "chat_id": self._channel_id,
                "text": build_caption(article, limit=TEXT_LIMIT),
                "parse_mode": "HTML",
                "link_preview_options": {"url": article.url, "prefer_large_media": True},
            },
        )
