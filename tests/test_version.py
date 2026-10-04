import re
from importlib.metadata import version

from scout import APP_VERSION


def test_version_is_semver():
    assert re.fullmatch(r"\d+\.\d+\.\d+", APP_VERSION)


def test_package_metadata_matches_app_version():
    # pyproject reads its version from scout/version.py, so they can't drift apart.
    assert version("scout") == APP_VERSION
