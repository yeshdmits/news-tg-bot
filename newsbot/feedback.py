"""Admin feedback loop: one approve/reject card at a time per review chat.

Two processes drive it, and the split matters because the webhook app runs
with ``minReplicas: 0`` and only wakes when Telegram pushes an update — it
can react to a button press, but it cannot start the loop:

- the **fetch job** enqueues newly archived items and sends a card to any
  review chat that has none. It is the seed and the repair path.
- the **webhook app** records the press, removes the card and sends the next
  one immediately, so an operator working through a queue never waits for
  the next cron tick.

Durability order is fixed: the label is written to Postgres *first*, and
only then does any Telegram traffic happen. A card that cannot be deleted,
or a next card that cannot be sent, never costs a decision — the fetch job
picks the chat back up within its interval.

Authorization is the chat itself: anyone who can see the card may press it.
The webhook secret token gates the endpoint, and a callback whose chat is
not a configured review chat is ignored without a reply.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

import psycopg
import structlog

from archive import feedback as feedbackdb
from archive import writer
from archive.models import ReviewRecord, ReviewState
from feedspec.loader import LoadedSpec
from feedspec.resolve import resolve_source
from newsbot.formatter import format_review_card
from newsbot.telegram import TelegramAPIError, TelegramClient, TelegramTransportError

log = structlog.get_logger(__name__)

CALLBACK_PREFIX = "fb"
_ACTIONS = {"a": True, "r": False}


async def register_commands(telegram: TelegramClient, chat_ids: list[str]) -> None:
    """Menu scoped to each review chat. Called from the migration job
    alongside setWebhook — never on container start, or every cold start
    would hit the Bot API. The ops chat keeps its own separate menu."""
    commands = [{"command": "next", "description": "show the next item to review"}]
    for chat_id in chat_ids:
        await telegram.api(
            "setMyCommands",
            {"commands": commands, "scope": {"type": "chat", "chat_id": chat_id}},
        )


def parse_callback_data(data: str | None) -> tuple[UUID, bool] | None:
    """``fb:a:<uuid>`` / ``fb:r:<uuid>`` → (item_id, approved).

    None for anything else — the ops chat may host other bots, and a stale
    card from an older format must not be guessed at."""
    if not data:
        return None
    parts = data.split(":")
    if len(parts) != 3 or parts[0] != CALLBACK_PREFIX or parts[1] not in _ACTIONS:
        return None
    try:
        return UUID(parts[2]), _ACTIONS[parts[1]]
    except ValueError:
        return None


class FeedbackBot:
    """Sends review cards and applies button presses.

    Depends on the spec's ``sources`` only — never on ``spec.errors``, which
    is optional. A spec with no error handling still gets a feedback loop."""

    # How many archived items to pull in when a chat's queue runs dry. One is
    # enough to keep a card on screen: every press calls advance() again, so
    # the stream is continuous without ever building a backlog to manage.
    top_up_batch = 1

    def __init__(
        self, conn: psycopg.Connection, telegram: TelegramClient, loaded: LoadedSpec
    ):
        self.conn = conn
        self.telegram = telegram
        self.loaded = loaded
        # source_name → EffectiveSource, for rendering; chat ids are derived
        # from it so a card can only ever go where the spec says.
        self._sources = {
            s.name: resolve_source(loaded.spec, s) for s in loaded.spec.sources
        }

    def chat_ids(self) -> list[str]:
        """Every enabled review chat, in spec order, without duplicates —
        several sources may share one chat."""
        seen: dict[str, None] = {}
        for source in self._sources.values():
            chat_id = source.feedback_chat_id
            if chat_id is not None:
                seen.setdefault(chat_id, None)
        return list(seen)

    def sources_for(self, chat_id: str) -> list[str]:
        """Source names feeding one review chat."""
        return sorted(
            name for name, source in self._sources.items()
            if source.feedback_chat_id == chat_id
        )

    @property
    def active(self) -> bool:
        """No token means no cards; the queue still fills in Postgres."""
        return bool(self.telegram.token) and bool(self.chat_ids())

    # --- sending -----------------------------------------------------------

    async def advance(self, chat_id: str) -> UUID | None:
        """Make sure exactly one card is showing in ``chat_id``.

        Returns the item now on display, or None when a card was already up,
        the queue is empty, or the send failed. Safe to call on every fetch
        pass — it is a no-op in the steady state."""
        if not self.telegram.token:
            return None
        # A claim with no message id is a card that was never delivered: the
        # replica died between claiming and sending. It blocks the chat, so
        # it is retried before anything new is claimed.
        review = feedbackdb.orphaned_claim(self.conn, chat_id)
        if review is not None and review.source_name not in self._sources:
            # The source left the spec while this claim was in flight. It
            # cannot be rendered and it holds the chat's only pending slot,
            # so let it go; claim_next filters it out from here on.
            feedbackdb.release_claim(self.conn, review.item_id)
            log.warning(
                "review_claim_released_source_gone",
                source=review.source_name, item_id=str(review.item_id),
            )
            review = None
        if review is None:
            # Items of a source that has since left the spec stay queued
            # rather than blocking the chat — same stance the archive takes
            # with routing decisions that outlive their binding. Restoring
            # the source resumes them where they were.
            review = feedbackdb.claim_next(self.conn, chat_id, sorted(self._sources))
        if review is None:
            # Queue dry. Rather than wait for the next article to be published
            # — which on a fully-archived feed can be hours — draw from the
            # corpus already on disk, so review is limited by how fast an
            # operator answers, not by how fast the world produces news.
            sources = self.sources_for(chat_id)
            if sources and feedbackdb.top_up(self.conn, chat_id, sources, self.top_up_batch):
                review = feedbackdb.claim_next(self.conn, chat_id, sorted(self._sources))
        if review is None:
            return None

        source = self._sources[review.source_name]
        card = format_review_card(writer.get_item(self.conn, review.item_id), source, chat_id)
        try:
            result = await self.telegram.api(
                "sendMessage",
                {
                    "chat_id": card.chat_id,
                    "text": card.text,
                    "parse_mode": "HTML",
                    "link_preview_options": {"is_disabled": True},
                    "reply_markup": card.reply_markup,
                },
            )
        except (TelegramAPIError, TelegramTransportError) as e:
            # Put it back at the head of the queue; the next fetch pass
            # retries. Leaving it claimed would stall the chat silently.
            feedbackdb.release_claim(self.conn, review.item_id)
            log.warning("review_card_send_failed", chat_id=chat_id, error=str(e))
            return None

        feedbackdb.attach_message(self.conn, review.item_id, result["message_id"])
        log.info(
            "review_card_sent",
            chat_id=chat_id, source=review.source_name, item_id=str(review.item_id),
        )
        return review.item_id

    async def advance_all(self) -> int:
        """Top up every review chat; returns how many cards were sent."""
        sent = 0
        for chat_id in self.chat_ids():
            if await self.advance(chat_id) is not None:
                sent += 1
        return sent

    # --- receiving ---------------------------------------------------------

    async def handle_callback(self, update: dict[str, Any]) -> str | None:
        """Apply one button press. Returns a short action tag for tests,
        None when the update is not ours."""
        query = update.get("callback_query") or {}
        parsed = parse_callback_data(query.get("data"))
        if parsed is None:
            return None
        item_id, approved = parsed

        message = query.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id", ""))
        if chat_id not in self.chat_ids():
            return None  # not a review chat of ours: no reply at all

        sender = query.get("from") or {}
        username = sender.get("username")
        who = f"@{username}" if username else str(sender.get("id", "?"))

        review = feedbackdb.decide(
            self.conn, item_id, approved=approved,
            user_id=sender.get("id"), username=username,
        )
        if review is None:
            # Two operators reaching for the same card, or a press on a card
            # that was already answered. The first press stands.
            existing = feedbackdb.get_review(self.conn, item_id)
            await self._answer(query, _already_text(existing))
            return "already"

        verdict = "approved" if approved else "rejected"
        log.info(
            "review_decided",
            item_id=str(item_id), approved=approved,
            chat_id=chat_id, by=sender.get("id"),
        )
        await self._answer(query, f"{'✅' if approved else '❌'} {verdict}")
        await self._stamp_card(review, chat_id, verdict, who)
        await self.advance(chat_id)
        return verdict

    async def handle_message(
        self, update: dict[str, Any], *, bot_username: str | None = None
    ) -> str | None:
        """Handle ``/next`` in a review chat: post the next card without
        waiting for the fetch job. Answering a card already advances the
        queue, so this is for the case where the chat has gone quiet — a
        failed send, or a queue that was empty when the last card was
        answered. Returns a short action tag for tests."""
        message = update.get("message") or {}
        if (message.get("from") or {}).get("is_bot"):
            return None
        chat_id = str((message.get("chat") or {}).get("id", ""))
        if chat_id not in self.chat_ids():
            return None  # not a review chat of ours: stay silent
        text = (message.get("text") or "").strip()
        if not text.startswith("/"):
            return None
        command, _, suffix = text.split()[0][1:].partition("@")
        if suffix and bot_username and suffix.lower() != bot_username.lower():
            return None  # addressed to another bot in the same chat
        if command.lower() != "next":
            return None

        if feedbackdb.pending_card(self.conn, chat_id) is not None:
            await self._reply(chat_id, message, "a card is already up — answer it first")
            return "next_busy"
        if await self.advance(chat_id) is None:
            await self._reply(chat_id, message, "nothing left to review 🎉")
            return "next_empty"
        return "next"

    async def _reply(self, chat_id: str, message: dict[str, Any], text: str) -> None:
        try:
            await self.telegram.api(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": text,
                    "reply_to_message_id": message.get("message_id"),
                },
            )
        except (TelegramAPIError, TelegramTransportError) as e:
            log.warning("review_reply_failed", error=str(e))

    async def _answer(self, query: dict[str, Any], text: str) -> None:
        """Clear the client-side spinner. Telegram gives ~10 s to answer a
        callback; failing to is cosmetic, so never let it break the flow."""
        try:
            await self.telegram.api(
                "answerCallbackQuery",
                {"callback_query_id": query.get("id"), "text": text[:200]},
            )
        except (TelegramAPIError, TelegramTransportError) as e:
            log.warning("answer_callback_failed", error=str(e))

    async def _stamp_card(
        self, review: ReviewRecord, chat_id: str, verdict: str, who: str
    ) -> None:
        """Mark an answered card in place — same model the alert engine uses:
        each message is owned and edited, never deleted.

        The card stays in the chat as the visible record of what was decided
        and by whom, so the chat reads as a review log rather than a conveyor
        belt. Only the buttons go, which is what stops it being answered
        twice. Presentation only: the label is already durable, so a failed
        edit costs nothing but a stale-looking card."""
        if review.message_id is None:
            return
        source = self._sources.get(review.source_name)
        if source is None:
            return
        card = format_review_card(
            writer.get_item(self.conn, review.item_id), source, chat_id
        )
        mark = "✅" if verdict == "approved" else "❌"
        try:
            await self.telegram.api(
                "editMessageText",
                {
                    "chat_id": chat_id,
                    "message_id": review.message_id,
                    "text": f"{card.text}\n\n{mark} {verdict} by {who}",
                    "parse_mode": "HTML",
                    "link_preview_options": {"is_disabled": True},
                    "reply_markup": {"inline_keyboard": []},
                },
            )
        except (TelegramAPIError, TelegramTransportError) as e:
            log.warning("review_card_stamp_failed", error=str(e))


def _already_text(review: ReviewRecord | None) -> str:
    """Why a press did nothing. Usually someone else got there first; it can
    also be a card whose delivery was recorded as failed and which is queued
    for a fresh send, in which case there is no decision to name."""
    if review is None:
        return "no longer in review"
    if review.state is not ReviewState.DECIDED:
        return "this card is stale — a fresh one is on its way"
    who = review.decided_by_username or review.decided_by_user_id or "someone"
    verdict = "approved" if review.approved else "rejected"
    return f"already {verdict} by @{who}"
