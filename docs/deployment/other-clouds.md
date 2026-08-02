# The provider contract (running this on another cloud)

The application knows nothing about any cloud. It reads **environment
variables and nothing else** — no Key Vault SDK, no Secrets Manager, no
metadata endpoints. Injecting values from a secret store is the deployment
layer's job. That is the whole portability story: to run this stack on AWS,
GCP, Fly, a VPS, or bare Kubernetes, you re-implement the *injection*, not
the application.

This document is the contract a new provider implementation must satisfy.
It describes what the Azure implementation (`deploy/terraform/`) does in
provider-neutral terms. Do not add provider-specific code to the
application to make a port work — if a port seems to need that, the port is
wrong.

## Compute: one image, three roles

All three roles run the same container image; only the command differs.

| Role | Command | Shape |
|---|---|---|
| fetch | `python -m cli run --once` | Cron, every ≤5 min (match the tightest `fetch_interval_min`). Timeout below the cron period; no retries. Overlapping executions are safe — a Postgres advisory lock (`newsbot:fetch`) makes the loser exit without work. |
| bot | `python -m cli serve` | HTTP service on `$PORT` with a public HTTPS endpoint (Telegram calls the webhook). Scale-to-zero is fine; state lives in Postgres. |
| migrate | `alembic upgrade head && python -m cli register-webhook` | Run-to-completion job, triggered by CI before each rollout. Must reach success **before** fetch/bot get the new image — new code may need the new schema. |

## Database

PostgreSQL 17 with the `pgcrypto` extension available (`CREATE EXTENSION`
must be allowed). TLS required (`?sslmode=require` in `DATABASE_URL`).

## Environment variables to inject, per role

Secret = should come from your secret store, not plain platform config.

| Variable | Secret | fetch | bot | migrate | Value |
|---|---|---|---|---|---|
| `SPEC_JSON` | yes | ✓ | ✓ | ✓ | The full spec JSON content. This *is* the application configuration, and it names private chat ids — treat it as a secret. Alternatively inject `SPEC_URL` or mount a file and set `SPEC_PATH`. |
| `DATABASE_URL` | yes | ✓ | ✓ | ✓ | `postgresql://user:pass@host:5432/db?sslmode=require` |
| `TELEGRAM_BOT_TOKEN` | yes | ✓ | ✓ | ✓ | From @BotFather |
| `TELEGRAM_WEBHOOK_SECRET` | yes | | ✓ | ✓ | Compared against Telegram's header on every webhook call |
| `WEBHOOK_URL` | no | | | ✓ | `https://<bot-public-fqdn>/telegram/webhook` — usually only knowable after the bot service exists; the migrate job passes it to Telegram's `setWebhook` |
| `DRY_RUN` | no | ✓ | ✓ | | `"true"`/`"false"`; keep `"true"` until the stack is verified |
| `PORT` | no | | ✓ | | Default 8000 |
| `HEALTHCHECK_URL` | yes | ✓ | | | Deadman ping target; the URL itself is the credential |
| `TRANSLATE_PROVIDER` | no | ✓ | | | `none` or `deepl` |
| `TRANSLATE_API_KEY` | yes | ✓ | | | Only with `deepl` |

Missing required variables fail at startup with a message naming all of
them at once, so a mis-wired deployment tells you everything on its first
crash. See `docs/configuration.md` for the full variable reference.

## The deploy sequence CI expects

The GitHub workflow only swaps image tags; infrastructure is applied
manually. A port keeps that split:

1. Build and push the image, tagged with the commit SHA.
2. Point the migrate job at the new tag and run it; wait for success.
3. Only then point fetch and bot at the new tag.

Consequence for IaC: your infrastructure code must **ignore drift on the
image field** of all three compute units (the Azure module uses Terraform
`ignore_changes`), because CI mutates it out-of-band on every deploy.

## Secret store expectations

The Azure implementation provisions a vault, writes each secret once (with
write-only Terraform arguments so values stay out of state), grants a
workload identity read access, and has the platform resolve secret
references at container start. An equivalent port should:

- reference secrets by URI/ARN from the runtime rather than copying values
  into platform config;
- put the spec content in the store as its own secret and inject it as
  `SPEC_JSON` (mind your platform's secret/env size ceiling — the reference
  spec is ~10 KB; `SPEC_URL` is the escape hatch for large specs);
- be explicit about what still lands in IaC state, and treat that state as
  a credential.

## What is deliberately absent

- No log pipeline: observability is the Telegram ops group plus the
  `error_events` table; console logs are for live debugging.
- No VNet/private networking: the reference stack accepts a public
  Postgres endpoint behind TLS + firewall to stay cheap. Harden if your
  threat model differs.
- No autoscaling beyond scale-to-zero on the bot.
