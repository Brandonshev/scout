"""The public status page: built read-only from the account databases, published to its own branch."""

import subprocess
from pathlib import Path

import pytest

from scout import status_page
from scout.db import open_db
from scout.demo import DemoEngine
from scout.service import build_plist, log_name, status_label
from tests.test_experiment import Clock, Outbox

pytestmark = pytest.mark.anyio


@pytest.fixture
def account(settings, tmp_path):
    s = settings.model_copy(update={"app": settings.app.model_copy(update={"db_path": tmp_path / "a.db"})})
    conn = open_db(s.app.db_path)
    clock = Clock()
    engine = DemoEngine(s, conn, clock=clock)
    engine.notifier = Outbox()
    engine.prices.update({"ETH": 2000.0, "BTC": 80_000.0}, clock())
    yield s, engine, clock
    conn.close()


async def test_page_shows_balance_positions_and_closed_trades(account):
    s, engine, clock = account
    await engine.open_position("ETH", "long", 0.05, "Bought ETH | because it broke out.", strategy="core", stop=1900.0)
    await engine.open_position("BTC", "long", 0.001, "Bought BTC.", strategy="core", stop=76_000.0)
    btc = next(x for x in engine.account.positions() if x.coin == "BTC")
    await engine.close(btc, "Selling BTC: the stop loss was hit.")
    db = s.app.db_path
    before = db.stat().st_mtime_ns
    text = status_page.build_page([status_page.AccountInfo("Breakout:1", "Breakouts.", s)],
                                  {"ETH": 2100.0, "BTC": 80_000.0}, clock(), s, "abc1234")
    assert text.startswith("# Scout status") and "code version `abc1234`" in text
    assert "## Breakout:1" in text and "*Breakouts.*" in text
    assert "| ETH | long |" in text and "Bought ETH / because it broke out." in text  # | can't break the table
    assert "**Closed trades:** 1 (0 won)" in text and "the stop loss was hit" in text
    assert db.stat().st_mtime_ns == before  # read-only: the account wasn't touched


def test_an_account_that_has_not_started(settings, tmp_path):
    s = settings.model_copy(update={"app": settings.app.model_copy(update={"db_path": tmp_path / "none.db"})})
    text = status_page.build_page([status_page.AccountInfo("SMART:3", "x", s)], {}, 0, s)
    assert "Not started yet." in text and not (tmp_path / "none.db").exists()


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def test_publish_replaces_one_commit_on_its_own_branch(tmp_path):
    remote, repo = tmp_path / "remote.git", tmp_path / "repo"
    git(tmp_path, "init", "--bare", "-q", str(remote))
    git(tmp_path, "init", "-q", "-b", "main", str(repo))
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "T")
    (repo / "code.py").write_text("x = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "code")
    git(repo, "remote", "add", "origin", str(remote))
    status_page.publish(repo, "first\n")
    status_page.publish(repo, "second\n")
    assert git(remote, "show", "status:STATUS.md") == "second"
    assert git(remote, "rev-list", "--count", "status") == "1"  # no pile-up of hourly commits
    assert git(remote, "ls-tree", "--name-only", "status") == "STATUS.md"
    assert git(repo, "status", "--porcelain") == "" and git(repo, "branch", "--show-current") == "main"


def test_publish_failure_is_reported(tmp_path):
    repo = tmp_path / "repo"
    git(tmp_path, "init", "-q", str(repo))
    with pytest.raises(RuntimeError, match="push failed"):
        status_page.publish(repo, "x\n")  # no remote


def test_hourly_job_plist(settings):
    label = status_label(settings)
    plist = build_plist(settings, Path("/bin/scout"), Path("/p"), Path("/p/config.yaml"), Path("/p/.env"),
                        command=("status-page", "make", "--publish"), label=label, every_seconds=3600)
    assert plist["StartInterval"] == 3600 and "KeepAlive" not in plist
    assert plist["ProgramArguments"][1:4] == ["status-page", "make", "--publish"]
    assert log_name(settings, label) == "status" and plist["StandardOutPath"].endswith("status.out.log")
