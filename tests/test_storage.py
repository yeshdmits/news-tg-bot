"""Storage tests: dedup, translation cache, and per-source posted tracking."""

from bot.storage import Storage


def test_posted_tracking_and_per_source_checks(tmp_path):
    storage = Storage(str(tmp_path / "posted.db"))
    storage.mark_posted("some-source:guid#0", "A title")
    assert storage.is_posted("some-source:guid#0")
    assert not storage.is_posted("some-source:other")
    assert storage.has_any_posts("some-source")
    assert not storage.has_any_posts("other-source")
    storage.close()


def test_translation_cache_does_not_mark_posted(tmp_path):
    storage = Storage(str(tmp_path / "posted.db"))
    storage.save_translation("src:1", "Titel", "Title", "lead")
    assert storage.get_translation("src:1") == ("Title", "lead")
    assert not storage.is_posted("src:1")
    assert not storage.has_any_posts("src")

    storage.mark_posted("src:1", "Titel")
    assert storage.is_posted("src:1")
    assert storage.get_translation("src:1") == ("Title", "lead")
    storage.close()
