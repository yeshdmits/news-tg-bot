# ADR 0003 — CI owns the running image tags; Terraform ignores them

## Status

Accepted.

## Context

Two tools touch the Container Apps: Terraform (topology) and GitHub Actions
(every merge to main ships a new image with `az containerapp ... update`).
If Terraform tracked the image field, every apply after a CI deploy would
try to roll the apps back to whatever tag the tfvars mentioned.

## Decision

Split ownership. Terraform manages everything about the three compute units
*except* the image:

```hcl
lifecycle { ignore_changes = [template[0].container[0].image] }
```

The `image` variable exists only to bootstrap the first apply. CI deploys by
mutating tags; Terraform never reverts them.

## Consequences

- `terraform plan` output never shows what is actually running — check with
  `az containerapp show ... --query .../image`. The interaction is invisible
  and must be learned (this ADR is the documentation).
- Rollback is a CI-side operation (re-point the tag), not a Terraform one.
- The inverse rule matters just as much: everything that is *not* the image
  — env vars, scaling, secrets — belongs to Terraform, and out-of-band `az`
  changes to those get reverted on the next apply (see ADR 0004).
