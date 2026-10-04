"""ntfy push alerts. The ntfy server is always faked: nothing is really sent."""

import json
import re
import stat
from contextlib import closing

import httpx
import pytest
from pydantic import ValidationError

from scout import doctor
from scout.config import load_settings
from scout.db import open_db
from scout.notify import IMessageBackend, Notifier, NotifyError, NtfyBackend, Priority, backends_from_settings

pytestmark = pytest.mark.anyio
TOPIC = "scout-abcd-efgh-jkmn-pqrs"


class FakeNtfy:
    def __init__(self, status=200, body='{"id":"x"}'):
        self.requests: list[httpx.Request] = []
        self.status = status
        self.body = body

    def transport(self):
        return httpx.MockTransport(self.handle)

    def handle(self, request):
        self.requests.append(request)
        return httpx.Response(self.status, text=self.body)

    def payloads(self):
        return [json.loads(r.content) for r in self.requests]


# ------------------------------------------------------------ the backend


async def test_sends_json_with_title_message_and_priority():
    fake = FakeNtfy()
    backend = NtfyBackend("https://ntfy.sh/", TOPIC, transport=fake.transport())
    await backend.send("Scout v0.11.0 · DEMO\n🟢 BOUGHT 0.08 ETH at $2,001.00\nWhy: breakout.", Priority.NORMAL)
    [request] = fake.requests
    assert str(request.url) == "https://ntfy.sh/"
    assert fake.payloads()[0] == {
        "topic": TOPIC, "title": "Scout v0.11.0 · DEMO",
        "message": "🟢 BOUGHT 0.08 ETH at $2,001.00\nWhy: breakout.", "priority": 3,
    }
    assert "authorization" not in request.headers


@pytest.mark.parametrize(("priority", "ntfy"), [(Priority.BATCH, 2), (Priority.NORMAL, 3), (Priority.CRITICAL, 5)])
async def test_priorities(priority, ntfy):
    fake = FakeNtfy()
    await NtfyBackend("https://ntfy.sh", TOPIC, transport=fake.transport()).send("t\nm", priority)
    assert fake.payloads()[0]["priority"] == ntfy


async def test_token_for_private_servers():
    fake = FakeNtfy()
    await NtfyBackend("https://ntfy.example.com", "alerts", token="tk_secret", transport=fake.transport()).send("t\nm")
    assert fake.requests[0].headers["authorization"] == "Bearer tk_secret"


async def test_refusals_and_network_errors_raise():
    with pytest.raises(NotifyError, match="429"):
        await NtfyBackend("https://ntfy.sh", TOPIC, transport=FakeNtfy(429, "limit reached").transport()).send("t\nm")

    def down(request):
        raise httpx.ConnectError("offline")

    with pytest.raises(NotifyError, match="couldn't reach"):
        await NtfyBackend("https://ntfy.sh", TOPIC, transport=httpx.MockTransport(down)).send("t\nm")


async def test_kill_switch_arrives_as_urgent_through_the_queue(settings, tmp_path):
    fake = FakeNtfy()
    with closing(open_db(tmp_path / "scout.db")) as conn:
        notifier = Notifier(conn, settings, [NtfyBackend("https://ntfy.sh", TOPIC, transport=fake.transport())])
        notifier.notify("🛑 KILL SWITCH: test", "risk", Priority.CRITICAL)
        assert await notifier.deliver() == 1
    [payload] = fake.payloads()
    assert payload["priority"] == 5 and payload["message"] == "🛑 KILL SWITCH: test"
    assert payload["title"].startswith("Scout v") and payload["title"].endswith("· DEMO")


# ------------------------------------------------------------ settings


def env(tmp_path, text):
    path = tmp_path / ".env"
    path.write_text(text)
    return path


def test_topic_comes_from_env_and_stays_secret(write_config, tmp_path):
    settings = load_settings(write_config("notify:\n  ntfy_enabled: true\n"),
                             env(tmp_path, f'SCOUT_NOTIFY__NTFY_TOPIC="{TOPIC}"\n'))
    assert settings.notify.ntfy_topic.get_secret_value() == TOPIC
    assert TOPIC not in repr(settings)


@pytest.mark.parametrize(("config", "env_text", "match"), [
    ("notify:\n  ntfy_enabled: true\n", "", "ntfy-setup"),
    ("", 'SCOUT_NOTIFY__NTFY_TOPIC="has spaces in it and more"\n', "letters, numbers"),
    ("", 'SCOUT_NOTIFY__NTFY_TOPIC="scout-alerts"\n', "at least 20 random characters"),
])
def test_bad_ntfy_settings_are_rejected(write_config, tmp_path, config, env_text, match):
    with pytest.raises(ValidationError, match=match):
        load_settings(write_config(config), env(tmp_path, env_text))


