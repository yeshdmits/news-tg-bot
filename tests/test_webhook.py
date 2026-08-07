"""Webhook app tests: the secret-token gate, update idempotency, and a
database-free health probe."""

from starlette.testclient import TestClient

from cli import Settings
from newsbot.feedback import FeedbackBot
from newsbot.ops import OpsBot
from newsbot.webhook import SECRET_HEADER, create_app
from tests.conftest import SPEC_FIXTURE

SECRET = "s3cret-token"
REVIEW_CHAT = -100999900010


def make_settings(database_url: str, **overrides) -> Settings:
    kwargs = dict(
        spec_path=SPEC_FIXTURE,
        database_url=database_url,
        telegram_bot_token="",  # keeps AlertEngine inactive: persist, no sends
        dry_run=True,
        webhook_secret=SECRET,
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


def telegram_update(update_id: int, text: str = "/status") -> dict:
    return {
        "update_id": update_id,
        "message": {
            "message_id": 1,
            "from": {"id": 42, "username": "maria", "is_bot": False},
            "chat": {"id": -4999900004},
            "text": text,
        },
    }


def callback_update(update_id: int, data: str) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": "cb-1",
            "from": {"id": 42, "username": "maria", "is_bot": False},
            "message": {"message_id": 101, "chat": {"id": REVIEW_CHAT}},
            "data": data,
        },
    }


def test_healthz_needs_no_db():
    """Health probes must not touch (or even configure) the database."""
    app = create_app(make_settings("postgresql://nobody@localhost:1/nowhere"))
    response = TestClient(app).get("/healthz")
    assert response.status_code == 200


def test_wrong_secret_401_no_body(db, pg_dsn):
    """A bad token → 401 with an empty body, and the probe is recorded as an
    UnauthorizedWebhookCall event."""
    client = TestClient(create_app(make_settings(pg_dsn)))
    response = client.post(
        "/telegram/webhook",
        json=telegram_update(1),
        headers={SECRET_HEADER: "wrong"},
    )
    assert response.status_code == 401
    assert response.content == b""
    assert db.execute(
        "SELECT count(*) AS n FROM error_events WHERE error_class = 'UnauthorizedWebhookCall'"
    ).fetchone()["n"] == 1
    assert db.execute("SELECT count(*) AS n FROM seen_updates").fetchone()["n"] == 0


def test_missing_header_401(db, pg_dsn):
    client = TestClient(create_app(make_settings(pg_dsn)))
    assert client.post("/telegram/webhook", json=telegram_update(1)).status_code == 401


def test_unset_secret_rejects_everything():
    """An unconfigured secret must fail closed, not open — and without a
    secret the app never needs the database to say no."""
    app = create_app(
        make_settings("postgresql://nobody@localhost:1/nowhere", webhook_secret="")
    )
    response = TestClient(app).post(
        "/telegram/webhook", json=telegram_update(1), headers={SECRET_HEADER: ""}
    )
    assert response.status_code == 401


def test_duplicate_update_id_handled_once(db, pg_dsn, monkeypatch):
    """Telegram redelivers on any non-200; the second delivery is dropped by
    seen_updates, not re-handled."""
    handled: list[int] = []

    async def fake_ensure_me(self):
        self.me_username = "newsbot"
        return True

    async def spy_handle_update(self, update):
        handled.append(update["update_id"])
        return "status"

    monkeypatch.setattr(OpsBot, "ensure_me", fake_ensure_me)
    monkeypatch.setattr(OpsBot, "handle_update", spy_handle_update)

    client = TestClient(create_app(make_settings(pg_dsn)))
    for _ in range(2):
        response = client.post(
            "/telegram/webhook",
            json=telegram_update(777),
            headers={SECRET_HEADER: SECRET},
        )
        assert response.status_code == 200

    assert handled == [777]
    assert db.execute("SELECT count(*) AS n FROM seen_updates").fetchone()["n"] == 1


def test_button_press_is_routed_to_the_feedback_bot(db, pg_dsn, monkeypatch):
    """The whole loop through the real HTTP surface: Telegram POSTs a
    callback_query, the label lands in Postgres, and getMe is never called —
    disambiguating /cmd@suffix is a message concern, and a scale-to-zero
    replica must not pay for it on a button press."""
    seen: list[tuple] = []

    async def spy_handle_callback(self, update):
        query = update["callback_query"]
        seen.append((query["data"], query["from"]["id"]))
        return "approved"

    async def explode(self):
        raise AssertionError("getMe must not be called for a callback_query")

    monkeypatch.setattr(FeedbackBot, "handle_callback", spy_handle_callback)
    monkeypatch.setattr(OpsBot, "ensure_me", explode)

    client = TestClient(create_app(make_settings(pg_dsn)))
    response = client.post(
        "/telegram/webhook",
        json=callback_update(9, "fb:a:0192f0c0-1a2b-7c3d-8e4f-a00000000001"),
        headers={SECRET_HEADER: SECRET},
    )

    assert response.status_code == 200
    assert seen == [("fb:a:0192f0c0-1a2b-7c3d-8e4f-a00000000001", 42)]
    assert db.execute("SELECT count(*) AS n FROM seen_updates").fetchone()["n"] == 1


def test_a_redelivered_button_press_is_not_applied_twice(db, pg_dsn, monkeypatch):
    """Telegram redelivers on any non-200. A press has side effects that must
    not repeat — seen_updates gates them exactly as it does for commands."""
    handled: list[int] = []

    async def spy_handle_callback(self, update):
        handled.append(update["update_id"])
        return "approved"

    monkeypatch.setattr(FeedbackBot, "handle_callback", spy_handle_callback)

    client = TestClient(create_app(make_settings(pg_dsn)))
    for _ in range(2):
        assert client.post(
            "/telegram/webhook",
            json=callback_update(555, "fb:r:0192f0c0-1a2b-7c3d-8e4f-a00000000002"),
            headers={SECRET_HEADER: SECRET},
        ).status_code == 200

    assert handled == [555]


def test_non_telegram_body_acked_without_state(db, pg_dsn):
    """Garbage with a valid secret is acked (nothing to retry) and leaves no
    idempotency row behind."""
    client = TestClient(create_app(make_settings(pg_dsn)))
    response = client.post(
        "/telegram/webhook", content=b"not json", headers={SECRET_HEADER: SECRET}
    )
    assert response.status_code == 200
    assert db.execute("SELECT count(*) AS n FROM seen_updates").fetchone()["n"] == 0
