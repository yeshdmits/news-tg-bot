"""Feedback loop against a real Postgres: the queue state machine, the
one-card-per-chat invariant, decision races, and the labelled export.

Telegram is a fake transport throughout — no test touches the network.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import psycopg
import pytest

from archive import feedback as feedbackdb
from archive import writer
from archive.models import ReviewState
from archive.stats import export_rows
from feedspec.loader import load_spec
from newsbot.feedback import FeedbackBot
from newsbot.runner import Deps, run_once
from newsbot.telegram import TelegramClient
from newsbot.translate import NoneProvider, TranslationService
from tests.conftest import SPEC_FIXTURE
from tests.test_pipeline_pg import FIXTURES, feed_transport, make_deps, reset_fetch_timers

REVIEW_CHAT = "-100999900010"
NOW = datetime(2026, 8, 7, 9, 0, tzinfo=timezone.utc)


class FakeBotAPI:
    """Bot API double with per-method scripting, so a test can make exactly
    one call fail without faking the whole protocol."""

    def __init__(self, fail: set[str] = frozenset()):
        self.calls: list[dict] = []
        self.fail = set(fail)
        self._next_message_id = 100

    def client(self, token: str = "42:token") -> TelegramClient:
        def handler(request: httpx.Request) -> httpx.Response:
            method = request.url.path.split("/")[-1]
            self.calls.append({"method": method, **json.loads(request.content)})
            if method in self.fail:
                return httpx.Response(
                    400, json={"ok": False, "description": f"{method} refused"}
                )
            self._next_message_id += 1
            return httpx.Response(
                200, json={"ok": True, "result": {"message_id": self._next_message_id}}
            )

        return TelegramClient(
            token, httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            dry_run=False,
        )

    def methods(self) -> list[str]:
        return [call["method"] for call in self.calls]


def real_spec():
    return load_spec(SPEC_FIXTURE)


def url_map(loaded):
    return {
        s.url: (FIXTURES / f"{s.name}.xml").read_bytes()
        for s in loaded.spec.sources
        if (FIXTURES / f"{s.name}.xml").exists()
    }


def seed_items(db, loaded, *, dry_run=False):
    """One full fetch pass; returns the deps so a test can run more passes."""
    deps, telegram = make_deps(db, loaded, url_map(loaded), dry_run=dry_run)
    return deps, telegram


def press(item_id, approved: bool, *, chat_id: str = REVIEW_CHAT, user=42) -> dict:
    return {
        "update_id": 1,
        "callback_query": {
            "id": "cb-1",
            "from": {"id": user, "username": f"admin{user}", "is_bot": False},
            "message": {"message_id": 101, "chat": {"id": int(chat_id)}},
            "data": f"fb:{'a' if approved else 'r'}:{item_id}",
        },
    }


def reviews(db, **where):
    sql = "SELECT * FROM feedback_reviews"
    if where:
        sql += " WHERE " + " AND ".join(f"{k} = %({k})s" for k in where)
    return db.execute(sql + " ORDER BY queued_utc, item_id", where).fetchall()


# --- enqueue -------------------------------------------------------------


async def test_only_sources_with_a_feedback_chat_are_queued(db):
    """swissinfo is the one source in the reference spec with a review chat;
    everything else archives without a review row."""
    loaded = real_spec()
    deps, _ = seed_items(db, loaded)

    stats = await run_once(deps)

    queued = reviews(db)
    assert {r["source_name"] for r in queued} == {"swissinfo"}
    assert {r["chat_id"] for r in queued} == {REVIEW_CHAT}
    assert stats.queued_for_review == len(queued)
    swissinfo_items = db.execute(
        "SELECT count(*) AS n FROM items WHERE source_name = 'swissinfo'"
    ).fetchone()["n"]
    assert len(queued) == swissinfo_items
    assert stats.new_items > swissinfo_items  # other sources archived, not queued


async def test_archive_only_source_is_still_reviewed(db, tmp_path):
    """The whole point of putting feedback on the source: a source with no
    channel bindings produces zero routing decisions and a full queue."""
    spec = {
        "version": 2,
        "channels": [],
        "sources": [
            {
                "name": "swissinfo",
                "url": "https://feeds.test/swissinfo",
                "language": "en",
                "url_verified": "2026-08-01",
                "cold_start_policy": "skip_all",
                "channels": [],
                "feedback": {"chat_id": REVIEW_CHAT},
                "mapping": {
                    "items": "channel/item", "id": "guid", "title": "title",
                    "lead": "description", "url": "link",
                },
            }
        ],
    }
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec))
    loaded = load_spec(path)
    deps, telegram = make_deps(
        db, loaded, {"https://feeds.test/swissinfo": (FIXTURES / "swissinfo.xml").read_bytes()}
    )

    stats = await run_once(deps)

    assert db.execute("SELECT count(*) AS n FROM routing_decisions").fetchone()["n"] == 0
    assert stats.queued_for_review == stats.new_items > 0
    assert stats.review_cards_sent == 1


async def test_duplicates_are_not_queued(db):
    """The label belongs to the story; the original already carries it."""
    loaded = real_spec()
    deps, _ = seed_items(db, loaded)
    await run_once(deps)

    dupes = db.execute(
        """
        SELECT count(*) AS n FROM items i
        JOIN feedback_reviews fr ON fr.item_id = i.item_id
        WHERE i.duplicate_of IS NOT NULL
        """
    ).fetchone()["n"]
    assert dupes == 0


async def test_enqueue_is_idempotent_across_reruns(db):
    loaded = real_spec()
    deps, _ = seed_items(db, loaded)
    await run_once(deps)
    before = len(reviews(db))

    reset_fetch_timers(db)
    second = await run_once(deps)

    assert second.queued_for_review == 0
    assert len(reviews(db)) == before


# --- the one-card invariant ----------------------------------------------


async def test_one_card_at_a_time_per_chat(db):
    loaded = real_spec()
    deps, _ = seed_items(db, loaded)
    await run_once(deps)

    pending = reviews(db, state="pending")
    assert len(pending) == 1
    assert pending[0]["message_id"] is not None
    # ...and it is the oldest queued item, FIFO.
    ordered = db.execute(
        """
        SELECT fr.item_id FROM feedback_reviews fr
        JOIN items i USING (item_id)
        ORDER BY fr.queued_utc, fr.item_id LIMIT 1
        """
    ).fetchone()
    assert pending[0]["item_id"] == ordered["item_id"]


async def test_a_second_claim_cannot_take_an_occupied_chat(db):
    loaded = real_spec()
    deps, _ = seed_items(db, loaded)
    await run_once(deps)

    names = sorted(s.name for s in loaded.spec.sources)
    assert feedbackdb.claim_next(db, REVIEW_CHAT, names) is None


async def test_the_partial_index_rejects_a_concurrent_second_pending(db, pg_dsn):
    """The application guard is a NOT EXISTS; the database is the real one.
    Two sessions writing 'pending' for the same chat: one must fail."""
    loaded = real_spec()
    deps, _ = seed_items(db, loaded)
    await run_once(deps)
    queued = reviews(db, state="queued")[0]

    other = psycopg.connect(pg_dsn, autocommit=True)
    try:
        with pytest.raises(psycopg.errors.UniqueViolation):
            other.execute(
                "UPDATE feedback_reviews SET state = 'pending' WHERE item_id = %s",
                (queued["item_id"],),
            )
    finally:
        other.close()


async def test_a_failed_send_returns_the_item_to_the_queue(db):
    """A claim whose send fails must not stall the chat forever."""
    loaded = real_spec()
    deps, _ = seed_items(db, loaded)
    api = FakeBotAPI(fail={"sendMessage"})
    deps.telegram = api.client()

    await run_once(deps)

    assert reviews(db, state="pending") == []
    assert len(reviews(db, state="queued")) == len(reviews(db))
    # ...and the next pass retries it.
    deps.telegram = FakeBotAPI().client()
    reset_fetch_timers(db)
    await run_once(deps)
    assert len(reviews(db, state="pending")) == 1


async def test_an_orphaned_claim_is_resent_not_skipped(db):
    """Killed between claiming and sending: the row is pending with no
    message id, and it holds the chat's only slot until it is resent."""
    loaded = real_spec()
    deps, _ = seed_items(db, loaded)
    await run_once(deps)
    pending = reviews(db, state="pending")[0]
    db.execute(
        "UPDATE feedback_reviews SET message_id = NULL WHERE item_id = %s",
        (pending["item_id"],),
    )

    api = FakeBotAPI()
    bot = FeedbackBot(db, api.client(), loaded)
    assert await bot.advance(REVIEW_CHAT) == pending["item_id"]

    assert api.methods() == ["sendMessage"]
    still_pending = reviews(db, state="pending")
    assert len(still_pending) == 1
    assert still_pending[0]["item_id"] == pending["item_id"]
    assert still_pending[0]["message_id"] is not None


