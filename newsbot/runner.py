"""Scheduler loop: fetch → archive → route → post.

Transaction boundaries: one transaction per source fetch cycle (fetch
row, items, phase-A routing decisions, source state) so a crash retries the
whole cycle idempotently; one transaction per posted item (delivery + routing
decision) so a Telegram send never sits inside a long transaction. The
channel's posting window is claimed up front by a single conditional upsert,
which is what makes overlapping job executions safe.
"""

from __future__ import annotations

import asyncio
import signal
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx
import psycopg
import structlog

from archive import dedupe, writer
from archive import errors as errdb
from archive.db import transaction
from archive.models import Decision, DeliveryStatus, SourceState
from feedspec.loader import LoadedSpec
from feedspec.model import SourceDef, parse_cold_start
from feedspec.resolve import (
    EffectiveConfig,
    resolve_all,
    resolve_channel,
    resolve_source,
)
from fetcher.http import ConditionalState, FetchError, fetch_feed
from fetcher.parse import ParseError, RawItem, parse_feed
from newsbot.alerts import AlertEngine
from newsbot.formatter import format_post
from newsbot.router import Candidate, decide_item, select_for_channel
from newsbot.telegram import TelegramClient
from newsbot.translate import TranslationService

log = structlog.get_logger(__name__)

POST_PAUSE_S = 1.0  # spacing between consecutive sends in one burst
MIN_SLEEP_S = 5.0

# One fetch/post cycle at a time across all processes. Cron-triggered job
# executions can overlap when one runs long; the loser exits without work.
# The name is part of the deployment interface: two processes coordinate only
# when configured with the same lock name.
FETCH_LOCK_NAME = "newsbot:fetch"
BACKOFF_CAP_MIN = 60


@dataclass
class Deps:
    """Everything a run needs, bundled so tests can inject fakes."""

    loaded: LoadedSpec
    conn: psycopg.Connection
    http: httpx.AsyncClient
    telegram: TelegramClient
    translator: TranslationService
    base_dir: str = "."
    alerts: AlertEngine | None = None
    healthcheck_url: str = ""
    # filled by run_once from the spec: (source_name, channel_name) → EffectiveConfig
    pairs: dict[tuple[str, str], EffectiveConfig] = dataclass_field(default_factory=dict)

    async def report_error(self, **kwargs) -> None:
        """Best-effort error reporting; a missing engine is a silent no-op."""
        if self.alerts is not None:
            await self.alerts.report(**kwargs)


@dataclass
class RunStats:
    """Counters for one fetch/post pass."""

    fetched_sources: int = 0
    new_items: int = 0
    posted: int = 0
    errors: list[str] = dataclass_field(default_factory=list)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _pair_index(loaded: LoadedSpec) -> dict[tuple[str, str], EffectiveConfig]:
    return {(p.source_name, p.channel_name): p for p in resolve_all(loaded.spec)}


def _failure_backoff(interval_min: int, failures: int) -> int:
    """Minutes until the next attempt after the n-th consecutive failure.
    The first failure keeps the normal cadence (a dead feed never hot-loops,
    but also isn't punished for one blip); each further failure doubles it,
    capped so a recovered feed is noticed within the hour."""
    cap = max(BACKOFF_CAP_MIN, interval_min)
    return min(interval_min * 2 ** max(failures - 1, 0), cap)


