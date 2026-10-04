"""Scout's local dashboard (Streamlit). Start it with `uv run scout dashboard`.

READ-ONLY: the database is opened with SQLite's mode=ro, so this page cannot place, change or
close trades. The only thing it can write is the replay control file (pause / step / speed of a
replay), which changes the pace of a replay and nothing else.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import altair as alt
import streamlit as st

from scout import dashboard_data as data
from scout.config import load_settings
from scout.data import format_price
from scout.replay import Control, ReplayControl
from scout.version import APP_VERSION

MOOD_COLOURS = {"RISK_ON": "#2e9e5b", "NEUTRAL": "#9a9a9a", "RISK_OFF": "#d9534f"}
SPEEDS = ["100", "500", "2000", "5000", "20000", "50000", "max"]

st.set_page_config(page_title="Scout", page_icon="🔭", layout="wide")


@st.cache_resource
def settings(config: str, env_file: str):
    """Loaded once per config file (re-loaded if you point the dashboard at a different one)."""
    return load_settings(config, env_file)


def replay_db(cfg) -> Path:
    return cfg.app.db_path.parent / "replay.db"


def control_file(cfg) -> Path:
    return cfg.app.db_path.parent / "replay_control.json"


def when(ms: float | None, tz) -> str:
    return "—" if ms is None else datetime.fromtimestamp(ms / 1000, tz).strftime("%a %d %b %Y %H:%M")


def aud(value: float | None, signed: bool = False) -> str:
    if value is None:
        return "—"
    sign = ("+" if value >= 0 else "−") if signed else ("−" if value < 0 else "")
    return f"{sign}A${abs(value):,.2f}"


def md(text: str) -> str:
    """Escape dollar signs: Streamlit would otherwise read $...$ as a maths formula."""
    return text.replace("$", "\\$")


def pct(value: float | None) -> str:
    return "—" if value is None else f"{value:+.2f}%"


cfg = settings(os.environ.get("SCOUT_CONFIG", "config.yaml"), os.environ.get("SCOUT_ENV_FILE", ".env"))
tz = cfg.app.tz
usd_per_aud = cfg.demo.aud_to_usdc_rate

# ---------------------------------------------------------------- sidebar

with st.sidebar:
    st.title("🔭 Scout")
    st.caption(f"Dashboard v{APP_VERSION} · read-only")
    sources = {"Demo": cfg.app.db_path, "Replay": replay_db(cfg)}
    default = os.environ.get("SCOUT_DASHBOARD_SOURCE", "Demo")
    source = st.radio("Show", list(sources), index=list(sources).index(default) if default in sources else 0,
                      format_func=lambda s: f"{cfg.demo.name} (demo)" if s == "Demo" else s)
    auto = st.toggle("Refresh automatically", value=True, help="Re-read the database every few seconds.")

    if source == "Replay":
        st.divider()
        st.subheader("Replay controls")
        st.caption("These only change the pace of a replay. They can't trade.")
        control = ReplayControl(control_file(cfg))
        current = control.read()
        speed_label = "max" if current.speed is None else str(int(current.speed))
        col1, col2 = st.columns(2)
        if col1.button("▶ Resume" if current.paused else "⏸ Pause", width="stretch"):
            control.write(Control(not current.paused, current.speed, current.step))
            st.rerun()
        if col2.button("⏭ Step 4h", width="stretch", disabled=not current.paused,
                       help="While paused: play one 4-hour candle, then pause again."):
            control.write(Control(True, current.speed, current.step + 1))
            st.rerun()
        options = SPEEDS if speed_label in SPEEDS else sorted(
            [*SPEEDS[:-1], speed_label], key=float) + ["max"]

        def change_speed() -> None:
            # Only runs when you move the slider, never just because the page loaded.
            chosen = st.session_state["speed"]
            latest = control.read()
            control.write(Control(latest.paused, None if chosen == "max" else float(chosen), latest.step))

        st.select_slider("Speed (x real time)", options, value=speed_label, key="speed", on_change=change_speed)

# ---------------------------------------------------------------- page

@st.cache_resource
def connection(path: str, file_id: int):
    """One read-only connection per database file, reused across refreshes. `file_id` (the file's inode)
    changes when a new replay recreates the file, which opens a fresh connection."""
    return data.open_readonly(Path(path))


db_path = sources[source]
try:
    conn = connection(str(db_path), db_path.stat().st_ino)
except FileNotFoundError:
    st.info("Nothing to show yet. Start the demo with `uv run scout demo`, or a replay with "
            "`uv run scout replay --from 2025-01-01 --to 2025-03-31 --speed 2000x`.")
    st.stop()


@st.fragment(run_every=3 if auto else None)
def page() -> None:
    o = data.overview(conn, usd_per_aud)

    # Top line
    colour = {"RUNNING": "green", "PAUSED": "orange", "KILLED": "red"}.get(o["state"], "gray")
    version_note = "" if o["written_by"] in (None, APP_VERSION) else f" (data written by v{o['written_by']})"
    st.markdown(f"### :{colour}[{o['state']}] · {o['mode']} · Scout v{APP_VERSION}{version_note}")
    if o["replay"]:
        r = o["replay"]
        span = max(1.0, (r["to_ms"] or 0) - (r["from_ms"] or 0))
        done = min(1.0, max(0.0, ((r["now_ms"] or r["from_ms"]) - r["from_ms"]) / span))
        status = "⏸ paused" if r["paused"] else f"▶ {r['speed']}x" if r["speed"] != "max" else "▶ max speed"
        st.progress(done, text=f"Replay {when(r['from_ms'], tz)} → {when(r['to_ms'], tz)} · now "
                               f"{when(r['now_ms'], tz)} · {status} · {r['status'] or ''}")
    if o["killed_reason"] and o["state"] == "KILLED":
        st.error(md(f"Kill switch: {o['killed_reason']}"))
    if o["daily_limit_hit"]:
        st.warning("Daily loss limit hit: no new trades until midnight (Sydney).")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Balance", "—" if o["equity_aud"] is None else f"A${o['equity_aud']:,.0f}",
              help=f"Cash plus open positions, in AUD: {aud(o['equity_aud'])}.")
    c2.metric("Today", aud(o["today_aud"], True), pct(o["today_pct"]), help="Since midnight Sydney time.")
    c3.metric("Total", pct(o["total_pct"]), help=f"Since the start ({aud(o['initial_aud'])}).")
    c4.metric("BTC held instead", pct(o["btc_pct"]),
              None if o["total_pct"] is None or o["btc_pct"] is None
              else f"{o['total_pct'] - o['btc_pct']:+.2f} pts for Scout",
              help="Same starting money in Bitcoin, held the whole time.")
    st.caption(f"As of {when(o['as_of_ms'], tz)} (Sydney) · {db_path.name}")

    live, trades, glossary = st.tabs(["📈 Now", "📜 Trade history", "❓ What does this mean?"])

    with live:
        left, right = st.columns([3, 2])
        with left:
            st.subheader("Market mood")
            mood = data.latest_mood(conn)
            if mood:
                st.markdown(f"**:{'green' if mood['regime'] == 'RISK_ON' else 'red' if mood['regime'] == 'RISK_OFF' else 'gray'}"
                            f"[{mood['regime']}]** · volatility **{mood['volatility']}** · checked {when(mood['ts_ms'], tz)}")
                st.write(md(mood["summary"]))
                with st.expander("Why (the checks behind it)"):
                    for reason in mood["reasons"]:
                        st.markdown(md(f"- {reason}"))
                history = data.mood_history(conn, str(tz))
                if len(history) > 1:
                    bands = alt.Chart(history).mark_rect(opacity=0.25).encode(
                        x=alt.X("time:T", title=None), x2="until:T",
                        color=alt.Color("mood:N", scale=alt.Scale(domain=list(MOOD_COLOURS),
                                                                  range=list(MOOD_COLOURS.values())),
                                        legend=alt.Legend(orient="bottom", title=None)),
                    )
                    line = alt.Chart(history.dropna(subset=["btc_price"])).mark_line(color="black").encode(
                        x="time:T", y=alt.Y("btc_price:Q", title="BTC price (US$)", scale=alt.Scale(zero=False)),
                        tooltip=["time:T", "mood:N", "volatility:N", alt.Tooltip("btc_price:Q", format=",.0f")],
                    )
                    st.altair_chart((bands + line).properties(height=220), width="stretch")
            else:
                st.info("No mood reading yet.")

            st.subheader("Account value vs just holding BTC")
            curve = data.equity_curve(conn, usd_per_aud, str(tz))
            if len(curve) > 1:
                long_form = curve.melt(id_vars="time", value_vars=["Scout", "Just holding BTC"], var_name="series",
                                       value_name="aud").dropna()
                chart = alt.Chart(long_form).mark_line().encode(
                    x=alt.X("time:T", title=None), y=alt.Y("aud:Q", title="A$", scale=alt.Scale(zero=False)),
                    color=alt.Color("series:N", scale=alt.Scale(domain=["Scout", "Just holding BTC"],
                                                                range=["#1f77b4", "#f7931a"]),
                                    legend=alt.Legend(orient="bottom", title=None)),
                    tooltip=["time:T", "series:N", alt.Tooltip("aud:Q", title="A$", format=",.2f")],
                )
                st.altair_chart(chart.properties(height=240), width="stretch")
            else:
                st.caption("The chart appears after a few account snapshots.")

        with right:
            st.subheader("Open positions")
            positions = data.open_positions(conn, usd_per_aud, str(tz))
            if positions.empty:
                st.caption("No open positions.")
            for _, p in positions.iterrows():
                good = p["pnl_aud"] >= 0
                st.markdown(md(f"**#{p['id']} {p['coin']}** ({p['side']}) · entry ${format_price(p['entry'])} · now "
                               f"${format_price(p['now'])} · stop ${format_price(p['stop'])}") + " · "
                            + f":{'green' if good else 'red'}[{md(aud(p['pnl_aud'], True))} ({p['pnl_pct']:+.1f}%)]")
                st.caption(md(f"Opened {p['opened']:%d %b %H:%M}. {p['why']}"))

            ts, short = data.shortlist(conn, cfg.scanner.max_coins, cfg.scanner.min_score)
            st.subheader("Coin shortlist")
            if ts is None:
                st.caption("No scan yet.")
            else:
                st.caption(f"Scanned {when(ts, tz)}. Only shortlisted coins can be bought.")
                st.dataframe(short, hide_index=True, width="stretch", column_config={
                    "rank": st.column_config.NumberColumn("#", format="%d"),
                    "score": st.column_config.NumberColumn("Score", format="%+.1f"),
                    "note": st.column_config.TextColumn("Why", width="large"),
                    "shortlisted": st.column_config.CheckboxColumn("On list"),
                })

            st.subheader("Messages")
            msgs = data.messages(conn, str(tz), limit=8)
            if msgs.empty:
                st.caption("No messages yet.")
            for _, m in msgs.iterrows():
                note = {"sent": "", "pending": " (waiting)", "disabled": " (not sent: iMessage off / replay)",
                        "merged": " (sent in a digest)", "failed": " (failed)"}.get(m["status"], "")
                st.caption(f"{m['time']:%d %b %H:%M}{note}")
                st.text(m["text"])

    with trades:
        history = data.trade_history(conn, usd_per_aud, str(tz))
        if history.empty:
            st.info("No closed trades yet.")
        else:
            wins = (history["pnl_aud"] > 0).mean() * 100
            hint = "uv run scout explain <id>" + (" --replay" if source == "Replay" else "")
            st.markdown(md(f"**{len(history)} closed trades** · total {aud(history['pnl_aud'].sum(), True)} · "
                           f"{wins:.0f}% winners. For the full story of one trade: `{hint}`"))
            st.dataframe(
                history[["id", "coin", "side", "opened", "closed", "entry", "exit", "pnl_aud", "pnl_pct",
                         "why_opened", "why_closed"]],
                hide_index=True, width="stretch",
                column_config={
                    "pnl_aud": st.column_config.NumberColumn("Result (A$)", format="%+.2f"),
                    "pnl_pct": st.column_config.NumberColumn("Result %", format="%+.1f%%"),
                    "opened": st.column_config.DatetimeColumn("Opened", format="D MMM HH:mm"),
                    "closed": st.column_config.DatetimeColumn("Closed", format="D MMM HH:mm"),
                    "why_opened": st.column_config.TextColumn("Why it opened", width="large"),
                    "why_closed": st.column_config.TextColumn("Why it closed", width="large"),
                },
            )
        events = data.risk_events(conn, str(tz))
        if not events.empty:
            st.subheader("Risk manager log")
            for _, e in events.iterrows():
                st.caption(md(f"{e['time']:%d %b %H:%M} · {e['message']}"))

    with glossary:
        st.write("Every term on this page, in plain English.")
        for term, meaning in data.GLOSSARY:
            st.markdown(md(f"**{term}** — {meaning}"))


page()
