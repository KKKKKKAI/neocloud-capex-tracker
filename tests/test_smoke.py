"""Smoke test — confirms the package is importable."""
from __future__ import annotations


def test_package_importable():
    import src  # noqa: F401
    from src.protocol import PROTOCOL_VERSION
    assert isinstance(PROTOCOL_VERSION, str)
