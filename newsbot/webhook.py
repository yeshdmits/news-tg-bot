"""Telegram webhook app — updates arrive by push; the bot never polls
getUpdates.

Two routes only: ``POST /telegram/webhook`` (secret-token gated, idempotent
via ``seen_updates``) and ``GET /healthz`` (no database, no state — probes
must not keep a connection warm). Runs with ``minReplicas: 0``, so everything
expensive — spec load, database connection, Telegram client — initializes
lazily on the first request that needs it and is rebuilt if the connection
died while the replica was idle.
"""

from __future__ import annotations

import asyncio
import hmac
from dataclasses import dataclass

import httpx
import psycopg
import structlog
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from archive import errors as errdb
from cli import Settings

log = structlog.get_logger(__name__)

SECRET_HEADER = "x-telegram-bot-api-secret-token"


@dataclass
class _Runtime:
    conn: psycopg.Connection
    http: httpx.AsyncClient
    engine: "AlertEngine"  # noqa: F821 — imported lazily in _build
    ops: "OpsBot"  # noqa: F821
    feedback: "FeedbackBot"  # noqa: F821


class _AppState:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._rt: _Runtime | None = None
        self._lock = asyncio.Lock()

    async def acquire(self) -> _Runtime:
        """Live runtime, built on first use. A replica can idle long enough
        for Postgres to drop the session, so ping and rebuild on failure."""
        async with self._lock:
            if self._rt is not None:
                try:
                    self._rt.conn.execute("SELECT 1")
                except psycopg.Error:
                    log.warning("webhook_db_connection_stale_rebuilding")
                    await self._close()
            if self._rt is None:
                self._rt = self._build()
            return self._rt

    def _build(self) -> _Runtime:
        from archive import writer
        from archive.db import connect
        from fetcher.http import make_client
        from newsbot.alerts import AlertEngine
        from newsbot.feedback import FeedbackBot
        from newsbot.ops import OpsBot
        from newsbot.telegram import TelegramClient
        from specsource import load_spec_for

        loaded = load_spec_for(self.settings)
        conn = connect(self.settings.database_url)
        # error_events.spec_hash references spec_versions — this replica's
        # baked spec may be newer than anything the fetch job has recorded.
        writer.ensure_spec_version(conn, loaded.spec_hash, loaded.raw)
        http = make_client()
        telegram = TelegramClient(
            self.settings.telegram_bot_token, http, dry_run=self.settings.dry_run
        )
        engine = AlertEngine(conn, telegram, loaded.spec.errors, spec_hash=loaded.spec_hash)
        ops = OpsBot(conn, telegram, loaded, engine)
        feedback = FeedbackBot(conn, telegram, loaded)
        return _Runtime(
            conn=conn, http=http, engine=engine, ops=ops, feedback=feedback
        )

    async def _close(self) -> None:
        if self._rt is None:
            return
        rt, self._rt = self._rt, None
        try:
            rt.conn.close()
        except Exception:
            pass
        try:
            await rt.http.aclose()
        except Exception:
            pass


async def _healthz(request: Request) -> Response:
    return PlainTextResponse("ok")


def create_app(settings: Settings) -> Starlette:
    """Starlette app with the two routes. All heavy state sits in one
    lazily built _AppState shared across requests."""
    state = _AppState(settings)

    async def _report_unauthorized() -> None:
        """Best-effort: the 401 already left, and a database outage must not
        turn a scanner probe into a crash. Fingerprinting groups every such
        call into one event, so the existing cooldown/budget ladder is the
        rate limit."""
        try:
            rt = await state.acquire()
            await rt.engine.report(
                component="newsbot", error_class="UnauthorizedWebhookCall",
                message="webhook call with invalid secret token",
            )
        except Exception:
            log.warning("unauthorized_webhook_call_unreported")

    async def webhook(request: Request) -> Response:
        """Handle one Telegram update: check the secret token (401), dedupe
        on update_id, dispatch to the ops bot (messages) or the feedback bot
        (button presses). The status code steers Telegram: non-200 asks for
        redelivery, 200 acknowledges."""
        secret = settings.webhook_secret
        header = request.headers.get(SECRET_HEADER, "")
        if not secret or not hmac.compare_digest(header, secret):
            log.warning("unauthorized_webhook_call")
            await _report_unauthorized()
            return Response(status_code=401)

        try:
            update = await request.json()
        except Exception:
            return Response(status_code=200)  # not Telegram; nothing to retry
        update_id = update.get("update_id") if isinstance(update, dict) else None
        if not isinstance(update_id, int):
            return Response(status_code=200)

        # Ordering rule: every failure before mark_update_seen answers
        # non-200, so Telegram redelivers and the retry starts clean; once
        # the update is marked seen, a crash mid-handling is never replayed.
        # Handling has side effects that must not run twice (acks, mutes,
        # replies), so past that point at-most-once is the safe side.
        try:
            rt = await state.acquire()
        except Exception:
            log.exception("webhook_state_init_failed")
            return Response(status_code=503)
        # getMe only disambiguates /cmd@suffix, so it is resolved for message
        # updates only — a button press must not pay for a Bot API round trip
        # on every cold start.
        is_callback = "callback_query" in update
        if not is_callback and not await rt.ops.ensure_me():
            return Response(status_code=503)
        try:
            if not errdb.mark_update_seen(rt.conn, update_id):
                return Response(status_code=200)  # redelivery, already handled
        except psycopg.Error:
            log.exception("webhook_seen_updates_failed")
            return Response(status_code=503)
        try:
            if is_callback:
                await rt.feedback.handle_callback(update)
            else:
                # The loader forbids a review chat from also being the ops
                # chat, so the two handlers can never both claim a message;
                # whichever chat it came from decides.
                handled = await rt.feedback.handle_message(
                    update, bot_username=rt.ops.me_username
                )
                if handled is None:
                    await rt.ops.handle_update(update)
        except Exception:
            log.exception("webhook_update_failed", update_id=update_id)
        return Response(status_code=200)

    return Starlette(
        routes=[
            Route("/healthz", _healthz, methods=["GET"]),
            Route("/telegram/webhook", webhook, methods=["POST"]),
        ]
    )


