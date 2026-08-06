# Adding a source

A walkthrough adding a deliberately awkward feed: an **Atom** feed (the
bundled sources are all RSS 2.0) from a fictional central-bank press office.
Atom exercises the options RSS lets you skip — a default XML namespace, an
attribute-valued link, ISO 8601 dates, a tag-URI id that needs a regex, and
an HTML summary. If you can add this one, the easy ones are easy.

Everything below was run against the real code; you can follow along without
any credentials.

## 0. Look at the actual XML first

```bash
curl -s https://press.example.eu/feed.atom | head -30
```

```xml
<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Example Press Office</title>
  <entry>
    <id>tag:press.example.eu,2026:release/12345</id>
    <title>Rates unchanged at 2.15%</title>
    <link rel="self" href="https://press.example.eu/feed.atom"/>
    <link rel="alternate" href="https://press.example.eu/en/2026/rel12345.html"/>
    <updated>2026-08-01T14:30:00Z</updated>
    <summary type="html">&lt;p&gt;The Governing Council decided to keep rates unchanged.&lt;/p&gt;</summary>
  </entry>
</feed>
```

Four things to notice, each of which needs a spec option:

1. `xmlns="http://www.w3.org/2005/Atom"` — a **default namespace**. XPath
   cannot address elements in a default namespace without a prefix, so the
   source must declare one in `namespaces` and every expression must use it.
   Plain `//entry` will match nothing.
2. The item URL is an **attribute** (`href`) on the *second* `link` — you
   need a predicate plus the trailing-`@attr` extension.
3. `updated` is **ISO 8601**, not the RFC 822 default.
4. `id` is a tag URI. Usable as-is, but ugly in the archive —
   `id_pattern` extracts the numeric part. (The id only needs to be stable
   and unique per source.)

## 1. Write the source entry

Add to *your* spec — a copy of the tracked example
(`cp spec.example.json spec.json`, gitignored); channel `econ-alerts`
assumed to exist in your registry:

```json
{
  "name": "example-press",
  "url": "https://press.example.eu/feed.atom",
  "language": "en",
  "kind": "central_bank",
  "fetch_interval_min": 5,
  "cold_start_policy": "post_newest:2",
  "url_verified": "2026-08-02",
  "namespaces": { "atom": "http://www.w3.org/2005/Atom" },
  "mapping": {
    "items": "//atom:entry",
    "id": "atom:id",
    "id_pattern": "release/(\\d+)",
    "title": "atom:title",
    "url": "atom:link[@rel='alternate']@href",
    "published": "atom:updated",
    "published_format": "iso8601",
    "lead": ["atom:summary", "atom:title"],
    "lead_html": true,
    "category_from": "url_path_segment:1"
  },
  "channels": [
    {
      "name": "econ-alerts",
      "lead_max_length": 400,
      "when": { "keywords_any": ["rate", "inflation", "monetary"] }
    }
  ]
}
```

Choices worth explaining:

- **`url: "atom:link[@rel='alternate']@href"`** — the predicate picks the
  right `link`, the trailing `@href` reads its attribute (the evaluator
  turns it into `…/@href`). Without the predicate you would get the feed's
  self-link.
- **`lead` as a list** is an ordered fallback: if an entry has no
  `summary`, the title is used rather than dropping the item (press feeds
  do this for terse releases).
- **`category_from: "url_path_segment:1"`** derives a category from the item
  URL — segment 1 of `/en/2026/rel12345.html` is `en`. Useful when a feed
  has no `category` elements but encodes sections in its URLs.
- **`cold_start_policy: "post_newest:2"`** — for a press feed you probably
  want the two most recent releases posted on day one. The default
  `skip_all` would archive the whole backlog silently and post nothing until
  the next release.
- **`fetch_interval_min: 5`** is the floor: the production cron runs every
  5 minutes, so smaller values change nothing.
- **`when.keywords_any`** filters at the binding, so the same source could
  also feed an unfiltered channel with a second binding.