async def process_source(deps: Deps, source: SourceDef, stats: RunStats) -> None:
    """Fetch, parse, archive and route one source. Every exit path persists
    a fetch row and updated source state in one transaction; fetch/parse
    failures back off exponentially and report an error event, success
    resets the failure streak and resolves any open fetch events."""
    spec = deps.loaded.spec
    effective = resolve_source(spec, source)
    conn = deps.conn
    now = _now()
    state = writer.load_source_state(conn, source.name, now)
    next_fetch = now + timedelta(minutes=effective.fetch_interval_min)
    slog = log.bind(source=source.name)

    try:
        result = await fetch_feed(
            deps.http,
            effective.url,
            ConditionalState(etag=state.etag, last_modified=state.last_modified),
        )
    except FetchError as e:
        stats.errors.append(f"{source.name}: {e}")
        slog.warning("fetch_failed", error=str(e))
        failures = state.consecutive_failures + 1
        with transaction(conn):
            writer.record_fetch(
                conn, source_name=source.name, started_utc=now, finished_utc=_now(),
                http_status=e.status, etag=state.etag, last_modified=state.last_modified,
                item_count=0, new_item_count=0, possible_gap=False,
                spec_hash=deps.loaded.spec_hash, error=str(e),
            )
            writer.save_source_state(
                conn,
                state.model_copy(
                    update={
                        "next_fetch_utc": now + timedelta(
                            minutes=_failure_backoff(effective.fetch_interval_min, failures)
                        ),
                        "consecutive_failures": failures,
                    }
                ),
            )
        await deps.report_error(
            component="fetcher", error_class=e.error_class, message=str(e),
            source_name=source.name, detail={"status": e.status},
        )
        return

    stats.fetched_sources += 1

    if result.not_modified:
        with transaction(conn):
            writer.record_fetch(
                conn, source_name=source.name, started_utc=now, finished_utc=result.fetched_at,
                http_status=304, etag=state.etag, last_modified=state.last_modified,
                item_count=0, new_item_count=0, possible_gap=False,
                spec_hash=deps.loaded.spec_hash,
            )
            writer.save_source_state(
                conn,
                state.model_copy(
                    update={
                        "next_fetch_utc": next_fetch,
                        "last_fetch_utc": now,
                        "consecutive_failures": 0,
                    }
                ),
            )
        if state.consecutive_failures and deps.alerts is not None:
            await deps.alerts.resolve_source_events(source.name)
        return

    problems: list[tuple[str, str, dict]] = []
    try:
        items = parse_feed(
            result.body, effective, base_dir=deps.base_dir,
            on_problem=lambda cls, msg, detail: problems.append((cls, msg, detail)),
        )
    except ParseError as e:
        stats.errors.append(str(e))
        slog.warning("parse_failed", error=str(e))
        failures = state.consecutive_failures + 1
        with transaction(conn):
            writer.record_fetch(
                conn, source_name=source.name, started_utc=now, finished_utc=result.fetched_at,
                http_status=result.status_code, etag=result.etag,
                last_modified=result.last_modified, item_count=0, new_item_count=0,
                possible_gap=False, spec_hash=deps.loaded.spec_hash, error=str(e),
            )
            writer.save_source_state(
                conn,
                state.model_copy(
                    update={
                        "next_fetch_utc": now + timedelta(
                            minutes=_failure_backoff(effective.fetch_interval_min, failures)
                        ),
                        "consecutive_failures": failures,
                    }
                ),
            )
        await deps.report_error(
            component="fetcher", error_class=e.error_class, message=str(e),
            source_name=source.name,
        )
        return

    cold_start = not state.cold_start_done
    eligible_ids = _cold_start_eligible(items, effective.cold_start_policy) if cold_start else None

    with transaction(conn):
        fetch_id = writer.record_fetch(
            conn, source_name=source.name, started_utc=now, finished_utc=result.fetched_at,
            http_status=result.status_code, etag=result.etag,
            last_modified=result.last_modified, item_count=len(items),
            new_item_count=0, possible_gap=False, spec_hash=deps.loaded.spec_hash,
        )
        new_count = 0
        for item in items:
            new_count += _store_and_decide(
                deps, fetch_id, item, source, effective.kind, now,
                cold_start_eligible=eligible_ids is None or item.source_item_id in eligible_ids,
            )
        possible_gap = (
            state.last_fetch_utc is not None
            and len(items) >= effective.fetch_limit
            and new_count == len(items)
        )
        conn.execute(
            "UPDATE fetches SET new_item_count = %s, possible_gap = %s WHERE fetch_id = %s",
            (new_count, possible_gap, fetch_id),
        )
        writer.save_source_state(
            conn,
            SourceState(
                source_name=source.name,
                next_fetch_utc=next_fetch,
                last_fetch_utc=now,
                etag=result.etag,
                last_modified=result.last_modified,
                cold_start_done=True,
            ),
        )
    stats.new_items += new_count

    # Fetch succeeded — the streak resets (SourceState above starts at
    # 0) and any open fetch events resolve, editing their alerts in place.
    if state.consecutive_failures and deps.alerts is not None:
        await deps.alerts.resolve_source_events(source.name)

    for error_class, message, detail in problems:
        await deps.report_error(
            component="fetcher", error_class=error_class, message=message,
            source_name=source.name, detail=detail,
        )
    if possible_gap:
        slog.warning("possible_gap", fetched=len(items), all_new=True)
        await deps.report_error(
            component="fetcher", error_class="PossibleGap",
            message=f"{source.name}: {len(items)} items fetched, all new — window likely overflowed",
            source_name=source.name,
        )
    if cold_start:
        await deps.report_error(
            component="newsbot", error_class="ColdStartSkipped",
            message=f"{source.name}: first fetch, {new_count} items stored under "
                    f"{effective.cold_start_policy}",
            source_name=source.name,
        )
    slog.info("fetched", items=len(items), new=new_count, cold_start=cold_start)


