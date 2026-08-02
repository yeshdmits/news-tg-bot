"""Translation cache behaviour against a real Postgres."""

from archive.dedupe import content_hash
from newsbot.translate import NoneProvider, TranslationService, make_provider


class CountingProvider:
    name = "counting"

    def __init__(self):
        self.calls = 0

    def translate(self, text, source_lang, target_lang):
        self.calls += 1
        return f"[{target_lang}] {text}"


class ExplodingProvider:
    name = "exploding"

    def translate(self, text, source_lang, target_lang):
        raise RuntimeError("provider down")


def test_cache_prevents_rebilling(db):
    """A repeated translation is served from the cache and never calls the
    provider again."""
    provider = CountingProvider()
    service = TranslationService(db, provider)
    chash = content_hash("https://example.org/a", "Ein Titel")

    first = service.translate_field(
        content_hash=chash, field="lead", text="Der Text",
        source_lang="de", target_lang="en",
    )
    assert first == "[en] Der Text"
    assert provider.calls == 1

    second = service.translate_field(
        content_hash=chash, field="lead", text="Der Text",
        source_lang="de", target_lang="en",
    )
    assert second == first
    assert provider.calls == 1  # cache hit — not re-billed

    row = db.execute("SELECT provider, translated FROM translations").fetchone()
    assert row["provider"] == "counting"
    assert row["translated"] == "[en] Der Text"


def test_fields_cached_independently(db):
    provider = CountingProvider()
    service = TranslationService(db, provider)
    chash = content_hash("https://example.org/a", "Ein Titel")

    service.translate_field(content_hash=chash, field="title", text="Ein Titel",
                            source_lang="de", target_lang="en")
    service.translate_field(content_hash=chash, field="lead", text="Der Text",
                            source_lang="de", target_lang="en")
    assert provider.calls == 2
    assert db.execute("SELECT count(*) AS n FROM translations").fetchone()["n"] == 2


def test_same_language_short_circuits(db):
    provider = CountingProvider()
    service = TranslationService(db, provider)
    result = service.translate_field(
        content_hash=b"\x01" * 32, field="lead", text="Hello",
        source_lang="en", target_lang="en",
    )
    assert result == "Hello"
    assert provider.calls == 0


def test_provider_failure_returns_original(db):
    """Provider failure never drops the post — the original text comes back,
    nothing is cached, and the failure is surfaced in pending_errors for the
    runner to report."""
    service = TranslationService(db, ExplodingProvider())
    result = service.translate_field(
        content_hash=b"\x01" * 32, field="lead", text="Der Text",
        source_lang="de", target_lang="en",
    )
    assert result == "Der Text"
    assert db.execute("SELECT count(*) AS n FROM translations").fetchone()["n"] == 0
    assert service.pending_errors == ["exploding: provider down"]


def test_make_provider_fallbacks():
    assert isinstance(make_provider("none", ""), NoneProvider)
    # deepl without a key must not crash the bot — it degrades to none
    assert isinstance(make_provider("deepl", ""), NoneProvider)
