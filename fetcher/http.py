"""Conditional GET with retries and timeouts. No DB knowledge: the caller
supplies the stored validators and persists the returned ones."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
import structlog

log = structlog.get_logger(__name__)

USER_AGENT = "news-tg-bot/0.1"
RETRIES = 2
RETRY_DELAY_S = 2.0


class FetchError(RuntimeError):
    """Base fetch failure. ``error_class`` feeds error classification."""

    error_class = "FetchNetworkError"
    status: int | None = None


class FetchTimeout(FetchError):
    """The request timed out on every attempt."""

    error_class = "FetchTimeout"


class FetchHttpError(FetchError):
    """Non-success HTTP status; ``error_class`` splits 4xx from 5xx."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status
        self.error_class = "FetchHttpError4xx" if status < 500 else "FetchHttpError5xx"


@dataclass(frozen=True)
class ConditionalState:
    """Validators from the previous fetch, replayed as
    If-None-Match / If-Modified-Since."""

    etag: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True)
class FetchResult:
    """Outcome of a successful fetch (200 or 304), carrying the
    validators the caller should persist for the next cycle."""

    status_code: int
    body: bytes | None  # None on 304 Not Modified
    etag: str | None
    last_modified: str | None
    fetched_at: datetime

    @property
    def not_modified(self) -> bool:
        """True on a 304 — ``body`` is None and the stored validators still hold."""
        return self.status_code == 304


def make_client() -> httpx.AsyncClient:
    """AsyncClient configured for feed fetching (bot User-Agent,
    redirects, overall timeout). The caller owns its lifecycle."""
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        timeout=httpx.Timeout(20.0),
        follow_redirects=True,
    )


async def fetch_feed(
    client: httpx.AsyncClient, url: str, cond: ConditionalState | None = None
) -> FetchResult:
    """Conditional GET of a feed with bounded retries.

    Returns a FetchResult on 200 or 304. Timeouts, network errors and
    5xx responses are retried with linear backoff; other non-200
    statuses raise FetchHttpError immediately. When retries run out,
    the last failure is raised as the matching FetchError subclass.
    """
    headers: dict[str, str] = {}
    if cond and cond.etag:
        headers["If-None-Match"] = cond.etag
    if cond and cond.last_modified:
        headers["If-Modified-Since"] = cond.last_modified

    last_error: Exception | None = None
    for attempt in range(1 + RETRIES):
        try:
            response = await client.get(url, headers=headers)
        except httpx.TimeoutException as e:
            last_error = FetchTimeout(f"{url}: timeout: {e}")
            log.warning("fetch_retry", url=url, attempt=attempt, error=str(e))
            await asyncio.sleep(RETRY_DELAY_S * (attempt + 1))
            continue
        except httpx.HTTPError as e:
            last_error = FetchError(f"{url}: {e}")
            log.warning("fetch_retry", url=url, attempt=attempt, error=str(e))
            await asyncio.sleep(RETRY_DELAY_S * (attempt + 1))
            continue

        fetched_at = datetime.now(timezone.utc)
        if response.status_code == 304:
            return FetchResult(304, None, cond.etag if cond else None,
                               cond.last_modified if cond else None, fetched_at)
        if response.status_code >= 500:
            last_error = FetchHttpError(
                f"{url} returned {response.status_code}", response.status_code
            )
            await asyncio.sleep(RETRY_DELAY_S * (attempt + 1))
            continue
        if response.status_code != 200:
            raise FetchHttpError(
                f"{url} returned {response.status_code}", response.status_code
            )

        return FetchResult(
            status_code=200,
            body=response.content,
            etag=response.headers.get("ETag"),
            last_modified=response.headers.get("Last-Modified"),
            fetched_at=fetched_at,
        )

    if isinstance(last_error, FetchError):
        raise last_error
    raise FetchError(f"{url}: exhausted retries: {last_error}")
