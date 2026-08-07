# Azure root — remote state + provider + the azure-stack module.
# Workflow:
#   cd deploy/terraform/azure
#   cp backend.hcl.example backend.hcl        # your state storage (gitignored)
#   cp terraform.tfvars.example terraform.tfvars
#   terraform init -backend-config=backend.hcl
#   terraform plan / apply
#
# Bootstrap ordering: the state storage account must exist BEFORE the first
# `terraform init`, so it is created out-of-band (by hand, once) and is not
# managed by this configuration:
#   az group create -n <tfstate-rg> -l <region>
#   az storage account create -n <globally-unique-account> -g <tfstate-rg> \
#     -l <region> --sku Standard_LRS --min-tls-version TLS1_2 \
#     --allow-blob-public-access false
#   az storage container create -n tfstate --account-name <account>
#
# Remote state still contains secrets in plaintext (at minimum the Postgres
# admin password via the server resource) — keep the account private, never
# in git.

terraform {
  # >= 1.11 for write-only secret arguments in the module.
  required_version = ">= 1.11"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.37"
    }
  }

  # Partial configuration: a backend block cannot reference variables, so
  # the account names live in the gitignored backend.hcl, passed at init
  # time with -backend-config. See backend.hcl.example.
  backend "azurerm" {}
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
}

module "stack" {
  source = "../modules/azure-stack"

  name_prefix     = var.name_prefix
  key_vault_name  = var.key_vault_name
  image           = var.image
  location        = var.location
  ci_principal_id = var.ci_principal_id

  pg_password             = var.pg_password
  telegram_bot_token      = var.telegram_bot_token
  telegram_webhook_secret = var.telegram_webhook_secret
  translate_api_key       = var.translate_api_key
  healthcheck_url         = var.healthcheck_url
  spec_url                = var.spec_url
  secrets_wo_version      = var.secrets_wo_version
  dry_run                 = var.dry_run
  translate_provider      = var.translate_provider
  client_ip               = var.client_ip
}

# Root variables mirror the module contract 1:1 (see ../README.md).

variable "subscription_id" {
  type        = string
  description = "Azure subscription to deploy into"

  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}(-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}$", var.subscription_id))
    error_message = "subscription_id must be a GUID."
  }
}

variable "name_prefix" {
  type        = string
  description = "Prefix for every resource name; see the module variable for constraints"
}

variable "key_vault_name" {
  type        = string
  description = "Globally unique Key Vault name (3-24 alphanumeric/dash)"
}

variable "image" {
  type        = string
  description = "Bootstrap image reference, e.g. ghcr.io/<owner>/<repo>:latest — CI owns the running tag afterwards"
}

variable "location" {
  type    = string
  default = "westeurope"
}

variable "ci_principal_id" {
  type        = string
  default     = ""
  description = "Object id of your GitHub OIDC deploy service principal; empty disables the Contributor grant"
}

variable "pg_password" {
  type      = string
  sensitive = true
}
variable "telegram_bot_token" {
  type      = string
  sensitive = true
}
variable "telegram_webhook_secret" {
  type      = string
  sensitive = true
}
variable "translate_api_key" {
  type      = string
  sensitive = true
}
variable "healthcheck_url" {
  type      = string
  sensitive = true
}

variable "spec_url" {
  type        = string
  sensitive   = true
  description = "https:// URL serving your spec JSON; becomes the Key Vault spec-url secret injected as SPEC_URL. Editing the spec at that URL needs no Terraform run."

  # Mirrors the runtime's own guard (specsource.py) so a plain-http value
  # fails at plan time rather than at container start.
  validation {
    condition     = startswith(var.spec_url, "https://")
    error_message = "spec_url must be https:// — the runtime refuses to fetch anything else."
  }
}

variable "secrets_wo_version" {
  type        = number
  default     = 1
  description = "Bump after changing any secret value to push it to Key Vault"
}

variable "dry_run" {
  type    = string
  default = "true"
}
variable "translate_provider" {
  type    = string
  default = "deepl"
}
variable "client_ip" {
  type    = string
  default = ""
}

output "bot_fqdn" {
  value = module.stack.bot_fqdn
}

output "webhook_url" {
  value = module.stack.webhook_url
}

output "key_vault_name" {
  value = module.stack.key_vault_name
}
