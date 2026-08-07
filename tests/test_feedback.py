"""Feedback loop, database-free half: callback parsing and card rendering."""

from datetime import datetime, timezone
from uuid import UUID, uuid4

from archive.models import ItemRecord
from feedspec.loader import load_spec
from feedspec.resolve import resolve_source
from newsbot.feedback import parse_callback_data
from newsbot.formatter import REVIEW_LEAD_MAX, format_review_card
from tests.conftest import SPEC_FIXTURE

CHAT = "-100999900010"
NOW = datetime(2026, 8, 7, 9, 0, tzinfo=timezone.utc)


def make_item(**overrides) -> ItemRecord:
    fields = dict(
        item_id=uuid4(),
        source_name="swissinfo",
        source_item_id="id-1",
        title="Council raises the benchmark rate",
        lead_original="The decision follows three quarters of persistent inflation.",
        lead_language="en",
        url_raw="https://example.org/a?utm_source=rss",
        canonical_url="https://example.org/a",
        image_url=None,
        published_raw=None,
        published_utc=NOW,
        first_seen_utc=NOW,
        content_hash=b"\x00" * 32,
        title_simhash=1,
        duplicate_of=None,
        copyright_holder=None,
        spec_hash="abc",
    )
    fields.update(overrides)
    return ItemRecord(**fields)


def swissinfo_source():
    loaded = load_spec(SPEC_FIXTURE)
    source = next(s for s in loaded.spec.sources if s.name == "swissinfo")
    return resolve_source(loaded.spec, source)


# --- callback_data -------------------------------------------------------


def test_parse_callback_data_round_trips_the_keyboard():
    item_id = uuid4()
    card = format_review_card(make_item(item_id=item_id), swissinfo_source(), CHAT)
    approve, reject = card.reply_markup["inline_keyboard"][0]

    assert parse_callback_data(approve["callback_data"]) == (item_id, True)
    assert parse_callback_data(reject["callback_data"]) == (item_id, False)


def test_callback_data_fits_telegrams_64_byte_cap():
    card = format_review_card(make_item(), swissinfo_source(), CHAT)
    for button in card.reply_markup["inline_keyboard"][0]:
        assert len(button["callback_data"].encode()) <= 64


def test_foreign_callback_data_is_ignored():
    """The chat can host other bots, and an old card may carry a format this
    build no longer knows — never guess at either."""
    for data in (
        None, "", "ack", "fb:a", "fb:x:" + str(uuid4()), "fb:a:not-a-uuid",
        "other:a:" + str(uuid4()), "fb:a:%s:extra" % uuid4(),
    ):
        assert parse_callback_data(data) is None


def test_parse_callback_data_accepts_a_bare_uuid_string():
    item_id = UUID("0192f0c0-1a2b-7c3d-8e4f-a00000000001")
    assert parse_callback_data(f"fb:r:{item_id}") == (item_id, False)


# --- card rendering ------------------------------------------------------


def test_card_carries_title_lead_link_and_two_buttons():
    card = format_review_card(make_item(), swissinfo_source(), CHAT)

    assert card.chat_id == CHAT
    assert "<b>Council raises the benchmark rate</b>" in card.text
    assert "The decision follows three quarters" in card.text
    assert '<a href="https://example.org/a?utm_source=rss">Read more</a>' in card.text
    assert "#swissinfo" in card.text and "#CH" in card.text

    buttons = card.reply_markup["inline_keyboard"][0]
    assert len(buttons) == 2
    assert "Approve" in buttons[0]["text"] and "Reject" in buttons[1]["text"]


def test_card_escapes_html_in_the_feed():
    card = format_review_card(
        make_item(title="Rates <b>surge</b> & hold", lead_original="a < b"),
        swissinfo_source(), CHAT,
    )
    assert "&lt;b&gt;surge&lt;/b&gt; &amp; hold" in card.text
    assert "a &lt; b" in card.text


def test_card_truncates_a_long_lead_on_a_word_boundary():
    lead = "word " * 400
    card = format_review_card(make_item(lead_original=lead), swissinfo_source(), CHAT)
    body = card.text.split("\n\n")[1]
    assert body.endswith("…")
    assert len(body) <= REVIEW_LEAD_MAX + 1


def test_card_drops_a_lead_that_only_repeats_the_title():
    title = "Council raises the benchmark rate"
    card = format_review_card(
        make_item(title=title, lead_original=title), swissinfo_source(), CHAT
    )
    assert card.text.count(title) == 1
    assert card.text.split("\n\n")[1].startswith("<a href=")


def test_card_without_a_lead_is_title_and_link_only():
    card = format_review_card(make_item(lead_original=None), swissinfo_source(), CHAT)
    assert len(card.text.split("\n\n")) == 2
