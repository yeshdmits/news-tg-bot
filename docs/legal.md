# Third-party content

This program retrieves, stores, and republishes content produced by others.
Running it makes **you** the republisher. This page states plainly what it
does with that content and which knobs change your exposure. It is not legal
advice.

## What it stores

For every item fetched from a feed, permanently (see `docs/data-model.md`):

- the raw XML fragment as published,
- the extracted title, lead/summary, URL, image URL, categories,
- the feed's copyright notice, when the mapping's `copyright` field is
  configured (stored per item as `copyright_holder`),
- machine translations of titles and leads, when translation is enabled.

Nothing content-related is ever deleted.

## What it republishes

Each Telegram post contains: the item **title**, a **lead** truncated to
`lead_max_length`, a **link to the original article**, a source hashtag, and
— depending on `post_style` — the article's own image
(`photo_full`) or Telegram's link preview. The full article body is never
fetched or republished; only what the publisher put in their feed. The lead
is the one part you can switch off: `post_style: link_preview_title_only`
republishes the headline and the link and nothing else.

A source configured with an empty `channels` list is
[archive-only](configuration.md#archive-only-sources): it is stored under
everything in "What it stores" above but republished nowhere. That narrows
the exposure to storage — it does not remove it, since storing a
publisher's content is itself subject to their terms.

## Whose terms apply

- **Feed publishers.** Offering an RSS/Atom feed does not imply permission
  to redistribute its content. Terms differ per publisher (personal vs
  commercial use, attribution, excerpt length). You are responsible for
  checking the terms of every feed you add to your spec. The tracked
  example (`spec.example.json`) contains only official public-institution
  feeds (ECB, Federal Reserve, SNB); commercial publisher feeds appear only
  as offline test fixtures (`tests/fixtures/`) used as a parser-correctness
  baseline. Do not assume any feed permits your use case.
- **Translation providers.** DeepL's terms govern what you may do with
  translations, and translated text is cached in your database.
- **Telegram.** Channel content must comply with Telegram's terms of
  service.

The polite-crawler behaviours are built in: conditional GETs
(ETag/Last-Modified), per-source fetch intervals, an honest User-Agent
identifying the bot, and XSD validation instead of scraping. None of that
substitutes for permission to republish.

## Configuration that changes exposure

| Option | Effect |
|---|---|
| `lead_max_length` (default 300) | Longer excerpts republish more of the publisher's text. |
| `post_style: photo_full` | Rehosts the publisher's image into your channel (vs `link_preview`, where Telegram renders a preview, or `text_only`). |
| `post_style: link_preview_title_only` | The one option here that *reduces* exposure: no excerpt of the publisher's text is republished, only the headline and the link. The lightest-touch style available. |
| `translate_lead: true` | Creates and stores derivative works (translations). |
| `mapping.copyright` | Captures the publisher's copyright notice alongside each item — configure it where the feed provides one. |
| `cli export` | Moves archived third-party content out of the database into files; distributing such exports is a further act of redistribution. |
| Public vs private channels | Republishing into a public channel reaches an audience; a private channel for personal use is a different posture. |

## Practical stance

Prefer official feeds, keep leads short, keep the link to the original
prominent (the formatter always includes it), honour a publisher's request
to stop, and when in doubt ask the publisher or drop the source.
