"""Scout settings.

Normal settings live in config.yaml; secrets live in .env (never committed).
Any setting can be overridden by an environment variable named
SCOUT_<SECTION>__<KEY>, e.g. SCOUT_RISK__RISK_PER_TRADE_PCT=0.5

Priority (highest wins): environment variables > .env > config.yaml > defaults.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, time
from enum import StrEnum
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

# Candle sizes Scout knows how to use (Hyperliquid supports more).
VALID_TIMEFRAMES = ("15m", "1h", "4h", "1d")


class Mode(StrEnum):
    DEMO = "demo"
    REPLAY = "replay"
    LIVE = "live"


MODE_DESCRIPTIONS = {
    Mode.DEMO: "fake money, real live prices",
    Mode.REPLAY: "fake money, past prices played back quickly",
    Mode.LIVE: "REAL MONEY",
}


def _check_timeframe(value: str) -> str:
    if value not in VALID_TIMEFRAMES:
        raise ValueError(f"{value!r} is not a supported timeframe {VALID_TIMEFRAMES}")
    return value


Timeframe = Annotated[str, AfterValidator(_check_timeframe)]


class Section(BaseModel):
    # Unknown keys are errors, so a typo in config.yaml can't be silently ignored.
    model_config = ConfigDict(extra="forbid")


class AppSettings(Section):
    timezone: str = "Australia/Sydney"
    db_path: Path = Path("data/scout.db")
    log_dir: Path = Path("logs")
    reports_dir: Path = Path("reports")
    backup_dir: Path = Path("data/backups")
    backup_keep_days: int = Field(14, ge=1)
    log_level: str = "INFO"
    log_max_bytes: int = Field(5_000_000, gt=0)
    log_backup_count: int = Field(10, ge=1)

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(f"unknown timezone {value!r}") from None
        return value

    @field_validator("log_level")
    @classmethod
    def _known_log_level(cls, value: str) -> str:
        value = value.upper()
        if value not in logging.getLevelNamesMapping():
            raise ValueError(f"unknown log level {value!r}")
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


class DemoSettings(Section):
    name: str = Field("Breakout:1", min_length=1, max_length=30)  # shown in alerts and reports
    starting_balance_aud: float = Field(1000.0, gt=0)
    aud_to_usdc_rate: float = Field(0.65, gt=0, lt=2)
    tick_seconds: float = Field(5.0, gt=0, le=60)  # how often stops and the account are checked
    snapshot_minutes: int = Field(5, ge=1)  # how often the account value is saved
    cycle_minutes: int = Field(60, ge=5)  # how often mood -> scan -> signals runs

    @property
    def starting_balance_usdc(self) -> float:
        return round(self.starting_balance_aud * self.aud_to_usdc_rate, 2)


class DataSettings(Section):
    api_url: str = "https://api.hyperliquid.xyz/info"
    ws_url: str = "wss://api.hyperliquid.xyz/ws"
    timeframes: list[Timeframe] = Field(default_factory=lambda: ["1h", "4h"], min_length=1)
    request_timeout_seconds: float = Field(10.0, gt=0)
    stale_after_seconds: int = Field(300, gt=0)
    weight_per_minute: int = Field(800, gt=0, le=1200)
    max_retries: int = Field(5, ge=0, le=10)
    backfill_intervals: list[Timeframe] = Field(
        default_factory=lambda: ["15m", "1h", "4h", "1d"], min_length=1
    )
    backfill_days: int = Field(365, ge=1)
    backfill_top_coins: int = Field(10, ge=1)


class RegimeSettings(Section):
    """Market mood thresholds. All based on daily candles."""

    benchmark_coin: str = "BTC"
    update_minutes: int = Field(60, ge=5)
    # Trend
    fast_ma_days: int = Field(50, ge=2)
    slow_ma_days: int = Field(200, ge=2)
    slope_days: int = Field(10, ge=1)
    slope_flat_pct: float = Field(0.25, ge=0)
    # Breadth
    breadth_top_coins: int = Field(30, ge=5)
    breadth_ma_days: int = Field(50, ge=2)
    breadth_min_coins: int = Field(10, ge=1)
    breadth_strong_pct: float = Field(60, gt=0, lt=100)
    breadth_weak_pct: float = Field(40, gt=0, lt=100)
    breadth_weight: int = Field(2, ge=0)
    history_candidate_coins: int = Field(50, ge=5)
    # Volatility
    atr_days: int = Field(14, ge=2)
    volatility_history_days: int = Field(365, ge=30)
    calm_percentile: float = Field(25, gt=0, lt=100)
    wild_percentile: float = Field(80, gt=0, lt=100)
    # Crowding
    crowded_funding_annual_pct: float = Field(30, gt=0)
    # Verdict
    risk_on_min_score: int = 3
    risk_off_max_score: int = -3
    require_btc_above_slow_ma_for_risk_on: bool = True
    # Rules for later steps
    wild_volatility_size_multiplier: float = Field(0.5, gt=0, le=1)

    @model_validator(mode="after")
    def _consistent(self) -> RegimeSettings:
        if self.fast_ma_days >= self.slow_ma_days:
            raise ValueError("fast_ma_days must be smaller than slow_ma_days")
        if self.breadth_weak_pct >= self.breadth_strong_pct:
            raise ValueError("breadth_weak_pct must be below breadth_strong_pct")
        if self.calm_percentile >= self.wild_percentile:
            raise ValueError("calm_percentile must be below wild_percentile")
        if self.risk_off_max_score >= self.risk_on_min_score:
            raise ValueError("risk_off_max_score must be below risk_on_min_score")
        if self.history_candidate_coins < self.breadth_top_coins:
            raise ValueError("history_candidate_coins must be at least breadth_top_coins")
        return self


class ScannerWeights(Section):
    rs_short: float = Field(1.0, ge=0)
    rs_long: float = Field(1.0, ge=0)
    volume: float = Field(0.5, ge=0)
    trend: float = Field(1.0, ge=0)
    volatility: float = Field(1.0, ge=0)


class ScannerSettings(Section):
    update_minutes: int = Field(60, ge=5)
    # Universe and hard filters
    universe_size: int = Field(25, ge=1)
    max_coins: int = Field(10, ge=1)
    min_score: float = 0.0  # coins scoring below this never make the shortlist
    min_24h_volume_usd: float = Field(20_000_000, gt=0)
    min_open_interest_usd: float = Field(10_000_000, ge=0)
    min_listing_days: int = Field(60, ge=0)
    max_spread_pct: float = Field(0.10, gt=0)
    depth_band_pct: float = Field(1.0, gt=0, le=5)
    min_depth_usd: float = Field(100_000, ge=0)
    stablecoins: list[str] = Field(
        default_factory=lambda: ["USDC", "USDT", "USDE", "USDH", "USD1", "DAI", "FDUSD", "PYUSD", "TUSD", "USDS"]
    )
    exclude_coins: list[str] = Field(default_factory=list)
    # Measurements
    rs_short_days: int = Field(7, ge=1)
    rs_long_days: int = Field(30, ge=2)
    rs_threshold_pct: float = Field(1.0, ge=0)
    volume_avg_days: int = Field(20, ge=2)
    volume_surge_ratio: float = Field(1.5, gt=1)
    volume_dry_ratio: float = Field(0.5, gt=0, lt=1)
    fast_ma_days: int = Field(20, ge=2)
    slow_ma_days: int = Field(50, ge=3)
    atr_days: int = Field(14, ge=2)
    high_volatility_atr_pct: float = Field(8.0, gt=0)
    weights: ScannerWeights = Field(default_factory=ScannerWeights)

    @model_validator(mode="after")
    def _consistent(self) -> ScannerSettings:
        if self.fast_ma_days >= self.slow_ma_days:
            raise ValueError("fast_ma_days must be smaller than slow_ma_days")
        if self.rs_short_days >= self.rs_long_days:
            raise ValueError("rs_short_days must be smaller than rs_long_days")
        if self.max_coins > self.universe_size:
            raise ValueError("max_coins can't be larger than universe_size")
        return self

    @property
    def history_days(self) -> int:
        """Daily candles needed per coin."""
        return max(self.min_listing_days, self.slow_ma_days, self.rs_long_days, self.volume_avg_days) + 5


class SignalSettings(Section):
    strategy: str = "breakout"
    # Shorts: off by default, never allowed with real money (see Settings validator).
    allow_shorts: bool = False
    timeframe: Timeframe = "4h"  # candles for breakouts, stops and exits
    # Uptrend (daily): price above its fast and slow averages
    trend_fast_ma_days: int = Field(20, ge=2)
    trend_slow_ma_days: int = Field(50, ge=3)
    # Breakout entry
    breakout_periods: int = Field(20, ge=2)
    volume_avg_periods: int = Field(20, ge=2)
    min_volume_ratio: float = Field(1.2, gt=0)
    rsi_periods: int = Field(14, ge=2)
    max_rsi: float = Field(75, gt=50, le=100)
    min_rsi_short: float = Field(25, ge=0, lt=50)
    # Stops and exits
    atr_periods: int = Field(14, ge=2)
    stop_atr_multiple: float = Field(2.0, gt=0)
    trail_atr_multiple: float = Field(3.0, gt=0)
    exit_ma_periods: int = Field(20, ge=2)
    max_stop_pct: float = Field(10.0, gt=0, le=50)
    candles_needed_days: int = Field(30, ge=5)  # history of signal candles to keep fresh

    @field_validator("strategy")
    @classmethod
    def _known_strategy(cls, value: str) -> str:
        known = ("breakout",)
        if value not in known:
            raise ValueError(f"unknown strategy {value!r} (choose from {', '.join(known)})")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> SignalSettings:
        if self.trend_fast_ma_days >= self.trend_slow_ma_days:
            raise ValueError("trend_fast_ma_days must be smaller than trend_slow_ma_days")
        if self.trail_atr_multiple < self.stop_atr_multiple:
            raise ValueError("trail_atr_multiple should be at least stop_atr_multiple (trailing stops only tighten)")
        return self


class RiskSettings(Section):
    risk_per_trade_pct: float = Field(1.0, gt=0, le=5)
    max_position_pct: float = Field(25.0, gt=0, le=100)
    max_open_positions: int = Field(3, ge=1)
    max_total_exposure_pct: float = Field(75.0, gt=0, le=100)  # all positions together, % of the account
    require_stop: bool = True  # every new position must have a stop loss (only the experiment turns this off)
    stale_price_seconds: int = Field(60, ge=5)  # no new trades on prices older than this
    daily_loss_limit_pct: float = Field(3.0, gt=0, le=20)
    kill_switch_drawdown_pct: float = Field(15.0, gt=0, le=50)
    max_leverage: float = Field(1.0, gt=0, le=1.0)
    taker_fee_pct: float = Field(0.045, ge=0)
    maker_fee_pct: float = Field(0.015, ge=0)
    slippage_pct: float = Field(0.05, ge=0)
    min_order_usd: float = Field(10.0, gt=0)


class WalkForwardSettings(Section):
    train_days: int = Field(180, ge=30)
    test_days: int = Field(90, ge=14)
    min_trades: int = Field(5, ge=1)  # settings with fewer training trades can't be judged
    grid: dict[str, list[int | float]] = Field(
        default_factory=lambda: {"breakout_periods": [10, 20, 40], "stop_atr_multiple": [1.5, 2.0, 3.0]}
    )

    @field_validator("grid")
    @classmethod
    def _grid_keys_are_signal_settings(cls, grid: dict[str, list[int | float]]) -> dict[str, list[int | float]]:
        unknown = set(grid) - set(SignalSettings.model_fields)
        if unknown:
            raise ValueError(f"grid keys must be signals settings; unknown: {', '.join(sorted(unknown))}")
        if any(not values for values in grid.values()):
            raise ValueError("every grid setting needs at least one value")
        return grid


class ServiceSettings(Section):
    """Running unattended with launchd."""

    label: str = "au.scout.demo"
    keep_awake: bool = True  # stop the Mac sleeping while Scout runs (only on mains power; lid must be open)
    restart_alert: bool = True  # iMessage when Scout starts again after a stop or crash
    crash_loop_restarts: int = Field(5, ge=2)  # this many starts within an hour = crash loop...
    crash_loop_pause_minutes: int = Field(15, ge=1)  # ...so wait this long before trying again


class CopySettings(Section):
    """Copy trading: follow wallets whose own recent trades were consistently profitable."""

    wallets: int = Field(10, ge=1, le=10)  # how many wallets to follow (each costs requests every poll)
    poll_seconds: int = Field(20, ge=10)  # how often to check the followed wallets' positions
    min_account_usd: float = Field(100_000, ge=0)
    lookback_days: int = Field(30, ge=7, le=90)
    candidates: int = Field(60, ge=10)  # wallets whose trades are checked in detail each day
    min_closing_trades: int = Field(10, ge=1)
    max_fills: int = Field(2000, ge=100)  # more than this in the lookback = a market-making bot, not copyable
    min_profit_factor: float = Field(1.3, gt=0)
    max_open_loss_pct: float = Field(10.0, gt=0)  # skip wallets sitting on bigger open losses (% of account)
    position_pct: float = Field(7.0, gt=0, le=10)  # of the account, per copied position
    min_wallet_position_pct: float = Field(2.0, ge=0)  # ignore positions smaller than this % of the wallet
    allow_shorts: bool = True  # copy the wallets' shorts too (fake money only)


class HighRiskSettings(Section):
    """High-risk: small coins with a sudden burst of volume and price. Expect most to lose."""

    scan_minutes: int = Field(15, ge=5)
    include_spot: bool = True  # Hyperliquid spot tokens (where most tiny coins live)
    include_perps: bool = True  # low-volume perps outside the main scanner
    min_volume_usd: float = Field(20_000, ge=0)
    max_volume_usd: float = Field(2_000_000, gt=0)
    min_change_pct: float = Field(15.0, gt=0)  # 24h price rise
    volume_surge: float = Field(3.0, gt=1)  # 24h volume vs its usual (when there's history)
    min_exit_depth_usd: float = Field(1_000, ge=0)  # buy orders within 5% of the price, so we can sell
    max_spread_pct: float = Field(3.0, gt=0)
    position_pct: float = Field(6.0, gt=0, le=10)
    take_profit_pct: float = Field(50.0, gt=0)
    max_hold_hours: float = Field(48.0, gt=0)


class ExperimentSettings(Section):
    """A separate fake account testing copy trading and high-risk coins beside the main demo."""

    name: str = Field("70/30:2", min_length=1, max_length=30)  # shown in alerts and reports
    db_path: Path = Path("data/experiment.db")
    starting_balance_aud: float = Field(1000.0, gt=0)
    copy_pct: float = Field(70.0, ge=0, le=100)
    high_risk_pct: float = Field(30.0, ge=0, le=100)
    max_trade_pct: float = Field(10.0, gt=0, le=25)  # hard cap per position
    kill_switch_drawdown_pct: float = Field(50.0, gt=0, le=50)  # last resort only
    wallet_refresh_hours: int = Field(24, ge=1)
    copy_trading: CopySettings = Field(default_factory=CopySettings)
    high_risk: HighRiskSettings = Field(default_factory=HighRiskSettings)

    @model_validator(mode="after")
    def _split(self) -> ExperimentSettings:
        if self.copy_pct + self.high_risk_pct > 100:
            raise ValueError("copy_pct + high_risk_pct can't be more than 100")
        return self


class SmartSettings(Section):
    """SMART:3: Bitcoin trend + a long/short ranking of the most-traded coins, in its own fake account."""

    name: str = Field("SMART:3", min_length=1, max_length=30)
    db_path: Path = Path("data/smart.db")
    starting_balance_aud: float = Field(1000.0, gt=0)
    btc_pct: float = Field(50.0, ge=0, le=100)  # the Bitcoin-trend half
    long_pct: float = Field(25.0, ge=0, le=100)  # spread over the best-ranked coins
    short_pct: float = Field(25.0, ge=0, le=100)  # spread over the worst-ranked coins (fake money only)
    btc_ma_days: int = Field(50, ge=10)
    universe: int = Field(40, ge=15, le=100)  # rank the 40 most-traded coins
    fetch_coins: int = Field(70, ge=20, le=150)  # download this many (by 24h volume) to find them
    picks: int = Field(6, ge=1, le=15)  # hold the best 6 and short the worst 6...
    keep_within: int = Field(12, ge=1)  # ...keeping them while they stay in the best/worst 12
    min_history_days: int = Field(60, ge=30)
    vol_days: int = Field(20, ge=5)
    stop_atr: float = Field(5.0, ge=0)  # emergency stop this many daily ranges away (0 = none)
    rebalance_after_utc: time = time(0, 10)  # just after the daily candle closes (10:10/11:10am Sydney)
    kill_switch_drawdown_pct: float = Field(30.0, gt=0, le=50)

    @field_validator("rebalance_after_utc", mode="before")
    @classmethod
    def _time_quoted(cls, value: object) -> object:
        if isinstance(value, int):
            raise ValueError('put times in quotes in config.yaml, e.g. "20:00"')
        return value

    @model_validator(mode="after")
    def _within_one_x(self) -> SmartSettings:
        if self.btc_pct + self.long_pct + self.short_pct > 100:
            raise ValueError("btc_pct + long_pct + short_pct can't be more than 100 (no borrowing)")
        if self.keep_within < self.picks:
            raise ValueError("keep_within can't be smaller than picks")
        return self


class NewsFeed(Section):
    name: str = Field(min_length=1)
    url: str = Field(pattern=r"^https://")


def _default_feeds() -> list[NewsFeed]:
    return [NewsFeed(name=n, url=u) for n, u in (
        ("CoinDesk", "https://www.coindesk.com/arc/outboundfeeds/rss/"),
        ("Cointelegraph", "https://cointelegraph.com/rss"),
        ("Decrypt", "https://decrypt.co/feed"),
        ("The Block", "https://www.theblock.co/rss.xml"),
        ("Bitcoin Magazine", "https://bitcoinmagazine.com/.rss/full/"),
    )]


class NewsSettings(Section):
    """Free crypto news headlines. Used by the experiment only (the main demo's test stays untouched)."""

    enabled: bool = True
    feeds: list[NewsFeed] = Field(default_factory=_default_feeds)
    refresh_minutes: int = Field(10, ge=5)  # be polite to free feeds
    lookback_hours: int = Field(48, ge=1, le=336)  # a warning counts for this long
    timeout_seconds: float = Field(15.0, gt=0)


class TaxSettings(Section):
    fx_url: str = "https://www.rba.gov.au/statistics/tables/csv/f11.1-data.csv"


class BacktestSettings(Section):
    # Fees and slippage come from the risk section, the same numbers the live bot uses.
    funding_rate_hourly_pct: float = Field(0.002, ge=0)  # flat funding paid by longs per hour
    warmup_days: int = Field(30, ge=5)  # signal-candle history needed before the first trade
    walk_forward: WalkForwardSettings = Field(default_factory=WalkForwardSettings)


class NotifySettings(Section):
    imessage_enabled: bool = False
    # Your iPhone's number (+614...) or Apple ID email. Secret: set it in .env as
    # SCOUT_NOTIFY__IMESSAGE_RECIPIENT, never in config.yaml.
    imessage_recipient: SecretStr | None = None
    # ntfy push notifications (free iPhone app). The topic works like a password: set it in .env
    # as SCOUT_NOTIFY__NTFY_TOPIC (`scout ntfy-setup` makes one). A token is only for private servers.
    ntfy_enabled: bool = False
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: SecretStr | None = None
    ntfy_token: SecretStr | None = None
    # Every account sends an update at each of these times (Sydney).
    update_times: list[time] = Field(default_factory=lambda: [time(8, 0), time(20, 0)], min_length=1)
    quiet_hours_start: time = time(23, 0)
    quiet_hours_end: time = time(7, 0)
    batch_minutes: int = Field(60, ge=1)
    weekly_report_day: str = "sunday"
    weekly_report_time: time = time(19, 0)
    min_seconds_between: int = Field(10, ge=0)
    max_per_hour: int = Field(20, ge=1)
    retry_attempts: int = Field(3, ge=1, le=10)
    retry_seconds: int = Field(30, ge=1)
    feed_down_seconds: int = Field(120, ge=30)
    max_length: int = Field(600, ge=100, le=2000)

    @field_validator("imessage_recipient")
    @classmethod
    def _looks_like_a_phone_or_email(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        text = value.get_secret_value().strip()
        phone = re.fullmatch(r"\+\d{8,15}", text.replace(" ", ""))
        email = re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", text)
        if not (phone or email):
            raise ValueError("must be a phone number in international format (e.g. +61412345678) or an Apple ID email")
        return SecretStr(text.replace(" ", "") if phone else text)

    @field_validator("ntfy_topic")
    @classmethod
    def _valid_topic(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and not re.fullmatch(r"[-_A-Za-z0-9]{1,64}", value.get_secret_value()):
            raise ValueError("ntfy topics may only use letters, numbers, - and _ (up to 64 characters)")
        return value

    @model_validator(mode="after")
    def _recipient_needed(self) -> NotifySettings:
        if self.imessage_enabled and self.imessage_recipient is None:
            raise ValueError("imessage_enabled is true but SCOUT_NOTIFY__IMESSAGE_RECIPIENT isn't set in .env")
        if self.ntfy_enabled and self.ntfy_topic is None:
            raise ValueError("ntfy_enabled is true but SCOUT_NOTIFY__NTFY_TOPIC isn't set in .env "
                             "(run `uv run scout ntfy-setup`)")
        public = "ntfy.sh" in self.ntfy_server and self.ntfy_token is None
        if public and self.ntfy_topic is not None and len(self.ntfy_topic.get_secret_value()) < 20:
            raise ValueError("on the public ntfy.sh server anyone who guesses the topic can read your alerts: "
                             "use at least 20 random characters (`uv run scout ntfy-setup` makes one)")
        return self

    @field_validator("weekly_report_day")
    @classmethod
    def _weekday(cls, value: str) -> str:
        days = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
        if value.lower() not in days:
            raise ValueError(f"must be a day of the week ({', '.join(days)})")
        return value.lower()

    @field_validator("update_times", "quiet_hours_start", "quiet_hours_end", "weekly_report_time", mode="before")
    @classmethod
    def _times_must_be_quoted(cls, value: object) -> object:
        # Unquoted 20:00 in YAML becomes the integer 1200, which pydantic would
        # happily read as "1200 seconds past midnight" (00:20). Refuse it.
        if isinstance(value, int) or (isinstance(value, list) and any(isinstance(v, int) for v in value)):
            raise ValueError('put times in quotes in config.yaml, e.g. "20:00"')
        return value


class _ScoutDotEnvSource(DotEnvSettingsSource):
    """Read .env but skip lines that belong to other programs (no SCOUT_ prefix),
    so the strict typo check only applies to Scout's own settings."""

    def __call__(self) -> dict[str, object]:
        data = super().__call__()
        return {key: value for key, value in data.items() if key in self.settings_cls.model_fields}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SCOUT_",
        env_nested_delimiter="__",
        env_file=".env",
        yaml_file="config.yaml",
        extra="forbid",
    )

    mode: Mode = Mode.DEMO
    app: AppSettings = Field(default_factory=AppSettings)
    demo: DemoSettings = Field(default_factory=DemoSettings)
    data: DataSettings = Field(default_factory=DataSettings)
    regime: RegimeSettings = Field(default_factory=RegimeSettings)
    scanner: ScannerSettings = Field(default_factory=ScannerSettings)
    signals: SignalSettings = Field(default_factory=SignalSettings)
    risk: RiskSettings = Field(default_factory=RiskSettings)
    backtest: BacktestSettings = Field(default_factory=BacktestSettings)
    service: ServiceSettings = Field(default_factory=ServiceSettings)
    experiment: ExperimentSettings = Field(default_factory=ExperimentSettings)
    news: NewsSettings = Field(default_factory=NewsSettings)
    smart: SmartSettings = Field(default_factory=SmartSettings)
    tax: TaxSettings = Field(default_factory=TaxSettings)
    notify: NotifySettings = Field(default_factory=NotifySettings)

    @field_validator("mode")
    @classmethod
    def _live_not_available(cls, value: Mode) -> Mode:
        if value is Mode.LIVE:
            raise ValueError(
                "live trading can't be enabled from config. It isn't built until v1.0.0 "
                "and needs several weeks of good demo results first."
            )
        return value

    @model_validator(mode="after")
    def _no_real_money_shorts(self) -> Settings:
        if self.mode is Mode.LIVE and self.signals.allow_shorts:
            raise ValueError("shorts are only allowed with fake money (demo/replay), never live")
        return self

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            _ScoutDotEnvSource(settings_cls),
            YamlConfigSettingsSource(settings_cls),
        )


def _anchor(path: Path, base: Path) -> Path:
    path = path.expanduser()
    return path if path.is_absolute() else base / path


def load_settings(
    config_path: Path | str = "config.yaml", env_file: Path | str | None = ".env"
) -> Settings:
    """Load and validate settings. Relative paths are resolved next to config.yaml,
    so Scout behaves the same whichever folder it is started from (e.g. by launchd)."""
    config_path = Path(config_path).expanduser().resolve()
    if not config_path.is_file():
        raise FileNotFoundError(f"config file not found: {config_path}")

    class _LoadedSettings(Settings):
        model_config = SettingsConfigDict(yaml_file=config_path, env_file=env_file)

    settings = _LoadedSettings()
    base = config_path.parent
    settings.app.db_path = _anchor(settings.app.db_path, base)
    settings.app.log_dir = _anchor(settings.app.log_dir, base)
    settings.app.reports_dir = _anchor(settings.app.reports_dir, base)
    settings.app.backup_dir = _anchor(settings.app.backup_dir, base)
    settings.experiment.db_path = _anchor(settings.experiment.db_path, base)
    settings.smart.db_path = _anchor(settings.smart.db_path, base)
    return settings


def summary_lines(settings: Settings) -> list[str]:
    """Plain-English summary of the settings, for startup and config-check."""
    s = settings
    now = datetime.now(s.app.tz)
    channels = [name for name, on in (("ntfy", s.notify.ntfy_enabled), ("iMessage", s.notify.imessage_enabled)) if on]
    notify = " + ".join(channels) + " (set in .env)" if channels else "off"
    return [
        f"Mode:        {s.mode.value.upper()} — {MODE_DESCRIPTIONS[s.mode]}",
        f"Demo money:  A${s.demo.starting_balance_aud:,.2f} ≈ {s.demo.starting_balance_usdc:,.2f} USDC "
        f"(at {s.demo.aud_to_usdc_rate} AUD→USD)",
        f"Local time:  {now:%a %d %b %Y %H:%M %Z} ({s.app.timezone})",
        f"Market data: Hyperliquid, {' + '.join(s.data.timeframes)} candles",
        f"Mood:        {s.regime.benchmark_coin} daily {s.regime.fast_ma_days}/{s.regime.slow_ma_days}-day averages, "
        f"breadth of top {s.regime.breadth_top_coins}, volatility, funding; every {s.regime.update_minutes} min",
        f"Scanner:     best {s.scanner.max_coins} of the top {s.scanner.universe_size} coins with ≥ "
        f"US${s.scanner.min_24h_volume_usd:,.0f} 24h volume, spread ≤ {s.scanner.max_spread_pct}%",
        f"Signals:     {s.signals.strategy} on {s.signals.timeframe} candles, "
        f"{'longs + shorts (fake money only)' if s.signals.allow_shorts else 'long-only'}, "
        f"stop {s.signals.stop_atr_multiple:g}×ATR, trail {s.signals.trail_atr_multiple:g}×ATR",
        f"Risk:        {s.risk.risk_per_trade_pct}% per trade, max {s.risk.max_open_positions} positions, "
        f"daily loss limit {s.risk.daily_loss_limit_pct}%, kill switch at {s.risk.kill_switch_drawdown_pct}% "
        f"drawdown, leverage {s.risk.max_leverage:g}x",
        f"Costs:       taker fee {s.risk.taker_fee_pct}%, maker fee {s.risk.maker_fee_pct}%, "
        f"slippage {s.risk.slippage_pct}%",
        f"Alerts:      {notify}; updates at {', '.join(f'{t:%H:%M}' for t in s.notify.update_times)}, "
        f"quiet {s.notify.quiet_hours_start:%H:%M}–{s.notify.quiet_hours_end:%H:%M} (except the kill switch)",
        f"Database:    {s.app.db_path}",
        f"Logs:        {s.app.log_dir}",
    ]
