# Input contract of the stack module.
#
# Identity rules:
#   - Nothing identifying an account, principal, or globally unique resource
#     has a default. A default that happens to be someone else's value fails
#     late and confusingly; a missing value fails at plan time with a name.
#   - Region, sizes and behaviour knobs have sensible defaults.
#
# State rules: secret *values* are written to Key Vault with write-only
# arguments (value_wo), so they do not persist in Terraform state — but the
# Postgres admin password is also an attribute of the server resource, and
# that attribute DOES land in state in plaintext. Treat the state store as a
# credential regardless.

variable "name_prefix" {
  type        = string
  description = "Prefix all resource names derive from (rg, env, pg, bot, fetch, migrate). Keep it short and lowercase; the Postgres server name <prefix>-pg must be globally unique under *.postgres.database.azure.com — a collision fails at create time, pick another prefix."

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,11}$", var.name_prefix))
    error_message = "name_prefix must be 3-12 chars, lowercase alphanumeric or dash, starting with a letter."
  }
}

variable "key_vault_name" {
  type        = string
  description = "Key Vault name — globally unique DNS label (3-24 alphanumeric/dash). Not derived from name_prefix because vault names collide globally."

  validation {
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9-]{1,22}[a-zA-Z0-9]$", var.key_vault_name))
    error_message = "key_vault_name must be 3-24 chars, alphanumeric and dashes, starting with a letter and ending alphanumeric."
  }
}

variable "image" {
  type        = string
  description = "Bootstrap image reference only — CI owns the running tag (ignore_changes), e.g. ghcr.io/<owner>/<repo>:latest"
}

variable "location" {
  type    = string
  default = "westeurope" # eastus is cheaper; keep an eye on WEU capacity
}

variable "pg_password" {
  type        = string
  sensitive   = true
  description = "Postgres admin password, 32+ chars; the server is publicly reachable. Lands in Terraform state via the server resource."
}

variable "telegram_bot_token" {
  type      = string
  sensitive = true
}

variable "telegram_webhook_secret" {
  type        = string
  sensitive   = true
  description = "Compared against X-Telegram-Bot-Api-Secret-Token; e.g. openssl rand -hex 32"
}

variable "translate_api_key" {
  type        = string
  sensitive   = true
  description = "DeepL API key; empty string is fine when translate_provider is none"
}

variable "healthcheck_url" {
  type        = string
  sensitive   = true
  description = "Deadman monitor ping target (healthchecks.io), hit after every completed fetch pass; the URL itself is the credential"
}

variable "spec_json" {
  type        = string
  sensitive   = true
  description = "Full spec JSON content, injected into the runtime as SPEC_JSON via Key Vault. Contains channel ids — kept out of state by the write-only secret argument."
}

# Single version counter for every write-only Key Vault secret write. The
# provider cannot see current secret values (that is the point), so it only
# pushes new ones when this number changes: bump it after editing any secret
# variable (or spec.local.json) and apply.
variable "secrets_wo_version" {
  type    = number
  default = 1
}

# Flip via tfvars/-var and `terraform apply` — NOT via `az containerapp
# update --set-env-vars`: ignore_changes on a single env entry would mask
# drift for the whole env set (hashicorp/terraform-provider-azurerm#30049),
# so env stays fully Terraform-managed and an az flip would be reverted on
# the next apply.
variable "dry_run" {
  type        = string
  default     = "true"
  description = "\"true\" suppresses all Telegram channel posts on the bot and fetch units"
}

variable "translate_provider" {
  type        = string
  default     = "deepl"
  description = "none | deepl; with none, translate_lead channels post untranslated"
}

variable "client_ip" {
  type        = string
  default     = ""
  description = "Optional developer IP allowed through the Postgres firewall (psql access)"
}

variable "ci_principal_id" {
  type        = string
  default     = ""
  description = "Object id of YOUR GitHub OIDC service principal that deploys images; empty (default) disables the grant entirely"
}

variable "pg_sku_name" {
  type    = string
  default = "B_Standard_B1ms"
}

variable "pg_storage_mb" {
  type    = number
  default = 32768
}

variable "pg_backup_retention_days" {
  type    = number
  default = 7
}

variable "fetch_cron" {
  type        = string
  default     = "*/5 * * * *"
  description = "Fetch job schedule; keep at or below the tightest fetch_interval_min in the spec"
}
