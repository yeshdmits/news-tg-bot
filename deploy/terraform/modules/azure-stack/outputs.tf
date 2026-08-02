output "bot_fqdn" {
  value = azurerm_container_app.bot.ingress[0].fqdn
}

output "webhook_url" {
  value = "https://${azurerm_container_app.bot.ingress[0].fqdn}/telegram/webhook"
}

output "key_vault_name" {
  value = azurerm_key_vault.kv.name
}

output "key_vault_uri" {
  value = azurerm_key_vault.kv.vault_uri
}

# Handy for out-of-band checks (`az role assignment list --assignee ...`).
output "runtime_identity_principal_id" {
  value = azurerm_user_assigned_identity.runtime.principal_id
}
