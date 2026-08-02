"""Mapping-expression evaluation.

The expression grammar is a strict subset of XPath 1.0 plus one extension: a
trailing ``@attr`` reads an attribute of the matched element. Expressions are
therefore translated to XPath and delegated to lxml rather than parsed by
hand — ``media:content[@width='700']@url`` becomes
``media:content[@width='700']/@url``.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import lxml.html
from lxml import etree

_WS_RE = re.compile(r"\s+")


class MappingEvalError(ValueError):
    """A mapping expression is malformed or uses an unsupported directive."""


def collapse_ws(text: str) -> str:
    """Collapse whitespace runs to single spaces and trim the ends."""
    return _WS_RE.sub(" ", text).strip()


def split_attr_suffix(expr: str) -> tuple[str, str | None]:
    """Split a trailing ``@attr`` off an expression.

    The tail after the last ``@`` is an attribute suffix only when it is a
    plain name — no ``]``, ``/`` or ``=`` — which protects predicate forms:
    ``media:content[@width='700']@url`` → (``media:content[@width='700']``, ``url``)
    ``category[@domain='x']``           → (``category[@domain='x']``, None)
    """
    head, sep, tail = expr.rpartition("@")
    if not sep or not tail or any(c in tail for c in "]/="):
        return expr, None
    return head, tail


def to_xpath(expr: str) -> str:
    """Translate a mapping expression to real XPath: a trailing
    ``@attr`` becomes ``/@attr``; everything else passes through."""
    path, attr = split_attr_suffix(expr)
    return f"{path}/@{attr}" if attr else path


class CompiledMapping:
    """Evaluates mapping expressions against an element, with namespaces."""

    def __init__(self, namespaces: dict[str, str]):
        # lxml XPath rejects an empty/None prefix — drop a default namespace.
        self.namespaces = {k: v for k, v in namespaces.items() if k}

    def _eval(self, element: etree._Element, expr: str) -> list[str]:
        try:
            results = element.xpath(to_xpath(expr), namespaces=self.namespaces)
        except etree.XPathEvalError as e:
            raise MappingEvalError(f"bad mapping expression {expr!r}: {e}") from e
        values: list[str] = []
        for result in results:
            if isinstance(result, etree._Element):
                text = "".join(result.itertext())
            else:  # attribute or text() result — a smart string
                text = str(result)
            text = collapse_ws(text)
            if text:
                values.append(text)
        return values

    def eval_first(self, element: etree._Element, expr: str | tuple[str, ...] | list[str] | None) -> str | None:
        """A single expression, or an ordered fallback list: first non-empty wins."""
        if expr is None:
            return None
        exprs = (expr,) if isinstance(expr, str) else tuple(expr)
        for candidate in exprs:
            values = self._eval(element, candidate)
            if values:
                return values[0]
        return None

    def eval_all(self, element: etree._Element, expr: str | None) -> list[str]:
        """Every non-empty match in document order; [] for a None expression."""
        if expr is None:
            return []
        return self._eval(element, expr)


def apply_id_pattern(raw_id: str, pattern: str | None) -> str | None:
    """Apply the ``id_pattern`` regex; first capture group wins, whole match
    if the pattern has no groups. None when the pattern does not match."""
    if pattern is None:
        return raw_id
    match = re.search(pattern, raw_id)
    if not match:
        return None
    return match.group(1) if match.groups() else match.group(0)


def parse_rfc822(raw: str) -> datetime | None:
    """RFC 822 date string → aware UTC datetime; None when unparseable.
    A missing timezone is taken as UTC."""
    try:
        parsed = parsedate_to_datetime(raw.strip())
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_iso8601(raw: str) -> datetime | None:
    """ISO 8601 date string → aware UTC datetime; None when unparseable.
    A missing timezone is taken as UTC."""
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_published(raw: str, published_format: str | None) -> datetime | None:
    """Parse a published date per the mapping's ``published_format``;
    rfc822 is the default when the format is unset."""
    if published_format == "iso8601":
        return parse_iso8601(raw)
    return parse_rfc822(raw)


def strip_html(text: str) -> str:
    """Plain text content of an HTML fragment, whitespace-collapsed;
    falls back to regex tag-stripping when lxml cannot parse it."""
    try:
        fragment = lxml.html.fromstring(f"<div>{text}</div>")
    except etree.ParserError:
        return collapse_ws(re.sub(r"<[^>]+>", " ", text))
    return collapse_ws(fragment.text_content())


def category_from_url(url: str, directive: str) -> str | None:
    """Only ``url_path_segment:N`` is implemented (1-based over non-empty segments)."""
    kind, _, n = directive.partition(":")
    if kind != "url_path_segment":
        raise MappingEvalError(f"unsupported category_from {directive!r}")
    segments = [s for s in urlsplit(url).path.split("/") if s]
    index = int(n)
    if 1 <= index <= len(segments):
        return segments[index - 1]
    return None


def clean_lead(
    lead: str | None,
    *,
    lead_html: bool,
    lead_remove: tuple[str, ...],
) -> str | None:
    """Normalize an extracted lead: optionally strip HTML, delete
    ``lead_remove`` regex matches, collapse whitespace. An empty
    result becomes None."""
    if lead is None:
        return None
    if lead_html:
        lead = strip_html(lead)
    for pattern in lead_remove:
        lead = re.sub(pattern, "", lead)
    lead = collapse_ws(lead)
    return lead or None
