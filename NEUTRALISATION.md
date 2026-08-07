# Neutrality policy

This repository is operator-neutral by design: no tracked file may contain a
value specific to one operator, one account, or one deployment. Everything a
running instance needs is supplied through environment variables, GitHub
repository variables and secrets, Terraform variables, or untracked local
files — a fork never has to edit a tracked file to deploy.

This document says where each kind of value belongs. The guard
(`scripts/check-neutral.sh`, run by CI on every push) enforces it.

Legend for the "belongs in" column:

- **env** — application environment variable (see `docs/configuration.md`)
- **repo var** — GitHub Actions repository variable (non-sensitive)
- **repo secret** — GitHub Actions repository secret
- **tfvar** — Terraform variable (`terraform.tfvars`, gitignored)
- **backend.hcl** — Terraform partial backend config (gitignored)
- **cloud secret** — Azure Key Vault secret provisioned by Terraform
- **untracked** — local file, gitignored, never committed

| Kind of value | Belongs in |
|---|---|
| Live spec (`spec.json`), incl. real Telegram chat and channel ids | untracked file via env `SPEC_PATH`, or env `SPEC_JSON`/`SPEC_URL` (cloud: fetched from the URL in Key Vault secret `spec-url`, injected as `SPEC_URL`). The tracked example uses only the designated fake `9999000` id block. |
| URL the live spec is served from | tfvar `spec_url` (required, no default) → Key Vault secret `spec-url`. Never tracked: an operator's host or account name in that URL is exactly what `scripts/check-neutral.sh` exists to catch. |
| Telegram bot token, webhook secret, DeepL key | env (cloud: Key Vault secrets) |
| Azure subscription id | tfvar `subscription_id` (required, no default) |
| CI service-principal object id | tfvar `ci_principal_id` (default `""` = role grant disabled) |
| Terraform remote-state storage account / resource group | `backend.hcl` via `terraform init -backend-config` (example tracked) |
| Cloud resource names | derived from tfvar `name_prefix`; CI uses repo vars `NAME_PREFIX`, `AZURE_RESOURCE_GROUP`; docs write `<name_prefix>-*` |
| Container registry namespace / image name | repo var `IMAGE_NAME` (default `ghcr.io/<owner>/<repo>`); tfvar `image` (required) |
| Registry credentials (non-GHCR) | repo secrets `DOCKERHUB_USERNAME`, `DOCKERHUB_TOKEN` |
| Operator timezone, quiet hours, rate limits | the operator's own spec; the tracked example uses `UTC` and neutral defaults |
| Maintainer's name | `LICENSE` only — a copyright notice names the rights holder on purpose, and `LICENSE` is the guard's single exemption. |

Runtime coordination names (such as the Postgres advisory lock
`newsbot:fetch`) are part of the deployment interface and deliberately
neutral: they must never encode an operator or brand.

## Enforcement

`scripts/check-neutral.sh` runs in CI on every push and fails if any
forbidden value pattern (`scripts/neutral-patterns.txt`) appears in the
tracked tree, if a real-looking Telegram id appears outside the designated
`9999000` fake block, or if a live config file (`spec.json`, `.env`,
`*.tfvars`, `backend.hcl`, `spec.local.json`) is ever tracked.
