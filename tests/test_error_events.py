"""Error-event persistence: fingerprinting, grouping, ack races, mutes,
resolution, retention."""

from datetime import datetime, timedelta, timezone

from archive import errors as errdb

NOW = datetime(2026, 8, 1, 14, 0, tzinfo=timezone.utc)


def test_fingerprint_groups_ephemeral_details():
    """Failures differing only in port/digits/uuids/query strings share one
    fingerprint; different class/source stays distinct."""
    a = errdb.fingerprint_of(
        "fetcher", "FetchNetworkError", "swissinfo", None,
        "connection refused to 10.0.0.4:53211",
    )
    b = errdb.fingerprint_of(
        "fetcher", "FetchNetworkError", "swissinfo", None,
        "connection refused to 10.0.0.4:53219",
    )
    assert a == b

    c = errdb.fingerprint_of(
        "fetcher", "FetchHttpError5xx", "blick", None,
        "https://blick.ch/rss.xml?session=deadbeef1234 returned 503",
    )
    d = errdb.fingerprint_of(
        "fetcher", "FetchHttpError5xx", "blick", None,
        "https://blick.ch/rss.xml?session=cafebabe9876 returned 503",
    )
    assert c == d
    assert a != c  # different class/source stays distinct


def test_normalise_message():
    assert errdb.normalise_message("timeout after 30s on attempt 2") == \
        errdb.normalise_message("timeout after 20s on attempt 3")
    uuid_msg = errdb.normalise_message("item 0d5f95a1-77a8-4828-93a4-f1dc39b125d0 failed")
    assert "*" in uuid_msg and "0d5f95a1" not in uuid_msg


def test_record_occurrence_groups_and_counts(db):
    kwargs = dict(
        component="fetcher", error_class="FetchTimeout", severity="warning",
        message="timeout to host:1234", source_name="swissinfo",
    )
    first = errdb.record_occurrence(db, **kwargs, now=NOW)
    assert first["is_new"] is True
    assert first["occurrence_count"] == 1
    assert len(first["short_id"]) == 8

    second = errdb.record_occurrence(db, **kwargs, now=NOW + timedelta(minutes=1))
    assert second["is_new"] is False
    assert second["fingerprint"] == first["fingerprint"]
    assert second["occurrence_count"] == 2
    assert second["consecutive_streak"] == 2

    assert db.execute("SELECT count(*) AS n FROM error_events").fetchone()["n"] == 1
    assert db.execute("SELECT count(*) AS n FROM error_occurrences").fetchone()["n"] == 2


def test_resolved_event_reopens_unacked(db):
    kwargs = dict(component="fetcher", error_class="FetchTimeout",
                  severity="warning", message="t", source_name="s1")
    errdb.record_occurrence(db, **kwargs, now=NOW)
    resolved = errdb.resolve_fetch_events(db, "s1", NOW)
    assert len(resolved) == 1
    row = errdb.record_occurrence(db, **kwargs, now=NOW + timedelta(hours=1))
    assert row["state"] == "unacked"
    assert row["consecutive_streak"] == 1  # streak restarts after recovery
    assert row["occurrence_count"] == 2    # lifetime counter continues
    assert row["resolved_utc"] is None


def test_ack_race_first_wins(db):
    """Acking an already-acked event reports the original acker and does
    not overwrite."""
    row = errdb.record_occurrence(
        db, component="x", error_class="Y", severity="error", message="m", now=NOW
    )
    outcome, first = errdb.ack_event(db, row["short_id"], 111, "maria")
    assert outcome == "acked"
    outcome2, second = errdb.ack_event(db, row["short_id"], 222, "tomas")
    assert outcome2 == "already"
    assert second["acked_by_user_id"] == 111
    assert second["acked_by_username"] == "maria"
    assert errdb.ack_event(db, "nope", 1, "x")[0] == "missing"


def test_mute_by_source_and_unmute(db):
    for source, message in (("s1", "problem one"), ("s1", "problem two"), ("s2", "other")):
        errdb.record_occurrence(
            db, component="fetcher", error_class="FetchTimeout", severity="warning",
            message=message, source_name=source, now=NOW,
        )
    muted = errdb.mute(db, source_name="s1", until=NOW + timedelta(hours=2), username="maria")
    assert muted == 2
    rows = errdb.query_events(db, source_name="s1")
    assert all(r["muted_until_utc"] is not None for r in rows)
    unmuted = errdb.mute(db, source_name="s1", until=None, username="maria")
    assert unmuted == 2


def test_query_events_filters(db):
    errdb.record_occurrence(db, component="a", error_class="C1",
                            severity="critical", message="crit", now=NOW)
    errdb.record_occurrence(db, component="a", error_class="C2",
                            severity="warning", message="warn", now=NOW)
    assert len(errdb.query_events(db, min_severity="error")) == 1
    assert len(errdb.query_events(db, min_severity="warning")) == 2
    assert errdb.query_events(db, min_severity="error")[0]["error_class"] == "C1"


def test_outbox_round_trip(db):
    errdb.queue_outbox(db, "fp1", "body one", "network down")
    errdb.queue_outbox(db, "fp2", "body two", "network down")
    pending = errdb.pending_outbox(db)
    assert len(pending) == 2
    errdb.mark_outbox_delivered(db, [p["outbox_id"] for p in pending])
    assert errdb.pending_outbox(db) == []


def test_bot_state_round_trip(db):
    assert errdb.get_state(db, "k") is None
    errdb.set_state(db, "k", "v1")
    errdb.set_state(db, "k", "v2")
    assert errdb.get_state(db, "k") == "v2"


def test_retention_cleanup(db):
    row = errdb.record_occurrence(
        db, component="a", error_class="Old", severity="warning", message="old",
        now=NOW - timedelta(days=400),
    )
    db.execute("UPDATE error_events SET state = 'resolved'")
    occ_deleted, ev_deleted = errdb.retention_cleanup(
        db, occurrences_days=30, events_days=365
    )
    assert occ_deleted == 1
    assert ev_deleted == 1
    assert errdb.get_event(db, row["short_id"]) is None
