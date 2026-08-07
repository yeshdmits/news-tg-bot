# news-tg-bot

A self-hosted bot that watches a list of RSS/Atom feeds, archives every item
in PostgreSQL, and posts filtered, optionally translated summaries to
Telegram channels.

Unlike a simple RSS-to-Telegram relay, everything is driven by one
declarative JSON spec (which feeds, which channels, what to filter, how
often), every item ever fetched is kept permanently with its raw XML and a
full audit trail of routing and delivery decisions, and failures don't page
you blindly — they are fingerprinted, grouped, and alerted into an ops
Telegram group with escalation, quiet hours, and `/ack` commands.

Posting is optional per source: a feed with an empty `channels` list is
[archive-only](docs/configuration.md#archive-only-sources) — fetched, parsed
and stored, but never published.

A post looks like this — real output of the formatter over a feed sample
from the test fixtures (HTML sent to Telegram: bold title, truncated lead,
a "Read more" link, per-source hashtags):

```
<b>Digital euro app to incorporate highest accessibility standards</b>

Digital euro app to incorporate highest accessibility standards

<a href="https://www.ecb.europa.eu/press/pr/date/2026/html/ecb.pr260730~3b3bfbb565.en.html">Read more</a> | #ecb_press #EU
```

## Quickstart

The front door needs **no bot token, no registry account, no cloud
subscription** — the example configuration watches public central-bank
feeds and the default `DRY_RUN=true` records posts instead of sending them:

```bash
git clone <your fork> && cd news-tg-bot
cp .env.example .env       # defaults are safe: DRY_RUN=true, empty token
docker compose up --build  # builds locally, migrates, evaluates real feeds
```

You should see the bot fetch the example sources and log decisions like:

```
... event=spec_loaded source=path location=/app/spec.json spec_sha256=9cc0f4a4c600...
... event=dry_run_post chat_id=-100999900003 title='Digital euro app to ...'
```

Without Docker (Python 3.12+, no database needed for these):

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
python -m cli validate --spec spec.example.json   # spec + JSON-Schema checks
pytest -m "not pg"                                # offline test tier
python -m cli fetch snb-press --spec spec.example.json  # live rehearsal, no DB
```

Going live needs credentials, in this order:

1. **A bot + your channels** (free): create a bot with @BotFather, copy
   `spec.example.json` to `spec.json` (gitignored), put your channel ids in
   it, set `SPEC_FILE=./spec.json` and `TELEGRAM_BOT_TOKEN=...` in `.env`,
   flip `DRY_RUN=false`.
2. **Cloud deployment** (optional): your own registry namespace, Azure
   subscription and repository variables/secrets — see
   [docs/deployment/](docs/deployment/).

Details in [docs/configuration.md](docs/configuration.md).

## What it is not

- Not a web app: output goes to Telegram, period.
- Not a scraper: it reads RSS/Atom feeds only, fetches politely
  (conditional GETs, per-source intervals, honest User-Agent), and
  republishes only what publishers put in their feeds — see
  [docs/legal.md](docs/legal.md) for your responsibilities as republisher.
- Not multi-tenant: one spec, one database, one bot. Run a second copy for
  a second deployment.
- The archive is not prunable by design — it grows forever
  ([docs/data-model.md](docs/data-model.md)).

## Documentation

| Doc | What's in it |
|---|---|
| [docs/configuration.md](docs/configuration.md) | Every env var and every `spec.json` field: defaults, validation rules, worked and rejected examples. |
| [docs/adding-a-source.md](docs/adding-a-source.md) | Tutorial: adding an awkward (namespaced Atom) feed, start to finish. |
| [docs/architecture.md](docs/architecture.md) | Components, dependency rules, runtime units, execution-flow diagrams. |
| [docs/data-model.md](docs/data-model.md) | The schema, what is stored and why, the keep-everything retention design. |
| [docs/deployment/terraform.md](docs/deployment/terraform.md) | Provisioning the Azure stack: backend bootstrap, variables, lifecycle rules, teardown. |
| [docs/deployment/azure.md](docs/deployment/azure.md) | The runtime, the CI deploy sequence, verification, rollback, costs. |
| [docs/deployment/other-clouds.md](docs/deployment/other-clouds.md) | The provider contract: what any cloud must inject so the app runs there. |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Symptom → cause → fix, including every "it runs but posts nothing" case. |
| [docs/adr/](docs/adr/) | Why the less-obvious decisions are the way they are. |
| [docs/legal.md](docs/legal.md) | Third-party content: what is stored and republished, whose terms apply. |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Dev setup, the two test tiers, fixture policy. |
| [SECURITY.md](SECURITY.md) | Reporting, and the accepted security tradeoffs. |

## Deployment in one paragraph

Terraform (`deploy/terraform/`) provisions a deliberately cheap Azure stack:
one burstable Postgres, a Key Vault holding every secret (including the URL
the configuration is fetched from, injected as `SPEC_URL`), and three Container Apps
units built from the same image — a cron fetch job, a scale-to-zero webhook
app, and a manual migration job. Every account-specific value is a variable;
nothing in the repository points at anyone's subscription. GitHub Actions
tests every push; on `main` it builds the image, pushes it to GHCR (Docker
Hub optional via repository variables), runs the migration job, waits for it
to succeed, and only then rolls the new image out. Terraform owns the
topology; CI owns the image tags. A fresh fork's CI builds and tests with
zero configuration and skips the deploy with instructions. Details in
[docs/deployment/](docs/deployment/).

## License

[MIT](LICENSE).
