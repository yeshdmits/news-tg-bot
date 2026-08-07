"""Command-line entry point: python -m cli <command>.

Commands:
    validate    load + validate spec, print resolved bindings, exit
    run         scheduler loop; --once for one cron job execution
    serve       Telegram webhook app (POST /telegram/webhook, GET /healthz)
    register-webhook  setWebhook + ops command menu; run from the migration job
    fetch       fetch one source, print what would post (--dry-run)
    stats       archive statistics
    export      export archive rows to parquet/csv
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

import structlog
from pydantic import BaseModel, ConfigDict

log = structlog.get_logger(__name__)


class SettingsError(ValueError):
    """One or more environment variables hold values that cannot be parsed.
    ``problems`` lists every one of them, not just the first."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(problems))


class Settings(BaseModel):
    """Process configuration, read once from the environment; immutable.

    Missing variables are not an error here — ``missing_env`` checks the
    per-command requirements in one place, so a bare start names every
    missing variable at once instead of failing on the first."""

    model_config = ConfigDict(frozen=True)

    spec_json: str = ""  # SPEC_JSON: full spec inline; wins over url/path
    spec_url: str = ""  # SPEC_URL: https:// fetch at startup
    spec_path: str = ""  # SPEC_PATH: local file, lowest precedence
    spec_base_dir: str = ""  # SPEC_BASE_DIR: XSD resolution for inline/url specs
    database_url: str = ""
    telegram_bot_token: str = ""
    dry_run: bool = True
    log_level: str = "info"
    translate_provider: str = "none"  # none | deepl
    translate_api_key: str = ""
    healthcheck_url: str = ""  # deadman-switch ping target, optional
    webhook_secret: str = ""  # X-Telegram-Bot-Api-Secret-Token; empty rejects all
    webhook_url: str = ""  # public endpoint passed to setWebhook
    port: int = 8000

    @classmethod
    def from_env(cls) -> "Settings":
        """Build Settings from environment variables, defaulting anything
        unset. Raises SettingsError naming every unparsable value."""
        def flag(name: str, default: str) -> bool:
            return os.environ.get(name, default).strip().lower() in ("1", "true", "yes")

        problems: list[str] = []
        port_raw = os.environ.get("PORT", "8000")
        try:
            port = int(port_raw)
        except ValueError:
            problems.append(f"PORT: not an integer: {port_raw!r}")
            port = 8000
        if problems:
            raise SettingsError(problems)

        return cls(
            spec_json=os.environ.get("SPEC_JSON", ""),
            spec_url=os.environ.get("SPEC_URL", ""),
            spec_path=os.environ.get("SPEC_PATH", ""),
            spec_base_dir=os.environ.get("SPEC_BASE_DIR", ""),
            database_url=os.environ.get("DATABASE_URL", ""),
            telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            dry_run=flag("DRY_RUN", "true"),
            log_level=os.environ.get("LOG_LEVEL", "info"),
            translate_provider=os.environ.get("TRANSLATE_PROVIDER", "none"),
            translate_api_key=os.environ.get("TRANSLATE_API_KEY", ""),
            healthcheck_url=os.environ.get("HEALTHCHECK_URL", ""),
            webhook_secret=os.environ.get("TELEGRAM_WEBHOOK_SECRET", ""),
            webhook_url=os.environ.get("WEBHOOK_URL", ""),
            port=port,
        )


# A command's hard requirements, validated in one place (main) so a bare
# start prints every missing variable at once. The _SPEC sentinel expands
# to the three spec-source options; TELEGRAM_BOT_TOKEN is deliberately not
# required for `run` (empty token disables sends, useful with DRY_RUN).
_SPEC = "spec"

