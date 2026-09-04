import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest

from ledger_daemon.datagen import generate, load_batch


@pytest.fixture(scope="session")
def batch(tmp_path_factory):
    d = tmp_path_factory.mktemp("batch")
    generate(42, 500, str(d))
    return load_batch(str(d))
