import json
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from scout.config import Settings, load_settings
from scout.logging_setup import reset_logging

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "hyperliquid"


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep the real environment and any real log handlers out of tests."""
    import os

    for key in list(os.environ):
        if key.startswith("SCOUT_"):
            monkeypatch.delenv(key)
    yield
    reset_logging()


@pytest.fixture
def write_config(tmp_path: Path) -> Callable[[str], Path]:
    """Write a config.yaml into a temp folder (logs and db stay inside it)."""

    def _write(body: str = "") -> Path:
        path = tmp_path / "config.yaml"
        path.write_text(body, encoding="utf-8")
        return path

    return _write


@pytest.fixture
def no_env_file(tmp_path: Path) -> Path:
    return tmp_path / "missing.env"


@pytest.fixture
def settings(write_config: Callable[[str], Path], no_env_file: Path) -> Settings:
    return load_settings(write_config(""), no_env_file)