REQUIRED_ENV: dict[str, tuple[str, ...]] = {
    "validate": (_SPEC,),
    "run": ("DATABASE_URL", _SPEC),
    "serve": ("DATABASE_URL", "TELEGRAM_WEBHOOK_SECRET", _SPEC),
    "register-webhook": (
        "TELEGRAM_BOT_TOKEN",
        "WEBHOOK_URL",
        "TELEGRAM_WEBHOOK_SECRET",
        _SPEC,
    ),
    "fetch": (_SPEC,),
    "stats": ("DATABASE_URL",),
    "export": ("DATABASE_URL",),
    "schema": (),
}

_ENV_OF_FIELD = {
    "DATABASE_URL": "database_url",
    "TELEGRAM_BOT_TOKEN": "telegram_bot_token",
    "TELEGRAM_WEBHOOK_SECRET": "webhook_secret",
    "WEBHOOK_URL": "webhook_url",
}


def missing_env(command: str, settings: Settings, args: argparse.Namespace) -> list[str]:
    """Every unmet environment requirement for ``command``, as printable
    lines. Empty list means the command may start."""
    problems: list[str] = []
    for requirement in REQUIRED_ENV.get(command, ()):
        if requirement == _SPEC:
            if getattr(args, "spec", None):
                continue  # explicit --spec waives the env requirement
            if not (settings.spec_json or settings.spec_url or settings.spec_path):
                from specsource import NO_SOURCE_MESSAGE

                problems.append(NO_SOURCE_MESSAGE)
        elif not getattr(settings, _ENV_OF_FIELD[requirement]):
            problems.append(f"{requirement} is not set")
    return problems


def configure_logging(level: str) -> None:
    """Configure structlog: console renderer on a TTY, JSON otherwise."""
    numeric = getattr(logging, level.upper(), logging.INFO)
    renderer = (
        structlog.dev.ConsoleRenderer()
        if sys.stderr.isatty()
        else structlog.processors.JSONRenderer()
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
    )


def cmd_validate(settings: Settings, args: argparse.Namespace) -> int:
    """Load + validate the spec and print every resolved source→channel
    binding. Returns 1 when the spec fails to load, 0 when it is valid."""
    from feedspec.loader import SpecError, load_spec_bytes
    from feedspec.resolve import resolve_all, resolve_source
    from feedspec.schema import iter_schema_errors
    from specsource import SpecSourceError, acquire_spec

    try:
        source, raw_bytes = acquire_spec(settings, override_path=args.spec)
    except SpecSourceError as e:
        print(f"INVALID: {e}", file=sys.stderr)
        return 1

    # Schema pass first: reports every structural problem at once, the way
    # an editor pointed at spec.schema.json would. The pydantic load below
    # stays authoritative.
    schema_errors = iter_schema_errors(raw_bytes)
    for error in schema_errors:
        print(f"  SCHEMA: {error}", file=sys.stderr)

    try:
        loaded = load_spec_bytes(raw_bytes)
    except SpecError as e:
        print(f"INVALID: {e}", file=sys.stderr)
        return 1

    spec = loaded.spec
    print(
        f"spec from {source.kind} {source.location} — "
        f"version {spec.version}, hash {loaded.spec_hash[:12]}…"
    )
    for warning in loaded.warnings:
        print(f"  WARNING: {warning}")

    pairs = resolve_all(spec)
    archive_only = 0
    reviewed = 0
    for source in spec.sources:
        eff_src = resolve_source(spec, source)
        state = "" if eff_src.enabled else "  [DISABLED]"
        print(
            f"\n{source.name} ({eff_src.kind}, {eff_src.language}, "
            f"every {eff_src.fetch_interval_min} min, limit {eff_src.fetch_limit}, "
            f"cold_start={eff_src.cold_start_policy}){state}"
        )
        if not source.channels:
            # Say it out loud: a source with no bindings would otherwise
            # print a header and nothing else, which reads as a bug.
            archive_only += 1
            print("    -> (archive only — stored, never posted)")
        for cfg in (p for p in pairs if p.source_name == source.name):
            flags = []
            if cfg.include_categories:
                flags.append(f"only {list(cfg.include_categories)}")
            if cfg.exclude_categories:
                flags.append(f"not {list(cfg.exclude_categories)}")
            if cfg.translate_lead:
                flags.append(f"{eff_src.language}->{cfg.channel_language}")
            if cfg.when is not None:
                flags.append("when=" + cfg.when.model_dump_json(exclude_defaults=True))
            if not cfg.channel_enabled:
                flags.append("CHANNEL DISABLED")
            print(
                f"    -> {cfg.channel_name} [{cfg.chat_id}] {cfg.post_style}, "
                f"lead<={cfg.lead_max_length}, max {cfg.max_posts_per_run}/run "
                f"every {cfg.post_interval_min} min, max_age={cfg.max_age_min} min, "
                f"{cfg.queue_policy}"
                + (", " + ", ".join(flags) if flags else "")
            )
        if eff_src.feedback is not None:
            state = "" if eff_src.feedback.enabled else " (disabled)"
            reviewed += eff_src.feedback.enabled
            print(f"    ~> review in [{eff_src.feedback.chat_id}]{state}")
    archive_note = f" ({archive_only} archive-only)" if archive_only else ""
    review_note = f", {reviewed} reviewed" if reviewed else ""
    print(
        f"\nOK: {len(spec.sources)} sources{archive_note}, "
        f"{len(spec.channels)} channels, {len(pairs)} bindings{review_note}"
    )
    return 0


