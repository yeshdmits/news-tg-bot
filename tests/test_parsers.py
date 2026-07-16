"""Parser tests for the generic news client, using inline payload fixtures."""

import pytest

from bot.config import ConfigError
from bot.news_client import FeedValidationError, parse_json_payload, parse_xml_payload
from bot.sources import load_sources

SOURCES_FILE = "bot/sources.json"


@pytest.fixture(scope="module")
def specs():
    return {spec.name: spec for spec in load_sources(SOURCES_FILE)}


@pytest.fixture(scope="module")
def twenty_min(specs):
    return specs["20min"]


@pytest.fixture(scope="module")
def bbc(specs):
    return specs["bbc-world"]


def test_default_sources_file_loads(specs):
    assert set(specs) == {"20min", "bbc-world"}
    assert specs["20min"].type == "json"
    assert specs["20min"].language == "de"
    assert len(specs["20min"].queries) == 3
    assert specs["bbc-world"].type == "xml"
    assert specs["bbc-world"].language == "en"
    assert specs["bbc-world"].xml_schema is not None


def test_load_sources_missing_file():
    with pytest.raises(ConfigError):
        load_sources("does/not/exist.json")


TWENTY_MIN_PAYLOAD = {
    "items": [
        {
            "contentId": 123,
            "title": "  Titel eins  ",
            "lead": "Lead eins",
            "url": "/story/titel-eins-123",
            "publishedAt": "2026-07-16T10:00:00Z",
            "mainCategoryFullUrlPath": "sport/wm-2026-in-usa",
            "image": {"variants": {"big": {"src": "https://img/big.jpg"}}},
        },
        {
            "contentId": 456,
            "title": "Titel zwei",
            "lead": "",
            "url": "https://www.20min.ch/story/456",
            "publishedAt": "2026-07-16T09:00:00Z",
            "image": {"variants": {"small": {"src": "https://img/small.jpg"}}},
        },
        {
            "contentId": 789,
            "title": "",
            "url": "/story/789",
        },
    ]
}


def test_parse_20min_payload(twenty_min):
    articles = parse_json_payload(TWENTY_MIN_PAYLOAD, twenty_min)
    assert [a.content_id for a in articles] == ["20min:123", "20min:456"]

    first = articles[0]
    assert first.title == "Titel eins"
    assert first.url == "https://www.20min.ch/story/titel-eins-123"
    assert first.image_url == "https://img/big.jpg"
    assert first.category == "sport_wm_2026_in_usa"
    assert first.language == "de"
    assert first.source == "20min"
    assert first.published_at is not None
    assert first.published_at.isoformat() == "2026-07-16T10:00:00+00:00"

    second = articles[1]
    assert second.image_url == "https://img/small.jpg"
    assert second.url == "https://www.20min.ch/story/456"
    assert second.category is None


def test_parse_20min_rejects_wrong_shape(twenty_min):
    with pytest.raises(FeedValidationError):
        parse_json_payload({"items": "not-a-list"}, twenty_min)


BBC_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:dc="http://purl.org/dc/elements/1.1/"
     xmlns:atom="http://www.w3.org/2005/Atom"
     xmlns:media="http://search.yahoo.com/mrss/" version="2.0">
<channel>
<title><![CDATA[ BBC News ]]></title>
<description><![CDATA[ BBC News - World ]]></description>
<link>https://www.bbc.co.uk/news/world</link>
<image>
<url>https://news.bbcimg.co.uk/nol/shared/img/bbc_news_120x60.gif</url>
<title>BBC News</title>
<link>https://www.bbc.co.uk/news/world</link>
</image>
<generator>RSS for Node</generator>
<lastBuildDate>Thu, 16 Jul 2026 14:35:21 GMT</lastBuildDate>
<atom:link href="https://feeds.bbci.co.uk/news/world/rss.xml" rel="self" type="application/rss+xml"/>
<language><![CDATA[ en-gb ]]></language>
<ttl>15</ttl>
<item>
<title><![CDATA[ Italian officials handed jail terms for Genoa bridge disaster ]]></title>
<description><![CDATA[ The ex-head of Italy's motorway operator was handed a 12-year term. ]]></description>
<link>https://www.bbc.co.uk/news/articles/c36dnz1zez5o?at_medium=RSS</link>
<guid isPermaLink="false">https://www.bbc.co.uk/news/articles/c36dnz1zez5o#0</guid>
<pubDate>Thu, 16 Jul 2026 13:51:28 GMT</pubDate>
<media:thumbnail width="240" height="135" url="https://ichef.bbci.co.uk/ace/standard/240/thumb.jpg"/>
</item>
<item>
<title><![CDATA[ Second story without thumbnail ]]></title>
<link>https://www.bbc.co.uk/news/articles/xyz</link>
<guid isPermaLink="false">https://www.bbc.co.uk/news/articles/xyz#0</guid>
<pubDate>Thu, 16 Jul 2026 12:00:00 GMT</pubDate>
</item>
</channel>
</rss>
"""


def test_parse_bbc_feed(bbc):
    articles = parse_xml_payload(BBC_FEED, bbc)
    assert len(articles) == 2

    first = articles[0]
    assert first.content_id == "bbc-world:https://www.bbc.co.uk/news/articles/c36dnz1zez5o#0"
    assert first.source == "bbc-world"
    assert first.title == "Italian officials handed jail terms for Genoa bridge disaster"
    assert first.lead == "The ex-head of Italy's motorway operator was handed a 12-year term."
    assert first.url == "https://www.bbc.co.uk/news/articles/c36dnz1zez5o?at_medium=RSS"
    assert first.image_url == "https://ichef.bbci.co.uk/ace/standard/240/thumb.jpg"
    assert first.category == "bbc_world"
    assert first.language == "en"
    assert first.published_at is not None
    assert first.published_at.isoformat() == "2026-07-16T13:51:28+00:00"

    second = articles[1]
    assert second.image_url is None
    assert second.lead == ""


def test_parse_bbc_rejects_wrong_shape(bbc):
    missing_guid = BBC_FEED.replace(
        '<guid isPermaLink="false">https://www.bbc.co.uk/news/articles/c36dnz1zez5o#0</guid>',
        "",
    )
    with pytest.raises(FeedValidationError):
        parse_xml_payload(missing_guid, bbc)


def test_parse_bbc_rejects_malformed_xml(bbc):
    with pytest.raises(FeedValidationError):
        parse_xml_payload("<rss><channel></rss>", bbc)


def test_id_pattern_extracts_unique_key(bbc):
    import dataclasses
    import re

    mapping = dataclasses.replace(
        bbc.mapping, id_pattern=re.compile(r"articles/([a-z0-9]+)")
    )
    spec = dataclasses.replace(bbc, mapping=mapping)

    articles = parse_xml_payload(BBC_FEED, spec)
    assert articles[0].content_id == "bbc-world:c36dnz1zez5o"
    # Second item's guid doesn't need extraction but still matches.
    assert articles[1].content_id == "bbc-world:xyz"


def test_id_pattern_no_match_keeps_full_value(bbc):
    import dataclasses
    import re

    mapping = dataclasses.replace(bbc.mapping, id_pattern=re.compile(r"video/(\d+)"))
    spec = dataclasses.replace(bbc, mapping=mapping)

    articles = parse_xml_payload(BBC_FEED, spec)
    assert (
        articles[0].content_id
        == "bbc-world:https://www.bbc.co.uk/news/articles/c36dnz1zez5o#0"
    )
