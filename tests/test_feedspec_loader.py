"""Spec loading and validation tests: schema and version checks, duplicate
detection, binding validation, and error-alerting configuration."""

import json

import pytest

from feedspec.loader import SpecError, load_spec
from feedspec.model import parse_cold_start
from tests.conftest import SPEC_FIXTURE


def minimal_spec() -> dict:
    return {
        "version": 2,
        "channels": [
            {"name": "news-ch", "id": "-1001"},
        ],
        "sources": [
            {
                "name": "src-a",
                "url": "https://example.org/rss",
                "language": "en",
                "url_verified": "2026-08-01",
                "channels": [{"name": "news-ch"}],
                "mapping": {
                    "items": "channel/item",
                    "id": "guid",
                    "title": "title",
                    "url": "link",
                },
            }
        ],
    }


def write_spec(tmp_path, spec: dict):
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec))
    return path


def test_minimal_spec_loads(tmp_path):
    loaded = load_spec(write_spec(tmp_path, minimal_spec()))
    assert loaded.spec.version == 2
    assert loaded.spec.sources[0].channels[0].name == "news-ch"
    assert len(loaded.spec_hash) == 64
    assert loaded.warnings == ()


def test_unknown_version_rejected(tmp_path):
    spec = minimal_spec()
    spec["version"] = 3
    with pytest.raises(SpecError, match="version"):
        load_spec(write_spec(tmp_path, spec))


@pytest.mark.parametrize(
    "key,value",
    [
        ("max_posts_per_run", 99),
        ("post_interval_min", 1),
        ("id", "-100999"),
        ("max_age_min", 60),
    ],
)
def test_channel_scoped_key_in_binding_fails_naming_the_binding(tmp_path, key, value):
    """Channel-scoped keys placed on a source→channel binding fail loudly at
    load, naming the source, channel, and offending key."""
    spec = minimal_spec()
    spec["sources"][0]["channels"][0][key] = value
    with pytest.raises(SpecError) as exc:
        load_spec(write_spec(tmp_path, spec))
    message = str(exc.value)
    assert "src-a" in message
    assert "news-ch" in message
    assert key in message
    assert "channel-scoped" in message


def test_binding_to_unknown_channel_rejected(tmp_path):
    spec = minimal_spec()
    spec["sources"][0]["channels"][0]["name"] = "nope"
    with pytest.raises(SpecError, match="unknown channel 'nope'"):
        load_spec(write_spec(tmp_path, spec))


def test_duplicate_source_names_rejected(tmp_path):
    spec = minimal_spec()
    spec["sources"].append(spec["sources"][0])
    with pytest.raises(SpecError, match="duplicate source names"):
        load_spec(write_spec(tmp_path, spec))


def test_duplicate_chat_ids_rejected(tmp_path):
    spec = minimal_spec()
    spec["channels"].append({"name": "other", "id": "-1001"})
    with pytest.raises(SpecError, match="duplicate channel chat ids"):
        load_spec(write_spec(tmp_path, spec))


def test_unknown_key_rejected(tmp_path):
    spec = minimal_spec()
    spec["sources"][0]["surprise"] = True
    with pytest.raises(SpecError, match="surprise"):
        load_spec(write_spec(tmp_path, spec))


def test_unknown_defaults_key_rejected(tmp_path):
    spec = minimal_spec()
    spec["defaults"] = {"no_such_knob": 1}
    with pytest.raises(SpecError, match="no_such_knob"):
        load_spec(write_spec(tmp_path, spec))


def test_null_url_verified_warns(tmp_path):
    spec = minimal_spec()
    del spec["sources"][0]["url_verified"]
    loaded = load_spec(write_spec(tmp_path, spec))
    assert any("url_verified" in w and "src-a" in w for w in loaded.warnings)


def test_cold_start_policy_validation(tmp_path):
    spec = minimal_spec()
    spec["sources"][0]["cold_start_policy"] = "post_newest:3"
    loaded = load_spec(write_spec(tmp_path, spec))
    assert parse_cold_start(loaded.spec.sources[0].cold_start_policy) == ("post_newest", 3)
    assert parse_cold_start("skip_all") == ("skip_all", 0)

    spec["sources"][0]["cold_start_policy"] = "post_everything"
    with pytest.raises(SpecError, match="cold_start_policy"):
        load_spec(write_spec(tmp_path, spec))


