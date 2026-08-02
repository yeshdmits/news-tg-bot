"""Translation with a content-keyed cache.

Archive owns the table access (get/store helpers in archive.writer); this
module owns the policy: cache first, provider only on a miss, original text
on provider failure — a broken translator must never block a post.
"""

from __future__ import annotations

from typing import Protocol

import psycopg
import structlog

from archive import writer

log = structlog.get_logger(__name__)


class TranslationProvider(Protocol):
    """Provider interface: translate one string or raise."""

    name: str

    def translate(self, text: str, source_lang: str, target_lang: str) -> str:
        """Translate text; any exception is caught by TranslationService."""
        ...


class NoneProvider:
    """Disabled translation: hands every text back unchanged."""

    name = "none"

    def translate(self, text: str, source_lang: str, target_lang: str) -> str:
        """Return the text unchanged."""
        return text


class DeepLProvider:
    """DeepL with a quota guard: warn at 80 % of the character budget, stop
    translating (return original text) at 95 % so the free tier is never
    fully exhausted by the bot alone."""

    name = "deepl"
    WARN_AT, STOP_AT = 0.80, 0.95
    USAGE_CHECK_EVERY = 20

    def __init__(self, api_key: str):
        import deepl  # optional dependency: pip install .[translate]

        self._client = deepl.Translator(api_key)
        self._calls = 0
        self._quota_exceeded = False

    def _check_quota(self) -> None:
        usage = self._client.get_usage()
        if usage.character.limit:
            ratio = usage.character.count / usage.character.limit
            if ratio >= self.STOP_AT:
                self._quota_exceeded = True
                log.error("deepl_quota_stop", ratio=round(ratio, 3))
            elif ratio >= self.WARN_AT:
                log.warning("deepl_quota_warning", ratio=round(ratio, 3))

    def translate(self, text: str, source_lang: str, target_lang: str) -> str:
        """Translate via DeepL, re-checking quota every USAGE_CHECK_EVERY
        calls; past the stop threshold the original text comes back."""
        if self._calls % self.USAGE_CHECK_EVERY == 0:
            self._check_quota()
        self._calls += 1
        if self._quota_exceeded:
            return text
        result = self._client.translate_text(
            text,
            source_lang=source_lang.upper(),
            target_lang="EN-GB" if target_lang.lower() == "en" else target_lang.upper(),
        )
        return result.text


def make_provider(provider: str, api_key: str) -> TranslationProvider:
    """Provider from config; unknown names and a DeepL setup without an
    API key fall back to NoneProvider."""
    if provider == "deepl":
        if not api_key:
            log.warning("deepl_configured_without_api_key_falling_back_to_none")
            return NoneProvider()
        return DeepLProvider(api_key)
    return NoneProvider()


class TranslationService:
    """Cache-first translation over one connection. Provider failures
    degrade to the original text and queue in ``pending_errors``."""

    def __init__(self, conn: psycopg.Connection, provider: TranslationProvider):
        self.conn = conn
        self.provider = provider
        # provider failures collected here; the (async) runner drains and
        # reports them as TranslationProviderError events
        self.pending_errors: list[str] = []

    def translate_field(
        self,
        *,
        content_hash: bytes,
        field: str,
        text: str,
        source_lang: str,
        target_lang: str,
    ) -> str:
        """Cache-keyed translation. On a hit the provider is never called;
        a miss translates, stores, returns."""
        if not text or source_lang.lower() == target_lang.lower():
            return text
        cached = writer.get_cached_translation(self.conn, content_hash, field, target_lang)
        if cached is not None:
            return cached
        try:
            translated = self.provider.translate(text, source_lang, target_lang)
        except Exception as e:
            log.warning("translation_failed", field=field, error=str(e))
            self.pending_errors.append(f"{self.provider.name}: {e}")
            return text
        writer.store_translation(
            self.conn,
            chash=content_hash,
            field=field,
            target_language=target_lang,
            translated=translated,
            provider=self.provider.name,
        )
        return translated
