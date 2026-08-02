# Serverless topology on Azure Container Apps: one Postgres Flexible Server,
# one Key Vault, and three compute units running the same image — the webhook
# app, the cron fetch job, and the manual migrate job. Terraform owns the
# topology only; day-to-day deploys are CI swapping image tags with `az`, so
# the image field is ignore_changes on all three units (see
# docs/deployment/terraform.md).
#
# Secret model: values are written to Key Vault with write-only arguments
# (value_wo), so they never persist in Terraform state or plan files, and the
# container units reference them by URI through a user-assigned identity —
# the platform's own configuration holds no secret values either. The one
# exception is the Postgres admin password, which is also an attribute of the
# server resource and therefore in state; treat the state store as a
# credential.

terraform {
  # >= 1.11 for write-only arguments (value_wo).
  required_version = ">= 1.11"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.37"
    }
  }
}

locals {
  rg_name      = "${var.name_prefix}-rg"
  env_name     = "${var.name_prefix}-env"
  pg_name      = "${var.name_prefix}-pg"
  db_name      = var.name_prefix
  # Postgres logins must be alphanumeric; strip dashes from the prefix.
  pg_admin     = "${replace(var.name_prefix, "-", "")}admin"
  bot_name     = "${var.name_prefix}-bot"
  fetch_name   = "${var.name_prefix}-fetch"
  migrate_name = "${var.name_prefix}-migrate"
  uami_name    = "${var.name_prefix}-runtime"

  database_url = "postgresql://${local.pg_admin}:${var.pg_password}@${azurerm_postgresql_flexible_server.pg.fqdn}:5432/${azurerm_postgresql_flexible_server_database.db.name}?sslmode=require"
}

data "azurerm_client_config" "current" {}

resource "azurerm_resource_group" "this" {
  name     = local.rg_name
  location = var.location
}

# Your GitHub OIDC deploy identity (federated to refs/heads/main) needs
# Contributor here to swap images and start the migrate job. Without this,
# ARM answers "does not exist" for every resource and the CI deploy job
# fails. Empty ci_principal_id (the default) disables the grant.
resource "azurerm_role_assignment" "ci_deploy" {
  count                = var.ci_principal_id == "" ? 0 : 1
  scope                = azurerm_resource_group.this.id
  role_definition_name = "Contributor"
  principal_id         = var.ci_principal_id
}

# --- Key Vault: the only place secret values live ---------------------------

resource "azurerm_key_vault" "kv" {
  name                = var.key_vault_name
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  tenant_id           = data.azurerm_client_config.current.tenant_id
  sku_name            = "standard"

  rbac_authorization_enabled = true
  # Deliberately destroyable, like the rest of the stack.
  purge_protection_enabled   = false
  soft_delete_retention_days = 7
}

# Runtime identity: the three compute units resolve their Key Vault secret
# references through this user-assigned identity.
resource "azurerm_user_assigned_identity" "runtime" {
  name                = local.uami_name
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
}

resource "azurerm_role_assignment" "runtime_kv_reader" {
  scope                = azurerm_key_vault.kv.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.runtime.principal_id
}

# Whoever runs terraform apply needs to write the secrets. RBAC grants can
# take a minute to propagate — a 403 on first apply usually just means
# re-apply after a short wait.
resource "azurerm_role_assignment" "deployer_kv_officer" {
  scope                = azurerm_key_vault.kv.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = data.azurerm_client_config.current.object_id
}

# value_wo is write-only: the value is sent to Key Vault and never stored in
# state or plan. The trade-offs: Terraform cannot detect out-of-band edits
# (`az keyvault secret set` sticks — a feature for spec updates), and a
# changed variable only pushes when secrets_wo_version is bumped.
resource "azurerm_key_vault_secret" "database_url" {
  name             = "database-url"
  key_vault_id     = azurerm_key_vault.kv.id
  value_wo         = local.database_url
  value_wo_version = var.secrets_wo_version
  depends_on       = [azurerm_role_assignment.deployer_kv_officer]
}

resource "azurerm_key_vault_secret" "telegram_bot_token" {
  name             = "telegram-bot-token"
  key_vault_id     = azurerm_key_vault.kv.id
  value_wo         = var.telegram_bot_token
  value_wo_version = var.secrets_wo_version
  depends_on       = [azurerm_role_assignment.deployer_kv_officer]
}

