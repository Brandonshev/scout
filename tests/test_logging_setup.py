import logging
import re

from scout import APP_VERSION
from scout.logging_setup import setup_logging


def test_log_lines_have_sydney_time_version_and_mode(settings):
    log_file = setup_logging(settings)
    logging.getLogger("scout.test").info("hello from the test")
    for handler in logging.getLogger().handlers:
        handler.flush()

    line = log_file.read_text(encoding="utf-8").strip().splitlines()[-1]
    assert f"| v{APP_VERSION} |" in line
    assert "| DEMO |" in line
    assert "hello from the test" in line
    # Sydney is UTC+10 (AEST) or UTC+11 (AEDT, daylight saving)
    assert re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}\+1[01]:00 ", line)


def test_setup_twice_does_not_duplicate_lines(settings):
    setup_logging(settings)
    log_file = setup_logging(settings)
    logging.getLogger("scout.test").warning("only once")
    for handler in logging.getLogger().handlers:
        handler.flush()
    assert log_file.read_text(encoding="utf-8").count("only once") == 1