async def test_items_of_a_removed_source_do_not_stall_the_chat(db, tmp_path):
    """A source leaving the spec strands its queued items. They stay queued
    (nothing is dropped) but must not block a chat other sources feed."""
    loaded = real_spec()
    deps, _ = seed_items(db, loaded)
    await run_once(deps)

    # Point every queued row at a source no spec knows about, and free the slot.
    db.execute("UPDATE feedback_reviews SET source_name = 'gone', state = 'queued'")
    api = FakeBotAPI()
    bot = FeedbackBot(db, api.client(), loaded)

    assert await bot.advance(REVIEW_CHAT) is None
    assert api.calls == []
    assert len(reviews(db, state="queued")) == len(reviews(db))  # kept, not dropped


# --- decisions -----------------------------------------------------------


async def test_approve_records_the_label_deletes_and_advances(db):
    loaded = real_spec()
    deps, _ = seed_items(db, loaded)
    await run_once(deps)
    first = reviews(db, state="pending")[0]

    api = FakeBotAPI()
    bot = FeedbackBot(db, api.client(), loaded)
    assert await bot.handle_callback(press(first["item_id"], True)) == "approved"

    decided = feedbackdb.get_review(db, first["item_id"])
    assert decided.state == ReviewState.DECIDED
    assert decided.approved is True
    assert decided.decided_by_user_id == 42
    assert decided.decided_by_username == "admin42"
    assert decided.decided_utc is not None

    assert api.methods() == ["answerCallbackQuery", "deleteMessage", "sendMessage"]
    deleted = next(c for c in api.calls if c["method"] == "deleteMessage")
    assert deleted["message_id"] == first["message_id"]

    # exactly one card again, and it is a different item
    pending = reviews(db, state="pending")
    assert len(pending) == 1
    assert pending[0]["item_id"] != first["item_id"]


