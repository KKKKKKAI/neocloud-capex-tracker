"""capex.paths: code lives with the checkout, data follows CAPEX_HOME."""
from __future__ import annotations

import pytest

from capex import paths


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    monkeypatch.delenv("CAPEX_HOME", raising=False)
    monkeypatch.delenv("CAPEX_DB_PATH", raising=False)


def test_home_defaults_to_the_checkout():
    assert paths.home() == paths.CODE_ROOT
    assert paths.db_path() == paths.CODE_ROOT / "data" / "db" / "capex.db"
    assert paths.dump_path() == paths.CODE_ROOT / "data" / "db" / "dump.sql"


def test_runtime_dirs_follow_capex_home_at_call_time(monkeypatch, tmp_path):
    monkeypatch.setenv("CAPEX_HOME", str(tmp_path))
    assert paths.db_path() == tmp_path / "data" / "db" / "capex.db"
    assert paths.sources_dir() == tmp_path / "data" / "_sources"
    assert paths.local_dir() == tmp_path / "data" / "_local"
    for fn, name in [
        (paths.workbook_dir, "workbook"), (paths.charts_dir, "charts"),
        (paths.site_dir, "site"), (paths.output_dir, "output"),
        (paths.logs_dir, "logs"), (paths.backups_dir, "backups"), (paths.run_dir, "run"),
    ]:
        assert fn() == tmp_path / name


def test_config_stays_in_the_checkout(monkeypatch, tmp_path):
    monkeypatch.setenv("CAPEX_HOME", str(tmp_path))
    assert paths.COVERAGE_YAML == paths.CODE_ROOT / "data" / "seeds" / "coverage.yaml"
    assert paths.IDENTITY_YAML.exists()
    assert paths.COVERAGE_YAML.exists()


def test_db_path_override(monkeypatch, tmp_path):
    monkeypatch.setenv("CAPEX_DB_PATH", str(tmp_path / "x.db"))
    assert paths.db_path() == tmp_path / "x.db"
    assert paths.dump_path() == tmp_path / "dump.sql"


def test_release_directory_requires_capex_home(monkeypatch, tmp_path):
    (tmp_path / paths.RELEASE_MARKER).touch()
    monkeypatch.setattr(paths, "CODE_ROOT", tmp_path)
    with pytest.raises(RuntimeError, match="CAPEX_HOME"):
        paths.home()
    monkeypatch.setenv("CAPEX_HOME", str(tmp_path / "data-volume"))
    assert paths.home() == tmp_path / "data-volume"


@pytest.mark.parametrize("stored", [
    None, "", "6k://BIDU/2026Q2", "xbrl://MSFT/10-K/2025", "restated-virtual://ORCL/1",
])
def test_virtual_or_empty_raw_paths_have_no_file(stored):
    assert paths.resolve_raw_path(stored) is None


def test_relative_raw_paths_resolve_under_home(monkeypatch, tmp_path):
    stored = "data/_sources/MSFT/_raw/[2025.07.30][MSFT][AR][10-K].htm"
    assert paths.resolve_raw_path(stored) == paths.CODE_ROOT / stored
    monkeypatch.setenv("CAPEX_HOME", str(tmp_path))
    assert paths.resolve_raw_path(stored) == tmp_path / stored
    # Windows-style separators from an old row still resolve.
    assert paths.resolve_raw_path(stored.replace("/", "\\")) == tmp_path / stored


def test_absolute_raw_paths_are_used_as_is(tmp_path):
    f = tmp_path / "filing.htm"
    assert paths.resolve_raw_path(str(f)) == f


def test_raw_path_key_round_trips(monkeypatch, tmp_path):
    monkeypatch.setenv("CAPEX_HOME", str(tmp_path))
    f = paths.sources_dir() / "MSFT" / "_raw" / "a.htm"
    f.parent.mkdir(parents=True)
    f.write_text("x")
    key = paths.raw_path_key(f)
    assert key == "data/_sources/MSFT/_raw/a.htm"
    assert paths.resolve_raw_path(key) == f


def test_raw_path_key_rejects_files_outside_home(monkeypatch, tmp_path):
    monkeypatch.setenv("CAPEX_HOME", str(tmp_path / "home"))
    with pytest.raises(ValueError):
        paths.raw_path_key(tmp_path / "elsewhere.htm")


def test_llm_cwd_is_created(monkeypatch, tmp_path):
    monkeypatch.setenv("CAPEX_HOME", str(tmp_path))
    cwd = paths.llm_cwd()
    assert cwd.is_dir()
    assert list(cwd.iterdir()) == []
