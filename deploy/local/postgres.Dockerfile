# Local development Postgres: the stock image plus pg_cron.
#
# The production server (Azure Flexible Server) already has pg_cron in
# shared_preload_libraries but NOT in the azure.extensions allow-list, so
# CREATE EXTENSION pg_cron fails there and the retention/archive jobs run as
# application-level scheduled tasks instead (see docs/data-model.md).
# pg_cron is kept available locally so that decision stays reversible without
# rebuilding the stack — see docs/infrastructure-runbook.md.
#
# Pinned to 17 to match production. Debian-based rather than -alpine because
# postgresql-17-cron is packaged for Debian; that is the only reason the
# development database is no longer the alpine image.
FROM postgres:17

RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-17-cron \
 && rm -rf /var/lib/apt/lists/*
