from datetime import time

import pytest
from pydantic import ValidationError

from scout.config import Mode, load_settings, summary_lines
from tests.conftest import REPO_ROOT


def test_shipped_config_yaml_is_valid(tmp_path):
    # The shipped file is also your live config, so give it the secrets it may need (e.g. an ntfy topic).
    env = tmp_path / ".env"
    env.write_text('SCOUT_NOTIFY__NTFY_TOPIC="scout-test-test-test-test"\n')
    settings = load_settings(REPO_ROOT / "config.yaml", env)
    assert settings.mode is Mode.DEMO
    assert settings.demo.starting_balance_aud == 1000
    assert settings.data.timeframes == ["1h", "4h"]
    assert settings.signals.allow_shorts is False
    assert settings.risk.max_leverage == 1.0
    assert settings.notify.update_times == [time(8, 0), time(20, 0)]
    assert settings.notify.quiet_hours_start == time(23, 0)
    assert settings.notify.quiet_hours_end == time(7, 0)


def test_defaults_when_config_is_empty(settings):
    assert settings.mode is Mode.DEMO
    assert settings.app.timezone == "Australia/Sydney"
    assert settings.demo.starting_balance_usdc == 650.0


def test_yaml_overrides_defaults(write_config, no_env_file):
    settings = load_settings(write_config("demo:\n  starting_balance_aud: 500\n"), no_env_file)
    assert settings.demo.starting_balance_aud == 500


def test_env_var_overrides_yaml(write_config, no_env_file, monkeypatch):
    monkeypatch.setenv("SCOUT_DEMO__STARTING_BALANCE_AUD", "250")
    settings = load_settings(write_config("demo:\n  starting_balance_aud: 500\n"), no_env_file)
    assert settings.demo.starting_balance_aud == 250


def test_dotenv_file_overrides_yaml(write_config, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("SCOUT_RISK__RISK_PER_TRADE_PCT=0.5\n", encoding="utf-8")
    settings = load_settings(write_config(""), env_file)
    assert settings.risk.risk_per_trade_pct == 0.5


def test_dotenv_lines_for_other_programs_are_ignored(write_config, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("OTHER_APP_TOKEN=abc\nSCOUT_DEMO__STARTING_BALANCE_AUD=750\n", encoding="utf-8")
    settings = load_settings(write_config(""), env_file)
    assert settings.demo.starting_balance_aud == 750


def test_relative_paths_resolve_next_to_config(settings, tmp_path):
    assert settings.app.db_path == tmp_path / "data" / "scout.db"
    assert settings.app.log_dir == tmp_path / "logs"


def test_missing_config_file_is_an_error(tmp_path, no_env_file):
    with pytest.raises(FileNotFoundError):
        load_settings(tmp_path / "nope.yaml", no_env_file)


@pytest.mark.parametrize(
    "body",
    [
        "mode: live\n",
        "risk:\n  max_leverage: 2\n",
        "signals:\n  max_rsi: 150\n",
        "signals:\n  strategy: magic\n",
        "notify:\n  update_times: [20:00]\n",  # unquoted time -> YAML integer 1200
        "risks:\n  risk_per_trade_pct: 1\n",  # typo'd section name
        "risk:\n  risk_per_trade: 1\n",  # typo'd key
        "app:\n  timezone: Sydney/Australia\n",
        "data:\n  timeframes: ['3h']\n",
        "regime:\n  fast_ma_days: 200\n  slow_ma_days: 50\n",
        "regime:\n  breadth_weak_pct: 70\n",
        "regime:\n  wild_volatility_size_multiplier: 2\n",
        "demo:\n  starting_balance_aud: 0\n",
    ],
)
def test_unsafe_or_invalid_config_is_rejected(write_config, no_env_file, body):
    with pytest.raises(ValidationError):
        load_settings(write_config(body), no_env_file)


def test_live_mode_cannot_be_enabled_by_env_var(settings, write_config, no_env_file, monkeypatch):
    monkeypatch.setenv("SCOUT_MODE", "live")
    with pytest.raises(ValidationError, match="v1.0.0"):
        load_settings(write_config(""), no_env_file)


def test_summary_is_plain_english(settings):
    text = "\n".join(summary_lines(settings))
    assert "DEMO — fake money, real live prices" in text
    assert "A$1,000.00" in text
    assert "leverage 1x" in text


def test_shorts_can_be_enabled_for_fake_money(write_config, no_env_file):
    settings = load_settings(write_config("signals:\n  allow_shorts: true\n"), no_env_file)
    assert settings.signals.allow_shorts is True


def test_imessage_recipient_comes_from_env_and_stays_secret(write_config, tmp_path):
    env = tmp_path / ".env"
    env.write_text('SCOUT_NOTIFY__IMESSAGE_RECIPIENT="+61 412 345 678"\n')
    settings = load_settings(write_config("notify:\n  imessage_enabled: true\n"), env)
    assert settings.notify.imessage_recipient.get_secret_value() == "+61412345678"
    assert "+61412345678" not in repr(settings)
    assert "+61412345678" not in "\n".join(summary_lines(settings))


@pytest.mark.parametrize("recipient", ["0412345678", "not an email", "+61"])
def test_bad_recipients_are_rejected(write_config, tmp_path, recipient):
    env = tmp_path / ".env"
    env.write_text(f'SCOUT_NOTIFY__IMESSAGE_RECIPIENT="{recipient}"\n')
    with pytest.raises(ValidationError, match="international format"):
        load_settings(write_config(""), env)


def test_imessage_on_needs_a_recipient(write_config, no_env_file):
    with pytest.raises(ValidationError, match="SCOUT_NOTIFY__IMESSAGE_RECIPIENT"):
        load_settings(write_config("notify:\n  imessage_enabled: true\n"), no_env_file)
