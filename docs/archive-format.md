# The archive format

The corpus outlives every version of the code that wrote it. This page is the
contract that makes that possible: what is written, how it is framed, and what
a reader may assume. See [ADR 0010](adr/0010-tiered-archive.md) for why the
archive is tiered at all.

**The governing rule: the corpus must be fully interpretable with no PostgreSQL
instance in existence.** Anything needed to read it belongs in object storage,
next to the data.

## Layout

```
{container}/
├── items/dt=2026-08-07/items-2026-08-07-0000.parquet   base corpus (immutable)
├── raw/source={name}/2026/08/07/{fetch_id}.jsonl.zst   provenance
├── decisions/dt=2026-08-07/decisions-2026-08-07.parquet
├── _manifests/2026-08-07.json
├── _meta/spec_versions.parquet                         every spec ever seen
├── _meta/sources.parquet
│
└── (reserved for future sidecars — directories exist, contents do not)
    ├── embeddings/dt=…
    ├── clusters/dt=…
    └── feedback/dt=…                                   future labels join here
```

`dt=` is Hive-style partitioning, so a query engine prunes by date without
reading anything.

**Blob storage has no directories.** Azurite emulates the Blob API only — it
does **not** implement ADLS Gen2 hierarchical namespace — so all access stays
on the flat Blob surface: no directory rename, no recursive delete, no
atomic directory operations. Local and cloud therefore behave identically. The
"reserved" prefixes above are zero-byte marker blobs, not real directories.

## Raw XML framing

Raw XML is written **one blob per fetch**, not one per item:
`raw/source={name}/{yyyy}/{mm}/{dd}/{fetch_id}.jsonl.zst`. At 500k items/day
that is a few thousand objects a day instead of 500,000 — object count, request
cost, and the 128 KiB minimum billable size on Cool/Cold tiers all make
per-item blobs the wrong unit.

Each item's row carries `raw_blob_path`, `raw_blob_offset` and
`raw_blob_length`, and those offsets have to mean something.

**Framing: one zstd frame per record, concatenated.**

A single zstd frame over the whole batch would compress better, but zstd
decompresses sequentially from a frame boundary — an arbitrary byte offset into
a single-frame stream is not seekable, so a point read would mean fetching and
decompressing the entire fetch blob. The offset columns would be decoration.

Concatenated frames are a valid zstd stream, so the whole blob still
decompresses end to end with any standard reader (`zstd -d` included), while
`raw_blob_offset`/`raw_blob_length` address exactly one frame — an HTTP Range
GET followed by a single-frame decompress.

Measured on the 211 real feed items in `tests/fixtures/` (1623 B/record):

| Framing | Size | Ratio | Per record | Point read |
|---|---|---|---|---|
| Single frame | 59.1 KiB | 5.66× | 287 B | whole blob only |
| **Per record** | **138.1 KiB** | **2.42×** | **670 B** | **one frame** |
| Per record + trained dictionary | 64.7 KiB | 5.17× | 314 B | one frame |

Per-record framing costs **+133%** against a single frame; a trained dictionary
recovers **93%** of that. A point read decompresses one frame in ~2 µs against
~111 µs for the whole blob — **69× cheaper**, and that gap widens with batch
size.

**The dictionary is measured and deliberately not adopted by default.** It
would make every raw blob unreadable without a separate 67 KiB artifact, and
this archive's whole purpose is to stay interpretable for years with as few
mandatory external dependencies as possible. The saving is real but small in
context: raw XML lives in object storage, which is not the constrained
resource — the fixed 32 GiB PostgreSQL disk is, and this data has already left
it. Roughly 65 GB/year of blob at 500k items/day, a couple of dollars a month.

Turning it on later is a config change that applies to new days only; days
already written are never rewritten. If it is ever enabled, the dictionary must
be written to `raw/_dicts/{id}.zdict` **before** any blob referencing it, must
be immutable, and must never be deleted — losing it silently bricks every day
that used it.

## Schema evolution

The corpus will outlive several revisions of what we want in it. The rules
exist so that a reader written today still works in three years:

1. **Files already written are immutable.** A past day's Parquet is never
   rewritten to add a column.
2. **New derived data goes in sidecar datasets** keyed by `item_id`, under the
   same `dt=` partitioning — `embeddings/dt=…`, `clusters/dt=…`,
   `feedback/dt=…`. The reader joins them at query time. This is how future
   feedback labels attach to the corpus without touching the base files.
3. **`archive_schema_version` is in every manifest**, and a reader must handle
   a column being absent from an older file rather than failing.
4. **Additive changes only.** New columns are nullable; existing columns are
   never renamed or retyped. A genuinely breaking change bumps
   `archive_schema_version` and ships a documented migration path.

### Noted, not implemented: Delta Lake / Iceberg

Either format over these same Parquet files would add row-level deletes and
time travel. Not adopted — a substantial dependency for a single-user system —
but relevant if content ever has to be removed after the fact, which the schema
already anticipates by tracking `copyright_holder`. The layout above is
compatible with adopting one later.

## Per-day manifest

```json
{
  "dt": "2026-08-07",
  "written_utc": "2026-08-08T03:14:22Z",
  "archive_schema_version": 1,
  "row_count": 487213,
  "pg_row_count_at_export": 487213,
  "source_names": ["..."],
  "spec_hashes": ["..."],
  "files": [{"path": "items/dt=2026-08-07/items-2026-08-07-0000.parquet",
             "bytes": 91230144, "sha256": "..."}]
}
```

`archive_schema_version` is required: when a column is added later, the
training loader has to know which files to expect it in.

Days are exported in parts (`-0000`, `-0001`, …) of roughly 50k rows. Each part
is listed separately with its own byte count and sha256, and a day is only
verified once every part has landed. Parts also land closer to the 128 MB–1 GB
file size a columnar reader wants than one file per day would at this volume.
