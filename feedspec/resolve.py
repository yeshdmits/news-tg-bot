"""Channel merge / precedence.

Effective config for a (source, channel) pair is a shallow merge:

    documented default  <  spec defaults  <  channel registry entry  <  binding

Later wins; lists replace, never concatenate. Channel-scoped keys (id,
post_interval_min, max_posts_per_run, max_age_min) can only arrive from the
first three layers — the loader rejects them on bindings.
"""

from __future__ import annotations

from typing import Any

from feedspec.model import (
    DEFAULT_CLASSIFICATION,
    UNKNOWN_CLASS_RULE,
    Binding,
    ClassificationRule,
    ErrorsConfig,
    FrozenModel,
    Mapping,
    PostStyle,
    QueuePolicy,
    SourceDef,
    SourceKind,
    Spec,
    WhenClause,
)


class EffectiveSource(FrozenModel):
    """Per-source config after applying spec defaults."""

    name: str
    type: str
    url: str
    kind: SourceKind
    region: str | None
    language: str
    fetch_limit: int
    fetch_interval_min: int
    cold_start_policy: str
    enabled: bool
    url_verified: str | None
    namespaces: dict[str, str]
    schema_file: str | None
    mapping: Mapping
    notes: str | None


class EffectiveConfig(FrozenModel):
    """Fully resolved config for one (source, channel) pair."""

    source_name: str
    channel_name: str
    chat_id: str
    channel_language: str
    # channel-scoped — resolved from defaults/registry only
    post_interval_min: int
    max_posts_per_run: int
    max_age_min: int | None
    queue_policy: QueuePolicy
    channel_enabled: bool
    source_enabled: bool
    # binding-scoped
    include_categories: tuple[str, ...]
    exclude_categories: tuple[str, ...]
    lead_max_length: int
    translate_lead: bool
    post_style: PostStyle
    when: WhenClause | None


def _effective(model: Any, key: str, defaults: dict[str, Any]) -> Any:
    """documented default < spec defaults < explicitly-set model field."""
    if key in model.model_fields_set:
        return getattr(model, key)
    if key in defaults:
        return defaults[key]
    return getattr(model, key)


def resolve_source(spec: Spec, source: SourceDef) -> EffectiveSource:
    """EffectiveSource for one source, spec defaults applied to every
    overridable field. Identity fields (name, url, language, mapping) are
    taken from the source verbatim."""
    d = spec.defaults
    return EffectiveSource(
        name=source.name,
        type=_effective(source, "type", d),
        url=source.url,
        kind=_effective(source, "kind", d),
        region=_effective(source, "region", d),
        language=source.language,
        fetch_limit=_effective(source, "fetch_limit", d),
        fetch_interval_min=_effective(source, "fetch_interval_min", d),
        cold_start_policy=_effective(source, "cold_start_policy", d),
        enabled=_effective(source, "enabled", d),
        url_verified=source.url_verified,
        namespaces=_effective(source, "namespaces", d),
        schema_file=_effective(source, "schema_file", d),
        mapping=source.mapping,
        notes=source.notes,
    )


class EffectiveChannel(FrozenModel):
    """Channel-scoped config — the per-chat knobs, resolvable only
    from the registry entry and spec defaults, never from bindings."""

    name: str
    chat_id: str
    language: str
    post_interval_min: int
    max_posts_per_run: int
    max_age_min: int | None
    queue_policy: QueuePolicy
    enabled: bool


def resolve_channel(spec: Spec, channel_name: str) -> EffectiveChannel:
    """EffectiveChannel for the named registry entry, spec defaults
    applied. Raises KeyError for an unknown name."""
    channel = spec.channel(channel_name)
    d = spec.defaults
    return EffectiveChannel(
        name=channel.name,
        chat_id=channel.id,
        language=_effective(channel, "language", d),
        post_interval_min=_effective(channel, "post_interval_min", d),
        max_posts_per_run=_effective(channel, "max_posts_per_run", d),
        max_age_min=_effective(channel, "max_age_min", d),
        queue_policy=_effective(channel, "queue_policy", d),
        enabled=_effective(channel, "enabled", d),
    )


def _binding_only(binding: Binding, defaults: dict[str, Any], key: str) -> Any:
    if key in binding.model_fields_set:
        return getattr(binding, key)
    if key in defaults:
        return defaults[key]
    return getattr(binding, key)


def resolve_pair(spec: Spec, source: SourceDef, binding: Binding) -> EffectiveConfig:
    """Fully resolved EffectiveConfig for one (source, binding) pair,
    applying the module-level precedence chain to each field according
    to its scope. Raises KeyError if the binding names an unknown
    channel (the loader normally guarantees it exists)."""
    channel = spec.channel(binding.name)
    d = spec.defaults

    # post_style falls back binding → channel explicitly → defaults → channel doc default
    if binding.post_style is not None:
        post_style = binding.post_style
    elif "post_style" in channel.model_fields_set:
        post_style = channel.post_style
    elif "post_style" in d:
        post_style = d["post_style"]
    else:
        post_style = channel.post_style

    return EffectiveConfig(
        source_name=source.name,
        channel_name=channel.name,
        chat_id=channel.id,
        channel_language=_effective(channel, "language", d),
        post_interval_min=_effective(channel, "post_interval_min", d),
        max_posts_per_run=_effective(channel, "max_posts_per_run", d),
        max_age_min=_effective(channel, "max_age_min", d),
        queue_policy=_effective(channel, "queue_policy", d),
        channel_enabled=_effective(channel, "enabled", d),
        source_enabled=resolve_source(spec, source).enabled,
        include_categories=tuple(_binding_only(binding, d, "include_categories")),
        exclude_categories=tuple(_binding_only(binding, d, "exclude_categories")),
        lead_max_length=_binding_only(binding, d, "lead_max_length"),
        translate_lead=_binding_only(binding, d, "translate_lead"),
        post_style=post_style,
        when=binding.when,
    )


def classify_error(
    errors: "ErrorsConfig | None", error_class: str, source_name: str | None = None
) -> ClassificationRule:
    """Effective classification for an error class:
    source override < spec classification < built-in default < unknown-class rule."""
    if errors is not None:
        if source_name is not None:
            override = errors.source_overrides.get(source_name, {}).get(error_class)
            if override is not None:
                return override
        rule = errors.classification.get(error_class)
        if rule is not None:
            return rule
    return DEFAULT_CLASSIFICATION.get(error_class, UNKNOWN_CLASS_RULE)


def resolve_all(spec: Spec) -> list[EffectiveConfig]:
    """All (source, channel) pairs in spec order.

    The ordering is part of the contract: the router uses spec source order as
    the stable tiebreak when sorting candidates."""
    return [
        resolve_pair(spec, source, binding)
        for source in spec.sources
        for binding in source.channels
    ]
