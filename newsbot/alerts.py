"""Alert policy engine.

Persistence always comes first: every reported error lands in
``error_events`` before any Telegram traffic is attempted. Alerting is then
best-effort — failures queue to ``alert_outbox`` and flush on the next
successful contact.

Message ownership: each fingerprint owns one message in the ops chat
(edit, don't repost). A "notifying alert" is a policy event — it bumps
``alert_count``, arms the cooldown and consumes budget; physically it is the
initial send for a fresh fingerprint and an in-place edit afterwards
(Telegram edits are inherently silent — acknowledged trade-off; the
ladder counts are about alert accounting, not repeated buzzing).
"""

from __future__ import annotations

import html
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import psycopg
import structlog

from archive import errors as errdb
from feedspec.model import SEVERITY_RANK, ErrorsConfig
from feedspec.resolve import classify_error
from newsbot.telegram import TelegramAPIError, TelegramClient, TelegramTransportError

log = structlog.get_logger(__name__)

SEVERITY_EMOJI = {"critical": "🔴", "error": "🟠", "warning": "⚠️", "info": "ℹ️"}

# Counted, never alerted.
NEVER_ALERT = frozenset({"MappingOptionalFieldMissing", "ColdStartSkipped"})

STATE_OPS_CHAT = "ops_chat_id"
STATE_BUDGET = "alert_budget"      # "<window_start_iso>|<count>|<noticed 0/1>"
STATE_LAST_DIGEST = "last_digest_utc"
STATE_LAST_RETENTION = "last_retention_utc"


