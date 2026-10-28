"""Raw and readable archives use one locked paper membership snapshot."""

import zipfile
from doxograph import store, workspace_archive
from pdfs import minimal_pdf


def test_archive_copies_only_the_initial_locked_paper_set(monkeypatch):
    store.save_paper(store.new_paper('initial', title='Included paper'))
    original = store.paper_keys

    def import_after_listing():
        keys = original()
        store.save_paper(store.new_paper('late', title='Concurrent import'))
        store.pdf_path('late').write_bytes(minimal_pdf('Arrived after archive membership was captured'))
        return keys

    monkeypatch.setattr(store, 'paper_keys', import_after_listing)
    path = workspace_archive.build()
    try:
        with zipfile.ZipFile(path) as archive:
            assert 'data/papers/initial.json' in archive.namelist()
            assert 'papers/initial.md' in archive.namelist()
            assert not any('late' in name for name in archive.namelist())
    finally:
        path.unlink()


def test_archive_releases_partial_locks_during_concurrent_delete():
    """Exercise both Python mutex and cross-process flock contention, bounded."""
    import os
    import subprocess
    import sys
    script = r'''
import contextlib
import subprocess
import sys
import threading
import time
from doxograph import config, store, workspace_archive
config.ensure_dirs()
store.save_paper(store.new_paper('retired-keys'))
store.save_paper(store.new_paper('z'))
ready = config.data_dir() / 'delete-ready'
original = store.paper_lock
started = False
errors = []
worker = None

def delete():
    try:
        with original('z'):
            ready.touch()
            store.delete_paper('z')
    except BaseException as exc:
        errors.append(exc)

@contextlib.contextmanager
def coordinated(key, **kwargs):
    global started, worker
    if key == 'z' and not started:
        started = True
        if sys.argv[1] == 'process':
            worker = subprocess.Popen([sys.executable, '-c', "from doxograph import config, store; "
                "config.ensure_dirs(); "
                "exec(\"with store.paper_lock('z'):\\n (config.data_dir() / 'delete-ready').touch()\\n store.delete_paper('z')\")"])
        else:
            worker = threading.Thread(target=delete, daemon=True)
            worker.start()
        deadline = time.monotonic() + 2
        while not ready.exists():
            assert time.monotonic() < deadline, 'deletion never acquired z'
            time.sleep(.005)
    with original(key, **kwargs):
        yield
store.paper_lock = coordinated
path = workspace_archive.build()
path.unlink()
if sys.argv[1] == 'process':
    assert worker.wait(timeout=2) == 0
else:
    worker.join(timeout=2)
    assert not worker.is_alive() and not errors
assert started and not store.paper_path('z').exists()
'''
    for mode in ('thread', 'process'):
        # Each mode has fresh synchronization files and corpus data.
        env = dict(os.environ, DOXOGRAPH_DATA=os.environ['DOXOGRAPH_DATA'] + '-' + mode)
        result = subprocess.run([sys.executable, '-c', script, mode], env=env,
                                capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stdout + result.stderr
