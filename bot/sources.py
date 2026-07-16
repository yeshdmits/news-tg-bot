"""News source definitions loaded from a JSON file.

Each source is described by a URL, a payload type (json/xml), a validation
schema (inline JSON Schema for json, an XSD file for xml) and a field mapping
telling the parser where each Article field lives in the payload.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import jsonschema
import xmlschema

from .config import ConfigError

# Source names end up as the "name:" prefix of content ids and in SQL LIKE
# patterns, so keep them to a safe charset.
_NAME_RE = re.compile(r"^[a-z0-9_-]+$")

SOURCE_TYPES = ("json", "xml")
PUBLISHED_FORMATS = ("iso8601", "rfc822")
_REQUIRED_MAPPING_KEYS = ("items", "id", "title", "url")


def category_hashtag(value: object) -> str | None:
    """Normalize any category text to Telegram hashtag form.

    'sport/wm-2026-in-usa' -> 'sport_wm_2026_in_usa', 'Global health' -> 'global_health'
    """
    if not value or not isinstance(value, str):
        return None
    tag = re.sub(r"[^\w]+", "_", value.strip().lower()).strip("_")
    return tag or None


@dataclass(frozen=True)
class FieldMapping:
    """Where to find Article fields inside a fetched payload.

    JSON paths are dot-separated key descents ("image.variants.big.src").
    XML paths are ElementTree paths relative to the document root for
    ``items`` and relative to an item element for the rest; an ``@attr``
    suffix reads an attribute ("media:thumbnail@url").
    """

    items: str
    id: str
    title: str
    url: str
    lead: str | None = None
    # True when the lead field contains HTML markup: tags are stripped and
    # entities unescaped before the text is used.
    lead_html: bool = False
    image: tuple[str, ...] = ()
    published: str | None = None
    published_format: str = "iso8601"
    category: str | None = None
    # Optional regex applied to the extracted id value; the first capture
    # group (or the whole match) becomes the unique record key. No match
    # falls back to the full value.
    id_pattern: re.Pattern[str] | None = None


@dataclass(frozen=True)
class SourceSpec:
    name: str
    type: str
    url: str
    language: str
    # The hashtag posted to Telegram when the feed item carries no category
    # of its own (via mapping.category).
    category: str
    mapping: FieldMapping
    queries: tuple[dict[str, str], ...] = ({},)
    url_base: str | None = None
    namespaces: dict[str, str] = field(default_factory=dict)
    json_schema: dict | None = None
    xml_schema: xmlschema.XMLSchema | None = None


def _require_str(raw: dict, key: str, where: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}: '{key}' must be a non-empty string")
    return value.strip()


def _parse_mapping(raw: dict, where: str) -> FieldMapping:
    mapping = raw.get("mapping")
    if not isinstance(mapping, dict):
        raise ConfigError(f"{where}: 'mapping' must be an object")
    for key in _REQUIRED_MAPPING_KEYS:
        _require_str(mapping, key, f"{where}.mapping")

    image = mapping.get("image")
    if image is None:
        image_paths: tuple[str, ...] = ()
    elif isinstance(image, str):
        image_paths = (image,)
    elif isinstance(image, list) and all(isinstance(p, str) for p in image):
        image_paths = tuple(image)
    else:
        raise ConfigError(f"{where}.mapping: 'image' must be a string or list of strings")

    published_format = mapping.get("published_format", "iso8601")
    if published_format not in PUBLISHED_FORMATS:
        raise ConfigError(
            f"{where}.mapping: 'published_format' must be one of "
            f"{', '.join(PUBLISHED_FORMATS)}, got {published_format!r}"
        )

    id_pattern = None
    raw_pattern = mapping.get("id_pattern")
    if raw_pattern is not None:
        if not isinstance(raw_pattern, str) or not raw_pattern:
            raise ConfigError(f"{where}.mapping: 'id_pattern' must be a non-empty string")
        try:
            id_pattern = re.compile(raw_pattern)
        except re.error as exc:
            raise ConfigError(f"{where}.mapping: invalid 'id_pattern' regex: {exc}") from exc

    lead_html = mapping.get("lead_html", False)
    if not isinstance(lead_html, bool):
        raise ConfigError(f"{where}.mapping: 'lead_html' must be a boolean")

    return FieldMapping(
        items=mapping["items"].strip(),
        id=mapping["id"].strip(),
        title=mapping["title"].strip(),
        url=mapping["url"].strip(),
        lead=mapping.get("lead") or None,
        lead_html=lead_html,
        image=image_paths,
        published=mapping.get("published") or None,
        published_format=published_format,
        category=mapping.get("category") or None,
        id_pattern=id_pattern,
    )


def _parse_queries(raw: dict, where: str) -> tuple[dict[str, str], ...]:
    queries = raw.get("queries")
    if queries is None:
        return ({},)
    if not isinstance(queries, list) or not all(isinstance(q, dict) for q in queries):
        raise ConfigError(f"{where}: 'queries' must be a list of objects")
    return tuple({str(k): str(v) for k, v in q.items()} for q in queries) or ({},)


def _parse_source(raw: object, base_dir: Path, where: str) -> SourceSpec:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: each source must be an object")

    name = _require_str(raw, "name", where)
    if not _NAME_RE.match(name):
        raise ConfigError(f"{where}: 'name' must match [a-z0-9_-]+, got {name!r}")

    source_type = _require_str(raw, "type", where)
    if source_type not in SOURCE_TYPES:
        raise ConfigError(
            f"{where}: 'type' must be one of {', '.join(SOURCE_TYPES)}, got {source_type!r}"
        )

    namespaces = raw.get("namespaces") or {}
    if not isinstance(namespaces, dict):
        raise ConfigError(f"{where}: 'namespaces' must be an object")

    json_schema = None
    xml_schema = None
    if source_type == "json":
        json_schema = raw.get("schema")
        if not isinstance(json_schema, dict):
            raise ConfigError(f"{where}: json sources need an inline JSON Schema in 'schema'")
        try:
            jsonschema.validators.validator_for(json_schema).check_schema(json_schema)
        except jsonschema.SchemaError as exc:
            raise ConfigError(f"{where}: invalid JSON Schema: {exc.message}") from exc
    else:
        schema_file = _require_str(raw, "schema_file", where)
        schema_path = base_dir / schema_file
        try:
            xml_schema = xmlschema.XMLSchema(schema_path)
        except (OSError, xmlschema.XMLSchemaException) as exc:
            raise ConfigError(f"{where}: cannot load XSD {schema_path}: {exc}") from exc

    category = category_hashtag(_require_str(raw, "category", where))
    if not category:
        raise ConfigError(f"{where}: 'category' normalizes to an empty hashtag")

    return SourceSpec(
        name=name,
        type=source_type,
        url=_require_str(raw, "url", where),
        language=_require_str(raw, "language", where).lower(),
        category=category,
        mapping=_parse_mapping(raw, where),
        queries=_parse_queries(raw, where),
        url_base=raw.get("url_base") or None,
        namespaces={str(k): str(v) for k, v in namespaces.items()},
        json_schema=json_schema,
        xml_schema=xml_schema,
    )


def load_sources(path: str) -> list[SourceSpec]:
    file = Path(path)
    if not file.is_file():
        raise ConfigError(f"Sources file not found: {path}")
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Cannot read sources file {path}: {exc}") from exc

    raw_sources = data.get("sources") if isinstance(data, dict) else None
    if not isinstance(raw_sources, list) or not raw_sources:
        raise ConfigError(f"{path}: expected a non-empty top-level 'sources' list")

    specs: list[SourceSpec] = []
    names: set[str] = set()
    for index, raw in enumerate(raw_sources):
        spec = _parse_source(raw, file.parent, f"{path}: sources[{index}]")
        if spec.name in names:
            raise ConfigError(f"{path}: duplicate source name {spec.name!r}")
        names.add(spec.name)
        specs.append(spec)
    return specs
