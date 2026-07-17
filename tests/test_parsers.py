"""Parser tests for the generic news client, using inline payload fixtures."""

from pathlib import Path

import pytest

from bot.config import ConfigError
from bot.news_client import FeedValidationError, parse_json_payload, parse_xml_payload
from bot.sources import load_sources

SOURCES_FILE = str(Path(__file__).parent.parent / "bot" / "sources.json")


@pytest.fixture(scope="module")
def specs():
    return {spec.name: spec for spec in load_sources(SOURCES_FILE)}


# A standalone JSON-type source (the shipped sources.json is all-XML) to keep
# the JSON parser path covered: dot-paths, image fallback list, url_base,
# queries, and inline JSON Schema validation.
JSON_SOURCE = {
    "name": "json-demo",
    "type": "json",
    "url": "https://example.com/api/news",
    "queries": [{"limit": "{limit}", "timeFrame": "1h"}],
    "language": "de",
    "channel": "german",
    "category": "demo",
    "url_base": "https://example.com",
    "schema": {
        "type": "object",
        "required": ["items"],
        "properties": {"items": {"type": "array", "items": {"type": "object"}}},
    },
    "mapping": {
        "items": "items",
        "id": "contentId",
        "title": "title",
        "lead": "lead",
        "url": "url",
        "image": ["image.variants.big.src", "image.variants.small.src"],
        "published": "publishedAt",
        "published_format": "iso8601",
        "category": "mainCategoryFullUrlPath",
    },
}


@pytest.fixture(scope="module")
def json_spec(tmp_path_factory):
    import json

    file = tmp_path_factory.mktemp("sources") / "sources.json"
    file.write_text(json.dumps({"sources": [JSON_SOURCE]}))
    return load_sources(str(file))[0]


@pytest.fixture(scope="module")
def bbc(specs):
    return specs["bbc-world"]


def test_default_sources_file_loads(specs):
    """The shipped sources.json parses into valid specs, whatever it contains.

    Deliberately generic: adding a source must not require touching tests.
    """
    assert specs
    for spec in specs.values():
        assert spec.type in ("json", "xml")
        assert spec.language
        assert spec.channel
        assert spec.queries
        if spec.type == "json":
            assert spec.json_schema is not None
        else:
            assert spec.xml_schema is not None


def test_load_sources_missing_file():
    with pytest.raises(ConfigError):
        load_sources("does/not/exist.json")


def test_load_sources_requires_category(tmp_path):
    import json

    source = {
        "name": "acme",
        "type": "json",
        "url": "https://example.com/feed.json",
        "language": "en",
        "channel": "english",
        "schema": {"type": "object"},
        "mapping": {"items": "items", "id": "id", "title": "title", "url": "url"},
    }
    file = tmp_path / "sources.json"
    file.write_text(json.dumps({"sources": [source]}))
    with pytest.raises(ConfigError, match="category"):
        load_sources(str(file))

    source["category"] = "Acme/Top-News"
    file.write_text(json.dumps({"sources": [source]}))
    assert load_sources(str(file))[0].category == "acme_top_news"


JSON_PAYLOAD = {
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
            "url": "https://example.com/story/456",
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


def test_parse_json_payload(json_spec):
    articles = parse_json_payload(JSON_PAYLOAD, json_spec)
    assert [a.content_id for a in articles] == ["json-demo:123", "json-demo:456"]

    first = articles[0]
    assert first.title == "Titel eins"
    assert first.url == "https://example.com/story/titel-eins-123"
    assert first.image_url == "https://img/big.jpg"
    assert first.category == "sport_wm_2026_in_usa"
    assert first.language == "de"
    assert first.channel == "german"
    assert first.source == "json-demo"
    assert first.published_at is not None
    assert first.published_at.isoformat() == "2026-07-16T10:00:00+00:00"

    second = articles[1]
    assert second.image_url == "https://img/small.jpg"
    assert second.url == "https://example.com/story/456"
    # No category in the feed item -> the source's required category.
    assert second.category == "demo"


def test_parse_json_rejects_wrong_shape(json_spec):
    with pytest.raises(FeedValidationError):
        parse_json_payload({"items": "not-a-list"}, json_spec)


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


GUARDIAN_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:media="http://search.yahoo.com/mrss/"
     xmlns:dc="http://purl.org/dc/elements/1.1/" version="2.0">
<channel>
<title>World news | The Guardian</title>
<link>https://www.theguardian.com/world</link>
<item>
<title>Uganda calls for travel restrictions to be lifted</title>
<link>https://www.theguardian.com/global-development/2026/jul/16/uganda-ebola</link>
<description>&lt;p&gt;Country begins 42-day countdown&lt;/p&gt; &lt;a href="https://www.theguardian.com/x"&gt;Continue reading...&lt;/a&gt;</description>
<category domain="https://www.theguardian.com/global-development/global-health">Global health</category>
<category domain="https://www.theguardian.com/world/ebola">Ebola</category>
<pubDate>Thu, 16 Jul 2026 11:33:27 GMT</pubDate>
<guid>https://www.theguardian.com/global-development/2026/jul/16/uganda-ebola</guid>
<media:content width="140" url="https://i.guim.co.uk/img/140.jpg">
<media:credit scheme="urn:ebu">Photograph: Reuters</media:credit>
</media:content>
<media:content width="700" url="https://i.guim.co.uk/img/700.jpg">
<media:credit scheme="urn:ebu">Photograph: Reuters</media:credit>
</media:content>
<dc:creator>John Musenze in Kampala</dc:creator>
</item>
<item>
<title>Story without categories or images</title>
<link>https://www.theguardian.com/world/2026/jul/16/second</link>
<pubDate>Thu, 16 Jul 2026 10:00:00 GMT</pubDate>
<guid>https://www.theguardian.com/world/2026/jul/16/second</guid>
</item>
</channel>
</rss>
"""


def test_parse_guardian_feed(specs):
    guardian = specs["guardian-world"]
    articles = parse_xml_payload(GUARDIAN_FEED, guardian)
    assert len(articles) == 2

    first = articles[0]
    # [@width='700'] predicate picks the big rendition, not the first one.
    assert first.image_url == "https://i.guim.co.uk/img/700.jpg"
    # Feed category text with spaces becomes a clean hashtag.
    assert first.category == "global_health"
    assert first.language == "en"
    assert first.channel == "english"
    # lead_html strips the markup; lead_remove drops the trailing boilerplate.
    assert first.lead == "Country begins 42-day countdown"

    second = articles[1]
    assert second.image_url is None
    assert second.category == "guardian_world"


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
