# Infrastructure as code

```
terraform/
  modules/azure-stack/   # all Azure resources: Postgres + Key Vault + identity + three Container Apps units
  azure/                 # root: backend + provider + module call — run terraform HERE
```

Full walkthrough: [docs/deployment/terraform.md](../../docs/deployment/terraform.md).
Runtime and deploy pipeline: [docs/deployment/azure.md](../../docs/deployment/azure.md).

## Quick reference

```bash
cd deploy/terraform/azure
cp backend.hcl.example backend.hcl             # YOUR state storage (gitignored;
                                               # bootstrap it out-of-band — see main.tf)
cp terraform.tfvars.example terraform.tfvars   # fill real values (gitignored)
terraform init -backend-config=backend.hcl
terraform plan                                 # review before ANY apply
terraform apply
```

Required variables: `subscription_id`, `name_prefix` (every resource name
derives from it), `key_vault_name` (globally unique), `image`
(e.g. `ghcr.io/<owner>/<repo>:latest`), plus the secrets. Optional:
`spec_json_file` (default `spec.local.json`), `secrets_wo_version` (bump to
push changed secrets), `ci_principal_id` (empty disables the CI grant).
Full list: [docs/deployment/terraform.md](../../docs/deployment/terraform.md).

After a from-scratch apply, run the migration job once to create the schema
and register the Telegram webhook:

```bash
az containerapp job start -n <name_prefix>-migrate -g <name_prefix>-rg
az containerapp job execution list -n <name_prefix>-migrate -g <name_prefix>-rg \
  --query "[0].properties.status" -o tsv   # poll until Succeeded
```

## Rules

- **tfvars holds secrets in plaintext, and state still holds the Postgres
  admin password** (the other secrets are write-only Key Vault arguments and
  never enter state) — both stay out of git (state is remote in a private
  storage account; tfvars and backend.hcl are gitignored).
- **Images belong to CI** (`ignore_changes` on all three units); everything
  else, including `DRY_RUN`, belongs to Terraform: go live with
  `terraform apply -var dry_run=false`, never `az --set-env-vars`.
- The stack is deliberately destroyable (`prevent_destroy = false`
  everywhere): destroy/recreate is a supported workflow. Destroying wipes the
  archive database and changes the bot FQDN; the migrate job re-registers
  the webhook at the new URL.
