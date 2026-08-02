"""Direct Bot API calls over httpx — only sendMessage and sendPhoto."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import httpx
import structlog

from newsbot.formatter import Post

log = structlog.get_logger(__name__)

API_BASE = "https://api.telegram.org"


class TelegramTransportError(RuntimeError):
    """Network-level failure — Telegram itself unreachable."""


class TelegramAPIError(RuntimeError):
    """Bot API answered ok=false."""

    def __init__(self, status_code: int, description: str, parameters: dict | None = None):
        super().__init__(f"{status_code}: {description}")
        self.status_code = status_code
        self.description = description
        self.parameters = parameters or {}


@dataclass(frozen=True)
class SendResult:
    """Outcome of a send; errors are captured here, never raised."""

    ok: bool
    message_id: int | None = None
    error: str | None = None
    dry_run: bool = False


class TelegramClient:
    """Thin Bot API client. ``send`` never raises (errors travel in
    SendResult) and honours dry_run; ``api`` raises and ignores dry_run —
    its callers decide."""

    def __init__(self, token: str, client: httpx.AsyncClient, dry_run: bool = True):
        self.token = token
        self.client = client
        self.dry_run = dry_run

    async def send(self, post: Post) -> SendResult:
        """Deliver one post; never raises. Dry-run logs and reports success
        without calling the API; a failed sendPhoto falls back to
        sendMessage so a dead image URL never loses the post; a 429 is
        retried once after Telegram's retry_after."""
        if self.dry_run:
            log.info(
                "dry_run_post",
                chat_id=post.chat_id,
                photo=bool(post.image_url),
                text_preview=post.text[:120],
            )
            return SendResult(ok=True, dry_run=True)

        if post.image_url:
            result = await self._call(
                "sendPhoto",
                {
                    "chat_id": post.chat_id,
                    "photo": post.image_url,
                    "caption": post.text,
                    "parse_mode": "HTML",
                },
            )
            if result.ok:
                return result
            # A dead image URL must not lose the post — fall back to text.
            log.warning("send_photo_failed_falling_back", error=result.error)

        return await self._call(
            "sendMessage",
            {
                "chat_id": post.chat_id,
                "text": post.text,
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": not post.link_preview},
            },
        )

    async def api(self, method: str, payload: dict, *, read_timeout: float | None = None) -> dict:
        """Raw Bot API call. Returns the ``result`` object; raises
        TelegramAPIError on ok=false and TelegramTransportError on network
        failure. Used by the ops/alerting layer — no dry-run gating here,
        callers decide."""
        url = f"{API_BASE}/bot{self.token}/{method}"
        try:
            response = await self.client.post(
                url, json=payload,
                timeout=read_timeout if read_timeout is not None else httpx.USE_CLIENT_DEFAULT,
            )
        except httpx.HTTPError as e:
            raise TelegramTransportError(str(e)) from e
        try:
            body = response.json()
        except ValueError as e:
            raise TelegramTransportError(f"non-JSON response ({response.status_code})") from e
        if body.get("ok"):
            return body.get("result")
        raise TelegramAPIError(
            response.status_code,
            body.get("description", "unknown error"),
            body.get("parameters"),
        )

    async def _call(self, method: str, payload: dict) -> SendResult:
        url = f"{API_BASE}/bot{self.token}/{method}"
        for attempt in (0, 1):
            try:
                response = await self.client.post(url, json=payload)
            except httpx.HTTPError as e:
                return SendResult(ok=False, error=f"transport: {e}")
            body = response.json()
            if body.get("ok"):
                return SendResult(ok=True, message_id=body["result"]["message_id"])
            if response.status_code == 429 and attempt == 0:
                retry_after = (body.get("parameters") or {}).get("retry_after", 3)
                log.warning("telegram_rate_limited", retry_after=retry_after)
                await asyncio.sleep(retry_after)
                continue
            return SendResult(
                ok=False,
                error=f"{response.status_code}: {body.get('description', 'unknown error')}",
            )
        return SendResult(ok=False, error="unreachable")