def cmd_run(settings: Settings, args: argparse.Namespace) -> int:
    """Scheduler entry point: wire up the dependencies, then run_forever —
    or a single run_execution with --once. Exits non-zero only on an invalid
    spec or an unreachable database; a failing feed is normal operation."""
    import asyncio

    import psycopg

    from archive.db import connect
    from feedspec.loader import SpecError, load_spec_bytes
    from fetcher.http import make_client
    from newsbot.alerts import AlertEngine
    from newsbot.runner import Deps, run_execution, run_forever
    from newsbot.telegram import TelegramClient
    from newsbot.translate import TranslationService, make_provider
    from specsource import SpecSourceError, acquire_spec, spec_base_dir

    if not settings.database_url:
        raise SystemExit("DATABASE_URL is not set")
    try:
        source, raw_bytes = acquire_spec(settings)
    except SpecSourceError as e:
        # No bytes were acquired, so no ops chat is known — nothing to
        # alert into. The deadman HEALTHCHECK_URL covers a silent bot.
        raise SystemExit(str(e))
    try:
        loaded = load_spec_bytes(raw_bytes)
    except SpecError as e:
        # SpecValidationFailed: alert best-effort, then exit non-zero.
        _emergency_spec_alert(settings, raw_bytes, str(e))
        raise SystemExit(f"INVALID SPEC: {e}")
    try:
        conn = connect(settings.database_url)
    except psycopg.OperationalError as e:
        # Database unreachable is one of the two failures that may stop the
        # cron schedule (the other is an invalid spec).
        raise SystemExit(f"DATABASE UNREACHABLE: {e}")
    provider = make_provider(settings.translate_provider, settings.translate_api_key)

    async def main() -> int:
        async with make_client() as http:
            telegram = TelegramClient(
                settings.telegram_bot_token, http, dry_run=settings.dry_run
            )
            engine = AlertEngine(
                conn, telegram, loaded.spec.errors, spec_hash=loaded.spec_hash
            )
            deps = Deps(
                loaded=loaded,
                conn=conn,
                http=http,
                telegram=telegram,
                translator=TranslationService(conn, provider),
                base_dir=spec_base_dir(settings, source),
                alerts=engine,
                healthcheck_url=settings.healthcheck_url,
            )
            if args.once:
                # Exit non-zero only on failures that should stop the
                # schedule (DB unreachable; invalid spec exits above).
                # A failing feed is normal operation: it raised an error
                # event and the execution carried on.
                try:
                    await run_execution(deps)
                except psycopg.OperationalError as e:
                    log.error("database_lost_mid_run", error=str(e))
                    return 1
                except Exception:
                    log.exception("run_execution_failed")
                return 0
            await run_forever(deps)
            return 0

    try:
        return asyncio.run(main())
    finally:
        conn.close()