def test_source_without_bindings_is_archive_only(tmp_path):
    spec = minimal_spec()
    spec["sources"][0]["channels"] = []
    loaded = load_spec(write_spec(tmp_path, spec))
    assert loaded.spec.sources[0].channels == ()


def test_source_missing_channels_key_still_rejected(tmp_path):
    """Archive-only must be declared with an explicit []: a dropped or
    misspelled key must never silently stop a source from posting."""
    spec = minimal_spec()
    del spec["sources"][0]["channels"]
    with pytest.raises(SpecError, match="channels"):
        load_spec(write_spec(tmp_path, spec))


def test_archive_only_source_rejects_posting_cold_start(tmp_path):
    spec = minimal_spec()
    spec["sources"][0]["channels"] = []
    spec["sources"][0]["cold_start_policy"] = "post_newest:2"
    with pytest.raises(SpecError, match="archive-only.*cold_start_policy"):
        load_spec(write_spec(tmp_path, spec))


def test_archive_only_cold_start_check_uses_effective_policy(tmp_path):
    """The contradiction is just as real when the policy is inherited from
    spec defaults rather than set on the source."""
    spec = minimal_spec()
    spec["sources"][0]["channels"] = []
    spec["defaults"] = {"cold_start_policy": "post_newest:2"}
    with pytest.raises(SpecError, match="archive-only.*cold_start_policy"):
        load_spec(write_spec(tmp_path, spec))

    spec["sources"][0]["cold_start_policy"] = "skip_all"
    assert load_spec(write_spec(tmp_path, spec)).spec.sources[0].channels == ()


def test_target_spec_in_repo_is_valid():
    loaded = load_spec(SPEC_FIXTURE)
    assert len(loaded.spec.sources) == 8
    assert {c.name for c in loaded.spec.channels} == {"news-ch", "news-en", "econ-en"}
    assert loaded.spec.errors is not None
    assert loaded.spec.errors.alerting.escalation_ladder == (1, 10, 50, 200)


def test_ops_chat_id_must_not_be_a_news_channel(tmp_path):
    """Posting a stack trace into a public news channel is the one
    unrecoverable mistake — a spec whose ops chat id matches a news
    channel's chat id refuses to load."""
    spec = minimal_spec()
    spec["errors"] = {"ops_chat_id": "-1001"}  # == channel news-ch's chat id
    with pytest.raises(SpecError, match="refusing to alert into a news channel"):
        load_spec(write_spec(tmp_path, spec))


def test_errors_source_override_must_name_known_source(tmp_path):
    spec = minimal_spec()
    spec["errors"] = {
        "ops_chat_id": "-5000",
        "source_overrides": {"nonexistent": {"FetchHttpError4xx": {"severity": "info"}}},
    }
    with pytest.raises(SpecError, match="unknown source"):
        load_spec(write_spec(tmp_path, spec))


def test_classification_resolution_order(tmp_path):
    from feedspec.resolve import classify_error

    spec = minimal_spec()
    spec["errors"] = {
        "ops_chat_id": "-5000",
        "classification": {"FetchHttpError5xx": {"severity": "error", "escalate_after": 2}},
        "source_overrides": {"src-a": {"FetchHttpError5xx": {"severity": "info"}}},
    }
    loaded = load_spec(write_spec(tmp_path, spec))
    errors_cfg = loaded.spec.errors
    # source override beats spec classification beats built-in default
    assert classify_error(errors_cfg, "FetchHttpError5xx", "src-a").severity == "info"
    assert classify_error(errors_cfg, "FetchHttpError5xx", "other").severity == "error"
    assert classify_error(errors_cfg, "FetchTimeout", "src-a").severity == "warning"
    # unknown classes default to error / escalate_after 3
    unknown = classify_error(errors_cfg, "SomethingNovel", None)
    assert unknown.severity == "error"
    assert unknown.escalate_after == 3
