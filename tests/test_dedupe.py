"""Unit tests for the dedupe primitives: URL canonicalisation, title
normalisation, content hashing and simhash distance."""

from archive.dedupe import (
    canonical_url,
    content_hash,
    hamming,
    normalise_title,
    simhash64,
)


def test_canonical_url_strips_tracking_params():
    """URLs differing only by tracking params must canonicalise identically,
    so the same story hashes the same (the swissinfo case)."""
    a = "https://www.swissinfo.ch/eng/story?utm_source=rss&utm_medium=feed&linkType=rss"
    b = "https://www.swissinfo.ch/eng/story"
    assert canonical_url(a) == canonical_url(b)
    assert content_hash(canonical_url(a), "T") == content_hash(canonical_url(b), "T")


def test_canonical_url_keeps_meaningful_params():
    url = "https://example.org/watch?v=abc123&utm_campaign=x"
    assert canonical_url(url) == "https://example.org/watch?v=abc123"


def test_canonical_url_host_case_fragment_slash():
    assert (
        canonical_url("HTTPS://Example.ORG/Path/#section")
        == canonical_url("https://example.org/Path")
    )
    # path case is preserved — only the host folds
    assert canonical_url("https://example.org/Path") == "https://example.org/Path"


def test_canonical_url_drops_ref_and_fbclid():
    url = "https://example.org/a?ref=twitter&fbclid=XYZ&id=7"
    assert canonical_url(url) == "https://example.org/a?id=7"


def test_normalise_title_accent_fold_case_whitespace():
    assert normalise_title("Zürich  wählt\nneu") == "zurich wahlt neu"


def test_content_hash_is_stable_and_distinct():
    assert content_hash("https://a", "Title") == content_hash("https://a", "title ")
    assert content_hash("https://a", "Title") != content_hash("https://b", "Title")
    assert len(content_hash("https://a", "x")) == 32


def test_simhash_normalised_identical_titles_within_threshold():
    """The realistic near-dup case: the same story carried by two feeds with
    the same headline (fed-press-all vs fed-monetary) but different URLs.
    Case, accents, punctuation and whitespace differences must not matter."""
    a = simhash64("Federal Reserve issues FOMC statement!")
    b = simhash64("  federal reserve issues fomc statement")
    assert hamming(a, b) <= 3

    zurich = simhash64("Zürich: police close A1 motorway")
    zurich_ascii = simhash64("Zurich police close A1 motorway")
    assert hamming(zurich, zurich_ascii) <= 3


def test_simhash_distinct_titles_are_far():
    a = simhash64("ECB raises interest rates by 25 basis points")
    c = simhash64("Local festival cancelled after storm warning")
    assert hamming(a, c) > 3


def test_simhash_fits_signed_bigint():
    for title in ("a", "Zürich wählt neu", "ECB raises rates", ""):
        value = simhash64(title)
        assert -(1 << 63) <= value < (1 << 63)


def test_hamming():
    assert hamming(0, 0) == 0
    assert hamming(0b1011, 0b0010) == 2
    # signed representations of the same bit pattern
    assert hamming(-1, (1 << 64) - 1) == 0
