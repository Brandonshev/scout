"""One-way updates to your iPhone. Replies are never read.

- NotifyBackend is the interface. NtfyBackend sends push notifications through the free ntfy app
  (the main channel); IMessageBackend sends through the Messages app on this Mac. The Notifier tries
  the enabled channels in order, so a second one works as a fallback.
- The message text and recipient are passed to osascript as separate arguments, never pasted
  into the AppleScript, so no message can break or change the script.
- Notifier keeps a queue in the `notifications` table: nothing is lost while the Mac sleeps or
  Messages fails. It sends at most one message per `min_seconds_between`, `max_per_hour` an hour,
  waits out quiet hours (except CRITICAL), bundles BATCH updates, merges backlogs into one digest,
  and retries failures with growing waits. Every message starts with the version and DEMO/LIVE.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Sequence
from datetime import datetime, time
from enum import IntEnum

import httpx

from scout.config import Settings
from scout.data import format_price
from scout.db import log_event, now_ms
from scout.version import APP_VERSION

log = logging.getLogger(__name__)


class Priority(IntEnum):
    BATCH = 0  # minor (e.g. a stop moved): bundled into one message every batch_minutes
    NORMAL = 1  # trades, mood changes, risk events, daily summary: waits out quiet hours
    CRITICAL = 2  # kill switch: sent straight away, even in quiet hours


class NotifyError(RuntimeError):
    """A message couldn't be sent."""


# ---------------------------------------------------------------- backends


class NotifyBackend(ABC):
    name: str

    @abstractmethod
    async def send(self, text: str, priority: Priority = Priority.NORMAL) -> None:
        """Send one message, or raise NotifyError. Backends may ignore the priority."""


# The recipient and text arrive as arguments (argv), so they are never part of the script itself.
APPLESCRIPT = """on run argv
    set theRecipient to item 1 of argv
    set theMessage to item 2 of argv
    tell application "Messages"
        set theService to 1st account whose service type = iMessage
        try
            set theTarget to participant theRecipient of theService
        on error
            set theTarget to buddy theRecipient of theService
        end try
        send theMessage to theTarget
    end tell
end run"""

Runner = Callable[[Sequence[str], float], Awaitable[tuple[int, str, str]]]


async def run_osascript(args: Sequence[str], timeout: float) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        process.kill()
        raise NotifyError(f"osascript didn't finish within {timeout:g}s (is Messages stuck?)") from None
    return process.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


class IMessageBackend(NotifyBackend):
    name = "imessage"

    def __init__(self, recipient: str, timeout: float = 30.0, runner: Runner | None = None) -> None:
        self.recipient = recipient
        self.timeout = timeout
        self.runner = runner or run_osascript

    async def send(self, text: str, priority: Priority = Priority.NORMAL) -> None:
        try:
            code, _, err = await self.runner(["osascript", "-e", APPLESCRIPT, self.recipient, text], self.timeout)
        except OSError as exc:
            raise NotifyError(f"couldn't run osascript: {exc}") from None
        if code != 0:
            raise NotifyError(f"Messages refused (osascript exit {code}): {err.strip()[:300]}")


# ntfy priorities: 5 = urgent (long vibration bursts), 3 = normal sound, 2 = silent (in the list only)
NTFY_PRIORITY = {Priority.BATCH: 2, Priority.NORMAL: 3, Priority.CRITICAL: 5}