async def test_reject_records_false(db):
    loaded = real_spec()
    deps, _ = seed_items(db, loaded)
    await run_once(deps)
    first = reviews(db, state="pending")[0]

    bot = FeedbackBot(db, FakeBotAPI().client(), loaded)
    assert await bot.handle_callback(press(first["item_id"], False)) == "rejected"
    assert feedbackdb.get_review(db, first["item_id"]).approved is False


async def test_the_second_presser_loses_and_the_first_label_stands(db):
    loaded = real_spec()
    deps, _ = seed_items(db, loaded)
    await run_once(deps)
    first = reviews(db, state="pending")[0]

    api = FakeBotAPI()
    bot = FeedbackBot(db, api.client(), loaded)
    await bot.handle_callback(press(first["item_id"], True, user=42))
    api.calls.clear()

    assert await bot.handle_callback(press(first["item_id"], False, user=77)) == "already"
    assert feedbackdb.get_review(db, first["item_id"]).approved is True
    assert feedbackdb.get_review(db, first["item_id"]).decided_by_user_id == 42
    # only an acknowledgement — no delete, and above all no second advance
    assert api.methods() == ["answerCallbackQuery"]
    assert "already approved by @admin42" in api.calls[0]["text"]


async def test_a_press_on_a_stale_card_says_so_instead_of_guessing(db):
    """A send whose response was lost is released back to the queue while its
    card may already be in the chat. Pressing it must not read as a decision
    that never happened."""
    loaded = real_spec()
    deps, _ = seed_items(db, loaded)
    await run_once(deps)
    first = reviews(db, state="pending")[0]
    feedbackdb.release_claim(db, first["item_id"])

    api = FakeBotAPI()
    bot = FeedbackBot(db, api.client(), loaded)
    assert await bot.handle_callback(press(first["item_id"], True)) == "already"

    assert feedbackdb.get_review(db, first["item_id"]).approved is None
    assert "stale" in api.calls[0]["text"]


async def test_a_press_from_a_foreign_chat_is_ignored(db):
    """The chat is the authorization boundary: anyone inside may press,
    nobody outside can."""
    loaded = real_spec()
    deps, _ = seed_items(db, loaded)
    await run_once(deps)
    first = reviews(db, state="pending")[0]

    api = FakeBotAPI()
    bot = FeedbackBot(db, api.client(), loaded)
    assert await bot.handle_callback(
        press(first["item_id"], True, chat_id="-4999900004")
    ) is None

    assert api.calls == []
    assert feedbackdb.get_review(db, first["item_id"]).approved is None


