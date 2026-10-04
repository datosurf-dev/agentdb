import os
import pytest


def pytest_collection_modifyitems(config, items):
    if os.environ.get("AGENTDB_SLOW_TESTS") == "1":
        return
    skip_slow = pytest.mark.skip(reason="set AGENTDB_SLOW_TESTS=1 to run")
    for item in items:
        if item.get_closest_marker("slow"):
            item.add_marker(skip_slow)