def _fmt_duration(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    hours, minutes = divmod(total // 60, 60)
    if hours >= 24:
        return f"{hours // 24}d{hours % 24}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    return f"{minutes}m"


class AlertEngine:
    """Persist-then-alert engine over one ops chat. ``active`` gates all
    Telegram traffic (persistence still happens when inactive); it defaults
    to on when the spec enables alerting and a bot token is present."""

    def __init__(
        self,
        conn: psycopg.Connection,
        telegram: TelegramClient,
        cfg: ErrorsConfig | None,
        spec_hash: str | None = None,
        active: bool | None = None,
    ):
        self.conn = conn
        self.telegram = telegram
        self.cfg = cfg
        self.spec_hash = spec_hash
        if active is None:
            active = bool(cfg and cfg.alerting.enabled and telegram.token)
        self.active = bool(cfg) and active

    # --- reporting ---------------------------------------------------------

    async def report(
        self,
        *,
        component: str,
        error_class: str,
        message: str,
        source_name: str | None = None,
        channel_name: str | None = None,
        item_id=None,
        detail: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any] | None:
        """Persist first, then alert best-effort per policy."""
        now = now or datetime.now(timezone.utc)
        rule = classify_error(self.cfg, error_class, source_name)
        try:
            row = errdb.record_occurrence(
                self.conn,
                component=component, error_class=error_class, severity=rule.severity,
                message=message, source_name=source_name, channel_name=channel_name,
                item_id=item_id, detail=detail, spec_hash=self.spec_hash, now=now,
            )
        except psycopg.Error:
            log.exception("error_persistence_failed", error_class=error_class)
            return None

        escalated = False
        if (
            rule.escalate_after is not None
            and rule.escalates_to is not None
            and row["consecutive_streak"] >= rule.escalate_after
            and SEVERITY_RANK[row["severity"]] < SEVERITY_RANK[rule.escalates_to]
        ):
            errdb.set_severity(self.conn, row["fingerprint"], rule.escalates_to)
            row["severity"] = rule.escalates_to
            escalated = True

        try:
            await self._apply_alert_policy(row, escalated, now)
        except Exception:
            log.exception("alerting_failed", short_id=row["short_id"])
        return row

    # --- policy ------------------------------------------------------------

    async def _apply_alert_policy(self, row: dict[str, Any], escalated: bool, now: datetime) -> None:
        if not self.active or row["error_class"] in NEVER_ALERT:
            return
        if row["muted_until_utc"] and row["muted_until_utc"] > now:
            return  # counting continued in record_occurrence; alerting stops

        ladder = self.cfg.alerting.escalation_ladder
        notifying = escalated or row["occurrence_count"] in ladder

        if notifying and not escalated and row["last_alert_utc"] is not None:
            cooldown = timedelta(minutes=self.cfg.alerting.cooldown_min)
            if now - row["last_alert_utc"] < cooldown:
                notifying = False  # cooldown suppresses; the edit below still lands

        if notifying and row["severity"] != "critical" and not await self._budget_permits(now):
            return
        if not notifying and row["alert_message_id"] is None:
            return  # nothing owned yet and no reason to create one

        text = self.render_alert(row, now)
        silent = self._is_silent(row["severity"], now)

        if row["alert_message_id"] is None:
            message_id = await self._send_ops(
                row["fingerprint"], text, silent=silent, thread_id=self._alert_thread()
            )
            if message_id is not None:
                errdb.record_alert(
                    self.conn, row["fingerprint"], chat_id=self.ops_chat_id(),
                    message_id=message_id, thread_id=self._alert_thread(), now=now,
                )
                await self._maybe_pin(row, message_id)
        else:
            edited = await self._edit_owned(row, text)
            if edited and notifying:
                errdb.record_alert(self.conn, row["fingerprint"], now=now)
                if escalated and row["severity"] == "critical":
                    await self._maybe_pin(row, row["alert_message_id"])

    def _is_silent(self, severity: str, now: datetime) -> bool:
        # Warning/info never buzz; quiet hours widen the silent range.
        if SEVERITY_RANK[severity] < SEVERITY_RANK[self.cfg.alerting.silent_below]:
            return True
        quiet = self.cfg.alerting.quiet_hours
        if quiet is not None and SEVERITY_RANK[severity] < SEVERITY_RANK[quiet.suppress_below]:
            local = now.astimezone(ZoneInfo(quiet.timezone)).time()
            start = dtime.fromisoformat(quiet.from_time)
            end = dtime.fromisoformat(quiet.to)
            in_window = start <= local or local < end if start > end else start <= local < end
            if in_window:
                return True
        return False

    async def _budget_permits(self, now: datetime) -> bool:
        raw = errdb.get_state(self.conn, STATE_BUDGET)
        start, count, noticed = now, 0, False
        if raw:
            start_s, count_s, noticed_s = raw.split("|")
            start = datetime.fromisoformat(start_s)
            count, noticed = int(count_s), noticed_s == "1"
        if now - start >= timedelta(hours=1):
            start, count, noticed = now, 0, False
        if count >= self.cfg.alerting.max_alerts_per_hour:
            if not noticed:
                errdb.set_state(self.conn, STATE_BUDGET, f"{start.isoformat()}|{count}|1")
                await self._send_ops(
                    "budget",
                    "⚠️ further alerts suppressed this hour, /unchecked to review",
                    silent=False, thread_id=self._alert_thread(),
                )
            return False
        errdb.set_state(
            self.conn, STATE_BUDGET, f"{start.isoformat()}|{count + 1}|{'1' if noticed else '0'}"
        )
        return True

    # --- rendering ---------------------------------------------------------

    def render_alert(self, row: dict[str, Any], now: datetime) -> str:
        """HTML body of the owned alert message for one event row."""
        sev = row["severity"]
        scope = " · ".join(
            p for p in (row.get("source_name"), row["error_class"]) if p
        ) or row["error_class"]
        occ = row["occurrence_count"]
        ord_suffix = {1: "st", 2: "nd", 3: "rd"}.get(occ if occ < 20 else occ % 10, "th")
        if row["state"] == "acked":
            state_line = f"acked by @{row['acked_by_username'] or row['acked_by_user_id']}"
        else:
            state_line = row["state"]
        lines = [
            f"{SEVERITY_EMOJI[sev]} {sev.upper()}  {row['short_id']}",
            html.escape(scope),
            html.escape((row["message"] or "")[:300]),
            "",
            f"{occ}{ord_suffix} occurrence · {row['last_seen_utc']:%H:%M} UTC · {state_line}",
            f'Reply "ack" or /ack {row["short_id"]}',
        ]
        return "\n".join(lines)

    def render_resolved(self, row: dict[str, Any], now: datetime) -> str:
        """HTML body that replaces an alert once its event has recovered."""
        span = _fmt_duration(row["last_seen_utc"] - row["first_seen_utc"])
        scope = " · ".join(p for p in (row.get("source_name"), row["error_class"]) if p)
        return (
            f"✅ RESOLVED  {row['short_id']}\n"
            f"{html.escape(scope)}\n"
            f"Failed {row['occurrence_count']}× over {span}. "
            f"Recovered {now:%H:%M} UTC."
        )

    # --- transport ---------------------------------------------------------

    def ops_chat_id(self) -> str:
        """Stored override (supergroup migration) wins over the spec."""
        return errdb.get_state(self.conn, STATE_OPS_CHAT) or self.cfg.ops_chat_id

    def _alert_thread(self) -> int | None:
        return self.cfg.ops_topic_id

    async def _send_ops(
        self,
        fingerprint: str,
        text: str,
        *,
        silent: bool,
        thread_id: int | None = None,
        reply_to: int | None = None,
    ) -> int | None:
        payload: dict[str, Any] = {
            "chat_id": self.ops_chat_id(),
            "text": text,
            "parse_mode": "HTML",
            "disable_notification": silent,
        }
        if thread_id is not None:
            payload["message_thread_id"] = thread_id
        if reply_to is not None:
            payload["reply_to_message_id"] = reply_to
        for attempt in (0, 1):
            try:
                result = await self.telegram.api("sendMessage", payload)
            except TelegramTransportError as e:
                errdb.queue_outbox(self.conn, fingerprint, text, str(e))
                log.warning("alert_send_failed_queued", error=str(e))
                return None
            except TelegramAPIError as e:
                migrated = e.parameters.get("migrate_to_chat_id")
                if migrated and attempt == 0:
                    # Group → supergroup upgrade changed the chat id
                    errdb.set_state(self.conn, STATE_OPS_CHAT, str(migrated))
                    payload["chat_id"] = str(migrated)
                    errdb.record_occurrence(
                        self.conn, component="telegram", error_class="OpsChatMigrated",
                        severity="warning",
                        message=f"ops chat migrated to supergroup {migrated}",
                    )
                    continue
                errdb.queue_outbox(self.conn, fingerprint, text, str(e))
                log.warning("alert_send_rejected_queued", error=str(e))
                return None
            await self.flush_outbox()
            return result["message_id"]
        return None

    async def _edit_owned(self, row: dict[str, Any], text: str) -> bool:
        payload = {
            "chat_id": row["alert_chat_id"],
            "message_id": row["alert_message_id"],
            "text": text,
            "parse_mode": "HTML",
        }
        try:
            await self.telegram.api("editMessageText", payload)
            return True
        except TelegramTransportError as e:
            errdb.queue_outbox(self.conn, row["fingerprint"], text, str(e))
            return False
        except TelegramAPIError as e:
            description = e.description.lower()
            if "message is not modified" in description:
                return True
            if "can't be edited" in description or "message to edit not found" in description:
                # Bots cannot edit messages older than 48 h — start
                # a fresh owned message and adopt its id.
                message_id = await self._send_ops(
                    row["fingerprint"], text,
                    silent=self._is_silent(row["severity"], datetime.now(timezone.utc)),
                    thread_id=self._alert_thread(),
                )
                if message_id is not None:
                    errdb.set_alert_message(
                        self.conn, row["fingerprint"], self.ops_chat_id(), message_id
                    )
                    return True
                return False
            log.warning("alert_edit_rejected", error=str(e))
            return False

    async def _maybe_pin(self, row: dict[str, Any], message_id: int) -> None:
        if row["severity"] != "critical" or not self.cfg.alerting.pin_critical:
            return
        try:
            await self.telegram.api(
                "pinChatMessage",
                {"chat_id": self.ops_chat_id(), "message_id": message_id,
                 "disable_notification": True},
            )
            self.conn.execute(
                "UPDATE error_events SET pinned = true WHERE fingerprint = %s",
                (row["fingerprint"],),
            )
        except (TelegramAPIError, TelegramTransportError) as e:
            log.warning("pin_failed", error=str(e))

    # --- recovery --------------------------------------------------

    async def resolve_source_events(self, source_name: str, now: datetime | None = None) -> int:
        """Mark the source's open fetch events resolved, edit their owned
        alerts to the resolved text and unpin any pinned ones. Returns the
        number of events resolved."""
        now = now or datetime.now(timezone.utc)
        resolved = errdb.resolve_fetch_events(self.conn, source_name, now)
        for row in resolved:
            if not self.active or row["alert_message_id"] is None:
                continue
            await self._edit_owned(row, self.render_resolved(row, now))
            if row["pinned"]:
                try:
                    await self.telegram.api(
                        "unpinChatMessage",
                        {"chat_id": row["alert_chat_id"],
                         "message_id": row["alert_message_id"]},
                    )
                except (TelegramAPIError, TelegramTransportError):
                    pass
        return len(resolved)

    # --- outbox ---------------------------------------------------

    async def flush_outbox(self) -> int:
        """Deliver alerts queued while Telegram was unreachable: a summary
        header, plus the individual bodies when there are 10 or fewer.
        Returns the count marked delivered; 0 when the flush itself failed
        (entries stay queued for the next contact)."""
        pending = errdb.pending_outbox(self.conn)
        if not pending or not self.active:
            return 0
        oldest = min(p["queued_utc"] for p in pending)
        header = (
            f"↩️ {len(pending)} alerts queued while unreachable, "
            f"oldest {oldest:%H:%M} UTC."
        )
        payload = {
            "chat_id": self.ops_chat_id(), "text": header,
            "parse_mode": "HTML", "disable_notification": True,
        }
        try:
            await self.telegram.api("sendMessage", payload)
            if len(pending) <= 10:
                for entry in pending:
                    await self.telegram.api(
                        "sendMessage",
                        {"chat_id": self.ops_chat_id(), "text": entry["body"],
                         "parse_mode": "HTML", "disable_notification": True},
                    )
        except (TelegramAPIError, TelegramTransportError) as e:
            log.warning("outbox_flush_failed", error=str(e))
            return 0
        errdb.mark_outbox_delivered(self.conn, [p["outbox_id"] for p in pending])
        return len(pending)

    # --- digest ----------------------------------------------------

    async def maybe_digest(self, now: datetime | None = None) -> bool:
        """Send the unacked-events digest once per digest_interval_min.
        Returns True when a digest was rendered; the timer advances even
        when there was nothing unacked to report."""
        if not self.active:
            return False
        now = now or datetime.now(timezone.utc)
        last_raw = errdb.get_state(self.conn, STATE_LAST_DIGEST)
        interval = timedelta(minutes=self.cfg.alerting.digest_interval_min)
        if last_raw and now - datetime.fromisoformat(last_raw) < interval:
            return False
        unacked = errdb.query_events(self.conn, state="unacked", limit=20)
        errdb.set_state(self.conn, STATE_LAST_DIGEST, now.isoformat())
        if not unacked:
            return False
        lines = [f"🗒 DIGEST — {len(unacked)} unacked"]
        for row in unacked[:10]:
            scope = " · ".join(p for p in (row.get("source_name"), row["error_class"]) if p)
            lines.append(
                f"{SEVERITY_EMOJI[row['severity']]} {row['short_id']} "
                f"{html.escape(scope)} ×{row['occurrence_count']}"
            )
        if len(unacked) > 10:
            lines.append(f"… and {len(unacked) - 10} more — /unchecked")
        payload = {
            "chat_id": self.ops_chat_id(), "text": "\n".join(lines),
            "parse_mode": "HTML",
        }
        thread = self.cfg.digest_topic_id or self.cfg.ops_topic_id
        if thread is not None:
            payload["message_thread_id"] = thread
        try:
            await self.telegram.api("sendMessage", payload)
        except (TelegramAPIError, TelegramTransportError) as e:
            log.warning("digest_send_failed", error=str(e))
        return True

    # --- retention ------------------------------------------------

    def maybe_retention(self, now: datetime | None = None) -> None:
        """Run the retention cleanup at most once per 24 h, deleting
        occurrences and resolved events older than the configured windows."""
        if self.cfg is None:
            return
        now = now or datetime.now(timezone.utc)
        last_raw = errdb.get_state(self.conn, STATE_LAST_RETENTION)
        if last_raw and now - datetime.fromisoformat(last_raw) < timedelta(hours=24):
            return
        occ, events = errdb.retention_cleanup(
            self.conn,
            occurrences_days=self.cfg.retention.occurrences_days,
            events_days=self.cfg.retention.events_days,
        )
        errdb.set_state(self.conn, STATE_LAST_RETENTION, now.isoformat())
        if occ or events:
            log.info("retention_cleanup", occurrences=occ, events=events)
