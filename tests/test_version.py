"""Guards against exactly the drift that shipped as v8.0.1: pyproject.toml's
version and rss_ticker.__version__ (what the running app actually reports,
in /api/health and the outbound User-Agent) are two independent strings with
nothing tying them together, so a version bump that only touches one of them
compiles, tests, and ships fine -- and silently reports the old version.
"""
import tomllib
from pathlib import Path

from rss_ticker import __version__


def test_package_version_matches_pyproject():
    pyproject = tomllib.loads(
        (Path(__file__).parent.parent / "pyproject.toml").read_text()
    )
    assert __version__ == pyproject["project"]["version"]