If you do not have a channel for this feed yet — or want to watch what it
publishes before pointing it at anyone — ship it with `"channels": []` and
`"cold_start_policy": "skip_all"`. That makes it
[archive-only](configuration.md#archive-only-sources): fetched, parsed and
stored, never posted. Everything below still applies; only the routing lines
disappear from the output. Add the binding whenever you are ready — but note
it will route only items fetched *after* that, not the ones already archived.

## 2. Validate — offline, no credentials

```bash
python -m cli validate --spec spec.json
```

```
example-press (central_bank, en, every 5 min, limit 10, cold_start=post_newest:2)
    -> econ-alerts [-100999900005] link_preview, lead<=400, max 10/run every 5 min, max_age=240 min, drain_all, when={"keywords_any":["rate","inflation","monetary"]}

OK: 1 sources, 1 channels, 1 bindings
```

`validate` catches structural mistakes (unknown channel, channel-scoped keys
on the binding, bad regex in `lead_remove`, unknown fields) but cannot know
whether your XPath matches the real feed. That is the next step.

## 3. Rehearse against the live feed — network, still no credentials

```bash
python -m cli fetch example-press --dry-run --spec spec.json
```

(`--spec` selects the file directly; without it the spec comes from the
`SPEC_JSON` / `SPEC_URL` / `SPEC_PATH` environment chain — see
`docs/configuration.md`.)

This fetches the real URL, applies the mapping, and prints per-item results
and the routing decision each binding would make — without a database or a
Telegram token. Check that ids, titles, URLs, dates and leads look right and
that your `when` filter passes/blocks the items you expect.

## 4. Ship it

In the cloud the spec is a Key Vault secret injected as `SPEC_JSON`
(`docs/deployment/terraform.md`): update `spec.local.json` and either
`az keyvault secret set --vault-name <kv> --name spec-json --file spec.local.json`
(picked up on the fetch job's next run) or bump `secrets_wo_version` and
`terraform apply`. No image rebuild is involved. Locally,
`docker compose up` mounts your spec file (`SPEC_FILE`, default
`spec.example.json`) into the containers, so you iterate without
rebuilding.

The first production fetch applies your `cold_start_policy`; from then on
the source is fetched on its interval.

## When it does not work

| Symptom | Likely cause |
|---|---|
| `validate` fails | Read the message — every load error names the offending element (`docs/configuration.md` lists them all). |
| `cli fetch` shows 0 items | The `items` expression missed. Almost always the default-namespace trap: declare a prefix in `namespaces` and use it everywhere (`//atom:entry`, not `//entry`). |
| Items skipped as unidentifiable | `id_pattern` does not match the raw id — the item is skipped and a problem is reported. Test your regex against the actual id text. |
| `MappingEvalError: bad mapping expression …` | The expression is not valid XPath after the `@attr` rewrite. Remember only a *trailing* plain `@name` is special. |
| Wrong URL extracted | Ambiguous `link` match — add a predicate (`[@rel='alternate']`). |
| Date missing on every item | Wrong `published_format`. Atom/ISO timestamps parse as garbage under the RFC 822 default; unparseable dates fall back to fetch time and raise `PublishedDateUnparseable` warnings. |
| Lead full of markup | Set `lead_html: true`; use `lead_remove` regexes for boilerplate ("Read more…"). |
| Everything works but nothing posts | See `docs/troubleshooting.md` — usually `DRY_RUN`, cold start, or the posting window. |
| `validate` prints `-> (archive only …)` but you expected a channel | The source's `channels` list is empty. Add the binding; `validate` will then show the `-> channel` line. |
| `… has no channel bindings (archive-only) but cold_start_policy is …` | `post_newest:N` only controls posting, so it cannot apply to an archive-only source. Set `"cold_start_policy": "skip_all"` on it explicitly (needed even when the value came from `defaults`). |

If the feed publishes a schema, drop the XSD into `schemas/`, set
`schema_file`, and format drift will surface as a `SchemaValidationFailed`
alert instead of silent garbage.
