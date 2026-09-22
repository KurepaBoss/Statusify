"""The suite must never touch the real app folder (see conftest.py)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main

_SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_user_data_is_redirected():
    assert os.path.normcase(main._APP_DIR) != os.path.normcase(_SRC_DIR)
    for p in (main._LOG_FILE, main._CONFIG_PATH, main._HIST_FILE):
        assert os.path.normcase(os.path.dirname(p)) == os.path.normcase(main._APP_DIR)


def test_bundled_resources_still_come_from_source():
    assert os.path.exists(os.path.join(main._RES_DIR, "lyrics-bridge.js"))