def _cold_start_eligible(items: list[RawItem], policy: str) -> set[str]:
    """Item ids that stay postable on a source's very first fetch."""
    kind, n = parse_cold_start(policy)
    if kind == "skip_all" or n <= 0:
        return set()
    newest = sorted(
        items,
        key=lambda i: i.published_utc or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )[:n]
    return {i.source_item_id for i in newest}


def _store_and_decide(
    deps: Deps,
    fetch_id: UUID,
    item: RawItem,
    source: SourceDef,
    source_kind: str,
    now: datetime,
    *,
    cold_start_eligible: bool,
) -> int:
    """Store one item and record a decision for every bound channel.
    Returns 1 when the item is new, 0 when it was already archived."""
    conn = deps.conn
    spec = deps.loaded.spec
    canon = dedupe.canonical_url(item.url)
    already = conn.execute(
        "SELECT item_id FROM items WHERE source_name = %s AND source_item_id = %s",
        (item.source_name, item.source_item_id),
    ).fetchone()
    if already:
        return 0

    duplicate_of = dedupe.find_duplicate(
        conn,
        dedupe.content_hash(canon, item.title),
        dedupe.simhash64(item.title),
        now,
    )
    item_id, is_new = writer.store_item(conn, fetch_id, item, deps.loaded.spec_hash, duplicate_of)
    if not is_new:
        return 0

    # No bindings is a supported shape: an archive-only source stores its
    # items and deliberately produces zero routing_decisions rows.
    for binding in source.channels:
        cfg = deps.pairs[(source.name, binding.name)]
        channel = resolve_channel(spec, binding.name)
        route = decide_item(
            cfg,
            title=item.title,
            lead=item.lead,
            categories=item.categories,
            published_utc=item.published_utc,
            source_kind=source_kind,
            now=now,
            is_duplicate=duplicate_of is not None,
            duplicate_of=duplicate_of,
            cold_start_eligible=cold_start_eligible,
            max_age_min=channel.max_age_min,
        )
        writer.record_routing_decision(
            conn, item_id, binding.name, route.decision,
            reason=route.reason, matched_clause=route.matched_clause,
        )
    return 1


