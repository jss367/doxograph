import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--browser-shard", metavar="INDEX/COUNT",
        help="Run one browser shard, for example -m browser --browser-shard=1/4",
    )


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    shard = config.getoption("--browser-shard")
    if shard is None:
        return
    try:
        index, count = map(int, shard.split("/"))
        if not 1 <= index <= count:
            raise ValueError
    except ValueError:
        raise pytest.UsageError("--browser-shard must be INDEX/COUNT with 1 <= INDEX <= COUNT") from None
    if any(item.get_closest_marker("browser") is None for item in items):
        raise pytest.UsageError("--browser-shard requires selecting only browser tests (-m browser)")
    # Round-robin by node id spreads large parameterized families across jobs
    # while remaining deterministic and covering each selected test once.
    ordered = sorted(items, key=lambda item: item.nodeid)
    selected = ordered[index - 1::count]
    deselected = [item for item in ordered if item not in selected]
    config.hook.pytest_deselected(items=deselected)
    items[:] = selected


@pytest.fixture(scope="session", autouse=True)
def browser_runtime():
    yield
    from browser_support import runtime
    runtime.close()


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
