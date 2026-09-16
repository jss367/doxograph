import pytest


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    """Point every test at a throwaway corpus."""
    monkeypatch.setenv("DOXOGRAPH_DATA", str(tmp_path / "corpus"))
    monkeypatch.delenv("DOXOGRAPH_EXPORT", raising=False)
    from doxograph import config, quotes, search
    config.ensure_dirs()
    # The text a paper was read from is cached in memory for the life of the
    # process, keyed on the file it came from. Two tests in one process use
    # the same temporary names for different papers.
    quotes._cache.clear()
    search._texts.clear()
    search._texts_size = 0
    return config.data_dir()
