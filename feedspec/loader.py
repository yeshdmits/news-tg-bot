"""Spec parsing and validation. Pure — no I/O beyond the optional
``load_spec`` file-read convenience; acquisition from env/URL lives in
``specsource``."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from feedspec.model import Binding, ChannelDef, SourceDef, Spec

# Properties of the Telegram chat, not of the source→chat relationship.
# They resolve only from the channel registry; a binding declaring one is a
# spec bug and must fail loudly at load time.
CHANNEL_SCOPED: frozenset[str] = frozenset(
    {"id", "post_interval_min", "max_posts_per_run", "max_age_min"}
)

ALLOWED_DEFAULT_KEYS: frozenset[str] = frozenset(
    (set(Binding.model_fields) - {"name"})
    | (set(SourceDef.model_fields) - {"name", "url", "language", "channels", "mapping", "notes", "url_verified"})
    | (set(ChannelDef.model_fields) - {"name", "id"})
)


class SpecError(ValueError):
    """The spec file is invalid. The message names the offending element."""


@dataclass(frozen=True)
class LoadedSpec:
    """A validated spec together with the hash of the exact bytes it came
    from, the raw JSON, and any non-fatal warnings."""

    spec: Spec
    spec_hash: str
    raw: dict[str, Any]
    warnings: tuple[str, ...] = field(default=())


def spec_hash_of(raw_bytes: bytes) -> str:
    """Content hash identifying an exact spec revision."""
    return hashlib.sha256(raw_bytes).hexdigest()


def _reject_channel_scoped_binding_keys(raw: dict[str, Any]) -> None:
    for source in raw.get("sources", ()):
        if not isinstance(source, dict):
            continue
        source_name = source.get("name", "<unnamed>")
        for binding in source.get("channels", ()):
            if not isinstance(binding, dict):
                continue
            bad = CHANNEL_SCOPED & binding.keys()
            if bad:
                channel_name = binding.get("name", "<unnamed>")
                raise SpecError(
                    f"source '{source_name}' binding to channel '{channel_name}': "
                    f"key(s) {sorted(bad)} are channel-scoped and must be set in the "
                    f"channel registry, not on a binding"
                )


def _cross_checks(spec: Spec) -> None:
    channel_names = [c.name for c in spec.channels]
    if len(channel_names) != len(set(channel_names)):
        raise SpecError(f"duplicate channel names: {sorted(channel_names)}")
    chat_ids = [c.id for c in spec.channels]
    if len(chat_ids) != len(set(chat_ids)):
        raise SpecError(f"duplicate channel chat ids: {sorted(chat_ids)}")
    source_names = [s.name for s in spec.sources]
    if len(source_names) != len(set(source_names)):
        raise SpecError(f"duplicate source names: {sorted(source_names)}")

    registry = set(channel_names)
    for source in spec.sources:
        seen: set[str] = set()
        for binding in source.channels:
            if binding.name not in registry:
                raise SpecError(
                    f"source '{source.name}' binds to unknown channel '{binding.name}'"
                )
            if binding.name in seen:
                raise SpecError(
                    f"source '{source.name}' binds channel '{binding.name}' twice"
                )
            seen.add(binding.name)

    unknown_defaults = set(spec.defaults) - ALLOWED_DEFAULT_KEYS
    if unknown_defaults:
        raise SpecError(f"unknown key(s) in defaults: {sorted(unknown_defaults)}")

    # Startup guard: posting a stack trace into a public news channel is
    # the one unrecoverable mistake in this design.
    if spec.errors is not None:
        clash = next((c for c in spec.channels if c.id == spec.errors.ops_chat_id), None)
        if clash is not None:
            raise SpecError(
                f"errors.ops_chat_id {spec.errors.ops_chat_id!r} is also the chat id of "
                f"news channel '{clash.name}' — refusing to alert into a news channel"
            )
        unknown_sources = set(spec.errors.source_overrides) - {s.name for s in spec.sources}
        if unknown_sources:
            raise SpecError(
                f"errors.source_overrides for unknown source(s): {sorted(unknown_sources)}"
            )


def _collect_warnings(spec: Spec) -> tuple[str, ...]:
    return tuple(
        f"source '{s.name}': url_verified is null — verify the feed URL and set a date"
        for s in spec.sources
        if s.url_verified is None
    )


def load_spec_bytes(raw_bytes: bytes) -> LoadedSpec:
    """Parse and fully validate a version-2 spec from its exact bytes.

    The bytes may come from a file, an environment variable or an HTTPS
    fetch — the loader does not care. ``spec_hash`` is always the hash of
    exactly these bytes, and ``raw`` is the parsed JSON unmodified, so the
    archive's ``spec_versions`` rows stay faithful to what was loaded.

    Raises SpecError on any problem — bad JSON, wrong version,
    channel-scoped keys on a binding, pydantic validation failure, or a
    failed cross-check (duplicate names/ids, unknown channel references).
    """
    try:
        raw = json.loads(raw_bytes)
    except json.JSONDecodeError as e:
        raise SpecError(f"spec is not valid JSON: {e}") from e
    if not isinstance(raw, dict):
        raise SpecError("spec root must be a JSON object")

    version = raw.get("version")
    if version != 2:
        raise SpecError(f"unknown spec version {version!r}; this loader implements version 2")

    _reject_channel_scoped_binding_keys(raw)

    # "$schema" is an editor affordance (points at spec.schema.json), not
    # part of the format; Spec forbids unknown keys, so validate a copy
    # without it while keeping ``raw`` byte-faithful.
    try:
        spec = Spec.model_validate({k: v for k, v in raw.items() if k != "$schema"})
    except ValidationError as e:
        raise SpecError(f"spec failed validation:\n{e}") from e

    _cross_checks(spec)

    return LoadedSpec(
        spec=spec,
        spec_hash=spec_hash_of(raw_bytes),
        raw=raw,
        warnings=_collect_warnings(spec),
    )


def load_spec(path: Path | str) -> LoadedSpec:
    """Read, parse and fully validate a version-2 spec file."""
    return load_spec_bytes(Path(path).read_bytes())
