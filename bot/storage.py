"""SQLite storage for dedup tracking and translation caching.

An article row exists once we have seen (and possibly translated) it;
``posted_at`` is set only after the Telegram message was sent successfully.
Caching translations means a failed Telegram send does not burn DeepL quota
again on the next cycle.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    content_id INTEGER PRIMARY KEY,
    title      TEXT NOT NULL,
    title_en   TEXT,
    lead_en    TEXT,
    posted_at  TEXT
);
"""


class Storage:
    def __init__(self, db_path: str) -> None:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path)
        self._conn.execute(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def is_posted(self, content_id: int) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM articles WHERE content_id = ? AND posted_at IS NOT NULL",
            (content_id,),
        ).fetchone()
        return row is not None

    def has_any_posts(self) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM articles WHERE posted_at IS NOT NULL LIMIT 1"
        ).fetchone()
        return row is not None

    def get_translation(self, content_id: int) -> tuple[str, str | None] | None:
        """Return (title_en, lead_en) if this article was already translated."""
        row = self._conn.execute(
            "SELECT title_en, lead_en FROM articles WHERE content_id = ? AND title_en IS NOT NULL",
            (content_id,),
        ).fetchone()
        return (row[0], row[1]) if row else None

    def save_translation(
        self, content_id: int, title: str, title_en: str, lead_en: str | None
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO articles (content_id, title, title_en, lead_en)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(content_id) DO UPDATE SET title_en = excluded.title_en,
                                                  lead_en = excluded.lead_en
            """,
            (content_id, title, title_en, lead_en),
        )
        self._conn.commit()

    def mark_posted(self, content_id: int, title: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            """
            INSERT INTO articles (content_id, title, posted_at)
            VALUES (?, ?, ?)
            ON CONFLICT(content_id) DO UPDATE SET posted_at = excluded.posted_at
            """,
            (content_id, title, now),
        )
        self._conn.commit()

    def cleanup_old(self, days: int = 30) -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        cur = self._conn.execute(
            "DELETE FROM articles WHERE posted_at IS NOT NULL AND posted_at < ?",
            (cutoff,),
        )
        self._conn.commit()
        if cur.rowcount:
            logger.info("Cleaned up %d articles older than %d days", cur.rowcount, days)
        return cur.rowcount