def _emergency_spec_alert(settings: Settings, raw_bytes: bytes, error: str) -> None:
    """The spec failed validation, so the alert config is unavailable —
    pull ops_chat_id straight from the exact bytes that failed and send one
    plain message."""
    import json as jsonlib

    import httpx

    if not settings.telegram_bot_token:
        return
    try:
        raw = jsonlib.loads(raw_bytes)
        ops_chat_id = raw.get("errors", {}).get("ops_chat_id")
    except Exception:
        return
    if not ops_chat_id:
        return
    try:
        httpx.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json={
                "chat_id": ops_chat_id,
                "text": "🔴 CRITICAL\nSpecValidationFailed — bot refusing to start\n\n"
                + error[:3500],
            },
            timeout=10,
        )
    except Exception:
        pass


def cmd_serve(settings: Settings, args: argparse.Namespace) -> int:
    """Webhook app for the serverless bot unit."""
    import uvicorn

    from newsbot.webhook import create_app

    uvicorn.run(create_app(settings), host="0.0.0.0", port=settings.port)
    return 0


def cmd_register_webhook(settings: Settings, args: argparse.Namespace) -> int:
    """Point Telegram at the webhook app. Runs from the migration job — never
    on container start, or every cold start would call setWebhook and get
    rate-limited."""
    import asyncio

    from feedspec.loader import SpecError
    from fetcher.http import make_client
    from newsbot.ops import register_commands
    from newsbot.telegram import TelegramAPIError, TelegramClient, TelegramTransportError
    from specsource import SpecSourceError, load_spec_for

    try:
        loaded = load_spec_for(settings)
    except SpecSourceError as e:
        raise SystemExit(str(e))
    except SpecError as e:
        raise SystemExit(f"INVALID SPEC: {e}")
    if loaded.spec.errors is None:
        raise SystemExit("spec has no errors config — no ops chat to register commands for")
    ops_chat_id = str(loaded.spec.errors.ops_chat_id)
    if settings.database_url:
        # The chat may have migrated to a supergroup since the spec was
        # written; bot_state holds the healed id (same rule as AlertEngine).
        try:
            from archive import errors as errdb
            from archive.db import connect

            conn = connect(settings.database_url)
            try:
                ops_chat_id = errdb.get_state(conn, "ops_chat_id") or ops_chat_id
            finally:
                conn.close()
        except Exception as e:
            log.warning("ops_chat_override_unavailable", error=str(e))

    async def main() -> int:
        async with make_client() as http:
            telegram = TelegramClient(settings.telegram_bot_token, http, dry_run=False)
            try:
                await telegram.api(
                    "setWebhook",
                    {
                        "url": settings.webhook_url,
                        "secret_token": settings.webhook_secret,
                        # Only what the bot handles — anything more wakes the
                        # scale-to-zero container for nothing. callback_query
                        # carries the review buttons; without it Telegram
                        # silently never delivers a press.
                        "allowed_updates": ["message", "callback_query"],
                    },
                )
                await register_commands(telegram, ops_chat_id)
            except (TelegramAPIError, TelegramTransportError) as e:
                # Non-zero so the CI migrate gate fails the deploy.
                print(f"webhook registration failed: {e}", file=sys.stderr)
                return 1
        print(f"webhook registered: {settings.webhook_url}")
        return 0

    return asyncio.run(main())


