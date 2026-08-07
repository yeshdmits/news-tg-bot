# Troubleshooting

Symptom → cause → fix. Error strings below are verbatim from the code.

## It will not start

| Symptom | Cause | Fix |
|---|---|---|
| `DATABASE_URL is not set` | `run`, `stats` or `export` started without a database URL. | Set `DATABASE_URL`; locally, copy `.env.example` to `.env`. |
| `DATABASE UNREACHABLE: …` | URL set but Postgres not answering. | Start the db (`docker compose up -d db`), check host/port — see the port note below. |
| `INVALID SPEC: …` | `spec.json` failed validation; the message names the offending element. | Run `python -m cli validate --spec …` and fix what it reports. All load-time errors are listed in `docs/configuration.md`. If this happens at the start of a production run and an ops chat is configured, one emergency alert is posted before exit. |
| `TELEGRAM_BOT_TOKEN is not set` / `WEBHOOK_URL is not set` / `TELEGRAM_WEBHOOK_SECRET is not set` | `register-webhook` needs all three. | Set them; this command normally runs from the migrate job, which gets them from Terraform. |
| `spec has no errors config — no ops chat to register commands for` | `register-webhook` with a spec lacking the `errors` section. | Add `errors.ops_chat_id` to the spec. |
| `ValueError: invalid literal for int()` at startup | `PORT` set to a non-numeric value. | Fix `PORT`. |
| `RuntimeError: DATABASE_URL is not set` from alembic | Migration run without the env var. | `DATABASE_URL=… alembic upgrade head`. |
| `connection refused` on localhost:5433 when running `pytest` | The compose db is not up — `tests/conftest.py` defaults to host port **5433**, which is exactly where `docker-compose.yml` publishes the db. | `docker compose up -d db`, then plain `pytest` works. If you changed the compose port mapping, point `TEST_DATABASE_URL` at your port. |
| `relation "seen_updates" does not exist` (or another missing table) after `docker compose up migrate` reported success | The migrate container ran a **stale local image** — `docker compose up` does not rebuild, so an image built before the latest migration silently "succeeds" at an old head. In `run --once` the error is caught and logged as `run_execution_failed` with exit 0, and in JSON log mode the traceback is not rendered — easy to miss. | `docker compose up --build migrate`, then re-run. |

## It starts but posts nothing

Work through these in order — each one is by design:

1. **`DRY_RUN` defaults to `true`.** Posts are recorded as `skipped` with
   error `dry_run` and nothing is sent. Locally: set `DRY_RUN=false` in
   `.env`. In Azure: `terraform apply -var dry_run=false` — never
   `az --set-env-vars` (see `docs/deployment/terraform.md`).
