"""Post formatting: lead truncation, translation, post styles.

Post structure:

    <b>Title</b>

    Lead, truncated on a word boundary.

    Read more | #source_name #REGION

"Read more" is an HTML anchor to the article, so the raw URL never clutters
the message; the hashtags come from the source's name and region, making
posts filterable per outlet inside Telegram.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from archive.models import ItemRecord
from feedspec.resolve import EffectiveConfig, EffectiveSource
from newsbot.translate import TranslationService

TELEGRAM_MESSAGE_LIMIT = 4096
TELEGRAM_CAPTION_LIMIT = 1024
ELLIPSIS = "…"
_MIN_LEAD_BUDGET = 40


@dataclass(frozen=True)
class Post:
    """One ready-to-send Telegram message."""

    chat_id: str
    text: str
    image_url: str | None  # set → sendPhoto with text as caption
    link_preview: bool


def truncate_word_boundary(text: str, max_chars: int) -> str:
    """Truncate on a word boundary with an ellipsis appended."""
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    head, _, _ = cut.rpartition(" ")
    return (head.rstrip() or cut[: max_chars - len(ELLIPSIS)].rstrip()) + ELLIPSIS


def hashtag(text: str, *, lower: bool = True) -> str:
    """Telegram-safe hashtag: runs of non-alphanumerics become underscores."""
    slug = re.sub(r"[^0-9A-Za-z]+", "_", text).strip("_")
    return "#" + (slug.lower() if lower else slug)


def format_post(
    item: ItemRecord,
    cfg: EffectiveConfig,
    translator: TranslationService,
    source: EffectiveSource,
) -> Post:
    """Build the Post for one (item, channel) pair: translate title/lead when
    the binding asks for it, fit the lead inside the Telegram limit for the
    chosen style (caption for photo_full with an image, message otherwise),
    and assemble the title/lead/footer HTML."""
    title, lead = item.title, item.lead_original
    if cfg.translate_lead and source.language.lower() != cfg.channel_language.lower():
        title = translator.translate_field(
            content_hash=item.content_hash, field="title", text=title,
            source_lang=source.language, target_lang=cfg.channel_language,
        )
        if lead:
            lead = translator.translate_field(
                content_hash=item.content_hash, field="lead", text=lead,
                source_lang=source.language, target_lang=cfg.channel_language,
            )

    tags = [hashtag(source.name)]
    if source.region:
        tags.append(hashtag(source.region, lower=False))
    footer = (
        f'<a href="{html.escape(item.url_raw, quote=True)}">Read more</a>'
        f" | {' '.join(tags)}"
    )
    title_html = f"<b>{html.escape(title)}</b>"

    use_photo = cfg.post_style == "photo_full" and item.image_url is not None
    limit = TELEGRAM_CAPTION_LIMIT if use_photo else TELEGRAM_MESSAGE_LIMIT

    # The lead absorbs any overflow — footer markup must never be cut, or the
    # HTML breaks and Telegram rejects the send.
    lead = truncate_word_boundary(lead, cfg.lead_max_length) if lead else None
    if lead == title:
        lead = None  # e.g. the title-fallback leads of press feeds
    if lead:
        lead_budget = limit - len(title_html) - len(footer) - 4  # "\n\n" × 2
        if lead_budget < _MIN_LEAD_BUDGET:
            lead = None
        elif len(html.escape(lead)) > lead_budget:
            # escaping only grows text, so trimming the raw lead by the
            # escaped overshoot always converges below the budget
            overshoot = len(html.escape(lead)) - lead_budget
            lead = truncate_word_boundary(lead, max(len(lead) - overshoot, _MIN_LEAD_BUDGET))

    parts = [title_html]
    if lead:
        parts.append(html.escape(lead))
    parts.append(footer)
    text = "\n\n".join(parts)

    if use_photo:
        return Post(chat_id=cfg.chat_id, text=text, image_url=item.image_url,
                    link_preview=False)
    return Post(
        chat_id=cfg.chat_id,
        text=text,
        image_url=None,
        link_preview=cfg.post_style != "text_only",
    )
