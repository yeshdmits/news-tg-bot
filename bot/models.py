"""Domain models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class Article:
    content_id: str
    title: str
    lead: str
    url: str
    image_url: str | None
    published_at: datetime | None
    category: str
    # Channel key from the source definition; resolved to a Telegram chat id
    # via <KEY>_TELEGRAM_CHANNEL_ID.
    channel: str = ""
    language: str = "de"
    title_en: str | None = None
    lead_en: str | None = None

    @property
    def source(self) -> str:
        """The source name prefix of the namespaced content id."""
        return self.content_id.split(":", 1)[0]

    @property
    def display_title(self) -> str:
        return self.title_en or self.title

    @property
    def display_lead(self) -> str:
        return self.lead_en if self.lead_en is not None else self.lead
