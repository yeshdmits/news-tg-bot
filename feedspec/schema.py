"""JSON Schema for the spec format, generated from the pydantic models.

The committed ``spec.schema.json`` is the output of ``spec_json_schema()``
(regenerate with ``python -m cli schema > spec.schema.json``; a drift test
keeps the file honest). Editors pointed at it via the example's ``$schema``
key get autocomplete and inline errors.

The schema is deliberately looser than ``load_spec``: cross-checks such as
duplicate names, unknown channel references and channel-scoped binding keys
are not expressible in JSON Schema, and ``defaults`` stays a free-form
object (the loader's ALLOWED_DEFAULT_KEYS check remains the enforcement).
"""

from __future__ import annotations

import json
from typing import Any

from feedspec.model import Spec

SCHEMA_ID = "https://raw.githubusercontent.com/OWNER/REPO/main/spec.schema.json"


def spec_json_schema() -> dict[str, Any]:
    """The JSON Schema describing a version-2 spec document."""
    schema = Spec.model_json_schema(by_alias=True)
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "news-tg-bot spec (version 2)",
        **schema,
    }
    # The example carries a "$schema" pointer for editor support; Spec
    # forbids unknown keys, so the schema must allow this one explicitly.
    schema.setdefault("properties", {})["$schema"] = {
        "type": "string",
        "description": "Editor pointer at spec.schema.json; ignored by the loader.",
    }
    return schema


def iter_schema_errors(raw_bytes: bytes) -> list[str]:
    """Every JSON-Schema violation in ``raw_bytes``, as printable strings.

    Returns an empty list for valid documents and for byte strings that are
    not JSON at all — the loader reports those with a better message."""
    import jsonschema

    try:
        raw = json.loads(raw_bytes)
    except json.JSONDecodeError:
        return []
    validator = jsonschema.Draft202012Validator(spec_json_schema())
    return [
        "/" + "/".join(str(p) for p in error.absolute_path) + f": {error.message}"
        for error in validator.iter_errors(raw)
    ]
