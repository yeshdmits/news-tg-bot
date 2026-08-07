# Terraform

All infrastructure lives in `deploy/terraform/`:

```
deploy/terraform/
  azure/                 # root module: backend, provider, module call — run terraform HERE
  modules/azure-stack/   # every resource: Postgres + Key Vault + identity + Container Apps environment + 3 units
```

Terraform owns the **topology**. It deliberately does not own the running
image tags — CI does (see [What Terraform ignores](#what-terraform-manages-and-what-it-ignores)).

## Prerequisites

- Terraform ≥ 1.11 (write-only secret arguments; provider
  `hashicorp/azurerm ~> 4.37`).
- Azure CLI, logged in (`az login`) with rights on the target subscription.
- The secrets listed under [Variables](#variables).

## Backend bootstrap (once, before the first `terraform init`)

State is remote in an Azure storage account: it still contains secrets in
plaintext (at minimum the Postgres admin password, via the server resource —
see [Key Vault and state](#key-vault-and-what-stays-out-of-state)). The
account is created out-of-band, once, and is not managed by this
configuration:

```bash
az group create -n <tfstate-rg> -l <region>
az storage account create -n <globally-unique-storage-account> -g <tfstate-rg> \
  -l <region> --sku Standard_LRS --min-tls-version TLS1_2 \
  --allow-blob-public-access false
az storage container create -n tfstate --account-name <globally-unique-storage-account>
```

The backend block in `azure/main.tf` is a **partial configuration**
(`backend "azurerm" {}`) — a backend block cannot reference variables, so
your account names live in a gitignored `backend.hcl`:

```bash
cd deploy/terraform/azure
cp backend.hcl.example backend.hcl   # fill in YOUR names
terraform init -backend-config=backend.hcl
```

## Variables

Copy `azure/terraform.tfvars.example` to `azure/terraform.tfvars` (gitignored)
and fill it in.

| Variable | Sensitive | Default | Meaning |
|---|---|---|---|
| `subscription_id` | no | — | Azure subscription GUID to deploy into. |
| `name_prefix` | no | — | Prefix every resource name derives from (`<name_prefix>-rg`, `-env`, `-pg`, `-bot`, `-fetch`, `-migrate`, `-runtime`). Short, lowercase; `<name_prefix>-pg` must be globally unique. |
| `key_vault_name` | no | — | Key Vault name — globally unique DNS label (3–24 alphanumeric/dash), so it is not derived from the prefix. |
| `image` | no | — | Bootstrap image reference, e.g. `ghcr.io/<owner>/<repo>:latest`. CI owns the running tag afterwards. |
| `pg_password` | yes | — | Postgres admin password. 32+ random chars; the server has a public endpoint. |
| `telegram_bot_token` | yes | — | From @BotFather. |
| `telegram_webhook_secret` | yes | — | e.g. `openssl rand -hex 32`. |
| `translate_api_key` | yes | — | DeepL key (empty is fine with `translate_provider = "none"`). |
| `healthcheck_url` | yes | — | Deadman ping URL (the URL itself is the credential). |
| `spec_url` | yes | — | `https://` URL serving your spec; becomes the `spec-url` Key Vault secret. The URL itself is the credential. Rejected at plan time if not `https://`. |
| `secrets_wo_version` | no | `1` | Bump after changing any secret value to push it to Key Vault — see below. Spec edits are **not** one of those. |
| `dry_run` | no | `"true"` | See below — the go-live switch. |
| `translate_provider` | no | `"deepl"` | `none` disables translation. |
| `client_ip` | no | `""` | Optional developer IP allowed through the Postgres firewall. Empty = no rule. |
| `location` | no | `westeurope` | Azure region. |
| `ci_principal_id` | no | `""` | Object id of **your** GitHub OIDC deploy service principal. Empty (the default) disables the Contributor grant — CI deploys will fail until you set it to your own SP's object id. |

Module-only variables (defaults used unless you wire them through the root):
`pg_sku_name`, `pg_storage_mb`, `pg_backup_retention_days` (database
sizing), `fetch_cron` (keep at or below the tightest `fetch_interval_min`
in the spec).

## Key Vault, and what stays out of state

All runtime secrets are Key Vault secrets, written by Terraform with
**write-only arguments** (`value_wo`) — the values never land in the state
file. The one exception is the Postgres admin password, which is still in
state via the server resource attribute; that is why the state account
stays private. The container apps reference the secrets by
`key_vault_secret_id` and resolve them through the `<name_prefix>-runtime`
user-assigned identity.

The spec is **not** in Key Vault. What is stored there is `spec-url`, the
`https://` address the runtime fetches the spec from at startup, injected as
the `SPEC_URL` env var. It is a secret because whoever holds the URL can read
the spec, which names your private chat ids.

Updating the spec is therefore not a Terraform operation at all: edit it at
the URL, and the fetch job picks it up on its next run. Nothing else in this
document applies to a spec change. Validate before you publish — an
unreachable or invalid spec is fatal for all three units:

```bash
python -m cli validate --spec spec.json        # the local working copy
# publish it to whatever serves spec_url, then confirm what the cloud will get:
SPEC_URL="https://<host>/<path>/spec.json" python -m cli validate
```

Changing the URL itself *is* a Terraform change: update `spec_url`, bump
`secrets_wo_version`, and apply.

## Plan and apply

```bash
cd deploy/terraform/azure
terraform init -backend-config=backend.hcl
terraform plan     # review — always
terraform apply
```

Outputs: `bot_fqdn` and `webhook_url` (the generated Container Apps
hostname).

After a from-scratch apply the database is empty and no webhook is
registered — start the migrate job once:

```bash
az containerapp job start -n <name_prefix>-migrate -g <name_prefix>-rg
az containerapp job execution list -n <name_prefix>-migrate -g <name_prefix>-rg \
  --query "[0].properties.status" -o tsv    # poll until Succeeded
```

## What Terraform manages, and what it ignores

Every `lifecycle` block in the module, and why it is there:

- **All three compute units** (`<name_prefix>-bot`, `<name_prefix>-fetch`,
  `<name_prefix>-migrate`):

  ```hcl
  lifecycle {
    ignore_changes = [template[0].container[0].image]
  }
  ```

  CI deploys by mutating the image tag with `az containerapp ... update`.
  Terraform is told to ignore the image so a later `apply` does not silently
  roll the apps back to the bootstrap tag. **This interaction is invisible in
  a plan** — remember: image = CI's, everything else = Terraform's.

- **Postgres** (`<name_prefix>-pg`):

  ```hcl
  lifecycle {
    prevent_destroy = false
    ignore_changes  = [zone]
  }
  ```

  `zone` is assigned by Azure at create time (westeurope zone capacity is
  unreliable, so no zone is pinned); without `ignore_changes` every plan
  would try to move the server. `prevent_destroy = false` is deliberate:
  destroy/recreate is a supported workflow for this stack.

Consequences of the split:

- **`DRY_RUN` is flipped with Terraform, never with `az`**:
  `terraform apply -var dry_run=false`. Using
  `az containerapp update --set-env-vars` would be reverted on the next
  apply, and ignoring individual env entries is not possible — the azurerm
  provider treats the env set as one attribute
  (hashicorp/terraform-provider-azurerm#30049), so a partial ignore would
  mask drift for every variable.
- Changing infrastructure or a secret is a Terraform change; changing code
  is a CI deploy; changing the spec is neither — it is an edit at `spec_url`.
  If more than one changes, apply Terraform first, then let CI roll the
  image.

## Postgres specifics

- `azure.extensions = pgcrypto` is allow-listed via a server configuration
  resource — Flexible Server refuses `CREATE EXTENSION` otherwise, and
  migration 0001 needs pgcrypto.
- The firewall passes Azure-internal traffic (the 0.0.0.0 rule) plus the
  optional `client_ip`. TLS is enforced; the connection string ends in
  `sslmode=require`.
- The full `DATABASE_URL` is assembled inside the module and written to Key
  Vault as `database-url`. Being a write-only secret it does not diff — if
  you change any of its inputs (e.g. `pg_password`), bump
  `secrets_wo_version` so the new value is actually pushed.

## Teardown

```bash
cd deploy/terraform/azure
terraform destroy
```

Destroying **wipes the archive database permanently** (7-day point-in-time
backups exist while the server lives, but destroy removes the server) and
releases the app FQDN — a later re-apply gets a new hostname, and the
migrate job re-registers the Telegram webhook at the new URL automatically.
The tfstate storage account is outside Terraform and survives.

## What Terraform deliberately does not do

- No Log Analytics workspace — see `docs/adr/0002-telegram-as-observability.md`.
- No VNet integration for Postgres — see `docs/adr/0005-public-postgres-endpoint.md`.
- No CI execution of Terraform: `plan`/`apply` are manual, from a
  workstation. CI only swaps image tags.
