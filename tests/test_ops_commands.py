"""Ops-group command handling."""

import time
from datetime import UTC, datetime, timedelta

import pytest

from archive import errors as errdb
from feedspec.loader import load_spec
from newsbot.alerts import AlertEngine
from newsbot.ops import OpsBot, parse_duration
from newsbot.telegram import TelegramClient
from tests.conftest import SPEC_FIXTURE
from tests.test_alerts_pg import OPS_CHAT, FakeBotAPI

NOW = datetime(2026, 8, 1, 14, 0, tzinfo=UTC)

ADMIN_ID = 111
MEMBER_ID = 222


@pytest.fixture
def api():
    api = FakeBotAPI()
    return api


@pytest.fixture
def bot(db, api):
    loaded = load_spec(SPEC_FIXTURE)  # its errors.ops_chat_id == OPS_CHAT
    telegram = TelegramClient("token", api.client(), dry_run=False)
    engine = AlertEngine(db, telegram, loaded.spec.errors, active=True)
    ops = OpsBot(db, telegram, loaded, engine)
    ops.me_username = "newsbot"
    ops.me_id = 999
    ops._admin_cache = (time.monotonic(), frozenset({ADMIN_ID}))
    return ops


def update(
    text: str,
    *,
    user_id: int = MEMBER_ID,
    username: str = "maria",
    chat_id: str = OPS_CHAT,
    is_bot: bool = False,
    reply_to_message_id: int | None = None,
    message_id: int = 10,
) -> dict:
    message = {
        "message_id": message_id,
        "from": {"id": user_id, "username": username, "is_bot": is_bot},
        "chat": {"id": int(chat_id)},
        "text": text,
    }
    if reply_to_message_id is not None:
        message["reply_to_message"] = {
            "message_id": reply_to_message_id,
            "from": {"id": 999, "is_bot": True},
        }
    return {"update_id": 1, "message": message}


def seed_event(db, **overrides) -> dict:
    kwargs = dict(
        component="fetcher", error_class="FetchHttpError5xx", severity="warning",
        message="503 from origin", source_name="swissinfo", now=NOW,
    )
    kwargs.update(overrides)
    return errdb.record_occurrence(db, **kwargs)


# --- addressing -------------------------------------------


async def test_command_addressed_to_other_bot_ignored(bot, api):
    """Commands addressed to another bot are ignored; the bot's own @mention
    suffix matches case-insensitively."""
    assert await bot.handle_update(update("/status@otherbot")) is None
    assert api.sent == []
    assert await bot.handle_update(update("/status")) == "status"
    assert await bot.handle_update(update("/status@newsbot")) == "status"
    assert await bot.handle_update(update("/STATUS@NewsBot")) == "status"
    assert len(api.sent) == 3


async def test_updates_from_bots_dropped(bot, api, db):
    """Updates from bots are dropped — another bot echoing '/ack all' must
    not act on state."""
    seed_event(db)
    assert await bot.handle_update(update("/ack all", user_id=ADMIN_ID, is_bot=True)) is None
    assert api.sent == []
    assert db.execute(
        "SELECT count(*) AS n FROM error_events WHERE state = 'acked'"
    ).fetchone()["n"] == 0


async def test_wrong_chat_fully_ignored(bot, api, db):
    """Commands from any other chat are fully ignored — not even a refusal
    leaves the ops chat boundary."""
    seed_event(db)
    assert await bot.handle_update(update("/status", chat_id="-999999")) is None
    assert await bot.handle_update(update("/ack all", user_id=ADMIN_ID, chat_id="-999999")) is None
    assert api.sent == []


# --- authorization --------------------------------------------------


async def test_read_allowed_write_refused_for_member(bot, api, db):
    """Read commands work for any member; /ack refuses once, audibly, and
    the denial lands in operator_actions."""
    row = seed_event(db)
    assert await bot.handle_update(update("/errors")) == "errors"
    assert len(api.sent) == 1

    result = await bot.handle_update(update(f"/ack {row['short_id']}"))
    assert result == "denied"
    assert any("requires admin" in s["text"] for s in api.sent)
    audit = db.execute(
        "SELECT * FROM operator_actions WHERE result = 'denied'"
    ).fetchall()
    assert len(audit) == 1
    assert audit[0]["user_id"] == MEMBER_ID
    # the refusal raised UnauthorizedCommand (warning — never notifies)
    assert db.execute(
        "SELECT count(*) AS n FROM error_events WHERE error_class = 'UnauthorizedCommand'"
    ).fetchone()["n"] == 1


async def test_write_allowlist_extends_admins(bot, api, db):
    from feedspec.model import AuthorizationCfg

    row = seed_event(db)
    bot.cfg = bot.cfg.model_copy(
        update={"authorization": AuthorizationCfg(write_allowlist=(MEMBER_ID,))}
    )
    assert await bot.handle_update(update(f"/ack {row['short_id']}")) == "ack"
    assert db.execute(
        "SELECT state FROM error_events"
    ).fetchone()["state"] == "acked"