async def post_phase(deps: Deps, stats: RunStats) -> None:
    """Post per channel from the routed backlog: flip aged-out items to
    too_old, claim the posting window, select under the per-run cap, send,
    and record each delivery in its own transaction."""
    spec = deps.loaded.spec
    conn = deps.conn
    source_order = {s.name: i for i, s in enumerate(spec.sources)}

    for channel_def in spec.channels:
        channel = resolve_channel(spec, channel_def.name)
        if not channel.enabled:
            continue
        now = _now()
        backlog = writer.get_postable_backlog(conn, channel.name)

        # max_age applies always — items that aged out in the backlog are
        # flipped to too_old so the archive says why they never posted.
        fresh = []
        for record in backlog:
            if record.published_utc and channel.max_age_min is not None and (
                now - record.published_utc > timedelta(minutes=channel.max_age_min)
            ):
                writer.update_routing_decision(
                    conn, record.item_id, channel.name, Decision.TOO_OLD,
                    reason=f"aged out in backlog, max_age_min={channel.max_age_min}",
                )
            else:
                fresh.append(record)

        if not fresh:
            continue
        if not writer.try_acquire_post_slot(conn, channel.name, channel.post_interval_min):
            # Window closed, or a concurrent execution claimed it first. The
            # gated: prefix keeps these in the backlog for the next window,
            # unlike drop_oldest's terminal rate_limited.
            for record in fresh:
                writer.update_routing_decision(
                    conn, record.item_id, channel.name, Decision.RATE_LIMITED,
                    reason=f"{writer.GATED_REASON_PREFIX} "
                           f"post_interval_min={channel.post_interval_min}",
                )
            continue
        selection = select_for_channel(
            [
                Candidate(r.item_id, r.source_name, r.published_utc, r.first_seen_utc)
                for r in fresh
            ],
            channel,
            last_post_utc=None,  # the slot upsert above already decided the gate
            now=now,
            source_order=source_order,
        )
        for candidate in selection.dropped:
            writer.update_routing_decision(
                conn, candidate.item_id, channel.name, Decision.RATE_LIMITED,
                reason=f"over max_posts_per_run={channel.max_posts_per_run} (drop_oldest)",
            )

        records = {r.item_id: r for r in fresh}
        for candidate in selection.to_post:
            record = records[candidate.item_id]
            cfg = deps.pairs.get((record.source_name, channel.name))
            if cfg is None:
                # The backlog is keyed by channel alone, so decisions outlive
                # the spec that made them: a source turned archive-only, or
                # removed, leaves routed rows with no binding left to post
                # them under. Retire them terminally (no gated: prefix) so
                # they leave the backlog instead of being rescanned forever.
                writer.update_routing_decision(
                    conn, record.item_id, channel.name, Decision.CHANNEL_DISABLED,
                    reason="binding removed from spec",
                )
                continue
            effective_source = resolve_source(
                spec, next(s for s in spec.sources if s.name == record.source_name)
            )
            post = format_post(record, cfg, deps.translator, effective_source)
            for translation_error in deps.translator.pending_errors:
                # degradation, not failure: the post proceeded untranslated
                await deps.report_error(
                    component="translator", error_class="TranslationProviderError",
                    message=translation_error, channel_name=channel.name,
                    item_id=record.item_id,
                )
            deps.translator.pending_errors.clear()
            result = await deps.telegram.send(post)
            status = (
                DeliveryStatus.SKIPPED if result.dry_run
                else DeliveryStatus.SENT if result.ok
                else DeliveryStatus.FAILED
            )
            with transaction(conn):
                writer.record_delivery(
                    conn,
                    item_id=record.item_id,
                    channel_name=channel.name,
                    chat_id=channel.chat_id,
                    status=status,
                    telegram_message_id=result.message_id,
                    posted_utc=_now() if result.ok else None,
                    error="dry_run" if result.dry_run else result.error,
                )
                if result.ok:
                    writer.mark_posted_after_gate(conn, record.item_id, channel.name)
            if result.ok:
                stats.posted += 1
            else:
                stats.errors.append(
                    f"{channel.name}/{record.source_item_id}: {result.error}"
                )
                error_text = result.error or ""
                if error_text.startswith("403"):
                    error_class = "TelegramForbidden"
                elif error_text.startswith("429"):
                    error_class = "TelegramRateLimited"
                elif error_text.startswith("400"):
                    error_class = "TelegramBadRequest"
                else:
                    error_class = "TelegramSendFailed"  # unknown class defaults to error
                await deps.report_error(
                    component="telegram", error_class=error_class,
                    message=f"{channel.name}: {error_text}",
                    channel_name=channel.name, item_id=record.item_id,
                )
            await asyncio.sleep(POST_PAUSE_S if not deps.telegram.dry_run else 0)