def cmd_fetch(settings: Settings, args: argparse.Namespace) -> int:
    """Fetch and parse one source without touching the database, printing
    each item and the routing decision every bound channel would make."""
    import asyncio

    from feedspec.loader import load_spec_bytes
    from feedspec.resolve import resolve_all, resolve_channel, resolve_source
    from fetcher.http import fetch_feed, make_client
    from fetcher.parse import parse_feed
    from newsbot.router import decide_item
    from specsource import acquire_spec, spec_base_dir

    spec_source, raw_bytes = acquire_spec(settings, override_path=args.spec)
    loaded = load_spec_bytes(raw_bytes)
    spec = loaded.spec
    source = next((s for s in spec.sources if s.name == args.source), None)
    if source is None:
        raise SystemExit(
            f"unknown source {args.source!r}; known: {[s.name for s in spec.sources]}"
        )
    effective = resolve_source(spec, source)
    base_dir = spec_base_dir(settings, spec_source)

    async def main() -> int:
        from datetime import datetime, timezone

        async with make_client() as http:
            result = await fetch_feed(http, effective.url)
        items = parse_feed(result.body, effective, base_dir=base_dir)
        now = datetime.now(timezone.utc)
        pairs = [p for p in resolve_all(spec) if p.source_name == source.name]
        print(f"{source.name}: {len(items)} items (fetch_limit {effective.fetch_limit})")
        if not pairs:
            print("archive only — no channel bindings; items would be stored, not posted")
        print()
        for item in items:
            print(f"[{item.published_utc:%Y-%m-%d %H:%M}] {item.title}")
            print(f"    id={item.source_item_id} categories={list(item.categories)}")
            if item.lead and item.lead != item.title:
                print(f"    lead: {item.lead[:120]}")
            for cfg in pairs:
                channel = resolve_channel(spec, cfg.channel_name)
                route = decide_item(
                    cfg,
                    title=item.title,
                    lead=item.lead,
                    categories=item.categories,
                    published_utc=item.published_utc,
                    source_kind=effective.kind,
                    now=now,
                    is_duplicate=False,
                    max_age_min=channel.max_age_min,
                )
                print(f"    -> {cfg.channel_name}: {route.decision.value}"
                      + (f" ({route.reason})" if route.reason else ""))
            print()
        return 0

    return asyncio.run(main())


def _db_conn(settings: Settings):
    from archive.db import connect

    if not settings.database_url:
        raise SystemExit("DATABASE_URL is not set")
    return connect(settings.database_url)


def cmd_stats(settings: Settings, args: argparse.Namespace) -> int:
    """Print archive statistics: per-source item counts, routing-decision
    histogram, deliveries and the postable backlog."""
    from archive import feedback as feedbackdb
    from archive.stats import backlog_sizes, channel_stats, delivery_stats, source_stats

    conn = _db_conn(settings)
    try:
        print("== sources ==")
        for row in source_stats(conn):
            last = row["last_fetch_utc"]
            last_str = f"{last:%Y-%m-%d %H:%M}Z" if last else "never"
            print(
                f"  {row['source_name']:<16} items={row['items']:<5} "
                f"dups={row['duplicates']:<4} per_day={row['items_per_day'] or 0:<6} "
                f"gaps={row['gaps'] or 0:<3} fetch_errors={row['fetch_errors'] or 0:<3} "
                f"last_fetch={last_str}"
            )

        print("\n== routing decisions ==")
        for row in channel_stats(conn):
            print(f"  {row['channel_name']:<10} {row['decision']:<18} {row['n']}")

        print("\n== deliveries ==")
        rows = delivery_stats(conn)
        if not rows:
            print("  (none)")
        for row in rows:
            print(f"  {row['channel_name']:<10} {row['status']:<8} {row['n']}")

        print("\n== postable backlog ==")
        rows = backlog_sizes(conn)
        if not rows:
            print("  (empty)")
        for row in rows:
            print(f"  {row['channel_name']:<10} {row['n']}")

        print("\n== review queues ==")
        rows = feedbackdb.queue_stats(conn)
        if not rows:
            print("  (no source has a feedback chat)")
        for row in rows:
            labelled = row["approved"] + row["rejected"]
            rate = 100.0 * row["approved"] / labelled if labelled else 0.0
            print(
                f"  {row['chat_id']:<16} queued={row['queued']:<5} "
                f"showing={row['pending']:<2} labelled={labelled:<6} "
                f"({row['approved']} approved / {row['rejected']} rejected, "
                f"{rate:.0f}% approval)"
            )
        return 0
    finally:
        conn.close()


