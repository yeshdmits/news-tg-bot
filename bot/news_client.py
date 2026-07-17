"""Generic news feed client: fetches, validates and parses configured sources."""

from __future__ import annotations

import html
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from email.utils import parsedate_to_datetime

import httpx
import jsonschema
import xmlschema

from .models import Article
from .sources import SourceSpec, category_hashtag

logger = logging.getLogger(__name__)


class FeedValidationError(RuntimeError):
    """Fetched payload does not match the source's validation schema."""


def _parse_date(raw: object, fmt: str) -> datetime | None:
    if not raw or not isinstance(raw, str):
        return None
    if fmt == "rfc822":
        try:
            return parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _strip_html(text: str) -> str:
    """Reduce HTML markup to plain text: drop tags, unescape entities,
    collapse whitespace."""
    text = re.sub(r"<[^>]*>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _json_path(obj: object, path: str) -> object:
    """Dot-separated dict descent; None on any miss."""
    for part in path.split("."):
        if not isinstance(obj, dict):
            return None
        obj = obj.get(part)
    return obj


def _xml_value(element: ET.Element, path: str, namespaces: dict[str, str]) -> str | None:
    """Element text by path; an '@attr' suffix reads an attribute instead.

    A trailing '@attr' must come after any [] predicate, so an '@' inside a
    predicate (e.g. "category[@domain='x']") is not mistaken for a suffix.
    """
    elem_path, sep, attr = path.rpartition("@")
    if sep and "]" not in attr and "/" not in attr:
        target = element.find(elem_path.rstrip("/"), namespaces) if elem_path else element
        value = target.get(attr) if target is not None else None
        return value.strip() or None if value else None
    text = element.findtext(path, default="", namespaces=namespaces)
    return text.strip() or None


def _build_article(
    spec: SourceSpec,
    raw_id: object,
    title: str | None,
    lead: str | None,
    url: str | None,
    image_url: str | None,
    published_raw: object,
    category_raw: object,
) -> Article | None:
    title = (title or "").strip()
    url = (url or "").strip()
    if raw_id in (None, "") or not title or not url:
        return None
    if url.startswith("/") and spec.url_base:
        url = spec.url_base + url
    lead = (lead or "").strip()
    if lead and spec.mapping.lead_html:
        lead = _strip_html(lead)
    if lead and spec.mapping.lead_remove:
        for pattern in spec.mapping.lead_remove:
            lead = pattern.sub("", lead)
        lead = re.sub(r"\s+", " ", lead).strip()
    unique_id = str(raw_id)
    if spec.mapping.id_pattern:
        match = spec.mapping.id_pattern.search(unique_id)
        if match:
            unique_id = match.group(1) if match.groups() else match.group(0)
    return Article(
        content_id=f"{spec.name}:{unique_id}",
        title=title,
        lead=lead,
        url=url,
        image_url=image_url or None,
        published_at=_parse_date(published_raw, spec.mapping.published_format),
        category=category_hashtag(category_raw) or spec.category,
        channel=spec.channel,
        language=spec.language,
    )


def _parse_json_item(item: dict, spec: SourceSpec) -> Article | None:
    m = spec.mapping
    image_url = None
    for path in m.image:
        src = _json_path(item, path)
        if isinstance(src, str) and src:
            image_url = src
            break
    return _build_article(
        spec,
        raw_id=_json_path(item, m.id),
        title=_str_or_none(_json_path(item, m.title)),
        lead=_str_or_none(_json_path(item, m.lead)) if m.lead else None,
        url=_str_or_none(_json_path(item, m.url)),
        image_url=image_url,
        published_raw=_json_path(item, m.published) if m.published else None,
        category_raw=_json_path(item, m.category) if m.category else None,
    )


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def parse_json_payload(payload: object, spec: SourceSpec) -> list[Article]:
    """Validate a JSON payload against the source schema and map it to Articles."""
    if spec.json_schema is not None:
        try:
            jsonschema.validate(payload, spec.json_schema)
        except jsonschema.ValidationError as exc:
            raise FeedValidationError(
                f"payload failed JSON Schema validation: {exc.message}"
            ) from exc

    items = _json_path(payload, spec.mapping.items)
    if not isinstance(items, list):
        return []
    articles = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            article = _parse_json_item(item, spec)
        except (TypeError, ValueError) as exc:
            logger.warning("Source %s: skipping unparseable item: %s", spec.name, exc)
            continue
        if article:
            articles.append(article)
    return articles


def _parse_xml_item(element: ET.Element, spec: SourceSpec) -> Article | None:
    m = spec.mapping
    ns = spec.namespaces
    image_url = None
    for path in m.image:
        src = _xml_value(element, path, ns)
        if src:
            image_url = src
            break
    return _build_article(
        spec,
        raw_id=_xml_value(element, m.id, ns),
        title=_xml_value(element, m.title, ns),
        lead=_xml_value(element, m.lead, ns) if m.lead else None,
        url=_xml_value(element, m.url, ns),
        image_url=image_url,
        published_raw=_xml_value(element, m.published, ns) if m.published else None,
        category_raw=_xml_value(element, m.category, ns) if m.category else None,
    )


def parse_xml_payload(text: str, spec: SourceSpec) -> list[Article]:
    """Validate an XML payload against the source XSD and map it to Articles."""
    if spec.xml_schema is not None:
        try:
            spec.xml_schema.validate(text)
        except (xmlschema.XMLSchemaException, ET.ParseError) as exc:
            raise FeedValidationError(
                f"payload failed XSD validation: {exc}"
            ) from exc
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise FeedValidationError(f"malformed XML: {exc}") from exc

    articles = []
    for element in root.findall(spec.mapping.items, spec.namespaces):
        try:
            article = _parse_xml_item(element, spec)
        except (TypeError, ValueError) as exc:
            logger.warning("Source %s: skipping unparseable item: %s", spec.name, exc)
            continue
        if article:
            articles.append(article)
    return articles


class NewsClient:
    def __init__(self, fetch_limit: int = 10) -> None:
        self._fetch_limit = fetch_limit
        self._client = httpx.AsyncClient(
            timeout=20.0,
            headers={"User-Agent": "news-aggr-bot/1.0"},
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch_articles(self, spec: SourceSpec) -> list[Article]:
        """Fetch one source (one GET per configured query), deduplicated by id.

        A schema-validation failure means the feed changed shape — the whole
        source is skipped for this cycle rather than half-parsed.
        """
        seen: dict[str, Article] = {}
        for query in spec.queries:
            params = {
                key: value.replace("{limit}", str(self._fetch_limit))
                for key, value in query.items()
            }
            try:
                response = await self._client.get(spec.url, params=params or None)
                response.raise_for_status()
                if spec.type == "json":
                    parsed = parse_json_payload(response.json(), spec)
                else:
                    parsed = parse_xml_payload(response.text, spec)
            except FeedValidationError as exc:
                logger.error("Source %s: %s — skipping this cycle", spec.name, exc)
                return []
            except (httpx.HTTPError, ValueError) as exc:
                logger.warning(
                    "Source %s: fetch failed (params=%s): %s", spec.name, params, exc
                )
                continue
            for article in parsed:
                seen.setdefault(article.content_id, article)

        articles = list(seen.values())
        articles.sort(
            key=lambda a: a.published_at.timestamp() if a.published_at else 0.0,
            reverse=True,
        )
        return articles[: self._fetch_limit]
