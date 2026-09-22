"""Keep the test suite out of the user's real data.

main.py resolves statusify.log, statusify.cfg, history and caches from
_APP_DIR at import time. Without this, every test run appended fixture
tracks ("A — n3wn", "ARTIST — TITLE") to the real statusify.log.
STATUSIFY_DATA_DIR must be set before anything imports main, and conftest.py
is loaded before any test module.
"""
import os
import shutil
import tempfile

_DATA_DIR = tempfile.mkdtemp(prefix="statusify-tests-")
os.environ["STATUSIFY_DATA_DIR"] = _DATA_DIR


def pytest_unconfigure(config):
    shutil.rmtree(_DATA_DIR, ignore_errors=True)


import sys

import pytest


@pytest.fixture(autouse=True)
def _no_lrclib_network(monkeypatch):
    """No test may reach lrclib.net; tests that exercise it stub the search."""
    main = sys.modules.get("main")
    if main is not None:
        monkeypatch.setattr(main, "LRCLIB_ENABLED", False)
