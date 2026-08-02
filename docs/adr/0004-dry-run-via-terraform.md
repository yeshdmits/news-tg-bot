# ADR 0004 — DRY_RUN is flipped via Terraform, never via az

## Status

Accepted.

## Context

`DRY_RUN` is the go-live switch. The tempting way to flip it is
`az containerapp update --set-env-vars DRY_RUN=false` — instant, no plan.
But Terraform manages the container env blocks, and the azurerm provider
treats a unit's env set as a single attribute: you cannot `ignore_changes`
one env entry without ignoring them all
(hashicorp/terraform-provider-azurerm#30049).

## Decision

The env set stays fully Terraform-managed. `DRY_RUN` is a Terraform variable
(default `"true"`); going live is

```bash
terraform apply -var dry_run=false
```

An `az`-side flip is treated as drift and reverted by the next apply.

## Consequences

- One authoritative place answers "is this stack live?" — the Terraform
  state/tfvars, not a mutable runtime setting.
- Flipping requires a plan/apply cycle (minutes, and deliberate). This is
  considered a feature for a switch that starts posting to real channels.
- The failure mode this prevents is nasty: an `az` flip appears to work,
  then a routine apply weeks later silently reverts the bot to dry-run — or
  worse, the ignore-everything workaround masks real env drift.
