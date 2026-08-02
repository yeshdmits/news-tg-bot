# Feed fixtures

One saved real XML response per source. These are the correctness baseline for
the whole parser: all parser tests run offline against
them. Never regenerate silently — a refresh is a deliberate commit that may
require updating test assertions.

Recapture with: `curl -sL <url> -o tests/fixtures/<source>.xml`

| File | Source | URL | Captured |
|---|---|---|---|
| `swissinfo.xml` | swissinfo | https://cdn.prod.swi-services.ch/rss/eng/rssxml/top-news/rss | 2026-08-01 |
| `blick.xml` | blick | https://www.blick.ch/schweiz/rss.xml | 2026-08-01 |
| `guardian-world.xml` | guardian-world | https://www.theguardian.com/world/rss | 2026-08-01 |
| `bbc-world.xml` | bbc-world | https://feeds.bbci.co.uk/news/world/rss.xml | 2026-08-01 |
| `ecb-press.xml` | ecb-press | https://www.ecb.europa.eu/rss/press.xml | 2026-08-01 |
| `fed-press-all.xml` | fed-press-all | https://www.federalreserve.gov/feeds/press_all.xml | 2026-08-01 |
| `fed-monetary.xml` | fed-monetary | https://www.federalreserve.gov/feeds/press_monetary.xml | 2026-08-01 |
| `snb-press.xml` | snb-press | https://www.snb.ch/public/en/rss/news | 2026-08-01 |
