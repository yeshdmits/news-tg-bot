"""Effective-config resolution tests: defaults → channel → binding precedence,
list replacement semantics, and resolution of the full reference spec fixture."""

import json

from feedspec.loader import load_spec
from feedspec.resolve import resolve_all, resolve_pair, resolve_source
from tests.conftest import SPEC_FIXTURE


def build(tmp_path, **overrides) -> "Spec":
    spec = {
        "version": 2,
        "defaults": {
            "lead_max_length": 250,
            "post_style": "text_only",
            "fetch_interval_min": 45,
            "translate_lead": False,
        },
        "channels": [
            {"name": "ch-a", "id": "-1001", "post_style": "photo_full", "max_posts_per_run": 4},
            {"name": "ch-b", "id": "-1002"},
        ],
        "sources": [
            {
                "name": "src",
                "url": "https://example.org/rss",
                "language": "de",
                "url_verified": "2026-08-01",
                "channels": [
                    {"name": "ch-a", "lead_max_length": 500, "translate_lead": True},
                    {"name": "ch-b"},
                ],
                "mapping": {
                    "items": "channel/item",
                    "id": "guid",
                    "title": "title",
                    "url": "link",
                },
            }
        ],
    }
    for key, value in overrides.items():
        spec[key] = value
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec))
    return load_spec(path).spec


def test_precedence_defaults_channel_binding(tmp_path):
    spec = build(tmp_path)
    pair_a, pair_b = resolve_all(spec)

    # binding wins over defaults
    assert pair_a.lead_max_length == 500
    # defaults win where binding and channel are silent
    assert pair_b.lead_max_length == 250
    # channel wins over defaults
    assert pair_a.post_style == "photo_full"
    # defaults win over the documented channel default (link_preview)
    assert pair_b.post_style == "text_only"
    # documented default when nothing sets a value
    assert pair_b.post_interval_min == 30
    # channel registry explicit value
    assert pair_a.max_posts_per_run == 4


def test_channel_scoped_only_from_registry(tmp_path):
    """max_posts_per_run etc. come from the channel entry, never the binding."""
    spec = build(tmp_path)
    pair_a, pair_b = resolve_all(spec)
    assert pair_a.chat_id == "-1001"
    assert pair_b.chat_id == "-1002"
    assert pair_b.max_posts_per_run == 5  # documented default, untouched by binding


def test_lists_replace_never_concatenate(tmp_path):
    spec = build(
        tmp_path,
        defaults={"exclude_categories": ["sport", "obit"]},
    )
    source = spec.sources[0]
    binding_a = source.channels[0]
    # binding for ch-a does not set exclude_categories → defaults list applies
    cfg_a = resolve_pair(spec, source, binding_a)
    assert cfg_a.exclude_categories == ("sport", "obit")


def test_binding_list_overrides_defaults_entirely(tmp_path):
    spec = build(
        tmp_path,
        defaults={"exclude_categories": ["sport", "obit"]},
        sources=[
            {
                "name": "src",
                "url": "https://example.org/rss",
                "language": "en",
                "url_verified": "2026-08-01",
                "channels": [{"name": "ch-a", "exclude_categories": ["weather"]}],
                "mapping": {
                    "items": "channel/item",
                    "id": "guid",
                    "title": "title",
                    "url": "link",
                },
            }
        ],
    )
    cfg = resolve_all(spec)[0]
    assert cfg.exclude_categories == ("weather",)  # replaced, not merged


def test_empty_include_categories_means_no_filter_not_include_nothing(tmp_path):
    """An explicit include_categories: [] survives resolution as an empty
    tuple, meaning 'no filter' — it must never be interpreted as
    'include nothing'."""
    spec = build(
        tmp_path,
        sources=[
            {
                "name": "src",
                "url": "https://example.org/rss",
                "language": "en",
                "url_verified": "2026-08-01",
                "channels": [{"name": "ch-a", "include_categories": []}],
                "mapping": {
                    "items": "channel/item",
                    "id": "guid",
                    "title": "title",
                    "url": "link",
                },
            }
        ],
    )
    cfg = resolve_all(spec)[0]
    assert cfg.include_categories == ()


def test_source_defaults_merge(tmp_path):
    spec = build(tmp_path)
    eff = resolve_source(spec, spec.sources[0])
    assert eff.fetch_interval_min == 45  # from defaults
    assert eff.fetch_limit == 10  # documented default


def test_resolve_all_preserves_spec_order(tmp_path):
    spec = build(tmp_path)
    pairs = resolve_all(spec)
    assert [(p.source_name, p.channel_name) for p in pairs] == [
        ("src", "ch-a"),
        ("src", "ch-b"),
    ]


def test_target_spec_resolution():
    spec = load_spec(SPEC_FIXTURE).spec
    pairs = {(p.source_name, p.channel_name): p for p in resolve_all(spec)}
    assert len(pairs) == 8

    swiss = pairs[("swissinfo", "news-ch")]
    assert swiss.post_style == "photo_full"
    assert swiss.translate_lead is True
    assert swiss.max_posts_per_run == 5
    assert swiss.max_age_min == 720

    ecb = pairs[("ecb-press", "econ-en")]
    assert ecb.lead_max_length == 400  # binding beats defaults' 300
    assert ecb.post_style == "link_preview"
    assert ecb.queue_policy == "drain_all"
    assert ecb.post_interval_min == 5

    # snb-press's enabled flag is operator-tunable — assert only that the
    # resolver reflects whatever the spec says, not a fixed value
    snb = pairs[("snb-press", "econ-en")]
    snb_def = next(s for s in spec.sources if s.name == "snb-press")
    assert snb.source_enabled == snb_def.enabled