async def test_an_undeletable_card_still_keeps_the_label_and_advances(db):
    """Deletion is presentation. A bot may only delete its own messages for
    48 h, so beyond that the buttons are stripped instead — and the label,
    written before any Telegram traffic, is unaffected either way."""
    loaded = real_spec()
    deps, _ = seed_items(db, loaded)
    await run_once(deps)
    first = reviews(db, state="pending")[0]

    api = FakeBotAPI(fail={"deleteMessage"})
    bot = FeedbackBot(db, api.client(), loaded)
    await bot.handle_callback(press(first["item_id"], True))

    assert feedbackdb.get_review(db, first["item_id"]).approved is True
    assert api.methods() == [
        "answerCallbackQuery", "deleteMessage", "editMessageText", "sendMessage"
    ]
    edit = next(c for c in api.calls if c["method"] == "editMessageText")
    assert edit["reply_markup"] == {"inline_keyboard": []}
    assert "approved by @admin42" in edit["text"]
    assert len(reviews(db, state="pending")) == 1


async def test_an_empty_queue_leaves_the_chat_without_a_card(db):
    loaded = real_spec()
    deps, _ = seed_items(db, loaded)
    await run_once(deps)

    api = FakeBotAPI()
    bot = FeedbackBot(db, api.client(), loaded)
    for row in reviews(db):  # drain everything
        if row["state"] != "decided":
            await bot.handle_callback(press(row["item_id"], True))

    assert reviews(db, state="pending") == []
    assert reviews(db, state="queued") == []
    assert await bot.advance(REVIEW_CHAT) is None


async def test_no_token_fills_the_queue_but_sends_nothing(db):
    """An unconfigured bot still collects the corpus — cards resume when a
    token appears, which is exactly the local-development story."""
    loaded = real_spec()
    deps, _ = seed_items(db, loaded)
    deps.telegram = TelegramClient("", httpx.AsyncClient(), dry_run=False)

    stats = await run_once(deps)

    assert stats.queued_for_review > 0
    assert stats.review_cards_sent == 0
    assert reviews(db, state="pending") == []


async def test_cards_go_out_under_dry_run(db):
    """DRY_RUN suppresses channel posting only. Review is operator surface,
    like alerting: rehearsing the pipeline still exercises the loop."""
    loaded = real_spec()
    deps, _ = seed_items(db, loaded, dry_run=True)
    api = FakeBotAPI()
    deps.telegram = api.client()
    deps.telegram.dry_run = True

    stats = await run_once(deps)

    assert stats.review_cards_sent == 1
    assert api.methods() == ["sendMessage"]
    assert api.calls[0]["chat_id"] == REVIEW_CHAT


# --- the dataset ---------------------------------------------------------


async def test_export_carries_the_label(db):
    loaded = real_spec()
    deps, _ = seed_items(db, loaded)
    await run_once(deps)
    first = reviews(db, state="pending")[0]
    bot = FeedbackBot(db, FakeBotAPI().client(), loaded)
    await bot.handle_callback(press(first["item_id"], True))

    rows = list(export_rows(db, NOW.replace(year=2020), NOW.replace(year=2030)))
    by_id = {r["item_id"]: r for r in rows}

    labelled = by_id[first["item_id"]]
    assert labelled["review_approved"] is True
    assert labelled["review_state"] == "decided"
    assert labelled["review_decided_by_user_id"] == 42
    assert labelled["review_decided_utc"] is not None

    # An unreviewed item exports with NULLs, not a missing row.
    unreviewed = next(
        r for r in rows if r["source_name"] != "swissinfo"
    )
    assert unreviewed["review_state"] is None
    assert unreviewed["review_approved"] is None

    # One review per item, so the join must not fan the export out.
    assert len(rows) == len({(r["item_id"], r["channel_name"]) for r in rows})


async def test_queue_stats_tallies_the_labels(db):
    loaded = real_spec()
    deps, _ = seed_items(db, loaded)
    await run_once(deps)
    bot = FeedbackBot(db, FakeBotAPI().client(), loaded)

    pending = reviews(db, state="pending")[0]
    await bot.handle_callback(press(pending["item_id"], True))
    pending = reviews(db, state="pending")[0]
    await bot.handle_callback(press(pending["item_id"], False))

    stats = feedbackdb.queue_stats(db)
    assert len(stats) == 1
    assert stats[0]["chat_id"] == REVIEW_CHAT
    assert stats[0]["approved"] == 1
    assert stats[0]["rejected"] == 1
    assert stats[0]["pending"] == 1
