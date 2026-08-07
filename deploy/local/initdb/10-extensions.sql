-- Runs once, on an empty data directory, against POSTGRES_DB (= newsbot).
--
-- pgcrypto is what migration 0001 needs. pg_cron is created here only so the
-- local stack matches what the brief asks for; nothing in the codebase
-- schedules through it, because the production server does not allow-list it
-- (docs/infrastructure-runbook.md). cron.database_name is set to newsbot in
-- docker-compose.yml so pg_cron's own objects land in this database.
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_cron;
