"""Operator commands in the shared ops group chat.

Every command is a saved filter over ``error_events``. The chat contains
other moderators and other bots, so: updates from bots are dropped before
parsing, commands from any other chat are ignored silently, ``/cmd@otherbot``
is ignored, write commands are role-gated and audited, and replies to an
alert message resolve to its fingerprint.
"""

from __future__ import annotations

import html
import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg
import structlog

from archive import errors as errdb
from archive import stats as statsdb
from archive import writer
from feedspec.loader import LoadedSpec
from feedspec.resolve import resolve_source
from newsbot.alerts import SEVERITY_EMOJI, AlertEngine, _fmt_duration
from newsbot.telegram import TelegramAPIError, TelegramClient, TelegramTransportError

log = structlog.get_logger(__name__)

READ_COMMANDS = {"critical", "unchecked", "errors", "logs", "status", "detail", "whoami"}
WRITE_COMMANDS = {"ack", "mute", "unmute"}

_DURATION_RE = re.compile(r"^(\d+)([mhd])$")
PAGE_SIZE = 10


def parse_duration(raw: str) -> timedelta | None:
    """Parse "30m" / "2h" / "1d" into a timedelta; None when malformed."""
    match = _DURATION_RE.match(raw.strip().lower())
    if not match:
        return None
    value, unit = int(match.group(1)), match.group(2)
    return timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[unit]: value})


async def register_commands(telegram: TelegramClient, ops_chat_id: str) -> None:
    """Menu scoped to the ops chat, empty everywhere else. Called from the
    migration job alongside setWebhook — never on container start, or every
    cold start would hit the Bot API."""
    commands = [
        {"command": c, "description": d}
        for c, d in (
            ("status", "health snapshot"),
            ("critical", "open critical events"),
            ("unchecked", "everything unacked"),
            ("errors", "open errors [severity] [source] [page]"),
            ("logs", "all events, last 24h [page]"),
            ("detail", "one event, full detail"),
            ("ack", "acknowledge <short_id>|all"),
            ("mute", "mute <short_id|source> <duration>"),
            ("unmute", "unmute <short_id|source>"),
            ("whoami", "your id and role"),
        )
    ]
    await telegram.api(
        "setMyCommands",
        {"commands": commands, "scope": {"type": "chat", "chat_id": ops_chat_id}},
    )
    await telegram.api("setMyCommands", {"commands": [], "scope": {"type": "default"}})


