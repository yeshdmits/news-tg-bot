"""Spec acquisition: decide where the configuration comes from and fetch it.

The application reads its configuration from exactly one of three sources,
checked in this order, first one present wins:

    SPEC_JSON   the full spec as an inline string (cloud deployments inject
                it from a secret store; no filesystem needed)
    SPEC_URL    fetched over HTTPS at startup (large specs, or updating
                without redeploying)
    SPEC_PATH   a file path (local development and testing)

A failure to acquire from the selected source is fatal — the chain never
falls through to a lower-precedence source, so a network blip cannot
silently start the program on the wrong configuration.

The bytes are handed to ``feedspec.loader.load_spec_bytes`` untouched: the
spec hash is computed over exactly what was acquired, whichever source it
came from.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import structlog

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle
    import httpx

    from cli import Settings
    from feedspec.loader import LoadedSpec

log = structlog.get_logger(__name__)

NO_SOURCE_MESSAGE = (
    "no spec configured — set one of:\n"
    "  SPEC_JSON  the full spec JSON as an inline string\n"
    "  SPEC_URL   an https:// URL to fetch the spec from\n"
    "  SPEC_PATH  a path to a spec file\n"
    "spec.example.json in the repository root is a complete working example."
)


class SpecSourceError(RuntimeError):
    """The spec could not be acquired (no source configured, unreadable
    file, or a failed URL fetch). Distinct from feedspec.loader.SpecError,
    which means the acquired bytes are not a valid spec."""


@dataclass(frozen=True)
class SpecSource:
    """Which source won the precedence check. ``location`` never contains
    spec content — it is safe to log."""

    kind: Literal["inline", "url", "path"]
    location: str


def choose_source(settings: Settings, *, override_path: str | None = None) -> SpecSource:
    """Pick the spec source: an explicit --spec path beats everything, then
    SPEC_JSON > SPEC_URL > SPEC_PATH. Raises SpecSourceError when nothing
    is configured."""
    if override_path:
        return SpecSource(kind="path", location=override_path)
    if settings.spec_json:
        return SpecSource(kind="inline", location="<env:SPEC_JSON>")
    if settings.spec_url:
        return SpecSource(kind="url", location=settings.spec_url)
    if settings.spec_path:
        return SpecSource(kind="path", location=settings.spec_path)
    raise SpecSourceError(NO_SOURCE_MESSAGE)


def fetch_bytes(
    source: SpecSource,
    settings: Settings,
    *,
    transport: httpx.BaseTransport | None = None,
) -> bytes:
    """Acquire the exact spec bytes from the chosen source.

    Never falls through: a failure here is a SpecSourceError, not a cue to
    try the next source. ``transport`` exists for tests (httpx.MockTransport).
    """
    if source.kind == "inline":
        return settings.spec_json.encode("utf-8")

    if source.kind == "url":
        import httpx

        if not source.location.startswith("https://"):
            raise SpecSourceError(
                f"SPEC_URL must be https:// — refusing to fetch {source.location!r}"
            )
        try:
            with httpx.Client(transport=transport, follow_redirects=True, timeout=10) as client:
                response = client.get(source.location)
        except httpx.HTTPError as e:
            raise SpecSourceError(f"SPEC_URL fetch failed: {e}") from e
        if response.status_code != 200:
            raise SpecSourceError(
                f"SPEC_URL fetch failed: HTTP {response.status_code} from {source.location}"
            )
        return response.content

    try:
        return Path(source.location).read_bytes()
    except OSError as e:
        raise SpecSourceError(f"cannot read spec file {source.location}: {e}") from e


def acquire_spec(
    settings: Settings,
    *,
    override_path: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> tuple[SpecSource, bytes]:
    """choose_source + fetch_bytes, logging which source was used and a
    content hash — never the content itself, which names channel ids."""
    from feedspec.loader import spec_hash_of

    source = choose_source(settings, override_path=override_path)
    raw_bytes = fetch_bytes(source, settings, transport=transport)
    log.info(
        "spec_loaded",
        source=source.kind,
        location=source.location,
        spec_sha256=spec_hash_of(raw_bytes),
    )
    return source, raw_bytes


def load_spec_for(
    settings: Settings,
    *,
    override_path: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> LoadedSpec:
    """Acquire and validate in one call — the path used by commands that do
    not need the raw bytes afterwards."""
    from feedspec.loader import load_spec_bytes

    _, raw_bytes = acquire_spec(settings, override_path=override_path, transport=transport)
    return load_spec_bytes(raw_bytes)


def spec_base_dir(settings: Settings, source: SpecSource) -> str:
    """Directory against which a source's relative ``schema_file`` (XSD)
    paths resolve. For file-based specs that is the spec's own directory;
    for inline/URL specs there is no such directory, so SPEC_BASE_DIR (or
    the working directory, where the image keeps schemas/) is used."""
    if settings.spec_base_dir:
        return settings.spec_base_dir
    if source.kind == "path":
        return str(Path(source.location).parent)
    return "."
