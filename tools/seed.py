"""Synthetic archive data, for proving retention and scale locally.

Writes directly to Postgres with COPY rather than going through the runner:
this seeds *state*, it is not a load test (that is tools/loadtest.py). The
shapes it reproduces are the ones the scaling work depends on:

* ~50 sources with a realistic long-tail volume skew;
* 2-4 KB of RSS XML per item, so raw_xml sizing is honest;
* wire-story syndication — a configurable share of items republish an earlier
  item from a *different* source: some with the agency headline verbatim
  (Hamming 0, caught by the simhash branch), some genuinely rewritten (usually
  outside the Hamming <= 3 threshold — see ``_perturb``), plus a smaller share
  of exact same-URL duplicates that the content_hash branch catches;
* 1-4 categories per item, a spread of languages, plausible published_utc
  skew behind first_seen_utc;
* backdating, so a 7-day retention window and a weekly archive run can be
  exercised without waiting a week.

Determinism: every run with the same --rng-seed *and* --end produces the same
corpus, so a measurement can be repeated. --end defaults to now, which is what
makes a plain re-run differ.

    python -m tools.seed --items 100000 --days 10

Hash columns are computed with the production functions in archive.dedupe,
not reimplemented, so dedupe and the Phase 3 LSH backfill see realistic data.
simhash64 costs ~700 us/title, which dominates the run; --jobs fans it out
across processes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
import uuid
from datetime import UTC, datetime, timedelta
from multiprocessing import Pool

import psycopg
from psycopg.types.json import Jsonb

from archive.dedupe import canonical_url, content_hash, simhash64, simhash_bands
from archive.ids import timestamp_of, uuid7_at
from archive.writer import RETAINED_DECISIONS

# --- corpus shape ----------------------------------------------------------

LANGUAGES = ("en", "de", "fr", "it", "es")

# Neutral, obviously-synthetic source names (NEUTRALISATION.md: nothing in the
# tracked tree may carry a real operator's values).
SOURCE_PREFIXES = (
    "wire-agency", "daily-record", "evening-post", "capital-times",
    "regional-herald", "business-review", "morning-dispatch", "national-observer",
    "weekly-ledger", "coastal-gazette",
)

CHANNEL_NAMES = (
    "news-en", "news-de", "econ-en", "econ-de", "world-en", "archive-only",
)

CATEGORIES = (
    "politics", "economy", "markets", "technology", "science", "health",
    "culture", "sport", "environment", "energy", "labour", "housing",
    "transport", "education", "security", "trade",
)

# Headline vocabulary. Deliberately dry and repetitive — real headlines repeat
# structure too, and it keeps near-duplicate detection meaningful.
SUBJECTS = (
    "central bank", "finance ministry", "statistics office", "labour union",
    "energy regulator", "transport authority", "housing agency", "trade body",
    "health service", "research council", "port authority", "grid operator",
)
VERBS = (
    "holds", "raises", "lowers", "reviews", "publishes", "confirms",
    "postpones", "expands", "defends", "questions", "approves", "suspends",
)
OBJECTS = (
    "interest rates", "the quarterly outlook", "the inflation target",
    "wage guidance", "the capital buffer", "grid capacity plans",
    "the housing subsidy", "border checks", "the emissions cap",
    "rail investment", "the budget forecast", "licence conditions",
)
QUALIFIERS = (
    "as inflation cools", "amid weak demand", "after a sharp revision",
    "despite union objections", "ahead of the autumn review",
    "following a supervisory report", "under pressure from exporters",
    "as the labour market tightens",
)

# Filler for the RSS description, sized so each item lands in the 2-4 KB band.
FILLER = (
    "The measure takes effect at the start of the next reporting period. "
    "Officials said the decision reflected data published earlier in the week "
    "and would be reviewed if conditions change materially. Analysts had "
    "expected the outcome, though several noted the accompanying guidance was "
    "more cautious than the previous statement. A full assessment is due to be "
    "released alongside the next set of projections. "
)

DECISION_MIX = (
    # (decision, weight) — the routed share is deliberately small, which is
    # what makes routing_decisions the largest table in the system today.
    ("routed", 8),
    ("filtered_category", 34),
    ("predicate_failed", 28),
    ("too_old", 14),
    ("duplicate", 7),
    ("cold_start_skip", 5),
    ("rate_limited", 3),
    ("channel_disabled", 1),
)

ITEM_COLUMNS = (
    "item_id", "source_name", "fetch_id", "source_item_id", "raw_xml", "title",
    "lead_original", "lead_language", "url_raw", "canonical_url", "image_url",
    "published_raw", "published_utc", "first_seen_utc", "content_hash",
    "title_simhash", "duplicate_of", "copyright_holder", "spec_hash",
)
ITEM_TYPES = (
    "uuid", "text", "uuid", "text", "bytea", "text",
    "text", "text", "text", "text", "text",
    "text", "timestamptz", "timestamptz", "bytea",
    "int8", "uuid", "text", "text",
)

# The dedupe key written alongside every item. Seeding items without keys
# would produce a corpus in exactly the state migration 0005 refuses to leave
# behind: articles that re-ingest as new when their source republishes them.
KEY_COLUMNS = (
    "source_name", "source_item_id", "item_id", "content_hash", "title_simhash",
    "band0", "band1", "band2", "band3", "canonical_url", "duplicate_of",
    "published_utc", "first_seen_utc",
)
KEY_TYPES = (
    "text", "text", "uuid", "bytea", "int8",
    "int2", "int2", "int2", "int2", "text", "uuid",
    "timestamptz", "timestamptz",
)


def _title(rng: random.Random) -> str:
    parts = [
        rng.choice(SUBJECTS).capitalize(),
        rng.choice(VERBS),
        rng.choice(OBJECTS),
    ]
    if rng.random() < 0.6:
        parts.append(rng.choice(QUALIFIERS))
    return " ".join(parts)


def _perturb(title: str, rng: random.Random) -> str:
    """A wire story as a second outlet ran it.

    Measured against archive.dedupe on 30-80 character headlines, **no lexical
    edit reliably lands inside the NEAR_DUP_MAX_HAMMING = 3 threshold**: a
    single substituted character already averages Hamming ~7, and a one-word
    substitution 4-16. (The docstring on ``simhash64`` claims otherwise; see
    docs/data-model.md for the finding.) Generating only sub-threshold variants
    would therefore mean generating a corpus real feeds never produce.

    So this models syndication as it actually appears, and lets the threshold
    catch what it catches:

    * ``verbatim`` - the agency headline run unchanged by another outlet, only
      the URL differs. Normalises identically, so Hamming 0: caught by the
      simhash branch but *not* by content_hash, which is URL-keyed.
    * ``edited`` - the same story with a real editorial rewrite. Typically
      outside the current threshold, which is the point.
    """
    if rng.random() < 0.55:
        # Punctuation and case are stripped by normalise_title, so this is a
        # verbatim republication as far as dedupe is concerned.
        return title + "." if rng.random() < 0.5 else title.upper()

    choice = rng.random()
    if choice < 0.35:
        return title.replace(" the ", " a ", 1)
    if choice < 0.7:
        return f"{title} today"
    return f"Update: {title}"


def _raw_xml(title: str, lead: str, url: str, published_raw: str, rng: random.Random) -> bytes:
    """An RSS <item> fragment, as fetcher.parse would have serialised it."""
    # Sized so the serialised element lands in the 2-4 KB band real feeds
    # occupy; tests/test_seed_tools.py asserts the bounds.
    body = FILLER * rng.randint(5, 9)
    return (
        "<item>"
        f"<title>{_esc(title)}</title>"
        f"<link>{_esc(url)}</link>"
        f"<description>{_esc(lead + ' ' + body)}</description>"
        f"<pubDate>{published_raw}</pubDate>"
        f"<guid isPermaLink=\"false\">{_esc(url)}</guid>"
        "</item>"
    ).encode()


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _uuid4(rng: random.Random) -> uuid.UUID:
    """A v4 UUID drawn from the seeded RNG, so a corpus is fully reproducible.

    uuid.uuid4() would reseed from the OS and break the determinism promise in
    this module's docstring — and ids leak into source_item_id and the URLs,
    so they cannot simply be excluded from a comparison.

    Used for fetch_id only. Item ids are UUIDv7 (see _item_id).
    """
    return uuid.UUID(int=rng.getrandbits(128), version=4)


def _item_id(moment: datetime, rng: random.Random) -> tuple[uuid.UUID, datetime]:
    """A UUIDv7 for an item, and the first_seen_utc that goes with it.

    Mirrors what archive.writer.store_item does in production: the id carries
    the timestamp and first_seen_utc is read back out of it, so the two agree
    to the millisecond. Seeding them independently would produce a corpus whose
    partition dates and derived dates disagree — exactly the bug the derivation
    exists to prevent, hidden inside the fixture used to test for it.
    """
    item_id = uuid7_at(moment, entropy=rng.getrandbits(62), counter=rng.getrandbits(12))
    return item_id, timestamp_of(item_id)


def _weighted(rng: random.Random, pairs: tuple[tuple[str, int], ...]) -> str:
    total = sum(w for _, w in pairs)
    n = rng.randrange(total)
    for value, weight in pairs:
        n -= weight
        if n < 0:
            return value
    return pairs[-1][0]


# --- generation ------------------------------------------------------------


class Corpus:
    """Generates item rows in first_seen order, in flushable chunks."""

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.rng = random.Random(args.rng_seed)
        self.sources = [
            f"{SOURCE_PREFIXES[i % len(SOURCE_PREFIXES)]}-{i // len(SOURCE_PREFIXES) + 1:02d}"
            for i in range(args.sources)
        ]
        # Long tail: a few sources produce most of the volume.
        self.source_weights = [1.0 / (i + 1) ** 0.6 for i in range(len(self.sources))]
        self.spec_hash = hashlib.sha256(
            json.dumps({"version": 2, "synthetic": True, "sources": self.sources}).encode()
        ).hexdigest()
        # Anchoring to wall-clock would make two runs differ by whatever time
        # passed between them, so --end pins it when reproducibility matters.
        self.end = getattr(args, "end", None) or datetime.now(UTC).replace(microsecond=0)
        self.start = self.end - timedelta(days=args.days)
        # Originals eligible to be syndicated. Appended to during generation
        # rather than after each flush, so the very first chunk can already
        # produce duplicates and the measured rate matches --dup-rate. Safe
        # for the items.duplicate_of self-FK: FK triggers fire at end of
        # statement, so a row may reference another row of the same COPY.
        self.wire_pool: list[tuple[uuid.UUID, str, str]] = []
        # (day, channel, decision) -> count, flushed to routing_stats at the end.
        self.stat_counts: dict[tuple[object, str, str], int] = {}

    def chunks(self):
        """Yield (fetch_rows, item_rows, category_rows, decision_rows) chunks."""
        span = (self.end - self.start).total_seconds()
        remaining = self.args.items
        index = 0

        while remaining > 0:
            size = min(self.args.batch, remaining)
            fetches: dict[tuple[str, int], uuid.UUID] = {}
            fetch_rows = []
            raw_items = []

            for _ in range(size):
                # first_seen advances monotonically across the whole run so the
                # corpus looks like an ingest history rather than a shuffle.
                offset = span * index / max(self.args.items - 1, 1)
                first_seen = self.start + timedelta(seconds=offset)
                index += 1

                source = self.rng.choices(self.sources, weights=self.source_weights)[0]
                bucket = int(offset // 900)  # one fetch row per source per 15 min
                key = (source, bucket)
                if key not in fetches:
                    fetch_id = _uuid4(self.rng)
                    fetches[key] = fetch_id
                    fetch_rows.append((fetch_id, source, first_seen, self.spec_hash))

                raw_items.append(self._item(source, fetches[key], first_seen))

            yield self._materialise(raw_items, fetch_rows)
            remaining -= size

    def _item(self, source, fetch_id, first_seen):
        rng = self.rng
        item_id, first_seen = _item_id(first_seen, rng)
        duplicate_of = None
        roll = rng.random()

        if self.wire_pool and roll < self.args.dup_rate:
            origin_id, origin_title, origin_url = rng.choice(self.wire_pool)
            duplicate_of = origin_id
            if roll < self.args.dup_rate * 0.25:
                # Exact duplicate: same canonical URL and title. Exercises the
                # content_hash branch of find_duplicate.
                title, url = origin_title, origin_url
            else:
                # Near duplicate: the same story, another outlet's URL.
                title = _perturb(origin_title, rng)
                url = f"https://{source}.example/{first_seen:%Y/%m/%d}/{item_id.hex[:12]}"
        else:
            title = _title(rng)
            url = f"https://{source}.example/{first_seen:%Y/%m/%d}/{item_id.hex[:12]}"
            if rng.random() < 0.35:
                self.wire_pool.append((item_id, title, url))
                # Bounded, so syndication stays temporally local: a wire story
                # is picked up by other outlets within hours, not weeks.
                del self.wire_pool[: max(0, len(self.wire_pool) - 20_000)]

        published = first_seen - timedelta(minutes=rng.randint(2, 360))
        published_raw = published.strftime("%a, %d %b %Y %H:%M:%S +0000")
        lead = f"{title}. {FILLER[:rng.randint(120, 280)]}"
        language = rng.choice(LANGUAGES)

        return {
            "item_id": item_id,
            "source_name": source,
            "fetch_id": fetch_id,
            "source_item_id": f"{source}:{item_id.hex[:16]}",
            "raw_xml": _raw_xml(title, lead, url, published_raw, rng),
            "title": title,
            "lead": lead,
            "language": language,
            "url": url,
            "image_url": f"{url}/lead.jpg" if rng.random() < 0.5 else None,
            "published_raw": published_raw,
            "published_utc": published,
            "first_seen_utc": first_seen,
            "duplicate_of": duplicate_of,
            "copyright_holder": f"{source} synthetic feed" if rng.random() < 0.4 else None,
            "categories": rng.sample(CATEGORIES, rng.randint(1, 4)),
            "channels": rng.sample(CHANNEL_NAMES, rng.randint(0, 3)),
        }

    def _materialise(self, raw_items, fetch_rows):
        titles = [it["title"] for it in raw_items]
        canons = [canonical_url(it["url"]) for it in raw_items]

        if self.args.jobs > 1:
            with Pool(self.args.jobs) as pool:
                simhashes = pool.map(simhash64, titles, chunksize=256)
        else:
            simhashes = [simhash64(t) for t in titles]

        item_rows, key_rows, category_rows, decision_rows = [], [], [], []
        for it, canon, sim in zip(raw_items, canons, simhashes, strict=True):
            chash = content_hash(canon, it["title"])
            key_rows.append((
                it["source_name"], it["source_item_id"], it["item_id"], chash, sim,
                *simhash_bands(sim), canon, it["duplicate_of"],
                it["published_utc"], it["first_seen_utc"],
            ))
            item_rows.append((
                it["item_id"], it["source_name"], it["fetch_id"], it["source_item_id"],
                it["raw_xml"], it["title"], it["lead"], it["language"], it["url"],
                canon, it["image_url"], it["published_raw"], it["published_utc"],
                it["first_seen_utc"], chash, sim,
                it["duplicate_of"], it["copyright_holder"], self.spec_hash,
            ))
            for ordinal, category in enumerate(it["categories"]):
                category_rows.append((it["item_id"], it["first_seen_utc"], ordinal, category))
            for channel in it["channels"]:
                decision = "duplicate" if it["duplicate_of"] else _weighted(self.rng, DECISION_MIX)
                if decision in RETAINED_DECISIONS:
                    decision_rows.append((
                        it["item_id"], channel, it["first_seen_utc"], decision,
                        str(it["duplicate_of"]) if it["duplicate_of"] else None,
                        it["first_seen_utc"],
                    ))
                else:
                    # Everything else is a counter in production (migration
                    # 0004), so seeding a per-item row would produce a corpus
                    # that never occurs and would overstate routing_decisions
                    # in every sizing measurement taken from it.
                    key = (it["first_seen_utc"].date(), channel, decision)
                    self.stat_counts[key] = self.stat_counts.get(key, 0) + 1

        return fetch_rows, item_rows, key_rows, category_rows, decision_rows


# --- writing ---------------------------------------------------------------


def _dependants_are_partitioned(conn: psycopg.Connection) -> bool:
    """Whether migration 0006 has run. The seeder populates either shape so a
    migration can be tested against a realistic corpus before it is applied."""
    row = conn.execute(
        "SELECT count(*) FROM information_schema.columns "
        "WHERE table_name = 'item_categories' AND column_name = 'first_seen_utc'"
    ).fetchone()
    return bool(row[0])


def _ensure_backdated_partitions(conn: psycopg.Connection, corpus: Corpus) -> int:
    """Create partitions covering the backdated span, if items is partitioned.

    There is no default partition by design (see archive/partitions.py), so an
    insert into a day with no partition fails hard — which is what should
    happen in production, and what a backdating tool has to provision for
    itself. A no-op before migration 0006.
    """
    # This connection has no dict row factory, so index positionally.
    partitioned = conn.execute(
        "SELECT relkind = 'p' FROM pg_class WHERE relname = 'items'"
    ).fetchone()
    if not partitioned or not partitioned[0]:
        return 0

    from archive.partitions import partition_ddl

    statements = partition_ddl(
        corpus.start.date(), corpus.end.date(), lookahead=1
    )
    for statement in statements:
        conn.execute(statement)
    return len(statements)


def _write_prerequisites(conn: psycopg.Connection, corpus: Corpus) -> None:
    conn.execute(
        "INSERT INTO spec_versions (spec_hash, spec) VALUES (%s, %s) "
        "ON CONFLICT (spec_hash) DO NOTHING",
        (corpus.spec_hash, Jsonb({"version": 2, "synthetic": True})),
    )
    for source in corpus.sources:
        conn.execute(
            "INSERT INTO source_state (source_name, cold_start_done) VALUES (%s, true) "
            "ON CONFLICT (source_name) DO NOTHING",
            (source,),
        )
    for channel in CHANNEL_NAMES:
        conn.execute(
            "INSERT INTO channel_state (channel_name) VALUES (%s) "
            "ON CONFLICT (channel_name) DO NOTHING",
            (channel,),
        )


def _flush(conn: psycopg.Connection, fetch_rows, item_rows, key_rows,
           category_rows, decision_rows) -> None:
    with conn.cursor() as cur:
        with cur.copy(
            "COPY fetches (fetch_id, source_name, started_utc, spec_hash) "
            "FROM STDIN (FORMAT BINARY)"
        ) as cp:
            cp.set_types(("uuid", "text", "timestamptz", "text"))
            for row in fetch_rows:
                cp.write_row(row)

        with cur.copy(
            f"COPY items ({', '.join(ITEM_COLUMNS)}) FROM STDIN (FORMAT BINARY)"
        ) as cp:
            cp.set_types(ITEM_TYPES)
            for row in item_rows:
                cp.write_row(row)

        with cur.copy(
            f"COPY item_keys ({', '.join(KEY_COLUMNS)}) FROM STDIN (FORMAT BINARY)"
        ) as cp:
            cp.set_types(KEY_TYPES)
            for row in key_rows:
                cp.write_row(row)

        partitioned = _dependants_are_partitioned(conn)
        cat_cols = "item_id, first_seen_utc, ordinal, category" if partitioned \
            else "item_id, ordinal, category"
        cat_types = ("uuid", "timestamptz", "int4", "text") if partitioned \
            else ("uuid", "int4", "text")
        with cur.copy(f"COPY item_categories ({cat_cols}) FROM STDIN (FORMAT BINARY)") as cp:
            cp.set_types(cat_types)
            for row in category_rows:
                cp.write_row(row if partitioned else (row[0], row[2], row[3]))

        # routing_decisions.decision is an enum; text COPY keeps psycopg out of
        # enum-OID lookup and is fast enough for this table's width.
        dec_cols = (
            "item_id, channel_name, first_seen_utc, decision, reason, decided_utc"
            if partitioned
            else "item_id, channel_name, decision, reason, decided_utc"
        )
        with cur.copy(f"COPY routing_decisions ({dec_cols}) FROM STDIN") as cp:
            for row in decision_rows:
                cp.write_row(row if partitioned else (row[0], row[1], row[3], row[4], row[5]))


def _flush_routing_stats(conn: psycopg.Connection, counts: dict) -> None:
    """One upsert per (day, channel, decision) — a few hundred rows, so a
    single executemany rather than COPY."""
    if not counts:
        return
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO routing_stats (day, channel_name, decision, n)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (day, channel_name, decision) DO UPDATE
              SET n = routing_stats.n + EXCLUDED.n
            """,
            [(day, channel, decision, n) for (day, channel, decision), n in counts.items()],
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--items", type=int, default=100_000, help="how many items to generate")
    parser.add_argument("--days", type=int, default=10, help="backdate span, ending now")
    parser.add_argument("--sources", type=int, default=50, help="how many distinct sources")
    parser.add_argument(
        "--dup-rate", type=float, default=0.10,
        help="share of items that are syndicated duplicates (0.05-0.15 is realistic)",
    )
    parser.add_argument("--batch", type=int, default=20_000, help="rows per COPY flush")
    parser.add_argument(
        "--jobs", type=int, default=0,
        help="processes for simhash (0 = os.cpu_count() - 1, 1 = in-process)",
    )
    parser.add_argument("--rng-seed", type=int, default=20260807, help="determinism knob")
    parser.add_argument(
        "--end", type=datetime.fromisoformat, default=None,
        help="ISO timestamp the backdated span ends at (default: now). Pin it "
             "together with --rng-seed to reproduce a corpus exactly.",
    )
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", ""))
    args = parser.parse_args(argv)

    if not args.database_url:
        parser.error("DATABASE_URL is not set and --database-url was not given")
    if args.jobs == 0:
        args.jobs = max(1, (os.cpu_count() or 2) - 1)

    corpus = Corpus(args)
    started = datetime.now(UTC)
    written = 0

    with psycopg.connect(args.database_url, autocommit=True, options="-c timezone=UTC") as conn:
        # The role-level statement_timeout from migration 0003 is 30s; a COPY of
        # 20k rows can exceed it on a cold cache.
        conn.execute("SET statement_timeout = 0")
        conn.execute("SET idle_in_transaction_session_timeout = 0")
        _ensure_backdated_partitions(conn, corpus)
        _write_prerequisites(conn, corpus)
        for fetch_rows, item_rows, key_rows, category_rows, decision_rows in corpus.chunks():
            with conn.transaction():
                _flush(conn, fetch_rows, item_rows, key_rows, category_rows, decision_rows)
            written += len(item_rows)
            elapsed = (datetime.now(UTC) - started).total_seconds()
            print(
                f"  {written:>9,} / {args.items:,} items  "
                f"({written / max(elapsed, 0.001):,.0f}/s)",
                file=sys.stderr, flush=True,
            )

        _flush_routing_stats(conn, corpus.stat_counts)

    elapsed = (datetime.now(UTC) - started).total_seconds()
    counted = sum(corpus.stat_counts.values())
    print(
        f"seeded {written:,} items across {len(corpus.sources)} sources "
        f"spanning {args.days}d in {elapsed:,.1f}s "
        f"({counted:,} routing outcomes counted, not stored per item)",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
