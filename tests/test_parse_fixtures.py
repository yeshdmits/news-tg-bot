"""Parser tests against saved real feed responses (tests/fixtures/).

These fixtures are the correctness baseline for the whole project. Every enabled news source must parse offline, produce complete
required fields, and honour its mapping quirks.
"""

from pathlib import Path

import pytest

from feedspec.loader import load_spec
from feedspec.resolve import resolve_source
from fetcher.parse import ParseError, RawItem, parse_feed
from tests.conftest import SPEC_FIXTURE

FIXTURES = Path("tests/fixtures")
NEWS_SOURCES = ["swissinfo", "blick", "guardian-world", "bbc-world"]
ECON_SOURCES = ["ecb-press", "fed-press-all", "fed-monetary", "snb-press"]


@pytest.fixture(scope="module")
def spec():
    return load_spec(SPEC_FIXTURE).spec


def parse_fixture(spec, name: str) -> list[RawItem]:
    source = next(s for s in spec.sources if s.name == name)
    effective = resolve_source(spec, source)
    body = (FIXTURES / f"{name}.xml").read_bytes()
    return parse_feed(body, effective, base_dir=".")


@pytest.mark.parametrize("name", NEWS_SOURCES + ECON_SOURCES)
def test_fixture_parses_with_required_fields(spec, name):
    items = parse_fixture(spec, name)
    assert items, f"{name}: no items parsed"
    for item in items:
        assert item.source_item_id
        assert item.title
        assert item.url.startswith("http")
        assert item.published_utc is not None
        assert item.published_utc.tzinfo is not None
        assert item.published_raw
        assert item.raw_xml.startswith(b"<item")


@pytest.mark.parametrize("name", NEWS_SOURCES + ECON_SOURCES)
def test_fetch_limit_respected(spec, name):
    source = next(s for s in spec.sources if s.name == name)
    items = parse_fixture(spec, name)
    assert len(items) <= source.fetch_limit


def test_swissinfo_specifics(spec):
    items = parse_fixture(spec, "swissinfo")
    # copyright is mapped via media:copyright — the media prefix comes from the
    # document's own nsmap, not the spec (which declares no namespaces here)
    assert any(item.copyright_holder for item in items)
    assert any(item.image_url for item in items)
    assert any(item.categories for item in items)
    assert all(item.language == "en" for item in items)


def test_blick_id_pattern_yields_numeric_ids(spec):
    items = parse_fixture(spec, "blick")
    assert all(item.source_item_id.isdigit() for item in items)
    assert all(item.language == "de" for item in items)


def test_guardian_lead_html_stripped_and_trailer_removed(spec):
    items = parse_fixture(spec, "guardian-world")
    for item in items:
        assert item.lead is not None
        assert "<" not in item.lead
        assert "Continue reading" not in item.lead
    # image comes from the width=700 media:content by predicate
    assert any(item.image_url for item in items)


def test_bbc_thumbnail_extracted(spec):
    items = parse_fixture(spec, "bbc-world")
    assert any(item.image_url for item in items)


def test_ecb_empty_lead_uses_title_fallback(spec):
    """ecb-press items have no description element; they parse with the lead
    filled from the title fallback and post that way."""
    items = parse_fixture(spec, "ecb-press")
    assert items
    for item in items:
        assert item.lead == item.title  # lead_fallback: "title"
    # categories derived from the URL path (press/pr vs press/key etc.)
    derived = {c for item in items for c in item.categories}
    assert "pr" in derived  # press releases live under /press/pr/
    assert all(len(item.categories) <= 1 for item in items)


def test_fed_fixtures_parse_with_categories_and_bom(spec):
    # fed-press-all ships a UTF-8 BOM before the XML declaration — must parse
    items = parse_fixture(spec, "fed-press-all")
    assert len(items) == 20  # fetch_limit 20
    assert any(item.categories for item in items)
    assert all(item.lead for item in items)


def test_snb_iso8601_dates_and_title_fallback(spec):
    """snb-press has no pubDate and no description: dc:date (ISO 8601) maps to
    published_utc, the lead falls back to the title."""
    items = parse_fixture(spec, "snb-press")
    assert items
    for item in items:
        assert item.published_utc is not None
        assert item.published_utc.tzinfo is not None
        assert item.lead == item.title


def test_invalid_xml_raises_parse_error(spec):
    source = resolve_source(spec, spec.sources[0])
    with pytest.raises(ParseError, match="not well-formed"):
        parse_feed(b"this is not xml", source, base_dir=".")


def test_xsd_violation_raises_parse_error(spec):
    source = resolve_source(spec, next(s for s in spec.sources if s.name == "swissinfo"))
    body = b'<?xml version="1.0"?><rss version="2.0"><channel><item><surprise/></item></channel></rss>'
    with pytest.raises(ParseError, match="XSD"):
        parse_feed(body, source, base_dir=".")


def test_raw_xml_reparses_to_identical_fields(spec):
    """The stored per-item raw XML blob must be independently parseable with
    the same mapping, yielding identical field values."""
    from lxml import etree

    from fetcher.mapping import CompiledMapping

    items = parse_fixture(spec, "swissinfo")
    doc_root = etree.fromstring((FIXTURES / "swissinfo.xml").read_bytes())
    doc_ns = {k: v for k, v in doc_root.nsmap.items() if k}
    compiled = CompiledMapping(doc_ns)
    for item in items:
        element = etree.fromstring(item.raw_xml)
        assert compiled.eval_first(element, "title") == item.title
        assert compiled.eval_first(element, "link") == item.url
        assert compiled.eval_first(element, "media:copyright") == item.copyright_holder
