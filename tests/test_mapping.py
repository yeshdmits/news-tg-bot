"""Mapping-expression tests: XPath compilation and evaluation, id patterns,
date parsing, HTML stripping, and lead/category cleanup helpers."""

from datetime import datetime, timezone

import pytest
from lxml import etree

from fetcher.mapping import (
    CompiledMapping,
    MappingEvalError,
    apply_id_pattern,
    category_from_url,
    clean_lead,
    parse_rfc822,
    split_attr_suffix,
    strip_html,
    to_xpath,
)

ITEM = etree.fromstring(
    """
    <item xmlns:media="http://search.yahoo.com/mrss/">
      <title>  A   title with
        odd whitespace </title>
      <link>https://example.org/a</link>
      <category>Politics</category>
      <category>Europe</category>
      <enclosure url="https://img.example.org/full.jpg" type="image/jpeg"/>
      <media:content width="140" url="https://img.example.org/140.jpg"/>
      <media:content width="700" url="https://img.example.org/700.jpg"/>
      <media:copyright>Keystone-SDA</media:copyright>
      <empty></empty>
    </item>
    """
)

MEDIA_NS = {"media": "http://search.yahoo.com/mrss/"}


@pytest.mark.parametrize(
    "expr,expected",
    [
        ("enclosure@url", ("enclosure", "url")),
        ("media:content[@width='700']@url", ("media:content[@width='700']", "url")),
        ("category[@domain='x']", ("category[@domain='x']", None)),
        ("title", ("title", None)),
        ("media:copyright", ("media:copyright", None)),
    ],
)
def test_split_attr_suffix(expr, expected):
    assert split_attr_suffix(expr) == expected


def test_to_xpath():
    assert to_xpath("enclosure@url") == "enclosure/@url"
    assert to_xpath("media:content[@width='700']@url") == "media:content[@width='700']/@url"
    assert to_xpath("title") == "title"


def test_eval_element_text_collapses_whitespace():
    compiled = CompiledMapping({})
    assert compiled.eval_first(ITEM, "title") == "A title with odd whitespace"


def test_eval_attribute():
    compiled = CompiledMapping({})
    assert compiled.eval_first(ITEM, "enclosure@url") == "https://img.example.org/full.jpg"


def test_eval_predicate_with_namespace():
    compiled = CompiledMapping(MEDIA_NS)
    assert (
        compiled.eval_first(ITEM, "media:content[@width='700']@url")
        == "https://img.example.org/700.jpg"
    )


def test_eval_ordered_fallback_list():
    compiled = CompiledMapping(MEDIA_NS)
    # first expression matches nothing → falls through to the second
    assert (
        compiled.eval_first(ITEM, ["media:content[@width='9999']@url", "media:content@url"])
        == "https://img.example.org/140.jpg"
    )


def test_eval_empty_element_is_none():
    compiled = CompiledMapping({})
    assert compiled.eval_first(ITEM, "empty") is None
    assert compiled.eval_first(ITEM, "nonexistent") is None


def test_eval_all_repeating_categories():
    compiled = CompiledMapping({})
    assert compiled.eval_all(ITEM, "category") == ["Politics", "Europe"]
    assert compiled.eval_all(ITEM, None) == []


def test_bad_expression_raises():
    compiled = CompiledMapping({})
    with pytest.raises(MappingEvalError):
        compiled.eval_first(ITEM, "///")


def test_apply_id_pattern():
    assert apply_id_pattern("https://x/a-id22151243.html", r"id(\d+)\.html") == "22151243"
    assert apply_id_pattern("no match here", r"id(\d+)\.html") is None
    assert apply_id_pattern("raw-id", None) == "raw-id"
    # pattern without capture group → whole match
    assert apply_id_pattern("abc123", r"\d+") == "123"


def test_parse_rfc822():
    parsed = parse_rfc822("Sat, 1 Aug 2026 11:24:00 GMT")
    assert parsed is not None
    assert parsed.tzinfo == timezone.utc
    assert (parsed.year, parsed.hour) == (2026, 11)

    offset = parse_rfc822("Sat, 01 Aug 2026 13:24:00 +0200")
    assert offset == parsed  # same instant, normalised to UTC

    assert parse_rfc822("not a date") is None


def test_parse_iso8601():
    from fetcher.mapping import parse_iso8601, parse_published

    parsed = parse_iso8601("2026-07-31T07:00:00Z")
    assert parsed == datetime(2026, 7, 31, 7, 0, tzinfo=timezone.utc)
    offset = parse_iso8601("2026-07-31T09:00:00+02:00")
    assert offset == parsed
    assert parse_iso8601("not a date") is None
    assert parse_published("2026-07-31T07:00:00Z", "iso8601") == parsed
    assert parse_published("Fri, 31 Jul 2026 07:00:00 GMT", "rfc822") == parsed


def test_strip_html():
    assert (
        strip_html('<p>Hello <a href="x">world</a> &amp; friends</p>')
        == "Hello world & friends"
    )


def test_clean_lead_pipeline():
    lead = clean_lead(
        '<p>Some text</p> Continue reading...',
        lead_html=True,
        lead_remove=(r"Continue reading\.{3}\s*$",),
    )
    assert lead == "Some text"
    assert clean_lead("   ", lead_html=False, lead_remove=()) is None
    assert clean_lead(None, lead_html=True, lead_remove=()) is None


def test_category_from_url():
    url = "https://www.ecb.europa.eu/press/pr/date/2026/html/x.en.html"
    assert category_from_url(url, "url_path_segment:2") == "pr"
    assert category_from_url(url, "url_path_segment:1") == "press"
    assert category_from_url("https://x.org/", "url_path_segment:2") is None
    with pytest.raises(MappingEvalError):
        category_from_url(url, "query_param:x")
