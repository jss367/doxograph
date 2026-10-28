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