2. **The source has no channel bindings.** `"channels": []` makes a source
   [archive-only](configuration.md#archive-only-sources): fetched and stored,
   never routed. It looks perfectly healthy in `/status` and `cli stats`
   because it *is* — it just has no `routing_decisions` rows at all.
   `cli validate` marks these `-> (archive only — stored, never posted)`.
   Note that adding a binding now only routes **new** items; anything already
   archived stays unposted.
3. **Cold start skips.** The default `cold_start_policy: skip_all` archives
   the entire first fetch of a new source without posting — the backlog of an
   old feed would otherwise flood the channel. Use `post_newest:3` if you
   want a new source to say something immediately.
4. **The posting window is closed.** Each channel posts at most
   `max_posts_per_run` items every `post_interval_min` minutes. Items wait as
   `rate_limited` (`gated:` reason) until the next window — or are dropped if
   `queue_policy` is `drop_oldest` and the backlog exceeds the cap.
5. **The source is not due.** Sources are fetched when `fetch_interval_min`
   has elapsed since the last fetch; the cron runs every 5 minutes but most
   sources fetch every 15+.
6. **`fetch_lock_held_elsewhere_skipping` in the logs.** Another execution
   holds the advisory lock; this one exits cleanly having done nothing.
   Normal under overlap; investigate only if it happens every run (a stuck
   long-running execution).
7. **Items are filtered.** Check `routing_decisions` (or `cli stats`):
   `filtered_category`, `predicate_failed`, `too_old` and `duplicate` all
   mean the pipeline worked and said no. Remember: an **empty**
   `include_categories` list means "no filter" — it never means "include
   nothing".
8. **The bot token is empty.** With `TELEGRAM_BOT_TOKEN` unset, sending is
   silently disabled everywhere; errors are still written to the database.

## Fetching problems

| Symptom | Cause | Fix |
|---|---|---|
| `unknown source 'x'; known: […]` | `cli fetch` with a name not in the spec. | Use a listed name. |
| A source alert: `FetchHttpError4xx` | Feed URL wrong or moved; 4xx is treated as probably-permanent (escalates to critical after 2). | Verify the URL in a browser, update the spec, set `url_verified`. |
| `FetchTimeout` / `FetchHttpError5xx` / `FetchNetworkError` warnings | Transient upstream trouble. Fetches retry twice with backoff inside one attempt; across runs the source backs off (interval doubles per consecutive failure, cap 60 min) without affecting other sources. | Usually nothing — the alert resolves itself when the source recovers. |
| `SchemaValidationFailed` (critical) | The feed no longer matches its XSD in `schemas/`. | Diff the live feed against the XSD; either the publisher changed the format (update mapping + XSD) or the feed is serving garbage. |
| `MappingRequiredFieldMissing` | An item lacks `id`, `title` or `url` after mapping. | Check the mapping expressions against the current feed XML (`cli fetch <source> --dry-run` prints per-item results). |
| `PossibleGap` warnings | A fetch returned only new items — the feed may have moved faster than the fetch cadence, so items may have been missed. | Raise `fetch_limit` or lower `fetch_interval_min` for that source. |
| Source warning at load: `url_verified is null — verify the feed URL and set a date` | Housekeeping nudge, not an error. | Confirm the URL works and set `url_verified: "YYYY-MM-DD"`. |

## Webhook / ops-bot problems

| Symptom | Cause | Fix |
|---|---|---|
| Every webhook call gets 401 | Secret mismatch — or `TELEGRAM_WEBHOOK_SECRET` is empty, which **rejects everything** (fail closed). Repeated strangers' calls surface as `UnauthorizedWebhookCall` events. | Set the same secret in the environment and in `register-webhook`; re-run the migrate job after changing it. |
| Ops commands do nothing | Commands only work inside the configured ops chat; messages from other chats and from bots are ignored without reply. | Talk to the bot in the ops group. |
| `/ack` refused | Write commands need the `admin` role in the ops chat (cached ~15 min) or membership in `write_allowlist`. The refusal is logged to `operator_actions`. | Promote the user or add their id to `errors.authorization.write_allowlist`. |
| Webhook not registered after standing up the stack | `register-webhook` runs only in the migrate job — never on container start. | Start `<name_prefix>-migrate` once (see `docs/deployment/terraform.md`). |

## Review / feedback problems

| Symptom | Cause | Fix |
|---|---|---|
| Cards appear but pressing a button does nothing | `allowed_updates` does not include `callback_query`, so Telegram never delivers the press. Registrations made by an older build ask for `message` only. | Re-run `register-webhook` (the migrate job does it on every deploy). |
| No card ever appears | The source has no `feedback` block or it is `enabled: false`; or `TELEGRAM_BOT_TOKEN` is empty, which fills the queue without sending. `cli validate` prints `~> review in [...]` for every reviewed source. | Add the block, set a token, and wait one fetch interval — the fetch job seeds the chat. |
| Cards stopped, queue is not empty | A card is still showing (only one at a time), or a claim's send failed and is waiting for the next fetch pass. `cli stats` shows `showing=` and `queued=` per chat. | Answer the card. If `showing=1` but the chat is visibly empty, the card was deleted by hand — clear it with `UPDATE feedback_reviews SET state='queued', message_id=NULL WHERE state='pending'`. |
| Answered cards stay in the chat with their buttons stripped | A bot may delete its own messages only for 48 h; beyond that it needs admin rights. The label is written before any Telegram call, so nothing was lost. | Promote the bot to admin in the review chat. |
| A press answers "already approved/rejected" | Two people pressed the same card; the first press stands. | Nothing to fix — this is the intended race outcome. |
| `review_approved` is empty in the export | Those items are not labelled yet (`review_state` is `queued`/`pending`), or their source has no review chat (`review_state` is empty too). | Filter on `review_approved IS NOT NULL` when building a training set. |

To exercise the loop locally with no bot token and no Telegram account, POST
a synthetic press at the webhook and watch `cli stats` move:

```bash
docker compose --profile dev up -d       # db, migrate, bot, webhook:8000, adminer:8080
docker compose run --rm bot python -m cli stats     # find the pending item_id
curl -s localhost:8000/telegram/webhook \
  -H "x-telegram-bot-api-secret-token: $TELEGRAM_WEBHOOK_SECRET" \
  -H 'content-type: application/json' \
  -d '{"update_id":1,"callback_query":{"id":"1","from":{"id":42,"username":"me"},
       "message":{"message_id":100,"chat":{"id":-100999900010}},
       "data":"fb:a:<item_id>"}}'
docker compose run --rm bot python -m cli stats     # labelled count went up
```

The outbound Bot API calls fail harmlessly without a token; what this checks
is that the Postgres state machine advances.

## Translation problems

| Symptom | Cause | Fix |
|---|---|---|
| Posts go out untranslated; log line `deepl_configured_without_api_key_falling_back_to_none` (or the provider is set to an unknown name) | Misconfigured provider degrades to a no-op instead of blocking posts. | Set `TRANSLATE_PROVIDER=deepl` and a valid `TRANSLATE_API_KEY`. |
| Log lines `deepl_quota_warning` / `deepl_quota_stop`; posts untranslated | The DeepL quota guard stops translating at 95 % of the character budget (warns at 80 %) so the account is never fully exhausted by the bot. | Wait for the quota reset or raise the plan. Posts continue untranslated; nothing is lost. |
| `parquet export needs pyarrow: pip install '.[export]'` | The `export` extra is not installed (it is also absent from the Docker image — export is a workstation tool). | `pip install -e '.[export]'` locally, or use `--format csv`. |
| `parquet needs --out <path>` | Parquet cannot stream to stdout. | Pass `--out file.parquet`. |

## Deploy problems

| Symptom | Cause | Fix |
|---|---|---|
| CI deploy fails with Azure saying a resource "does not exist" | The OIDC service principal lacks Contributor on the resource group (the role assignment is Terraform-managed, gated on `ci_principal_id`). | Check `ci_principal_id` and re-apply Terraform. |
| "migration timed out after 10 minutes" in CI | The migrate job did not reach `Succeeded`. | `az containerapp job execution list -n <name_prefix>-migrate …` and the job logs; fix, then re-run the workflow. The old image keeps running meanwhile. |
| `DRY_RUN` flipped with `az` reverts by itself | Terraform manages the full env set and reverts out-of-band changes on the next apply. | Flip it via `terraform apply -var dry_run=false`. |
| Deadman monitor fires | Fetch passes stopped completing: crashed job, stuck lock, dead database, or an expired cron. The ping is sent only after a fully successful pass. | Check the latest `<name_prefix>-fetch` executions and the ops chat for the underlying alert. |
