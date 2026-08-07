"""Telegram post formatting tests: HTML escaping, hashtags, truncation,
translation, post styles, and the photo-caption length limit."""

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

from archive.models import ItemRecord
from newsbot.formatter import format_post, hashtag, truncate_word_boundary
from tests.test_router import make_cfg

NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


class RecordingTranslator:
    def __init__(self):
        self.calls = []

    def translate_field(self, *, content_hash, field, text, source_lang, target_lang):
        self.calls.append(field)
        return f"[{target_lang}] {text}"


def make_source(**overrides) -> SimpleNamespace:
    values = dict(name="bbc-world", region="GLOBAL", language="de")
    values.update(overrides)
    return SimpleNamespace(**values)


def make_record(**overrides) -> ItemRecord:
    values = dict(
        item_id=uuid4(),
        source_name="src",
        source_item_id="id-1",
        title="Ein Titel <mit> Winkeln",
        lead_original="Der Lead & mehr Text hier",
        lead_language="de",
        url_raw="https://example.org/a?x=1",
        canonical_url="https://example.org/a?x=1",
        image_url=None,
        published_raw=None,
        published_utc=NOW,
        first_seen_utc=NOW,
        content_hash=b"\x01" * 32,
        title_simhash=0,
        duplicate_of=None,
        copyright_holder=None,
        spec_hash="h",
    )
    values.update(overrides)
    return ItemRecord(**values)


def test_truncate_word_boundary():
    assert truncate_word_boundary("short", 300) == "short"
    result = truncate_word_boundary("one two three four five", 14)
    assert result == "one two three…"
    assert len(result) <= 14
    # no space before the cut → hard cut
    assert truncate_word_boundary("abcdefghij", 5) == "abcd…"


def test_hashtag_slugs():
    assert hashtag("bbc-world") == "#bbc_world"
    assert hashtag("fed-press-all") == "#fed_press_all"
    assert hashtag("GLOBAL", lower=False) == "#GLOBAL"
    assert hashtag("EU", lower=False) == "#EU"


def test_post_structure_read_more_and_hashtags():
    post = format_post(make_record(), make_cfg(), RecordingTranslator(), make_source())
    title, lead, footer = post.text.split("\n\n")
    assert title == "<b>Ein Titel &lt;mit&gt; Winkeln</b>"
    assert lead == "Der Lead &amp; mehr Text hier"
    assert footer == '<a href="https://example.org/a?x=1">Read more</a> | #bbc_world #GLOBAL'
    assert post.image_url is None
    assert post.link_preview is True


def test_region_tag_omitted_when_absent():
    post = format_post(
        make_record(), make_cfg(), RecordingTranslator(), make_source(region=None)
    )
    assert post.text.endswith("Read more</a> | #bbc_world")


def test_text_only_disables_preview():
    post = format_post(
        make_record(), make_cfg(post_style="text_only"), RecordingTranslator(), make_source()
    )
    assert post.link_preview is False
    assert "Read more" in post.text  # the article link stays reachable


def test_title_only_drops_the_lead_but_keeps_the_preview():
    post = format_post(
        make_record(), make_cfg(post_style="link_preview_title_only"),
        RecordingTranslator(), make_source(),
    )
    title, footer = post.text.split("\n\n")
    assert title == "<b>Ein Titel &lt;mit&gt; Winkeln</b>"
    assert footer == '<a href="https://example.org/a?x=1">Read more</a> | #bbc_world #GLOBAL'
    assert "Der Lead" not in post.text
    assert post.link_preview is True


def test_title_only_does_not_translate_the_lead():
    """The lead is dropped before the translation step, so it costs no quota."""
    translator = RecordingTranslator()
    post = format_post(
        make_record(), make_cfg(post_style="link_preview_title_only", translate_lead=True),
        translator, make_source(),
    )
    assert translator.calls == ["title"]
    assert post.text.startswith("<b>[en] Ein Titel")


def test_title_only_stays_a_text_post_when_the_item_has_an_image():
    post = format_post(
        make_record(image_url="https://img.example.org/x.jpg"),
        make_cfg(post_style="link_preview_title_only"), RecordingTranslator(), make_source(),
    )
    assert post.image_url is None
    assert post.link_preview is True
    assert "Der Lead" not in post.text


def test_photo_full_uses_image_as_caption_post():
    record = make_record(image_url="https://img.example.org/x.jpg")
    post = format_post(record, make_cfg(post_style="photo_full"), RecordingTranslator(), make_source())
    assert post.image_url == "https://img.example.org/x.jpg"
    assert len(post.text) <= 1024
    assert post.text.endswith("#bbc_world #GLOBAL")


def test_photo_full_without_image_falls_back_to_text():
    post = format_post(
        make_record(image_url=None), make_cfg(post_style="photo_full"),
        RecordingTranslator(), make_source(),
    )
    assert post.image_url is None


def test_translation_applied_when_languages_differ():
    translator = RecordingTranslator()
    post = format_post(
        make_record(), make_cfg(translate_lead=True), translator, make_source()
    )
    assert translator.calls == ["title", "lead"]
    assert "[en]" in post.text


def test_translation_skipped_same_language():
    translator = RecordingTranslator()
    format_post(
        make_record(), make_cfg(translate_lead=True), translator,
        make_source(language="en"),
    )
    assert translator.calls == []


def test_lead_truncated_to_configured_length():
    record = make_record(lead_original="word " * 200)
    post = format_post(record, make_cfg(lead_max_length=50), RecordingTranslator(), make_source())
    lead_line = post.text.split("\n\n")[1]
    assert len(lead_line) <= 50
    assert lead_line.endswith("…")


def test_lead_equal_to_title_not_repeated():
    record = make_record(lead_original="Ein Titel <mit> Winkeln")
    post = format_post(record, make_cfg(), RecordingTranslator(), make_source())
    assert post.text.count("Ein Titel") == 1


def test_caption_overflow_shrinks_lead_never_footer():
    """The footer's HTML anchor must never be cut mid-tag — the lead absorbs
    the caption limit instead."""
    record = make_record(
        image_url="https://img.example.org/x.jpg",
        lead_original="word " * 300,  # 1500 chars, over the 1024 caption limit
    )
    post = format_post(
        record, make_cfg(post_style="photo_full", lead_max_length=2000),
        RecordingTranslator(), make_source(),
    )
    assert len(post.text) <= 1024
    assert post.text.endswith('Read more</a> | #bbc_world #GLOBAL')
    assert post.text.count("<a href=") == 1
