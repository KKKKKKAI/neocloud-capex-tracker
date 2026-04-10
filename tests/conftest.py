"""Pytest configuration shared by all test modules.

Adds the `network` marker so tests that hit live SEC EDGAR / HKEXnews
endpoints can be opted-in only when desired. By default network tests
are skipped to keep CI deterministic and offline-friendly.

Run network tests with:
    RUN_NETWORK_TESTS=1 pytest

Mark a test as network-dependent with:
    @pytest.mark.network
    def test_live_sec_msft():
        ...
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Make src/ importable for tests without requiring `pip install -e .`
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "network: test requires live network access (SEC EDGAR, HKEXnews). "
        "Skipped unless RUN_NETWORK_TESTS=1 is set.",
    )


def pytest_collection_modifyitems(config, items):
    if os.environ.get("RUN_NETWORK_TESTS"):
        return
    import pytest

    skip_network = pytest.mark.skip(reason="network test (set RUN_NETWORK_TESTS=1 to enable)")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip_network)
