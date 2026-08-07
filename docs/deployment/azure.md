# Azure runtime and CI deployment

The production runtime is Azure Container Apps plus one Postgres Flexible
Server, provisioned by Terraform (`docs/deployment/terraform.md`) and fed
images by GitHub Actions.

## Resources

Every name derives from the Terraform `name_prefix` variable (`<name_prefix>`
below):

| Resource | Name | Role |
|---|---|---|
| Resource group | `<name_prefix>-rg` | Everything below lives here. |
| Container Apps environment | `<name_prefix>-env` | Consumption profile, no Log Analytics. |
| Container App | `<name_prefix>-bot` | `python -m cli serve` — the Telegram webhook + ops commands. External ingress on 8000, scale-to-zero (0–2 replicas). |
| Container App Job | `<name_prefix>-fetch` | `python -m cli run --once` — cron `*/5 * * * *`, timeout 280 s, no retries, parallelism 1. |
| Container App Job | `<name_prefix>-migrate` | `alembic upgrade head && python -m cli register-webhook` — manual trigger, started by CI. |
| PostgreSQL Flexible Server | `<name_prefix>-pg` | B1ms burstable, Postgres 17, 32 GB, 7-day backups, public endpoint behind the server firewall. |
| Key Vault | `key_vault_name` var | Holds every runtime secret, including the URL the spec is fetched from (`spec-url`). |
| User-assigned identity | `<name_prefix>-runtime` | Attached to all three compute units; grants them read access to the Key Vault secrets. |

All three compute units run the **same image** with different commands.
Secrets (database URL, bot token, webhook secret, DeepL key) live in the
Key Vault and reach the units as Container Apps secret references resolved
through the user-assigned identity. The spec is neither baked into the image
nor stored in Azure: each unit fetches it at startup from the URL in the
`spec-url` Key Vault secret, injected as the `SPEC_URL` environment
variable. Editing the spec at that URL updates the deployment on the fetch
job's next run — no apply, no image rebuild. The cost of that convenience is
a startup dependency on the host serving it: a failed fetch aborts the unit
rather than falling back (see [Configuration](../configuration.md)).

## How CI deploys

One workflow: `.github/workflows/pipeline.yml`. Releases, image publishing
and deploys are all jobs of this single workflow — deliberately, because
tags created with the built-in `GITHUB_TOKEN` cannot trigger other
workflows (see `docs/adr/0009-tag-on-merge-releases.md`).

- **Triggers**: every push and PR to `main` runs the checks and a build;
  only a push to `main` publishes, and only a release deploys.
- **`check-neutral`**: runs `scripts/check-neutral.sh` so operator-specific
  values never re-enter the tracked tree.
- **`test`**: installs the package, `compileall`, validates
  `spec.example.json`, `pytest` against a Postgres 17 service container.
  Unreachable Postgres fails CI (no silent skips).
- **`release`** (main pushes only): reads the Conventional Commits since
  the last `v*` tag and, when they warrant a release, creates the tag and
  the GitHub Release in this run and outputs the version to the later jobs.
  A merge with only `docs:`/`chore:`-class commits releases nothing and the
  pipeline still succeeds. See `docs/releasing.md`.
- **`build-and-push`**: builds the Docker image and pushes it to
  `ghcr.io/<owner>/<repo>` by default, authenticated with the built-in
  `GITHUB_TOKEN` — zero secrets on a fork. Every main push publishes the
  commit-sha tag; a release additionally publishes `X.Y.Z`, `X.Y`, `X` and
  moves `latest` (non-release pushes leave `latest` alone). **Make the GHCR
  package public once after the first push** so Container Apps can pull it
  anonymously. To use Docker Hub instead, set the repository variable
  `IMAGE_NAME=docker.io/<user>/<repo>` plus the
  `DOCKERHUB_USERNAME`/`DOCKERHUB_TOKEN` secrets.
- **`deploy`** (needs release + build; releases only): runs only when the
  repository variables `NAME_PREFIX` and `AZURE_RESOURCE_GROUP` are set —
  otherwise a `deploy-skipped` job prints setup instructions instead of
  failing. Deploys are serialised through the `deploy-production`
  concurrency group and roll out the `X.Y.Z` image the release job just
  published. Logs into Azure with **OIDC** (`azure/login` +
  `AZURE_CLIENT_ID` / `AZURE_TENANT_ID` / `AZURE_SUBSCRIPTION_ID` secrets;
  no stored password). The identity is a service principal federated to
  `refs/heads/main` with Contributor on `<name_prefix>-rg` only.

