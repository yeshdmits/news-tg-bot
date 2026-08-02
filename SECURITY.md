# Security policy

## Reporting a vulnerability

Please report suspected vulnerabilities privately via
[GitHub security advisories](../../security/advisories/new) for this
repository. Do not open a public issue for security reports. You should get a
response within a week.

## Known and accepted design decisions

The following are deliberate tradeoffs, documented in `docs/adr/`. Please do
not report them as vulnerabilities unless you can show impact beyond what is
described here.

- **The PostgreSQL server has a public endpoint.** Access is limited by the
  server firewall (Azure-internal traffic plus an optional single developer
  IP), TLS is enforced, and the only credential is a long random password.
  VNet integration was rejected on cost grounds. See
  `docs/adr/0005-public-postgres-endpoint.md`.
- **Terraform state contains secrets in plaintext** (database password, bot
  token, webhook secret, API keys). State lives in a private Azure storage
  account and is never committed; `terraform.tfvars` is gitignored. This is
  the standard Terraform tradeoff, accepted here.
- **The webhook endpoint is publicly reachable.** Every request must carry
  the `X-Telegram-Bot-Api-Secret-Token` header matching a shared secret;
  requests without it get an empty 401. An empty configured secret rejects
  all requests rather than allowing all.
- **The container runs as root** (no `USER` directive in the Dockerfile). The
  container is short-lived, runs no inbound services except the webhook app,
  and holds no host mounts in production.
- **Ops commands are authorized by Telegram chat membership**: read commands
  for any member of the ops group, destructive commands for admins (cached)
  or an explicit allowlist. Anyone added to the ops group can read error
  details.

## Secrets handling

All secrets reach the app as environment variables (locally via `.env`,
gitignored; in Azure via Container Apps secrets set by Terraform). No secret
is baked into the image or committed to the repository.