resource "azurerm_key_vault_secret" "webhook_secret" {
  name             = "telegram-webhook-secret"
  key_vault_id     = azurerm_key_vault.kv.id
  value_wo         = var.telegram_webhook_secret
  value_wo_version = var.secrets_wo_version
  depends_on       = [azurerm_role_assignment.deployer_kv_officer]
}

resource "azurerm_key_vault_secret" "translate_api_key" {
  name             = "translate-api-key"
  key_vault_id     = azurerm_key_vault.kv.id
  value_wo         = var.translate_api_key
  value_wo_version = var.secrets_wo_version
  depends_on       = [azurerm_role_assignment.deployer_kv_officer]
}

resource "azurerm_key_vault_secret" "healthcheck_url" {
  name             = "healthcheck-url"
  key_vault_id     = azurerm_key_vault.kv.id
  value_wo         = var.healthcheck_url
  value_wo_version = var.secrets_wo_version
  depends_on       = [azurerm_role_assignment.deployer_kv_officer]
}

# The application configuration itself is a secret (it names private chat
# ids). The runtime reads it as SPEC_JSON; the image carries no config.
resource "azurerm_key_vault_secret" "spec_json" {
  name             = "spec-json"
  key_vault_id     = azurerm_key_vault.kv.id
  value_wo         = var.spec_json
  value_wo_version = var.secrets_wo_version
  depends_on       = [azurerm_role_assignment.deployer_kv_officer]
}

# --- Container Apps environment ---------------------------------------------

# No logs_destination / log_analytics_workspace_id: Log Analytics bills per
# GB and a chatty container can out-cost the compute. Observability is the
# Telegram ops group + error_events; console logs stream via
# `az containerapp job logs show` while a replica lives.
resource "azurerm_container_app_environment" "this" {
  name                = local.env_name
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location

  workload_profile {
    name                  = "Consumption"
    workload_profile_type = "Consumption"
  }
}

# --- Postgres -----------------------------------------------------------------

# Public endpoint + enforced SSL + long password is a conscious decision:
# VNet integration would erase this stack's cost advantage. Residual risk
# accepted: the server is reachable from any Azure tenant. No `zone` pin —
# westeurope zone capacity is flaky; let Azure pick on create.
resource "azurerm_postgresql_flexible_server" "pg" {
  name                = local.pg_name
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location

  version             = "17"
  sku_name            = var.pg_sku_name
  storage_mb          = var.pg_storage_mb
  administrator_login = local.pg_admin
  # In state in plaintext — the provider has no write-only variant that
  # avoids the attribute; the state store is the security boundary here.
  administrator_password        = var.pg_password
  auto_grow_enabled             = false
  backup_retention_days         = var.pg_backup_retention_days
  geo_redundant_backup_enabled  = false
  public_network_access_enabled = true

  # Deliberately destroyable: recreate-from-scratch is a supported workflow
  # for this stack and the archive is rebuildable dry-run data.
  lifecycle {
    prevent_destroy = false
    ignore_changes  = [zone] # assigned by Azure at create time
  }
}

resource "azurerm_postgresql_flexible_server_database" "db" {
  name      = local.db_name
  server_id = azurerm_postgresql_flexible_server.pg.id
  charset   = "UTF8"
  collation = "en_US.utf8"
}

# Flexible Server refuses CREATE EXTENSION unless allow-listed here;
# migration 0001 needs pgcrypto.
resource "azurerm_postgresql_flexible_server_configuration" "extensions" {
  name      = "azure.extensions"
  server_id = azurerm_postgresql_flexible_server.pg.id
  value     = "pgcrypto"
}

resource "azurerm_postgresql_flexible_server_firewall_rule" "allow_azure" {
  name             = "allow-azure-services"
  server_id        = azurerm_postgresql_flexible_server.pg.id
  start_ip_address = "0.0.0.0"
  end_ip_address   = "0.0.0.0"
}

