"""Alert policy engine against a real Postgres with a fake Bot API."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from archive import errors as errdb
from feedspec.model import ErrorsConfig
from newsbot.alerts import AlertEngine
from newsbot.telegram import TelegramClient

NOW = datetime(2026, 8, 1, 14, 0, tzinfo=timezone.utc)
OPS_CHAT = "-4999900004"


class FakeBotAPI:
    """Programmable Bot API double behind httpx.MockTransport."""

    def __init__(self):
        self.sent: list[dict] = []
        self.edited: list[dict] = []
        self.pinned: list[dict] = []
        self.unpinned: list[dict] = []
        self.fail_transport = False
        self.migrate_from: str | None = None
        self.migrate_to: int | None = None
        self.fail_edit_description: str | None = None
        self._next_id = 100

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handle))

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.fail_transport:
            raise httpx.ConnectError("telegram unreachable")
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content)
        if method == "sendMessage":
            if self.migrate_from and str(payload["chat_id"]) == self.migrate_from:
                return httpx.Response(400, json={
                    "ok": False, "description": "Bad Request: group chat was upgraded",
                    "parameters": {"migrate_to_chat_id": self.migrate_to},
                })
            self._next_id += 1
            self.sent.append(payload)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": self._next_id}})
        if method == "editMessageText":
            if self.fail_edit_description:
                return httpx.Response(400, json={
                    "ok": False, "description": self.fail_edit_description,
                })
            self.edited.append(payload)
            return httpx.Response(200, json={"ok": True, "result": True})
        if method == "pinChatMessage":
            self.pinned.append(payload)
        if method == "unpinChatMessage":
            self.unpinned.append(payload)
        return httpx.Response(200, json={"ok": True, "result": True})


def make_cfg(**alerting_overrides) -> ErrorsConfig:
    alerting = {
        "escalation_ladder": [1, 10, 50, 200],
        "cooldown_min": 0,
        "max_alerts_per_hour": 1000,
        "silent_below": "error",
        "pin_critical": True,
    }
    alerting.update(alerting_overrides)
    return ErrorsConfig.model_validate({
        "ops_chat_id": OPS_CHAT,
        "alerting": alerting,
        "classification": {
            "TestWarn": {"severity": "warning", "escalate_after": 5, "escalates_to": "error"},
            "TestEsc": {"severity": "warning", "escalate_after": 2, "escalates_to": "error"},
            "TestPlain": {"severity": "warning"},
        },
    })


@pytest.fixture
def api():
    return FakeBotAPI()


def make_engine(db, api, **alerting_overrides) -> AlertEngine:
    telegram = TelegramClient("token", api.client(), dry_run=False)
    return AlertEngine(db, telegram, make_cfg(**alerting_overrides), spec_hash=None, active=True)


async def report_n(engine, n, *, error_class="TestWarn", message="failure at host:1234",
                   start=NOW, step_minutes=1, source_name="swissinfo"):
    row = None
    for i in range(n):
        row = await engine.report(
            component="fetcher", error_class=error_class, message=message,
            source_name=source_name, now=start + timedelta(minutes=i * step_minutes),
        )
    return row


async def test_persisted_even_when_telegram_down(db, api):
    """Persistence never depends on Telegram being reachable — the event
    still lands in the DB and the alert is queued for later delivery."""
    api.fail_transport = True
    engine = make_engine(db, api)
    row = await engine.report(
        component="fetcher", error_class="TestWarn", message="boom", now=NOW
    )
    assert row is not None
    assert db.execute("SELECT count(*) AS n FROM error_events").fetchone()["n"] == 1
    outbox = errdb.pending_outbox(db)
    assert len(outbox) == 1  # queued for later delivery


async def test_hundred_occurrences_ladder(db, api):
    """100 occurrences → 3 notifying alerts (at 1, 10, 50), one row with
    occurrence_count=100, a single alert_message_id edited in place."""
    engine = make_engine(db, api)
    row = await report_n(engine, 100, error_class="TestPlain")
    assert row["occurrence_count"] == 100
    event = errdb.get_event(db, row["short_id"])
    assert event["alert_count"] == 3
    assert len(api.sent) == 1  # one owned message…
    assert len(api.edited) == 99  # …edited in place ever after
    assert event["alert_message_id"] is not None
    edited_ids = {e["message_id"] for e in api.edited}
    assert edited_ids == {event["alert_message_id"]}


async def test_escalation_notifies_inside_cooldown(db, api):
    """Warning → error escalation re-fires despite cooldown."""
    engine = make_engine(db, api, cooldown_min=60)
    await report_n(engine, 1, error_class="TestEsc", start=NOW)
    row = await report_n(engine, 1, error_class="TestEsc", start=NOW + timedelta(minutes=1))
    assert row["severity"] == "error"  # escalated on 2nd consecutive occurrence
    event = errdb.get_event(db, row["short_id"])
    assert event["severity"] == "error"
    assert event["alert_count"] == 2  # occurrence 1 + the escalation, cooldown ignored


async def test_warning_and_info_are_silent(db, api):
    """Warning/info sends carry disable_notification=true; errors notify."""
    engine = make_engine(db, api)
    await engine.report(component="a", error_class="TestWarn", message="w", now=NOW)
    await engine.report(component="a", error_class="SomeNewThing", message="e", now=NOW)
    silent_flags = {s["text"].split("\n")[0].split()[1]: s["disable_notification"] for s in api.sent}
    assert silent_flags["WARNING"] is True
    assert silent_flags["ERROR"] is False  # unknown class defaults to error


async def test_budget_suppression_and_critical_bypass(db, api):
    """Over-budget alerts yield a single suppression notice; critical
    bypasses the hourly budget entirely."""
    engine = make_engine(db, api, max_alerts_per_hour=2)
    for i in range(5):
        await engine.report(
            component="a", error_class="TestWarn", message=f"distinct problem {chr(97 + i)}",
            now=NOW + timedelta(seconds=i),
        )
    suppressions = [s for s in api.sent if "suppressed this hour" in s["text"]]
    assert len(suppressions) == 1
    assert len(api.sent) == 3  # 2 within budget + 1 suppression notice

    await engine.report(
        component="a", error_class="DatabaseError", message="archive down", now=NOW
    )
    assert any("CRITICAL" in s["text"] for s in api.sent)  # bypassed the budget


async def test_muted_fingerprint_counts_but_never_alerts(db, api):
    """A muted fingerprint keeps counting occurrences but never alerts —
    no new sends, no edits — until the mute expires."""
    engine = make_engine(db, api)
    row = await report_n(engine, 1)
    errdb.mute(db, short_id=row["short_id"], until=NOW + timedelta(hours=4), username="maria")
    sent_before, edited_before = len(api.sent), len(api.edited)
    row = await report_n(engine, 9, start=NOW + timedelta(minutes=5))
    assert row["occurrence_count"] == 10  # counting continues
    assert len(api.sent) == sent_before
    assert len(api.edited) == edited_before  # alerting fully stops


async def test_recovery_edits_to_resolved_no_new_message(db, api):
    """Source recovery marks the event resolved and edits the existing alert
    to RESOLVED — no new message is posted."""
    engine = make_engine(db, api)
    await report_n(engine, 3, error_class="FetchHttpError5xx", message="503 from origin")
    sent_before = len(api.sent)
    resolved = await engine.resolve_source_events("swissinfo", NOW + timedelta(hours=1))
    assert resolved == 1
    assert len(api.sent) == sent_before  # no new message
    assert any("RESOLVED" in e["text"] for e in api.edited)
    state = db.execute("SELECT state FROM error_events").fetchone()["state"]
    assert state == "resolved"


async def test_migrate_to_chat_id_updates_and_retries(db, api):
    """Supergroup migration is caught, the new chat id persisted, and the
    send retried once against it."""
    api.migrate_from = OPS_CHAT
    api.migrate_to = -1004999900004
    engine = make_engine(db, api)
    await engine.report(component="a", error_class="TestWarn", message="w", now=NOW)
    assert errdb.get_state(db, "ops_chat_id") == "-1004999900004"
    assert len(api.sent) == 1
    assert str(api.sent[0]["chat_id"]) == "-1004999900004"
    assert engine.ops_chat_id() == "-1004999900004"  # override wins from now on


async def test_edit_too_old_posts_fresh_message(db, api):
    """A 48h+ owned message can't be edited — post anew and adopt the new
    message id."""
    engine = make_engine(db, api)
    row = await report_n(engine, 1)
    old_id = errdb.get_event(db, row["short_id"])["alert_message_id"]
    api.fail_edit_description = "Bad Request: message can't be edited"
    await report_n(engine, 1, start=NOW + timedelta(days=3))
    new_id = errdb.get_event(db, row["short_id"])["alert_message_id"]
    assert new_id is not None and new_id != old_id
    assert len(api.sent) == 2


async def test_html_hazards_escaped(db, api):
    """HTML hazards — <, >, & and underscore-laden URLs — render safely."""
    engine = make_engine(db, api)
    nasty = "Element '<media:content>': attribute & missing at https://x.org/a_b_c?" \
            "utm_source=rss"
    await engine.report(component="fetcher", error_class="TestWarn", message=nasty, now=NOW)
    text = api.sent[0]["text"]
    assert "&lt;media:content&gt;" in text
    assert "&amp;" in text
    assert "<media" not in text
    assert api.sent[0]["parse_mode"] == "HTML"


async def test_never_alert_classes_recorded_only(db, api):
    engine = make_engine(db, api)
    await engine.report(component="newsbot", error_class="ColdStartSkipped",
                        message="first fetch", now=NOW)
    assert db.execute("SELECT count(*) AS n FROM error_events").fetchone()["n"] == 1
    assert api.sent == [] and api.edited == []


async def test_critical_gets_pinned(db, api):
    engine = make_engine(db, api)
    await engine.report(component="archive", error_class="DatabaseError",
                        message="pool exhausted", now=NOW)
    assert len(api.pinned) == 1
    assert db.execute("SELECT pinned FROM error_events").fetchone()["pinned"] is True


async def test_outbox_flush_posts_recovery_line(db, api):
    engine = make_engine(db, api)
    api.fail_transport = True
    await engine.report(component="a", error_class="TestWarn", message="w1", now=NOW)
    assert len(errdb.pending_outbox(db)) == 1
    api.fail_transport = False
    flushed = await engine.flush_outbox()
    assert flushed == 1
    assert any("queued while unreachable" in s["text"] for s in api.sent)
    assert errdb.pending_outbox(db) == []


async def test_digest_summarises_unacked(db, api):
    engine = make_engine(db, api)
    await engine.report(component="a", error_class="TestWarn", message="w", now=NOW)
    assert await engine.maybe_digest(NOW) is True
    assert any("DIGEST" in s["text"] for s in api.sent)
    # interval not elapsed → no second digest
    assert await engine.maybe_digest(NOW + timedelta(minutes=5)) is False
