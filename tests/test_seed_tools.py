"""The synthetic corpus generator must generate what it claims to.

Offline tier: these exercise tools/seed.py's pure helpers only — no database.
The properties asserted here are the ones later phases depend on. If
``_perturb`` drifts outside the Hamming <= 3 threshold the corpus stops
containing near-duplicates at all, and the LSH banding work in migration 0005
would be measured against data with nothing to find.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime

import pytest

from archive.dedupe import NEAR_DUP_MAX_HAMMING, canonical_url, content_hash, hamming, simhash64
from tools.seed import DECISION_MIX, Corpus, _perturb, _raw_xml, _title, _weighted


def _rng() -> random.Random:
    return random.Random(4242)


def test_a_meaningful_share_of_syndicated_titles_is_within_the_threshold():
    """Verbatim republication must be detectable, or the corpus has nothing
    for the near-duplicate path to find."""
    rng = _rng()
    distances = [
        hamming(simhash64(t), simhash64(_perturb(t, rng)))
        for t in (_title(rng) for _ in range(400))
    ]
    caught = sum(1 for d in distances if d <= NEAR_DUP_MAX_HAMMING)
    assert 0.4 <= caught / len(distances) <= 0.7, (
        f"only {caught}/{len(distances)} syndicated titles are inside Hamming "
        f"<= {NEAR_DUP_MAX_HAMMING}; the verbatim share should put it near half"
    )


def test_editorial_rewrites_escape_the_current_threshold():
    """Pins a production finding, so a later tuning change is noticed here.

    archive.dedupe.simhash64's docstring claims character shingles "keep a
    one-word edit within the Hamming <= 3 near-duplicate threshold on short
    headline-length text". Measured over this corpus they do not: a one-word
    substitution lands at 4-16. If someone widens the threshold or changes the
    shingling, this test fails and the claim gets revisited deliberately.
    """
    rng = _rng()
    rewritten = [
        hamming(simhash64(t), simhash64(t.replace(" the ", " a ", 1)))
        for t in (_title(rng) for _ in range(200))
        if " the " in t
    ]
    assert rewritten, "vocabulary produced no titles containing ' the '"
    median = sorted(rewritten)[len(rewritten) // 2]
    assert median > NEAR_DUP_MAX_HAMMING, (
        f"a one-word edit now has median distance {median}, inside the "
        f"threshold — simhash64's docstring may have become true; re-measure"
    )


def test_distinct_titles_are_mostly_outside_the_threshold():
    """Guards the other direction: a corpus where everything collides would
    make dedupe measurements meaningless."""
    rng = _rng()
    titles = [_title(rng) for _ in range(200)]
    hashes = [simhash64(t) for t in titles]
    collisions = sum(
        1
        for i in range(len(hashes))
        for j in range(i + 1, len(hashes))
        if titles[i] != titles[j] and hamming(hashes[i], hashes[j]) <= NEAR_DUP_MAX_HAMMING
    )
    pairs = len(hashes) * (len(hashes) - 1) // 2
    assert collisions / pairs < 0.02, f"{collisions}/{pairs} unrelated titles collide"


def test_raw_xml_lands_in_the_two_to_four_kilobyte_band():
    rng = _rng()
    sizes = [
        len(_raw_xml(_title(rng), "lead text", "https://example.org/a", "Thu, 07 Aug 2026 00:00:00 +0000", rng))
        for _ in range(200)
    ]
    assert 2000 <= min(sizes), f"smallest item {min(sizes)} B is under the 2 KB floor"
    assert max(sizes) <= 4096, f"largest item {max(sizes)} B is over the 4 KB ceiling"


def test_raw_xml_is_well_formed():
    from lxml import etree

    rng = _rng()
    # Titles and leads go through _esc; a raw & or < would break the parse.
    element = etree.fromstring(
        _raw_xml("Rates & bonds <held>", "a > b", "https://e.org/x?a=1&b=2", "Thu, 07 Aug 2026 00:00:00 +0000", rng)
    )
    assert element.tag == "item"
    assert element.findtext("title") == "Rates & bonds <held>"


def test_weighted_covers_every_decision_and_respects_order_of_magnitude():
    rng = _rng()
    counts: dict[str, int] = {}
    for _ in range(20_000):
        counts[_weighted(rng, DECISION_MIX)] = counts.get(_weighted(rng, DECISION_MIX), 0) + 1
    assert set(counts) == {name for name, _ in DECISION_MIX}
    # routed must stay the minority outcome — that is what makes
    # routing_decisions the largest table and motivates routing_stats.
    assert counts["routed"] < counts["filtered_category"]


@pytest.mark.parametrize("seed", [1, 20260807])
def test_corpus_is_deterministic_for_a_given_rng_seed(seed):
    import argparse

    def build():
        args = argparse.Namespace(
            items=200, days=3, sources=10, dup_rate=0.1, batch=200, jobs=1, rng_seed=seed,
            # Pinned: without it the span is anchored to now() and two builds
            # a second apart legitimately differ.
            end=datetime(2026, 8, 7, tzinfo=UTC),
        )
        corpus = Corpus(args)
        _, item_rows, _, _, _ = next(corpus.chunks())
        return item_rows

    # Ids are drawn from the seeded RNG too (see _uuid4), so the whole corpus
    # is reproducible — including source_item_id and the URLs, which embed one.
    assert build() == build()


def test_generated_urls_canonicalise_to_themselves():
    """The seeder writes canonical_url from the production function; if the
    generated URLs were not already canonical the corpus would carry a shape
    real ingest never produces."""
    rng = _rng()
    for _ in range(100):
        url = f"https://wire-agency-01.example/2026/08/07/{rng.getrandbits(48):012x}"
        assert canonical_url(url) == url
        assert len(content_hash(url, _title(rng))) == 32