def _parse_utc(value: str) -> "datetime":
    from datetime import datetime, timezone

    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def cmd_export(settings: Settings, args: argparse.Namespace) -> int:
    """Export archive rows in [--from, --to) as CSV (stdout or file) or
    parquet (file only)."""
    from archive.stats import export_rows, write_csv, write_parquet

    from_utc, to_utc = _parse_utc(args.from_utc), _parse_utc(args.to_utc)
    conn = _db_conn(settings)
    try:
        rows = export_rows(conn, from_utc, to_utc)
        if args.format == "parquet":
            if args.out == "-":
                raise SystemExit("parquet needs --out <path>")
            count = write_parquet(rows, args.out)
        elif args.out == "-":
            count = write_csv(rows, sys.stdout)
        else:
            with open(args.out, "w", newline="") as handle:
                count = write_csv(rows, handle)
        print(f"exported {count} rows [{from_utc} .. {to_utc})", file=sys.stderr)
        return 0
    finally:
        conn.close()


def cmd_schema(settings: Settings, args: argparse.Namespace) -> int:
    """Print the JSON Schema for the spec format. Regenerate the committed
    copy with: python -m cli schema > spec.schema.json"""
    import json as jsonlib

    from feedspec.schema import spec_json_schema

    print(jsonlib.dumps(spec_json_schema(), indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    """Argument parser with one subcommand per COMMANDS entry."""
    parser = argparse.ArgumentParser(prog="cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_validate = sub.add_parser("validate", help="load + validate spec, print resolved bindings")
    p_validate.add_argument("--spec", default=None, help="spec path (default: SPEC_PATH env)")

    p_run = sub.add_parser("run", help="scheduler loop honouring per-source intervals")
    p_run.add_argument(
        "--once", action="store_true",
        help="one job execution (fetch, post, alerts, retention), then exit",
    )

    sub.add_parser("serve", help="webhook app: POST /telegram/webhook + GET /healthz")

    sub.add_parser(
        "register-webhook",
        help="setWebhook + command menu; run from the migration job",
    )

    p_fetch = sub.add_parser("fetch", help="fetch one source, print what would post")
    p_fetch.add_argument("source", help="source name from the spec")
    p_fetch.add_argument("--dry-run", action="store_true", default=True)
    p_fetch.add_argument("--spec", default=None, help="spec path (default: SPEC_PATH env)")

    sub.add_parser("stats", help="items/day/source, dup rate, gap count, decision histogram")

    sub.add_parser("schema", help="print the spec JSON Schema (spec.schema.json)")

    p_export = sub.add_parser("export", help="export archive rows")
    p_export.add_argument("--from", dest="from_utc", required=True)
    p_export.add_argument("--to", dest="to_utc", required=True)
    p_export.add_argument("--format", choices=("parquet", "csv"), default="csv")
    p_export.add_argument("--out", default="-", help="output path, '-' for stdout (csv only)")

    return parser


COMMANDS = {
    "validate": cmd_validate,
    "run": cmd_run,
    "serve": cmd_serve,
    "register-webhook": cmd_register_webhook,
    "fetch": cmd_fetch,
    "stats": cmd_stats,
    "export": cmd_export,
    "schema": cmd_schema,
}


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, validate the environment, configure logging,
    dispatch. Returns the exit code.

    Environment validation happens here, once, and reports every problem
    at the same time — a fresh deployment should learn about all its
    missing variables from a single failed start."""
    args = build_parser().parse_args(argv)
    try:
        settings = Settings.from_env()
    except SettingsError as e:
        for problem in e.problems:
            print(f"CONFIG: {problem}", file=sys.stderr)
        return 2
    problems = missing_env(args.command, settings, args)
    if problems:
        for problem in problems:
            print(f"CONFIG: {problem}", file=sys.stderr)
        return 2
    configure_logging(settings.log_level)
    return COMMANDS[args.command](settings, args)


if __name__ == "__main__":
    sys.exit(main())
