"""spec.schema.json stays in sync with the models, and spec.example.json
stays valid, neutral, and demonstrative."""

import json
from pathlib import Path

import jsonschema

from feedspec.loader import load_spec
from feedspec.schema import iter_schema_errors, spec_json_schema

EXAMPLE = Path("spec.example.json")
SCHEMA = Path("spec.schema.json")

# Every Telegram id in tracked examples/tests must come from this block —
# scripts/check-neutral.sh greps for real-looking ids that lack it.
FAKE_ID_TOKEN = "9999000"


def test_schema_file_matches_generator():
    """Regenerate with: python -m cli schema > spec.schema.json"""
    committed = json.loads(SCHEMA.read_text())
    assert committed == spec_json_schema()


def test_example_passes_loader():
    loaded = load_spec(EXAMPLE)
    assert loaded.spec.version == 2
    # The only warning allowed is the deliberate url_verified reminder on
    # the disabled demo source.
    assert all("ecb-fx-usd" in w for w in loaded.warnings)


def test_example_passes_json_schema():
    assert iter_schema_errors(EXAMPLE.read_bytes()) == []


def test_example_declares_schema_pointer():
    raw = json.loads(EXAMPLE.read_text())
    assert raw["$schema"] == "./spec.schema.json"


def test_example_ids_are_obviously_fake():
    raw = json.loads(EXAMPLE.read_text())
    ids = [c["id"] for c in raw["channels"]] + [raw["errors"]["ops_chat_id"]]
    ids += [str(uid) for uid in raw["errors"]["authorization"]["write_allowlist"]]
    for chat_id in ids:
        assert FAKE_ID_TOKEN in chat_id, chat_id


def test_example_uses_only_official_sources():
    raw = json.loads(EXAMPLE.read_text())
    official = ("ecb.europa.eu", "federalreserve.gov", "snb.ch")
    for source in raw["sources"]:
        assert any(domain in source["url"] for domain in official), source["url"]


def test_example_demonstrates_the_feature_surface():
    """The example is the living documentation of the format — keep the
    less-obvious features present when editing it."""
    text = EXAMPLE.read_text()
    for feature in (
        '"when"',
        '"post_newest:1"',
        '"text_only"',
        '"enabled": false',
        '"include_categories"',
        '"max_age_min": null,',
        '"statistics"',
        '"ops_topic_id": 42',
        '"write_allowlist": [999900001]',
        '"copyright"',
    ):
        assert feature in text, feature


def test_schema_rejects_unknown_keys_and_bad_types():
    good = json.loads(EXAMPLE.read_bytes())

    bogus_key = dict(good, flux_capacitor=True)
    assert any(
        "flux_capacitor" in e for e in iter_schema_errors(json.dumps(bogus_key).encode())
    )

    bad_type = json.loads(EXAMPLE.read_text())
    bad_type["channels"][0]["post_interval_min"] = "thirty"
    assert iter_schema_errors(json.dumps(bad_type).encode()) != []


def test_schema_is_a_valid_draft_2020_12_schema():
    jsonschema.Draft202012Validator.check_schema(spec_json_schema())


def test_non_json_bytes_are_left_to_the_loader():
    assert iter_schema_errors(b"not json at all") == []
