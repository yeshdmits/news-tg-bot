# ADR 0005 — Public Postgres endpoint instead of VNet integration

## Status

Accepted.

## Context

Azure offers two network postures for PostgreSQL Flexible Server: private
access (VNet integration — the server gets no public address) or public
access behind a server firewall. Private access requires a VNet, a delegated
subnet, private DNS, and a Container Apps environment wired into the same
VNet — infrastructure whose standing cost would rival or exceed the entire
rest of this stack.

## Decision

Public endpoint, with the exposure narrowed by:

- the server firewall passing only Azure-internal traffic
  (the `0.0.0.0` rule) plus one optional developer IP (`client_ip`);
- TLS enforced (`sslmode=require` in the connection string);
- a single long random admin password (32+ chars, per the variable's
  documentation);
- role-level `statement_timeout` and `idle_in_transaction_session_timeout`
  so stray connections cannot hold resources.

## Consequences

- Cost stays proportional to the workload; a developer can `psql` in from an
  allow-listed IP without a bastion.
- Residual, accepted risk: the endpoint is reachable from anything running
  in Azure (any tenant), so the password is the real boundary. This is
  documented in `SECURITY.md` as a known decision — do not report it as a
  vulnerability without demonstrating impact beyond this description.
- Revisit if the stack ever holds data more sensitive than public news
  items and its own operational state.