async def run_once(deps: Deps) -> RunStats:
    """One pass: fetch every due source, then post per channel."""
    stats = RunStats()
    conn = deps.conn
    writer.ensure_spec_version(conn, deps.loaded.spec_hash, deps.loaded.raw)
    deps.pairs = _pair_index(deps.loaded)

    now = _now()
    for source in deps.loaded.spec.sources:
        effective = resolve_source(deps.loaded.spec, source)
        if not effective.enabled:
            continue
        state = writer.load_source_state(conn, source.name, now)
        if state.next_fetch_utc <= now:
            await process_source(deps, source, stats)

    await post_phase(deps, stats)
    log.info(
        "run_complete",
        sources=stats.fetched_sources,
        new_items=stats.new_items,
        posted=stats.posted,
        errors=len(stats.errors),
    )
    return stats


async def _deadman_ping(deps: Deps) -> None:
    """The only thing that catches total failure — an external
    monitor that alerts when these pings stop."""
    if not deps.healthcheck_url:
        return
    try:
        await deps.http.get(deps.healthcheck_url, timeout=10)
    except Exception as e:
        log.warning("deadman_ping_failed", error=str(e))


@dataclass
class ExecutionResult:
    """Outcome of run_execution; acquired is False when another execution
    held the lock and this one exited without work."""

    acquired: bool
    stats: RunStats | None = None


async def run_execution(deps: Deps) -> ExecutionResult:
    """One serverless job execution: the full run_forever iteration —
    outbox flush, fetch/post pass, digest, retention, seen-update purge —
    under the cross-process advisory lock.

    The deadman ping fires only after a completed pass: a lock-skipped or
    crashing execution must look silent to the external monitor."""
    row = deps.conn.execute(
        "SELECT pg_try_advisory_lock(hashtext(%s)) AS ok", (FETCH_LOCK_NAME,)
    ).fetchone()
    if not row["ok"]:
        log.warning("fetch_lock_held_elsewhere_skipping")
        return ExecutionResult(acquired=False)
    try:
        if deps.alerts is not None:
            # Alerts queued while unreachable flush before anything else
            await deps.alerts.flush_outbox()
        stats = await run_once(deps)
        if deps.alerts is not None:
            await deps.alerts.maybe_digest()
            deps.alerts.maybe_retention()
        errdb.purge_seen_updates(deps.conn)
        await _deadman_ping(deps)
        return ExecutionResult(acquired=True, stats=stats)
    finally:
        try:
            deps.conn.execute(
                "SELECT pg_advisory_unlock(hashtext(%s))", (FETCH_LOCK_NAME,)
            )
        except psycopg.Error:
            pass  # session close releases it anyway


async def run_forever(deps: Deps) -> None:
    """Long-running mode: run_once in a loop, sleeping until the earliest
    next_fetch_utc; SIGINT/SIGTERM stop cleanly after the current pass."""
    if deps.telegram.dry_run:
        log.warning("DRY_RUN is enabled — nothing will be sent to Telegram")

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    if deps.alerts is not None:
        # Alerts queued while unreachable flush before anything else
        await deps.alerts.flush_outbox()

    while not stop.is_set():
        await run_once(deps)
        if deps.alerts is not None:
            await deps.alerts.maybe_digest()
            deps.alerts.maybe_retention()
        await _deadman_ping(deps)
        rows = deps.conn.execute(
            "SELECT min(next_fetch_utc) AS next FROM source_state"
        ).fetchone()
        now = _now()
        delay = MIN_SLEEP_S
        if rows and rows["next"]:
            delay = max((rows["next"] - now).total_seconds(), MIN_SLEEP_S)
        try:
            await asyncio.wait_for(stop.wait(), timeout=delay)
        except TimeoutError:
            continue
    log.info("shutdown_clean")