class NtfyBackend(NotifyBackend):
    """Push notifications through ntfy (https://ntfy.sh): the ntfy iPhone app shows them.

    Sent as JSON so emoji work in the title. The first line (Scout's version and DEMO/LIVE)
    becomes the notification title. The topic is a secret: anyone who knows it can read the alerts.
    """

    name = "ntfy"

    def __init__(self, server: str, topic: str, token: str | None = None, timeout: float = 15.0,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.server = server.rstrip("/")
        self.topic = topic
        self.token = token
        self.timeout = timeout
        self.transport = transport

    async def send(self, text: str, priority: Priority = Priority.NORMAL) -> None:
        title, _, body = text.partition("\n")
        payload = {"topic": self.topic, "title": title, "message": body.strip() or title,
                   "priority": NTFY_PRIORITY[Priority(priority)]}
        headers = {"User-Agent": f"Scout/{APP_VERSION}"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport) as http:
                response = await http.post(f"{self.server}/", json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise NotifyError(f"couldn't reach {self.server}: {exc}") from None
        if response.status_code >= 300:
            raise NotifyError(f"ntfy refused the message ({response.status_code}): {response.text.strip()[:200]}")


def backends_from_settings(settings: Settings, runner: Runner | None = None,
                           transport: httpx.AsyncBaseTransport | None = None,
                           include_disabled: bool = False) -> list[NotifyBackend]:
    """The alert channels to use, in order (ntfy first, then iMessage as a fallback).
    include_disabled=True also returns channels that are set up but switched off (for `notify-test`)."""
    n = settings.notify
    backends: list[NotifyBackend] = []
    if (n.ntfy_enabled or include_disabled) and n.ntfy_topic is not None:
        backends.append(NtfyBackend(n.ntfy_server, n.ntfy_topic.get_secret_value(),
                                    n.ntfy_token.get_secret_value() if n.ntfy_token else None, transport=transport))
    if (n.imessage_enabled or include_disabled) and n.imessage_recipient is not None:
        backends.append(IMessageBackend(n.imessage_recipient.get_secret_value(), runner=runner))
    return backends


# ----------------------------------------------------------------- helpers


_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def clean_text(text: str, max_length: int) -> str:
    """Remove control characters, squeeze blank lines, and shorten to max_length."""
    text = _CONTROL.sub("", text.replace("\r\n", "\n").replace("\t", " "))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text if len(text) <= max_length else text[: max_length - 1].rstrip() + "…"


def in_quiet_hours(local: time, start: time, end: time) -> bool:
    """True inside [start, end), which may wrap past midnight (e.g. 23:00–07:00)."""
    if start == end:
        return False
    if start < end:
        return start <= local < end
    return local >= start or local < end


def aud(usd: float, usd_per_aud: float, signed: bool = False) -> str:
    value = usd / usd_per_aud
    sign = ("+" if value >= 0 else "−") if signed else ("−" if value < 0 else "")
    return f"{sign}A${abs(value):,.2f}"


def duration(ms: int) -> str:
    minutes = max(0, ms) // 60_000
    days, rest = divmod(minutes, 1440)
    hours, mins = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    return f"{hours}h {mins}m" if hours else f"{mins}m"


# ----------------------------------------------------------------- notifier


class Notifier:
    def __init__(self, conn: sqlite3.Connection, settings: Settings, backends: Sequence[NotifyBackend],
                 clock: Callable[[], int] = now_ms, label: str | None = None) -> None:
        self.label = label  # the account's name, e.g. "70/30:2" (default: the main demo's name)
        self.conn = conn
        self.settings = settings
        self.cfg = settings.notify
        self.backends = list(backends)
        self.clock = clock

    @property
    def enabled(self) -> bool:
        return bool(self.backends)

    def header(self) -> str:
        mode = self.settings.mode.value.upper()
        if self.label:
            return f"Scout v{APP_VERSION} · {self.label} · {mode}"
        if self.settings.mode.value == "demo":
            return f"Scout v{APP_VERSION} · {self.settings.demo.name} · {mode}"
        return f"Scout v{APP_VERSION} · {mode}"

    def notify(self, text: str, category: str, priority: Priority = Priority.NORMAL) -> None:
        """Queue a message (sending happens in deliver()). Recorded as 'disabled' if iMessage is off."""
        self.conn.execute(
            "INSERT INTO notifications (created_ms, category, priority, text, status, app_version) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (self.clock(), category, int(priority), clean_text(text, self.cfg.max_length),
             "pending" if self.enabled else "disabled", APP_VERSION),
        )
        self.conn.commit()
        log.info("notification queued (%s): %s", category, text.splitlines()[0] if text else "")

    def quiet(self, now: int) -> bool:
        local = datetime.fromtimestamp(now / 1000, self.settings.app.tz).time()
        return in_quiet_hours(local, self.cfg.quiet_hours_start, self.cfg.quiet_hours_end)

    def pending(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM notifications WHERE status = 'pending' ORDER BY id").fetchall()

    async def deliver(self) -> int:
        """Send what's allowed right now. Returns how many messages went out."""
        now = self.clock()
        rows = [r for r in self.pending() if r["next_attempt_ms"] is None or r["next_attempt_ms"] <= now]
        sent = 0
        for row in (r for r in rows if r["priority"] == Priority.CRITICAL):
            sent += await self._attempt(row)
        if self.quiet(now):
            return sent

        last = self.conn.execute("SELECT MAX(sent_ms) FROM notifications WHERE status = 'sent'").fetchone()[0]
        if last is not None and now - last < self.cfg.min_seconds_between * 1000:
            return sent
        in_last_hour = self.conn.execute(
            "SELECT COUNT(*) FROM notifications WHERE status = 'sent' AND sent_ms > ?", (now - 3_600_000,)
        ).fetchone()[0]
        room = self.cfg.max_per_hour - in_last_hour
        if room <= 0:
            return sent

        normal = [r for r in rows if r["priority"] == Priority.NORMAL]
        batch = [r for r in rows if r["priority"] == Priority.BATCH]
        batch_due = bool(batch) and now - batch[0]["created_ms"] >= self.cfg.batch_minutes * 60_000
        waiting = normal + (batch if batch_due else [])
        if not waiting:
            return sent
        slept = any(self.quiet(r["created_ms"]) for r in waiting)
        # Merge into one digest after quiet hours, for the hourly batch, or when there's a backlog;
        # otherwise send one at a time (spaced out by min_seconds_between).
        merge = slept or batch_due or len(waiting) > min(3, room)
        if merge and len(waiting) > 1:
            return sent + await self._attempt(self._digest(waiting, slept, now))
        return sent + await self._attempt(waiting[0])

    def _digest(self, rows: Sequence[sqlite3.Row], slept: bool, now: int) -> sqlite3.Row:
        """Merge several waiting messages into one."""
        if slept:
            title = "While you were asleep:"
        elif all(r["priority"] == Priority.BATCH for r in rows):
            title = "Updates:"
        else:
            title = f"{len(rows)} updates:"
        body = "\n".join(f"• {r['text']}" for r in rows)
        with self.conn:
            cursor = self.conn.execute(
                "INSERT INTO notifications (created_ms, category, priority, text, status, app_version) "
                "VALUES (?, 'digest', ?, ?, 'pending', ?)",
                (now, int(Priority.NORMAL), clean_text(f"{title}\n{body}", self.cfg.max_length), APP_VERSION),
            )
            self.conn.executemany("UPDATE notifications SET status = 'merged' WHERE id = ?", [(r["id"],) for r in rows])
        return self.conn.execute("SELECT * FROM notifications WHERE id = ?", (cursor.lastrowid,)).fetchone()

    async def _attempt(self, row: sqlite3.Row) -> int:
        text = clean_text(f"{self.header()}\n{row['text']}", self.cfg.max_length)
        errors = []
        for backend in self.backends:
            try:
                await backend.send(text, Priority(row["priority"]))
            except NotifyError as exc:
                errors.append(f"{backend.name}: {exc}")
                continue
            with self.conn:
                self.conn.execute(
                    "UPDATE notifications SET status = 'sent', sent_ms = ?, backend = ?, attempts = attempts + 1 "
                    "WHERE id = ?", (self.clock(), backend.name, row["id"]),
                )
            log.info("notification sent via %s (%s)", backend.name, row["category"])
            return 1
        attempts = row["attempts"] + 1
        error = "; ".join(errors) or "no way to send messages is set up"
        with self.conn:
            if attempts >= self.cfg.retry_attempts:
                self.conn.execute(
                    "UPDATE notifications SET status = 'failed', attempts = ?, last_error = ? WHERE id = ?",
                    (attempts, error, row["id"]),
                )
                message = f"Couldn't send a message after {attempts} tries ({error}). It was: {row['text'][:120]}"
                log.error(message)
                log_event(self.conn, "ERROR", "notify", message, ts_ms=self.clock())
            else:
                wait = self.cfg.retry_seconds * 1000 * 2 ** (attempts - 1)
                self.conn.execute(
                    "UPDATE notifications SET attempts = ?, last_error = ?, next_attempt_ms = ? WHERE id = ?",
                    (attempts, error, self.clock() + wait, row["id"]),
                )
                log.warning("message not sent (try %d of %d): %s", attempts, self.cfg.retry_attempts, error)
        return 0

    async def send_now(self, text: str) -> str:
        """Send one message immediately, bypassing the queue (for `scout notify-test`). Returns the backend used."""
        if not self.backends:
            raise NotifyError("iMessage is off or has no recipient")
        full = clean_text(f"{self.header()}\n{text}", self.cfg.max_length)
        errors = []
        for backend in self.backends:
            try:
                await backend.send(full)
                return backend.name
            except NotifyError as exc:
                errors.append(f"{backend.name}: {exc}")
        raise NotifyError("; ".join(errors))


# ----------------------------------------------------------------- messages
# Short and plain. The Notifier adds the "Scout v0.8.0 · DEMO" header to each.


def trade_opened(coin: str, side: str, qty: float, price: float, stop: float, risk_usd: float,
                 notional_usd: float, why: str, regime: str, usd_per_aud: float, wild: bool = False) -> str:
    long = side == "long"
    distance = abs(price - stop) / price * 100
    lines = [
        f"{'🟢 BOUGHT' if long else '🟣 SHORTED'} {qty:g} {coin} at ${format_price(price)} "
        f"({aud(notional_usd, usd_per_aud)})",
        f"Why: {why}. Market mood {regime}.",
        f"Stop ${format_price(stop)} ({distance:.1f}% {'below' if long else 'above'}) · at risk "
        f"{aud(risk_usd, usd_per_aud)}" + (" · half size (wild volatility)" if wild else ""),
    ]
    return "\n".join(lines)


def trade_closed(coin: str, side: str, entry: float, exit_price: float, qty: float, pnl_usd: float,
                 reason: str, held_ms: int, usd_per_aud: float) -> str:
    pct = pnl_usd / (qty * entry) * 100 if qty and entry else 0.0
    verb = "SOLD" if side == "long" else "CLOSED SHORT"
    why = re.sub(rf"^(Selling|Closing the short in|Closing the {re.escape(coin)} short|Kill switch: closing) "
                 rf"{re.escape(coin)}?[:.]\s*", "", reason)
    return "\n".join([
        f"{'✅' if pnl_usd >= 0 else '🔻'} {verb} {coin}: {aud(pnl_usd, usd_per_aud, signed=True)} ({pct:+.1f}%) "
        "after fees",
        f"In ${format_price(entry)} → out ${format_price(exit_price)} · held {duration(held_ms)}",
        f"Why: {why}",
    ])


def stop_moved(coin: str, old: float, new: float) -> str:
    return f"{coin} stop moved to ${format_price(new)} (was ${format_price(old)})"


def mood_changed(old_regime: str | None, old_volatility: str | None, reading, slow_ma_days: int) -> str | None:
    """A message if the mood or the wild-volatility state changed, else None."""
    parts = []
    new = reading.regime.value
    if old_regime is not None and new != old_regime:
        coin = "Bitcoin"
        if new == "RISK_OFF":
            if reading.btc_price < reading.slow_ma:
                cause = f"{coin} dropped below its {slow_ma_days}-day average"
            elif reading.breadth_pct is not None and reading.breadth_pct < 50:
                cause = f"only {reading.breadth_pct:.0f}% of the big coins are still rising"
            else:
                cause = f"the trend has weakened (score {reading.score:+d})"
            rule = "No new buys until it recovers."
        elif new == "RISK_ON":
            breadth = f" and {reading.breadth_pct:.0f}% of the big coins are rising" if reading.breadth_pct else ""
            cause = f"{coin} is in an uptrend{breadth}"
            rule = "New buys allowed."
        else:
            cause = f"the signals are mixed (score {reading.score:+d})"
            rule = "Buys still allowed, with extra care."
        parts.append(f"Market mood changed to {new} (was {old_regime}) — {cause}. {rule}")
    new_vol = reading.volatility.value
    if old_volatility is not None and new_vol != old_volatility and "WILD" in (new_vol, old_volatility):
        parts.append("Volatility is now WILD: new positions are half size." if new_vol == "WILD"
                     else f"Volatility is back to {new_vol}: full position sizes again.")
    return "\n".join(parts) or None


def daily_limit_hit(loss_pct: float, limit_pct: float) -> str:
    return (f"⚠️ Daily loss limit hit: down {loss_pct:.1f}% today (limit {limit_pct:g}%). "
            "No new trades until midnight; open positions are still managed.")


def kill_switch(reason: str, closed: int, result_usd: float, usd_per_aud: float) -> str:
    return (f"🛑 KILL SWITCH: {reason}\nClosed {closed} position(s), result {aud(result_usd, usd_per_aud, True)}. "
            "Nothing new is traded until you run `scout reset-kill`.")


def feed_down(seconds: float) -> str:
    return (f"⚠️ Live prices have stopped for {seconds / 60:.0f}+ minutes (connection problem?). "
            "No new trades until they're back.")


def feed_back(minutes: float) -> str:
    return f"✅ Live prices are back after {minutes:.0f} min."


UPDATE_LATE_MS = 3 * 3_600_000  # an update this late (the Mac was asleep) is skipped, not sent


def due_update(now_ms: int, tz, times: Sequence[time], last_slot: str | None, opened_ms: int = 0) -> tuple[str, bool] | None:
    """The scheduled update that's due now: (slot like "2026-10-02 08:00", send it?), or None if nothing is due.
    send is False when the slot is over 3 hours old (e.g. after sleep, or the first start of the day) or the
    account opened after it: the caller records it as done without sending, so no "morning" update turns up
    in the afternoon."""
    local = datetime.fromtimestamp(now_ms / 1000, tz)
    passed = [t for t in sorted(times) if local.time() >= t]
    if not passed:
        return None
    at = local.replace(hour=passed[-1].hour, minute=passed[-1].minute, second=0, microsecond=0)
    slot = f"{at:%Y-%m-%d %H:%M}"
    if slot == last_slot:
        return None
    at_ms = at.timestamp() * 1000
    return slot, now_ms - at_ms <= UPDATE_LATE_MS and opened_ms <= at_ms


def daily_summary(day_label: str, equity_usd: float, start_usd: float, day_start_usd: float, trades_today: int,
                  positions: Sequence[tuple[str, float, float]], mood: str | None, btc_today_pct: float | None,
                  btc_since_start_pct: float | None, usd_per_aud: float) -> str:
    """positions: (coin, profit/loss in US$, profit/loss %)."""
    today = equity_usd - day_start_usd
    lines = [
        f"📊 Update ({day_label})",
        f"Balance {aud(equity_usd, usd_per_aud)} ({(equity_usd / start_usd - 1) * 100:+.2f}% since the start)",
        f"Today {aud(today, usd_per_aud, True)} ({today / day_start_usd * 100:+.2f}%) · {trades_today} trade(s)",
        "Open: " + (", ".join(f"{c} {aud(p, usd_per_aud, True)} ({pct:+.1f}%)" for c, p, pct in positions)
                    if positions else "none"),
    ]
    market = []
    if mood:
        market.append(f"mood {mood}")
    if btc_today_pct is not None:
        market.append(f"BTC today {btc_today_pct:+.1f}%")
    if market:
        lines.append("Market: " + ", ".join(market))
    if btc_since_start_pct is not None:
        lines.append(f"Since the start: Scout {(equity_usd / start_usd - 1) * 100:+.1f}% vs holding BTC "
                     f"{btc_since_start_pct:+.1f}%")
    return "\n".join(lines)
