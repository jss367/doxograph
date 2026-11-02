import pytest


def pytest_addoption(parser):
    parser.addoption(
        "--browser-shard", metavar="INDEX/COUNT",
        help="Run one browser shard, for example -m browser --browser-shard=1/4",
    )
    parser.addoption(
        "--python-shard", metavar="INDEX/COUNT",
        help='Run one Python shard, for example -m "not browser" --python-shard=1/4',
    )


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    browser = config.getoption("--browser-shard")
    python = config.getoption("--python-shard")
    if browser is not None and python is not None:
        raise pytest.UsageError("--browser-shard and --python-shard cannot be combined")
    option = "--browser-shard" if browser is not None else "--python-shard"
    shard = browser if browser is not None else python
    if shard is None:
        return
    try:
        index, count = map(int, shard.split("/"))
        if not 1 <= index <= count:
            raise ValueError
    except ValueError:
        raise pytest.UsageError(f"{option} must be INDEX/COUNT with 1 <= INDEX <= COUNT") from None
    if browser is not None:
        if any(item.get_closest_marker("browser") is None for item in items):
            raise pytest.UsageError("--browser-shard requires selecting only browser tests (-m browser)")
        # Preserve the existing browser distribution by node id.
        ordered = sorted(items, key=lambda item: item.nodeid)
        selected = ordered[index - 1::count]
    else:
        if any(item.get_closest_marker("browser") is not None for item in items):
            raise pytest.UsageError('--python-shard requires selecting only Python tests (-m "not browser")')
        # Keep each module together to make a stalled job easy to narrow down.
        # Sorting module paths partitions every selected test exactly once.
        modules = sorted({item.nodeid.split("::", 1)[0] for item in items})
        assigned = set(modules[index - 1::count])
        ordered = items
        selected = [item for item in ordered if item.nodeid.split("::", 1)[0] in assigned]
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
