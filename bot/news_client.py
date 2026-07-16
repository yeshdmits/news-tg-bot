"""Client for the public 20min.ch content API."""

from __future__ import annotations

import logging
from datetime import datetime

import httpx

from .models import Article

logger = logging.getLogger(__name__)

API_URL = "https://api.20min.ch/kaia/v1/most-consumed"
SITE_BASE = "https://www.20min.ch"
TENANT_ID = 6
TIME_FRAMES = ("1h", "6h", "24h")


def _parse_published_at(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _category_hashtag(path: str | None) -> str | None:
    """'sport/wm-2026-in-usa' -> 'sport_wm_2026_in_usa'"""
    if not path:
        return None
    tag = path.strip("/").replace("/", "_").replace("-", "_")
    return tag or None


def parse_item(item: dict) -> Article | None:
    content_id = item.get("contentId")
    title = (item.get("title") or "").strip()
    url = item.get("url") or ""
    if not content_id or not title or not url:
        return None
    if url.startswith("/"):
        url = SITE_BASE + url

    image_url = None
    variants = (item.get("image") or {}).get("variants") or {}
    for size in ("big", "small"):
        src = (variants.get(size) or {}).get("src")
        if src:
            image_url = src
            break

    return Article(
        content_id=int(content_id),
        title=title,
        lead=(item.get("lead") or "").strip(),
        url=url,
        image_url=image_url,
        published_at=_parse_published_at(item.get("publishedAt")),
        category=_category_hashtag(item.get("mainCategoryFullUrlPath")),
    )


class NewsClient:
    def __init__(self, fetch_limit: int = 10) -> None:
        self._fetch_limit = fetch_limit
        self._client = httpx.AsyncClient(
            timeout=20.0,
            headers={"User-Agent": "news-aggr-bot/1.0"},
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch_articles(self) -> list[Article]:
        """Fetch articles across several time windows, deduplicated by content_id."""
        seen: dict[int, Article] = {}
        for time_frame in TIME_FRAMES:
            try:
                response = await self._client.get(
                    API_URL,
                    params={
                        "tenantId": TENANT_ID,
                        "limit": self._fetch_limit,
                        "timeFrame": time_frame,
                    },
                )
                response.raise_for_status()
                items = response.json().get("items", [])
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning("Failed to fetch timeFrame=%s: %s", time_frame, exc)
                continue

            for item in items:
                try:
                    article = parse_item(item)
                except (TypeError, ValueError) as exc:
                    logger.warning("Skipping unparseable item: %s", exc)
                    continue
                if article and article.content_id not in seen:
                    seen[article.content_id] = article

        articles = list(seen.values())
        articles.sort(
            key=lambda a: a.published_at.timestamp() if a.published_at else 0.0,
            reverse=True,
        )
        return articles
