"""Shared test setup: every test uses config.example.toml and a throwaway database, never your real data/."""
import tomllib
from pathlib import Path

import pytest

from homeward.core import Listing, Store

ROOT = Path(__file__).resolve().parent.parent
MTZ = (48.173958, 11.532226)  # the workplace in config.example.toml


@pytest.fixture
def cfg():
    return tomllib.loads((ROOT / "config.example.toml").read_text(encoding="utf-8"))


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "test.sqlite3")


def make(**kw) -> Listing:
    """A listing that passes every rule; override fields to break one."""
    base = dict(source="wgg", id="1", url="https://example.org/1", title="WG-Zimmer in Moosach", district="Moosach",
                kind="WG-Zimmer", rent=650, lat=MTZ[0] + 0.01, lng=MTZ[1])
    return Listing(**{**base, **kw})
