"""Feed parsing: XSD validation, item extraction → RawItem."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import structlog
from lxml import etree
from pydantic import AwareDatetime, BaseModel, ConfigDict

from feedspec.resolve import EffectiveSource
from fetcher.mapping import (
    CompiledMapping,
    apply_id_pattern,
    category_from_url,
    clean_lead,
    parse_published,
)

log = structlog.get_logger(__name__)


class ParseError(RuntimeError):
    """Malformed feed — the whole source is skipped this cycle."""

    error_class = "FeedParseError"


class XsdValidationError(ParseError):
    """The feed no longer matches its XSD: the shape changed, the mapping is
    now lying."""

    error_class = "SchemaValidationFailed"


# parse_feed's optional on_problem(error_class, message, detail) hook surfaces
# item-level issues to the caller — the runner reports them, keeping fetcher
# free of archive imports.


class RawItem(BaseModel):
    """One extracted feed item — mapped and normalized, not yet
    deduplicated or routed."""

    model_config = ConfigDict(frozen=True)

    source_name: str
    source_item_id: str
    title: str
    url: str
    lead: str | None
    language: str
    image_url: str | None
    categories: tuple[str, ...]
    published_raw: str | None
    published_utc: AwareDatetime | None
    copyright_holder: str | None
    raw_xml: bytes


@lru_cache(maxsize=32)
def load_xsd(path: str) -> etree.XMLSchema:
    """Load a compiled XMLSchema, cached per path. Raises ParseError
    when the file cannot be read or is not a valid schema."""
    try:
        return etree.XMLSchema(etree.parse(path))
    except (OSError, etree.LxmlError) as e:
        raise ParseError(f"cannot load XSD {path}: {e}") from e


def parse_feed(
    body: bytes,
    source: EffectiveSource,
    base_dir: Path | str = ".",
    on_problem=None,
) -> list[RawItem]:
    """Extract up to ``fetch_limit`` RawItems from a feed payload.

    Raises ParseError when the document is not well-formed XML or fails XSD
    validation — the whole source is skipped for this cycle. Individual items
    missing required fields are skipped with a warning, not fatal.
    """
    try:
        root = etree.fromstring(body)
    except etree.XMLSyntaxError as e:
        raise ParseError(f"{source.name}: feed is not well-formed XML: {e}") from e

    if source.schema_file:
        schema = load_xsd(str(Path(base_dir) / source.schema_file))
        if not schema.validate(root.getroottree()):
            raise XsdValidationError(
                f"{source.name}: feed failed XSD validation: {schema.error_log.last_error}"
            )

    # The spec's namespaces win, but the document's own declarations fill the
    # gaps (e.g. swissinfo maps media:copyright without declaring `media`).
    doc_namespaces = {k: v for k, v in root.nsmap.items() if k}
    compiled = CompiledMapping({**doc_namespaces, **source.namespaces})
    mapping = source.mapping

    item_elements = root.xpath(mapping.items, namespaces=compiled.namespaces)
    items: list[RawItem] = []
    for element in item_elements[: source.fetch_limit]:
        raw_id = compiled.eval_first(element, mapping.id)
        url = compiled.eval_first(element, mapping.url)
        title = compiled.eval_first(element, mapping.title)
        if raw_id is None:
            raw_id = url
        else:
            raw_id = apply_id_pattern(raw_id, mapping.id_pattern) or url
        if not raw_id or not title or not url:
            log.warning(
                "item_skipped_missing_required_field",
                source=source.name,
                id=raw_id,
                url=url,
                has_title=bool(title),
            )
            if on_problem:
                on_problem(
                    "MappingRequiredFieldMissing",
                    f"{source.name}: item missing "
                    + "/".join(k for k, v in (("id", raw_id), ("title", title), ("url", url)) if not v),
                    {"id": raw_id, "url": url, "has_title": bool(title)},
                )
            continue

        lead = clean_lead(
            compiled.eval_first(element, mapping.lead),
            lead_html=mapping.lead_html,
            lead_remove=mapping.lead_remove,
        )
        if lead is None and mapping.lead_fallback == "title":
            lead = title
        if lead is None and not mapping.allow_empty_lead:
            log.warning("item_empty_lead", source=source.name, id=raw_id)

        categories = tuple(compiled.eval_all(element, mapping.category))
        if not categories and mapping.category_from:
            derived = category_from_url(url, mapping.category_from)
            if derived:
                categories = (derived,)

        published_raw = compiled.eval_first(element, mapping.published)
        published_utc = (
            parse_published(published_raw, mapping.published_format) if published_raw else None
        )
        if published_raw and published_utc is None:
            log.warning(
                "item_unparseable_published",
                source=source.name,
                id=raw_id,
                published_raw=published_raw,
            )
            if on_problem:
                on_problem(
                    "PublishedDateUnparseable",
                    f"{source.name}: unparseable published date",
                    {"id": raw_id, "published_raw": published_raw},
                )

        items.append(
            RawItem(
                source_name=source.name,
                source_item_id=raw_id,
                title=title,
                url=url,
                lead=lead,
                language=source.language,
                image_url=compiled.eval_first(element, mapping.image),
                categories=categories,
                published_raw=published_raw,
                published_utc=published_utc,
                copyright_holder=compiled.eval_first(element, mapping.copyright),
                raw_xml=etree.tostring(element),
            )
        )
    return items
