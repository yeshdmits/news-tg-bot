"""DeepL translation with quota guard and graceful fallback to German."""

from __future__ import annotations

import logging

import deepl

logger = logging.getLogger(__name__)

WARN_THRESHOLD = 0.80
STOP_THRESHOLD = 0.95


def truncate(text: str, max_chars: int) -> str:
    """Truncate on a word boundary with an ellipsis (saves DeepL quota,
    keeps captions tidy — video items carry very long transcript leads)."""
    if len(text) <= max_chars:
        return text
    cut = text[: max_chars - 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut + "…"


class Translator:
    def __init__(self, api_key: str, translate_lead: bool, lead_max_chars: int) -> None:
        self._client = deepl.Translator(api_key)
        self._translate_lead = translate_lead
        self._lead_max_chars = lead_max_chars

    def _quota_exceeded(self) -> bool:
        try:
            usage = self._client.get_usage()
        except deepl.DeepLException as exc:
            logger.warning("Could not check DeepL usage: %s", exc)
            return False
        if usage.character.limit:
            ratio = usage.character.count / usage.character.limit
            if ratio >= STOP_THRESHOLD:
                logger.warning(
                    "DeepL quota at %.0f%% — skipping translation, posting original text",
                    ratio * 100,
                )
                return True
            if ratio >= WARN_THRESHOLD:
                logger.warning("DeepL quota at %.0f%% of monthly limit", ratio * 100)
        return False

    def translate(
        self, title: str, lead: str, source_lang: str = "de"
    ) -> tuple[str, str | None]:
        """Return (title_en, lead_en). lead_en is None when lead translation
        is disabled. Falls back to the original text on any failure."""
        lead = truncate(lead, self._lead_max_chars)
        if self._quota_exceeded():
            return title, (lead if self._translate_lead else None)

        texts = [title]
        if self._translate_lead and lead:
            texts.append(lead)
        try:
            results = self._client.translate_text(
                texts, source_lang=source_lang.upper(), target_lang="EN-US"
            )
        except deepl.DeepLException as exc:
            logger.warning("DeepL translation failed, using original text: %s", exc)
            return title, (lead if self._translate_lead else None)

        title_en = results[0].text
        lead_en = results[1].text if len(results) > 1 else ("" if not lead else None)
        if not self._translate_lead:
            lead_en = None
        return title_en, lead_en

    def log_usage(self) -> None:
        try:
            usage = self._client.get_usage()
        except deepl.DeepLException as exc:
            logger.warning("Could not fetch DeepL usage: %s", exc)
            return
        if usage.character.limit:
            logger.info(
                "DeepL usage: %d / %d chars (%.1f%%)",
                usage.character.count,
                usage.character.limit,
                100.0 * usage.character.count / usage.character.limit,
            )