# Optional developer access from a fixed IP (psql, inspection).
resource "azurerm_postgresql_flexible_server_firewall_rule" "client_ip" {
  count            = var.client_ip == "" ? 0 : 1
  name             = "client-ip"
  server_id        = azurerm_postgresql_flexible_server.pg.id
  start_ip_address = var.client_ip
  end_ip_address   = var.client_ip
}

# --- Compute units ---------------------------------------------------------
#
# The image is public, so no registry {} block. For a private registry, add
# one per unit (server/username/password_secret_name with a PAT — GHCR does
# not support managed-identity pulls) and store the PAT in Key Vault.

# Webhook app. min_replicas 0 is a cost guardrail: 1 puts it on the idle
# meter and roughly doubles the bill.
resource "azurerm_container_app" "bot" {
  name                         = local.bot_name
  resource_group_name          = azurerm_resource_group.this.name
  container_app_environment_id = azurerm_container_app_environment.this.id
  revision_mode                = "Single"
  workload_profile_name        = "Consumption"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.runtime.id]
  }

  ingress {
    external_enabled = true
    target_port      = 8000

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  # versionless_id: rotations in Key Vault are picked up without a
  # Terraform change (on the next revision/restart).
  secret {
    name                = "database-url"
    identity            = azurerm_user_assigned_identity.runtime.id
    key_vault_secret_id = azurerm_key_vault_secret.database_url.versionless_id
  }
  secret {
    name                = "telegram-bot-token"
    identity            = azurerm_user_assigned_identity.runtime.id
    key_vault_secret_id = azurerm_key_vault_secret.telegram_bot_token.versionless_id
  }
  secret {
    name                = "webhook-secret"
    identity            = azurerm_user_assigned_identity.runtime.id
    key_vault_secret_id = azurerm_key_vault_secret.webhook_secret.versionless_id
  }
  secret {
    name                = "spec-json"
    identity            = azurerm_user_assigned_identity.runtime.id
    key_vault_secret_id = azurerm_key_vault_secret.spec_json.versionless_id
  }

  template {
    min_replicas = 0
    max_replicas = 2

    container {
      name    = local.bot_name
      image   = var.image
      cpu     = 0.25
      memory  = "0.5Gi"
      command = ["python"]
      args    = ["-m", "cli", "serve"]

      env {
        name        = "DATABASE_URL"
        secret_name = "database-url"
      }
      env {
        name        = "TELEGRAM_BOT_TOKEN"
        secret_name = "telegram-bot-token"
      }
      env {
        name        = "TELEGRAM_WEBHOOK_SECRET"
        secret_name = "webhook-secret"
      }
      env {
        name        = "SPEC_JSON"
        secret_name = "spec-json"
      }
      env {
        name  = "DRY_RUN"
        value = var.dry_run
      }
      env {
        name  = "PORT"
        value = "8000"
      }
    }
  }

  lifecycle {
    ignore_changes = [template[0].container[0].image]
  }

  # Provision-time Key Vault resolution needs the read grant to be live.
  depends_on = [azurerm_role_assignment.runtime_kv_reader]
}