The deploy sequence — **order matters, the steps must not be parallelised**:

1. `az containerapp job update -n <name_prefix>-migrate --image <version>`
   then `job start` — run migrations on the **new** image.
2. Poll `az containerapp job execution list` every 10 s (up to 10 min) until
   `Succeeded`; `Failed`/`Stopped`/`Degraded` or timeout aborts the deploy.
3. Only then `az containerapp job update -n <name_prefix>-fetch --image
   <version>` and `az containerapp update -n <name_prefix>-bot --image
   <version>`.

The new code may depend on the new schema; steps 1–2 guarantee no consumer
runs new code against an old database. If step 2 fails, `<name_prefix>-fetch`
and `<name_prefix>-bot` keep running the previous image.

GitHub repository **variables**: `NAME_PREFIX`, `AZURE_RESOURCE_GROUP`
(usually `<name_prefix>-rg`), optionally `IMAGE_NAME`. Repository
**secrets**: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`
(OIDC), plus `DOCKERHUB_USERNAME`/`DOCKERHUB_TOKEN` only when `IMAGE_NAME`
points at Docker Hub.

## Verifying a deploy

```bash
# What is actually running?
az containerapp show -n <name_prefix>-bot -g <name_prefix>-rg \
  --query "properties.template.containers[0].image" -o tsv
az containerapp job show -n <name_prefix>-fetch -g <name_prefix>-rg \
  --query "properties.template.containers[0].image" -o tsv

# Did the last fetch execution succeed?
az containerapp job execution list -n <name_prefix>-fetch -g <name_prefix>-rg \
  --query "[0].properties" -o table

# Live console logs while a replica exists:
az containerapp job logs show -n <name_prefix>-fetch -g <name_prefix>-rg \
  --container <name_prefix>-fetch
az containerapp logs show -n <name_prefix>-bot -g <name_prefix>-rg
```

Beyond that, the system reports on itself: the ops Telegram group receives
error alerts, and the deadman monitor (`HEALTHCHECK_URL`) alarms when fetch
passes stop completing.

## Rollback

Deploys only move image tags, so rollback is re-pointing them at the
previous version (or any commit sha — every main push publishes one):

```bash
SHA=<previous-version-or-sha>   # e.g. 0.3.1, or a 7-char commit sha
IMAGE=ghcr.io/<owner>/<repo>   # or your $IMAGE_NAME
az containerapp job update -n <name_prefix>-fetch -g <name_prefix>-rg --image $IMAGE:$SHA
az containerapp update     -n <name_prefix>-bot   -g <name_prefix>-rg --image $IMAGE:$SHA
```

Caveat: migrations are not automatically rolled back. If the bad deploy's
migration is incompatible with the old code, downgrade the schema first
(`alembic downgrade`, via the migrate job or a workstation connection).

## Cost model (indicative, written 2026-08)

Sized to fit largely inside the Container Apps free grant and a burstable
database:

| Item | Setting | Cost driver |
|---|---|---|
| `<name_prefix>-fetch` | 0.25 vCPU / 0.5 GiB, runs ≤ 280 s every 5 min | Consumption billing per vCPU-second; the monthly free grant absorbs much of it. |
| `<name_prefix>-bot` | 0.25 vCPU / 0.5 GiB, **min replicas 0** | Scale-to-zero: bills only while handling requests. Min replicas 1 would roughly double the stack's compute bill. |
| `<name_prefix>-migrate` | runs once per deploy | Negligible. |
| `<name_prefix>-pg` | B1ms, 32 GB, no geo-redundancy, auto-grow off | The dominant fixed cost (order of €15–25/month depending on region/reservations). |
| Key Vault | standard tier | Per-operation pricing; this workload's read volume is negligible. |
| Log Analytics | **none** | Deliberately absent — per-GB ingestion can out-cost the compute. |
| Registry | GHCR (public package) | Free. Docker Hub free tier if you opt in via `IMAGE_NAME`. |

The archive grows without bound by design (`docs/data-model.md`); 32 GB with
auto-grow off means storage pressure surfaces as an error rather than a
silently growing bill.
