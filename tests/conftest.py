import pytest


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    """Point every test at a throwaway corpus."""
    monkeypatch.setenv("DOXOGRAPH_DATA", str(tmp_path / "corpus"))
    monkeypatch.delenv("DOXOGRAPH_EXPORT", raising=False)
    from doxograph import config
    config.ensure_dirs()
    return config.data_dir()