def test_short_topics_are_fine_on_a_private_server(write_config, tmp_path):
    settings = load_settings(write_config("notify:\n  ntfy_server: https://ntfy.example.com\n"),
                             env(tmp_path, 'SCOUT_NOTIFY__NTFY_TOPIC="alerts"\n'))
    assert settings.notify.ntfy_topic.get_secret_value() == "alerts"


def test_channel_order_and_switched_off_channels(write_config, tmp_path):
    both = env(tmp_path, f'SCOUT_NOTIFY__NTFY_TOPIC="{TOPIC}"\nSCOUT_NOTIFY__IMESSAGE_RECIPIENT="+61412345678"\n')
    on = load_settings(write_config("notify:\n  ntfy_enabled: true\n  imessage_enabled: true\n"), both)
    assert [type(b) for b in backends_from_settings(on)] == [NtfyBackend, IMessageBackend]  # ntfy first
    off = load_settings(write_config(""), both)
    assert backends_from_settings(off) == []
    assert [b.name for b in backends_from_settings(off, include_disabled=True)] == ["ntfy", "imessage"]


# ------------------------------------------------------------ CLI


def test_ntfy_setup_makes_a_secret_topic_and_switches_alerts_on(tmp_path):
    from typer.testing import CliRunner

    from scout.cli import app
    from tests.conftest import REPO_ROOT

    config = tmp_path / "config.yaml"
    # The real file, with its comments, but alerts off (as it ships before anyone runs ntfy-setup)
    config.write_text(re.sub(r"(\n  ntfy_enabled: )true", r"\1false", (REPO_ROOT / "config.yaml").read_text()))
    env_file = tmp_path / ".env"
    env_file.write_text("# my settings\nOTHER=1\n")
    runner = CliRunner()

    result = runner.invoke(app, ["ntfy-setup", "-c", str(config), "--env-file", str(env_file)])
    assert result.exit_code == 0, result.output
    topic = re.search(r'SCOUT_NOTIFY__NTFY_TOPIC="(scout(-[a-z2-9]{4}){4})"', env_file.read_text()).group(1)
    assert topic in result.output and "apps.apple.com" in result.output
    assert "# my settings" in env_file.read_text() and "OTHER=1" in env_file.read_text()
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    assert "  ntfy_enabled: true" in config.read_text()
    assert "# push alerts via the free ntfy iPhone app" in config.read_text()  # comments kept
    assert load_settings(config, env_file).notify.ntfy_enabled

    again = runner.invoke(app, ["ntfy-setup", "-c", str(config), "--env-file", str(env_file)])
    assert "already set up" in again.output and topic in env_file.read_text()  # not replaced by accident
    fresh = runner.invoke(app, ["ntfy-setup", "--new", "-c", str(config), "--env-file", str(env_file)])
    assert fresh.exit_code == 0 and topic not in env_file.read_text()
    assert env_file.read_text().count("SCOUT_NOTIFY__NTFY_TOPIC") == 1


def test_notify_test_through_ntfy(write_config, tmp_path, monkeypatch):
    from typer.testing import CliRunner

    import scout.cli as cli
    from scout.cli import app

    fake = FakeNtfy()
    monkeypatch.setattr(cli, "make_backends", lambda settings, include_disabled=False: [
        NtfyBackend(settings.notify.ntfy_server, settings.notify.ntfy_topic.get_secret_value(),
                    transport=fake.transport())])
    result = CliRunner().invoke(app, ["notify-test", "-c", str(write_config("")), "--env-file",
                                      str(env(tmp_path, f'SCOUT_NOTIFY__NTFY_TOPIC="{TOPIC}"\n'))])
    assert result.exit_code == 0, result.output
    assert "ntfy: sent" in result.output and "Switched off in config.yaml" in result.output
    assert fake.payloads()[0]["message"].startswith("Test message")


# ------------------------------------------------------------ doctor


async def test_doctor_checks_the_ntfy_server(write_config, tmp_path):
    settings = load_settings(write_config("notify:\n  ntfy_enabled: true\n"),
                             env(tmp_path, f'SCOUT_NOTIFY__NTFY_TOPIC="{TOPIC}"\n'))
    async with httpx.AsyncClient(transport=FakeNtfy(200, '{"healthy":true}').transport()) as http:
        assert (await doctor.check_ntfy(settings, http)).status == "ok"
    async with httpx.AsyncClient(transport=FakeNtfy(200, '{"healthy":false}').transport()) as http:
        assert (await doctor.check_ntfy(settings, http)).status == "fail"
    off = load_settings(write_config(""), tmp_path / "missing.env")
    async with httpx.AsyncClient(transport=FakeNtfy().transport()) as http:
        check = await doctor.check_ntfy(off, http)
    assert check.status == "warn" and "ntfy-setup" in check.fix