# Fetch job. The cron default (*/5) matches the tightest fetch_interval_min
# in the reference spec. replica_timeout stays under the cron period so an
# execution can never outlive it; an overlap is handled by the advisory
# lock anyway.
resource "azurerm_container_app_job" "fetch" {
  name                         = local.fetch_name
  resource_group_name          = azurerm_resource_group.this.name
  location                     = azurerm_resource_group.this.location
  container_app_environment_id = azurerm_container_app_environment.this.id
  workload_profile_name        = "Consumption"
  replica_timeout_in_seconds   = 280
  replica_retry_limit          = 0

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.runtime.id]
  }

  schedule_trigger_config {
    cron_expression          = var.fetch_cron
    parallelism              = 1
    replica_completion_count = 1
  }

  secret {
    name                = "database-url"
    identity            = azurerm_user_assigned_identity.runtime.id
    key_vault_secret_id = azurerm_key_vault_secret.database_url.versionless_id
  }
  secret {
    name                = "telegram-bot-token"
    identity            = azurerm_user_assigned_identity.runtime.id
    key_vault_secret_id = azurerm_key_vault_secret.telegram_bot_token.versionless_id
  }
  secret {
    name                = "translate-api-key"
    identity            = azurerm_user_assigned_identity.runtime.id
    key_vault_secret_id = azurerm_key_vault_secret.translate_api_key.versionless_id
  }
  secret {
    name                = "healthcheck-url"
    identity            = azurerm_user_assigned_identity.runtime.id
    key_vault_secret_id = azurerm_key_vault_secret.healthcheck_url.versionless_id
  }
  secret {
    name                = "spec-json"
    identity            = azurerm_user_assigned_identity.runtime.id
    key_vault_secret_id = azurerm_key_vault_secret.spec_json.versionless_id
  }

  template {
    container {
      name    = local.fetch_name
      image   = var.image
      cpu     = 0.25
      memory  = "0.5Gi"
      command = ["python"]
      args    = ["-m", "cli", "run", "--once"]

      env {
        name        = "DATABASE_URL"
        secret_name = "database-url"
      }
      env {
        name        = "TELEGRAM_BOT_TOKEN"
        secret_name = "telegram-bot-token"
      }
      env {
        name        = "SPEC_JSON"
        secret_name = "spec-json"
      }
      env {
        name  = "DRY_RUN"
        value = var.dry_run
      }
      env {
        name        = "HEALTHCHECK_URL"
        secret_name = "healthcheck-url"
      }
      env {
        name  = "TRANSLATE_PROVIDER"
        value = var.translate_provider
      }
      env {
        name        = "TRANSLATE_API_KEY"
        secret_name = "translate-api-key"
      }
    }
  }

  lifecycle {
    ignore_changes = [template[0].container[0].image]
  }

  depends_on = [azurerm_role_assignment.runtime_kv_reader]
}

# Migration job: manual trigger only. CI starts it and waits for success
# before the other two units get a new image — the schema must be migrated
# before any consumer runs against it. Registers the Telegram webhook after
# migrating, never on container start (a scale-from-zero start would hit
# setWebhook rate limits).
resource "azurerm_container_app_job" "migrate" {
  name                         = local.migrate_name
  resource_group_name          = azurerm_resource_group.this.name
  location                     = azurerm_resource_group.this.location
  container_app_environment_id = azurerm_container_app_environment.this.id
  workload_profile_name        = "Consumption"
  replica_timeout_in_seconds   = 280
  replica_retry_limit          = 0

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.runtime.id]
  }

  manual_trigger_config {
    parallelism              = 1
    replica_completion_count = 1
  }

  secret {
    name                = "database-url"
    identity            = azurerm_user_assigned_identity.runtime.id
    key_vault_secret_id = azurerm_key_vault_secret.database_url.versionless_id
  }
  secret {
    name                = "telegram-bot-token"
    identity            = azurerm_user_assigned_identity.runtime.id
    key_vault_secret_id = azurerm_key_vault_secret.telegram_bot_token.versionless_id
  }
  secret {
    name                = "webhook-secret"
    identity            = azurerm_user_assigned_identity.runtime.id
    key_vault_secret_id = azurerm_key_vault_secret.webhook_secret.versionless_id
  }
  secret {
    name                = "spec-json"
    identity            = azurerm_user_assigned_identity.runtime.id
    key_vault_secret_id = azurerm_key_vault_secret.spec_json.versionless_id
  }

  template {
    container {
      name    = local.migrate_name
      image   = var.image
      cpu     = 0.25
      memory  = "0.5Gi"
      command = ["/bin/sh"]
      args    = ["-c", "alembic upgrade head && python -m cli register-webhook"]

      env {
        name        = "DATABASE_URL"
        secret_name = "database-url"
      }
      env {
        name        = "TELEGRAM_BOT_TOKEN"
        secret_name = "telegram-bot-token"
      }
      env {
        name        = "TELEGRAM_WEBHOOK_SECRET"
        secret_name = "webhook-secret"
      }
      env {
        name  = "WEBHOOK_URL"
        value = "https://${azurerm_container_app.bot.ingress[0].fqdn}/telegram/webhook"
      }
      env {
        name        = "SPEC_JSON"
        secret_name = "spec-json"
      }
    }
  }

  lifecycle {
    ignore_changes = [template[0].container[0].image]
  }

  depends_on = [azurerm_role_assignment.runtime_kv_reader]
}