async def test_ack_race_reports_original_acker(bot, api, db):
    """A second /ack on an already-acked event reports the original acker
    and does not overwrite who acked it."""
    row = seed_event(db)
    await bot.handle_update(
        update(f"/ack {row['short_id']}", user_id=ADMIN_ID, username="tomas")
    )
    bot._admin_cache = (time.monotonic(), frozenset({ADMIN_ID, 333}))
    await bot.handle_update(
        update(f"/ack {row['short_id']}", user_id=333, username="maria")
    )
    assert "already acked by @tomas" in api.sent[-1]["text"]
    acked_by = db.execute("SELECT acked_by_user_id FROM error_events").fetchone()
    assert acked_by["acked_by_user_id"] == ADMIN_ID


async def test_reply_to_alert_acks_correct_fingerprint(bot, api, db):
    """Replying "ack" to an alert message acks exactly the event that owns
    that message id — not any other open event."""
    row = seed_event(db)
    other = seed_event(db, message="unrelated problem", error_class="FetchTimeout")
    errdb.record_alert(db, row["fingerprint"], chat_id=OPS_CHAT, message_id=555)

    result = await bot.handle_update(
        update("ack", user_id=ADMIN_ID, username="tomas", reply_to_message_id=555)
    )
    assert result == "ack"
    acked = db.execute(
        "SELECT short_id FROM error_events WHERE state = 'acked'"
    ).fetchall()
    assert [r["short_id"] for r in acked] == [row["short_id"]]
    assert errdb.get_event(db, other["short_id"])["state"] == "unacked"


async def test_reply_to_unknown_message_is_ignored(bot, api, db):
    seed_event(db)
    assert await bot.handle_update(
        update("ack", user_id=ADMIN_ID, reply_to_message_id=444)
    ) is None
    assert api.sent == []


# --- commands ----------------------------------------------------------------


async def test_ack_all_and_confirmation(bot, api, db):
    seed_event(db)
    seed_event(db, message="second problem", error_class="FetchTimeout")
    assert await bot.handle_update(update("/ack all", user_id=ADMIN_ID, username="tomas")) == "ack"
    assert "2 events acked by @tomas" in api.sent[-1]["text"]


async def test_mute_by_source_with_duration(bot, api, db):
    seed_event(db)
    assert await bot.handle_update(
        update("/mute swissinfo 2h", user_id=ADMIN_ID)
    ) == "mute"
    muted = db.execute("SELECT muted_until_utc FROM error_events").fetchone()
    assert muted["muted_until_utc"] is not None
    assert await bot.handle_update(update("/unmute swissinfo", user_id=ADMIN_ID)) == "unmute"
    assert db.execute("SELECT muted_until_utc FROM error_events").fetchone()[
        "muted_until_utc"] is None


async def test_mute_bad_duration_rejected(bot, api, db):
    row = seed_event(db)
    await bot.handle_update(update(f"/mute {row['short_id']} fortnight", user_id=ADMIN_ID))
    assert "duration must look like" in api.sent[-1]["text"]


async def test_detail_and_unchecked_and_logs(bot, api, db):
    row = seed_event(db, detail={"status": 503})
    assert await bot.handle_update(update(f"/detail {row['short_id']}")) == "detail"
    assert row["short_id"] in api.sent[-1]["text"]
    assert await bot.handle_update(update("/unchecked")) == "unchecked"
    assert row["short_id"] in api.sent[-1]["text"]
    assert await bot.handle_update(update("/logs")) == "logs"
    assert await bot.handle_update(update("/whoami")) == "whoami"
    assert "role: read" in api.sent[-1]["text"]


async def test_unchecked_includes_resolved_but_unreviewed(bot, api, db):
    """Broke, fixed itself, nobody looked — /unchecked must show it."""
    row = seed_event(db)
    errdb.resolve_fetch_events(db, "swissinfo", NOW)
    await bot.handle_update(update("/unchecked"))
    assert row["short_id"] in api.sent[-1]["text"]


async def test_status_renders_snapshot(bot, api, db):
    from feedspec.resolve import resolve_source

    seed_event(db)
    await bot.handle_update(update("/status"))
    text = api.sent[-1]["text"]
    assert "SOURCES" in text and "CHANNELS" in text and "ARCHIVE" in text
    assert "swissinfo" in text
    # disabled sources render as paused, whichever ones the spec disables
    for source in bot.loaded.spec.sources:
        if not resolve_source(bot.loaded.spec, source).enabled:
            assert f"⏸️ {source.name}" in text
    assert "unacked" in text


async def test_replies_are_threaded_to_invoking_message(bot, api, db):
    await bot.handle_update(update("/status", message_id=77))
    assert api.sent[-1]["reply_to_message_id"] == 77


# --- misc -------------------------------------------------------------------


def test_parse_duration():
    assert parse_duration("30m") == timedelta(minutes=30)
    assert parse_duration("2h") == timedelta(hours=2)
    assert parse_duration("1d") == timedelta(days=1)
    assert parse_duration("soon") is None


def test_me_id_derived_from_token(db, api):
    """The bot id is the token prefix — known before any API call."""
    loaded = load_spec(SPEC_FIXTURE)
    telegram = TelegramClient("123456:ABC-secret", api.client(), dry_run=False)
    engine = AlertEngine(db, telegram, loaded.spec.errors, active=True)
    ops = OpsBot(db, telegram, loaded, engine)
    assert ops.me_id == 123456
    assert ops.me_username is None