class OpsBot:
    """Parses and executes operator commands from webhook updates. Holds
    only per-replica caches (bot identity, chat admins) — everything
    durable lives in ``error_events`` and its audit tables."""

    def __init__(
        self,
        conn: psycopg.Connection,
        telegram: TelegramClient,
        loaded: LoadedSpec,
        engine: AlertEngine,
    ):
        self.conn = conn
        self.telegram = telegram
        self.loaded = loaded
        self.cfg = loaded.spec.errors
        self.engine = engine
        self.me_username: str | None = None
        # The numeric id is the token's prefix — no API call needed.
        try:
            self.me_id: int | None = int(telegram.token.partition(":")[0])
        except ValueError:
            self.me_id = None
        self._admin_cache: tuple[float, frozenset[int]] | None = None

    async def ensure_me(self) -> bool:
        """Fill me_username lazily (once per warm replica). Without it,
        handle_update treats every /cmd@suffix as addressed to us — including
        another bot's /ack. On failure the webhook answers non-200 before
        marking the update seen, so Telegram's redelivery gets a clean retry."""
        if self.me_username is not None:
            return True
        try:
            me = await self.telegram.api("getMe", {})
        except (TelegramAPIError, TelegramTransportError) as e:
            log.warning("getme_failed", error=str(e))
            return False
        self.me_username = me["username"].lower()
        self.me_id = me["id"]
        return True

    # --- update handling (testable entry point) ----------------------------

    async def handle_update(self, update: dict[str, Any]) -> str | None:
        """Returns a short action tag for tests, None when dropped."""
        message = update.get("message")
        if not message:
            return None
        sender = message.get("from") or {}
        if sender.get("is_bot"):
            return None  # another bot must not act on our state
        chat_id = str((message.get("chat") or {}).get("id", ""))
        if chat_id != self.engine.ops_chat_id():
            return None  # wrong chat: no reply at all
        text = (message.get("text") or "").strip()
        if not text:
            return None

        reply = message.get("reply_to_message")
        if not text.startswith("/"):
            if reply is not None:
                return await self._handle_reply_shorthand(message, reply, text)
            return None

        first, _, arg_str = text.partition(" ")
        command, _, suffix = first[1:].partition("@")
        if suffix and self.me_username and suffix.lower() != self.me_username:
            return None  # addressed to another bot
        command = command.lower()
        args = arg_str.split()

        if command in WRITE_COMMANDS:
            if not await self._is_writer(sender):
                username = sender.get("username") or sender.get("id")
                await self._reply(message, f"@{username} /{command} requires admin")
                errdb.record_operator_action(
                    self.conn, user_id=sender["id"], username=sender.get("username"),
                    command=f"/{command}", target=arg_str or None, result="denied",
                )
                await self.engine.report(
                    component="newsbot", error_class="UnauthorizedCommand",
                    message=f"/{command} denied",
                )
                return "denied"
            return await self._dispatch_write(command, args, message, sender)
        if command in READ_COMMANDS:
            return await self._dispatch_read(command, args, message, sender, reply)
        return None  # unknown command: stay quiet, another bot may own it

    async def _handle_reply_shorthand(
        self, message: dict, reply: dict, text: str
    ) -> str | None:
        """Reply-to-ack: "ack", "mute 2h", "detail" on an alert message."""
        parts = text.lower().split()
        verb = parts[0]
        if verb not in ("ack", "mute", "detail", "unmute"):
            return None
        event = errdb.get_event_by_alert_message(
            self.conn, self.engine.ops_chat_id(), reply.get("message_id", -1)
        )
        if event is None:
            return None
        sender = message["from"]
        if verb == "detail":
            return await self._cmd_detail(message, [event["short_id"]])
        if not await self._is_writer(sender):
            username = sender.get("username") or sender.get("id")
            await self._reply(message, f"@{username} {verb} requires admin")
            return "denied"
        if verb == "ack":
            return await self._do_ack(message, sender, event["short_id"])
        if verb == "unmute":
            return await self._do_mute(message, sender, [event["short_id"]], unmute=True)
        return await self._do_mute(message, sender, [event["short_id"], *parts[1:]])

    # --- authorization ---------------------------------------------

    async def _is_writer(self, sender: dict[str, Any]) -> bool:
        user_id = sender.get("id")
        if user_id is None:
            return False
        if user_id in self.cfg.authorization.write_allowlist:
            return True
        return user_id in await self._admins()

    async def _admins(self) -> frozenset[int]:
        ttl = self.cfg.authorization.admin_cache_min * 60
        if self._admin_cache and time.monotonic() - self._admin_cache[0] < ttl:
            return self._admin_cache[1]
        try:
            admins = await self.telegram.api(
                "getChatAdministrators", {"chat_id": self.engine.ops_chat_id()}
            )
            ids = frozenset(a["user"]["id"] for a in admins)
        except (TelegramAPIError, TelegramTransportError) as e:
            log.warning("get_admins_failed", error=str(e))
            ids = self._admin_cache[1] if self._admin_cache else frozenset()
        self._admin_cache = (time.monotonic(), ids)
        return ids

    # --- dispatch ----------------------------------------------------------

    async def _dispatch_read(
        self, command: str, args: list[str], message: dict, sender: dict, reply: dict | None
    ) -> str:
        if command == "status":
            await self._reply(message, self._render_status())
        elif command == "critical":
            await self._reply(message, self._render_events(
                errdb.query_events(self.conn, min_severity="critical",
                                   exclude_resolved=True, limit=PAGE_SIZE),
                title="CRITICAL, open",
            ))
        elif command == "unchecked":
            await self._reply(message, self._render_events(
                errdb.query_events(self.conn, unreviewed=True, limit=PAGE_SIZE),
                title="UNACKED (incl. auto-resolved)", show_state=True,
            ))
        elif command == "errors":
            severity, source, page = self._parse_filter_args(args)
            await self._reply(message, self._render_events(
                errdb.query_events(
                    self.conn, min_severity=severity or "error", exclude_resolved=True,
                    source_name=source, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE,
                ),
                title=f"ERRORS p{page}",
            ))
        elif command == "logs":
            page = int(args[0]) if args and args[0].isdigit() else 1
            since = datetime.now(timezone.utc) - timedelta(hours=24)
            await self._reply(message, self._render_events(
                errdb.query_events(self.conn, since=since, limit=PAGE_SIZE,
                                   offset=(page - 1) * PAGE_SIZE),
                title=f"LOG 24h p{page}", show_state=True,
            ))
        elif command == "detail":
            return await self._cmd_detail(message, args)
        elif command == "whoami":
            role = "write" if await self._is_writer(sender) else "read"
            username = sender.get("username") or "-"
            await self._reply(
                message, f"id <code>{sender['id']}</code> · @{html.escape(str(username))} · role: {role}"
            )
        return command

    async def _dispatch_write(
        self, command: str, args: list[str], message: dict, sender: dict
    ) -> str:
        if command == "ack":
            if not args:
                await self._reply(message, "usage: /ack <short_id> | /ack all")
                return "ack"
            if args[0].lower() == "all":
                count = errdb.ack_all(self.conn, sender["id"], sender.get("username"))
                errdb.record_operator_action(
                    self.conn, user_id=sender["id"], username=sender.get("username"),
                    command="/ack", target="all", result=f"acked {count}",
                )
                await self._reply(
                    message, f"✅ {count} events acked by @{sender.get('username') or sender['id']}"
                )
                return "ack"
            return await self._do_ack(message, sender, args[0])
        if command == "mute":
            return await self._do_mute(message, sender, args)
        return await self._do_mute(message, sender, args, unmute=True)

    async def _do_ack(self, message: dict, sender: dict, short_id: str) -> str:
        outcome, row = errdb.ack_event(
            self.conn, short_id, sender["id"], sender.get("username")
        )
        errdb.record_operator_action(
            self.conn, user_id=sender["id"], username=sender.get("username"),
            command="/ack", target=short_id, result=outcome,
        )
        if outcome == "missing":
            await self._reply(message, f"no event <code>{html.escape(short_id)}</code>")
        elif outcome == "already":
            # Races: two moderators reaching for the same alert is normal
            acker = row["acked_by_username"] or row["acked_by_user_id"]
            await self._reply(
                message,
                f"already acked by @{html.escape(str(acker))} at {row['acked_utc']:%H:%M} UTC",
            )
        else:
            username = sender.get("username") or sender["id"]
            await self._reply(message, f"✅ {row['short_id']} acked by @{html.escape(str(username))}")
            if self.engine.active and row["alert_message_id"] is not None:
                await self.engine._edit_owned(
                    row | {"state": "acked",
                           "acked_by_username": sender.get("username"),
                           "acked_by_user_id": sender["id"]},
                    self.engine.render_alert(
                        row | {"state": "acked",
                               "acked_by_username": sender.get("username")},
                        datetime.now(timezone.utc),
                    ),
                )
        return "ack"

    async def _do_mute(
        self, message: dict, sender: dict, args: list[str], *, unmute: bool = False
    ) -> str:
        verb = "unmute" if unmute else "mute"
        if not args or (not unmute and len(args) < 2):
            await self._reply(message, f"usage: /{verb} <short_id|source>" + ("" if unmute else " <duration>"))
            return verb
        target = args[0]
        until = None
        if not unmute:
            duration = parse_duration(args[1])
            if duration is None:
                await self._reply(message, "duration must look like 30m, 2h or 1d")
                return verb
            until = datetime.now(timezone.utc) + duration
        source_names = {s.name for s in self.loaded.spec.sources}
        kwargs = {"source_name": target} if target in source_names else {"short_id": target}
        count = errdb.mute(
            self.conn, **kwargs, until=until, username=sender.get("username")
        )
        errdb.record_operator_action(
            self.conn, user_id=sender["id"], username=sender.get("username"),
            command=f"/{verb}", target=" ".join(args), result=f"{count} events",
        )
        username = sender.get("username") or sender["id"]
        if count == 0:
            await self._reply(message, f"nothing matches <code>{html.escape(target)}</code>")
        elif unmute:
            await self._reply(message, f"🔔 {count} event(s) unmuted by @{html.escape(str(username))}")
        else:
            await self._reply(
                message,
                f"🔇 {count} event(s) muted until {until:%H:%M} UTC by @{html.escape(str(username))}",
            )
        return verb

    async def _cmd_detail(self, message: dict, args: list[str]) -> str:
        if not args:
            await self._reply(message, "usage: /detail <short_id>")
            return "detail"
        event = errdb.get_event(self.conn, args[0])
        if event is None:
            await self._reply(message, f"no event <code>{html.escape(args[0])}</code>")
            return "detail"
        occurrences = errdb.recent_occurrences(self.conn, event["fingerprint"])
        lines = [
            f"{SEVERITY_EMOJI[event['severity']]} {event['severity'].upper()}  {event['short_id']} · {event['state']}",
            html.escape(" · ".join(p for p in (
                event["component"], event["error_class"],
                event.get("source_name") or "", event.get("channel_name") or "",
            ) if p)),
            html.escape(event["message"] or ""),
            f"first {event['first_seen_utc']:%m-%d %H:%M} · last {event['last_seen_utc']:%m-%d %H:%M} "
            f"· ×{event['occurrence_count']} · streak {event['consecutive_streak']}",
        ]
        if event["detail"]:
            blob = json.dumps(event["detail"], indent=2, default=str)[:1500]
            lines.append(f"<pre>{html.escape(blob)}</pre>")
        if occurrences:
            lines.append("last occurrences:")
            lines.extend(f"  {o['occurred_utc']:%m-%d %H:%M}" for o in occurrences)
        await self._reply(message, "\n".join(lines))
        return "detail"

    # --- rendering ---------------------------------------------------------

    def _parse_filter_args(self, args: list[str]) -> tuple[str | None, str | None, int]:
        severity = source = None
        page = 1
        source_names = {s.name for s in self.loaded.spec.sources}
        for arg in args:
            lowered = arg.lower()
            if lowered in ("critical", "error", "warning", "info"):
                severity = lowered
            elif arg in source_names:
                source = arg
            elif arg.isdigit():
                page = max(int(arg), 1)
        return severity, source, page

    def _render_events(
        self, rows: list[dict[str, Any]], *, title: str, show_state: bool = False
    ) -> str:
        if not rows:
            return f"{title}: nothing 🎉"
        lines = [title]
        for row in rows:
            scope = " · ".join(p for p in (row.get("source_name"), row["error_class"]) if p)
            state = f" · {row['state']}" if show_state else ""
            lines.append(
                f"{SEVERITY_EMOJI[row['severity']]} <code>{row['short_id']}</code> "
                f"{html.escape(scope)} ×{row['occurrence_count']} "
                f"{row['last_seen_utc']:%H:%M}{state}"
            )
        return "\n".join(lines)

    def _render_status(self) -> str:
        spec = self.loaded.spec
        now = datetime.now(timezone.utc)
        lines = [f"📊 STATUS  {now:%H:%M} UTC", "", "SOURCES"]
        for source in spec.sources:
            effective = resolve_source(spec, source)
            if not effective.enabled:
                lines.append(f"⏸️ {source.name:<15} disabled")
                continue
            state = writer.load_source_state(self.conn, source.name, now)
            fails = state.consecutive_failures
            mark = "⚠️" if fails else "✅"
            last = (
                f"last ok {_fmt_duration(now - state.last_fetch_utc)} ago"
                if state.last_fetch_utc else "never fetched"
            )
            due = state.next_fetch_utc - now
            next_str = f"next {_fmt_duration(due)}" if due.total_seconds() > 0 else "due now"
            lines.append(f"{mark} {source.name:<15} {last}   {next_str}   {fails} fails")

        lines += ["", "CHANNELS"]
        backlog = {b["channel_name"]: b["n"] for b in statsdb.backlog_sizes(self.conn)}
        for channel in spec.channels:
            state = writer.load_channel_state(self.conn, channel.name)
            last = (
                f"last post {_fmt_duration(now - state.last_post_utc)} ago"
                if state.last_post_utc else "never posted"
            )
            lines.append(f"{channel.name:<9} {last}   queue {backlog.get(channel.name, 0)}")

        counts = errdb.unacked_counts(self.conn)
        if counts:
            summary = " · ".join(
                f"{counts[s]} {s}" for s in ("critical", "error", "warning", "info") if counts.get(s)
            )
            lines += ["", f"ERRORS   {summary} unacked"]
        else:
            lines += ["", "ERRORS   none unacked"]

        archive_row = self.conn.execute(
            """
            SELECT (SELECT count(*) FROM items) AS items,
                   (SELECT count(*) FROM fetches
                    WHERE possible_gap AND started_utc > now() - interval '24 hours') AS gaps,
                   (SELECT count(*) FROM items WHERE duplicate_of IS NOT NULL) AS dups
            """
        ).fetchone()
        dup_rate = (
            100.0 * archive_row["dups"] / archive_row["items"] if archive_row["items"] else 0.0
        )
        lines.append(
            f"ARCHIVE  {archive_row['items']:,} items · {archive_row['gaps']} gaps 24h "
            f"· dup rate {dup_rate:.1f}%"
        )
        return "\n".join(lines)

    # --- transport ---------------------------------------------------------

    async def _reply(self, message: dict, text: str) -> None:
        """Responses reply to the invoking message so parallel
        conversations in a busy group stay legible."""
        payload = {
            "chat_id": self.engine.ops_chat_id(),
            "text": text[:4096],
            "parse_mode": "HTML",
            "reply_to_message_id": message.get("message_id"),
        }
        try:
            await self.telegram.api("sendMessage", payload)
        except (TelegramAPIError, TelegramTransportError) as e:
            log.warning("ops_reply_failed", error=str(e))
