"""Downloading, uploading, staging and recovering the PDFs themselves."""

from __future__ import annotations

import asyncio
import io
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from doxograph import __main__, config, extract, ingest, server, store


def test_concurrent_uploads_of_the_same_filename_keep_their_own_pdfs(monkeypatch):
    """Two uploads sharing a basename must not stage to the same path."""
    import threading
    import time

    def fake_guess(path, client, display_name=None):
        # Read, pause, read again: a shared staging path shows up as a mismatch.
        first = path.read_bytes()
        time.sleep(0.15)
        second = path.read_bytes()
        assert first == second, "staging file changed underneath this upload"
        marker = first.split(b"marker:")[1].split(b"\n")[0].decode()
        return {
            "title": f"Paper {marker}", "authors": [f"Author {marker}"], "year": 2026,
            "abstract": "", "venue": "", "doi": "",
            "source": {"kind": "file", "id": f"{marker}.pdf", "url": "", "pdf_url": ""},
        }

    monkeypatch.setattr(ingest, "guess_from_pdf", fake_guess)

    results, errors = {}, []

    def upload(marker):
        try:
            data = b"%PDF-1.4\nmarker:" + marker.encode() + b"\n" + bytes([0] * 64)
            key, _ = ingest.ingest_pdf_bytes(data, "paper.pdf")
            results[marker] = key
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=upload, args=(m,)) for m in ("alpha", "beta")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert all(not t.is_alive() for t in threads), "an upload thread never finished"

    assert not errors, errors
    assert len(set(results.values())) == 2, f"uploads collided: {results}"
    for marker, key in results.items():
        assert marker.encode() in store.pdf_path(key).read_bytes()
    leftovers = list(config.pdfs_dir().glob(".incoming-*"))
    assert leftovers == [], f"staging files left behind: {leftovers}"


def test_concurrent_uploads_that_share_a_citekey_get_separate_papers(monkeypatch):
    """Two different papers can produce the same coarse key at the same moment."""
    import threading
    import time

    barrier = threading.Barrier(2)

    def fake_guess(path, client, display_name=None):
        marker = path.read_bytes().split(b"marker:")[1].split(b"\n")[0].decode()
        # Both workers reach the key decision together, which is what made
        # exists()-then-write unsafe.
        barrier.wait(timeout=5)
        time.sleep(0.05)
        return {
            # Same surname, year and first title word => same coarse citekey.
            "title": f"A Study of {marker}", "authors": ["Jane Doe"], "year": 2026,
            "abstract": "", "venue": "", "doi": "",
            "source": {"kind": "file", "id": f"{marker}.pdf", "url": "", "pdf_url": ""},
        }

    monkeypatch.setattr(ingest, "guess_from_pdf", fake_guess)
    results, errors = {}, []

    def upload(marker):
        try:
            data = b"%PDF-1.4\nmarker:" + marker.encode() + b"\n" + bytes(64)
            key, _ = ingest.ingest_pdf_bytes(data, f"{marker}.pdf")
            results[marker] = key
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=upload, args=(m,)) for m in ("alpha", "beta")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    assert len(set(results.values())) == 2, f"both uploads took the same key: {results}"
    for marker, key in results.items():
        assert marker.encode() in store.pdf_path(key).read_bytes()
        assert store.load_paper(key)["title"] == f"A Study of {marker}"


def test_reservation_is_visible_to_deduplication_immediately():
    """A reserved key must already carry its identity, not an empty placeholder."""
    key = store.reserve_key("doe2026study", source={"kind": "arxiv", "id": "2602.06941"},
                            doi="", title="A Study")
    assert ingest.find_existing({"source": {"kind": "arxiv", "id": "2602.06941v2"}, "doi": ""}) == key


def test_concurrent_ingests_of_one_paper_make_one_paper(monkeypatch):
    """Two requests for the same arXiv ID must not race past each other."""
    import threading
    import time

    barrier = threading.Barrier(2)
    meta = {
        "title": "Recovery under steering", "authors": ["Jane Doe"], "year": 2026,
        "abstract": "", "venue": "arXiv", "doi": "",
        "source": {"kind": "arxiv", "id": "2602.06941", "url": "", "pdf_url": ""},
    }

    def fake_fetch(arxiv_id, client):
        barrier.wait(timeout=5)   # both arrive at the claim step together
        return {**meta, "source": {**meta["source"], "id": arxiv_id}}

    monkeypatch.setattr(ingest, "fetch_arxiv", fake_fetch)
    monkeypatch.setattr(ingest, "download_pdf",
                        lambda url, dest, client: (_ for _ in ()).throw(AssertionError("no pdf_url")))

    results, errors = {}, []

    def add(version):
        try:
            time.sleep(0.01)
            key, created = ingest.ingest_ref(ingest.Ref("arxiv", f"2602.06941{version}", ""))
            results[version] = (key, created)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=add, args=(v,)) for v in ("v1", "v2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, errors
    keys = {key for key, _ in results.values()}
    assert len(keys) == 1, f"the same paper was ingested twice: {results}"
    assert sum(1 for _, created in results.values() if created) == 1
    assert len(store.paper_keys()) == 1


def test_upload_metadata_comes_from_the_arrival_filename(monkeypatch):
    """The staging path is randomized; it must not reach the title or the key."""
    monkeypatch.setattr(ingest, "pdf_first_page_text", lambda path, pages=2: "no identifiers")
    data = b"%PDF-1.4\n" + bytes(64)
    key, created = ingest.ingest_pdf_bytes(data, "Attention_Is_All_You_Need.pdf")
    paper = store.load_paper(key)
    assert created
    assert paper["title"] == "Attention Is All You Need"
    assert paper["source"]["id"] == "Attention_Is_All_You_Need.pdf"
    assert "incoming" not in key and "incoming" not in paper["title"]


def test_the_same_upload_twice_gets_the_same_metadata(monkeypatch):
    """Randomized staging names used to make each upload look like a new paper."""
    monkeypatch.setattr(ingest, "pdf_first_page_text", lambda path, pages=2: "no identifiers")
    data = b"%PDF-1.4\n" + bytes(64)
    first, _ = ingest.ingest_pdf_bytes(data, "paper.pdf")
    second, _ = ingest.ingest_pdf_bytes(data, "paper.pdf")
    titles = {store.load_paper(k)["title"] for k in (first, second)}
    assert titles == {"paper"}, titles


def test_re_adding_a_paper_recovers_a_missing_pdf(monkeypatch):
    """A transient download failure must not make the paper unrecoverable."""
    meta = {
        "title": "Recovery under steering", "authors": ["Jane Doe"], "year": 2026,
        "abstract": "", "venue": "arXiv", "doi": "",
        "source": {"kind": "arxiv", "id": "2602.06941", "url": "",
                   "pdf_url": "https://arxiv.org/pdf/2602.06941"},
    }
    monkeypatch.setattr(ingest, "fetch_arxiv", lambda i, c: meta)

    attempts = {"n": 0}

    def flaky(url, client):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("network blip")
        path = config.pdfs_dir() / ".download-test.pdf"
        path.write_bytes(b"%PDF-1.4\nrecovered")
        return path

    monkeypatch.setattr(ingest, "fetch_pdf", flaky)

    key, created = ingest.ingest_ref(ingest.Ref("arxiv", "2602.06941", ""))
    assert created
    assert not store.pdf_path(key).exists()
    assert "PDF download failed" in store.load_paper(key)["notes"]

    again, created_again = ingest.ingest_ref(ingest.Ref("arxiv", "2602.06941", ""))
    assert (again, created_again) == (key, False)
    assert store.pdf_path(key).read_bytes() == b"%PDF-1.4\nrecovered"
    assert store.load_paper(key)["notes"] == ""
    assert len(store.paper_keys()) == 1


def test_a_present_pdf_is_not_downloaded_again(monkeypatch):
    meta = {
        "title": "A Study", "authors": ["Jane Doe"], "year": 2026, "abstract": "",
        "venue": "arXiv", "doi": "",
        "source": {"kind": "arxiv", "id": "2602.06941", "url": "", "pdf_url": "https://x/y.pdf"},
    }
    monkeypatch.setattr(ingest, "fetch_arxiv", lambda i, c: meta)
    calls = {"n": 0}

    def counted(url, client):
        calls["n"] += 1
        path = config.pdfs_dir() / f".download-{calls['n']}.pdf"
        path.write_bytes(b"%PDF-1.4\n")
        return path

    monkeypatch.setattr(ingest, "fetch_pdf", counted)
    ingest.ingest_ref(ingest.Ref("arxiv", "2602.06941", ""))
    ingest.ingest_ref(ingest.Ref("arxiv", "2602.06941", ""))
    assert calls["n"] == 1


def test_publishing_a_pdf_for_a_removed_paper_leaves_no_orphan():
    """A download finishing after Remove must not recreate the PDF."""
    store.save_paper(store.new_paper("doe2026study"))
    staged = config.pdfs_dir() / ".download-staged.pdf"
    staged.write_bytes(b"%PDF-1.4\n")

    store.delete_paper("doe2026study")
    assert ingest.publish_pdf("doe2026study", staged) is False
    assert not store.pdf_path("doe2026study").exists()
    assert not staged.exists(), "the staged file was left behind"


def test_publishing_a_pdf_for_a_live_paper_succeeds():
    store.save_paper(store.new_paper("doe2026study"))
    staged = config.pdfs_dir() / ".download-staged.pdf"
    staged.write_bytes(b"%PDF-1.4\nbody")
    assert ingest.publish_pdf("doe2026study", staged) is True
    assert store.pdf_path("doe2026study").read_bytes() == b"%PDF-1.4\nbody"


def test_uploading_to_a_removed_paper_leaves_no_orphan_pdf(monkeypatch):
    """An upload publishes through the locked helper, like a download does."""
    monkeypatch.setattr(ingest, "pdf_first_page_text", lambda path, pages=2: "no identifiers")

    real_publish = ingest.publish_pdf

    removed = []

    def publish_after_removal(key, staging):
        # Stand in for Remove landing between the reservation and the copy.
        removed.append(key)
        store.delete_paper(key)
        return real_publish(key, staging)

    monkeypatch.setattr(ingest, "publish_pdf", publish_after_removal)

    # Reporting success here would mark the job done for a paper that no longer
    # exists, so the ingest now fails loudly instead.
    with pytest.raises(ingest.PaperRemoved):
        ingest.ingest_pdf_bytes(b"%PDF-1.4\n" + bytes(64), "paper.pdf")

    assert removed, "the test did not exercise the removal path"
    assert not store.pdf_path(removed[0]).exists(), "an orphan PDF was left for a removed paper"
    assert list(config.pdfs_dir().glob(".incoming-*")) == []
    assert list(config.pdfs_dir().glob(".download-*")) == []


def test_a_normal_upload_still_attaches_its_pdf(monkeypatch):
    monkeypatch.setattr(ingest, "pdf_first_page_text", lambda path, pages=2: "no identifiers")
    key, created = ingest.ingest_pdf_bytes(b"%PDF-1.4\nbody", "paper.pdf")
    assert created
    assert store.pdf_path(key).read_bytes() == b"%PDF-1.4\nbody"
    assert list(config.pdfs_dir().glob(".incoming-*")) == []


def test_uploading_a_pdf_for_a_record_that_lacks_one_attaches_it(monkeypatch):
    """The existing-paper branch publishes through the helper too."""
    store.save_paper(store.new_paper(
        "doe2026study", title="A Study",
        source={"kind": "arxiv", "id": "2602.06941", "url": ""},
    ))
    monkeypatch.setattr(ingest, "guess_from_pdf", lambda path, client, display_name=None: {
        "title": "A Study", "authors": ["Jane Doe"], "year": 2026, "abstract": "",
        "venue": "arXiv", "doi": "",
        "source": {"kind": "arxiv", "id": "2602.06941", "url": "", "pdf_url": ""},
    })
    key, created = ingest.ingest_pdf_bytes(b"%PDF-1.4\nbody", "paper.pdf")
    assert (key, created) == ("doe2026study", False)
    assert store.pdf_path("doe2026study").read_bytes() == b"%PDF-1.4\nbody"
    assert len(store.paper_keys()) == 1


def test_needs_extraction_tracks_the_pdf_not_the_creation():
    store.save_paper(store.new_paper("doe2026study"))
    assert store.needs_extraction("doe2026study") is False      # no PDF yet

    store.pdf_path("doe2026study").write_bytes(b"%PDF-1.4\n")
    assert store.needs_extraction("doe2026study") is True       # recovered

    store.add_claim("doe2026study", {"text": "X."})
    assert store.needs_extraction("doe2026study") is False      # already read
    assert store.needs_extraction("no-such-paper") is False


def test_recovering_a_pdf_makes_the_cli_read_the_paper(monkeypatch, capsys):
    """The recovery path must reach extraction, not stop at created=False."""
    meta = {
        "title": "Recovery under steering", "authors": ["Jane Doe"], "year": 2026,
        "abstract": "", "venue": "arXiv", "doi": "",
        "source": {"kind": "arxiv", "id": "2602.06941", "url": "",
                   "pdf_url": "https://arxiv.org/pdf/2602.06941"},
    }
    monkeypatch.setattr(ingest, "fetch_arxiv", lambda i, c: meta)

    attempts = {"n": 0}

    def flaky(url, client):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectError("blip")
        path = config.pdfs_dir() / ".download-t.pdf"
        path.write_bytes(b"%PDF-1.4\n")
        return path

    monkeypatch.setattr(ingest, "fetch_pdf", flaky)

    read = []
    monkeypatch.setattr(extract, "extract_paper",
                        lambda key, keep_reviewed=True: read.append(key))

    args = __main__.build_parser().parse_args(["add", "2602.06941"])
    assert args.func(args) == 1              # first run: no PDF, reported
    assert read == []
    assert "no PDF stored" in capsys.readouterr().err

    assert args.func(args) == 0              # second run: recovered and read
    assert read == ["doe2026recovery"]


def open_descriptor_count() -> int:
    """How many file descriptors this process currently holds."""
    for probe in ("/dev/fd", "/proc/self/fd"):
        path = Path(probe)
        if path.is_dir():
            return len(list(path.iterdir()))
    pytest.skip("no way to count open descriptors on this platform")


def test_a_failed_download_closes_its_staging_descriptor():
    """An early failure must not leak the descriptor `mkstemp` handed back."""

    class Failing:
        def stream(self, *args, **kwargs):
            raise httpx.ConnectError("refused")

    # One failure first, so any one-off descriptors are already accounted for.
    with pytest.raises(httpx.ConnectError):
        ingest.fetch_pdf("https://example.org/x.pdf", Failing())

    before = open_descriptor_count()
    for _ in range(25):
        with pytest.raises(httpx.ConnectError):
            ingest.fetch_pdf("https://example.org/x.pdf", Failing())
    after = open_descriptor_count()

    assert after - before < 5, f"leaked about {after - before} descriptors over 25 failures"
    assert list(config.pdfs_dir().glob(".download-*")) == []


def test_a_failed_download_leaves_no_staging_file(monkeypatch):
    class NotAPdf:
        def stream(self, *args, **kwargs):
            class R:
                headers = {"content-type": "text/html"}

                def raise_for_status(self):
                    return None

                def iter_bytes(self, n):
                    yield b"<html>not a pdf</html>"

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False
            return R()

    with pytest.raises(ValueError, match="rather than a PDF"):
        ingest.fetch_pdf("https://example.org/x.pdf", NotAPdf())
    assert list(config.pdfs_dir().glob(".download-*")) == []


def test_two_processes_ingesting_one_paper_make_one_paper(tmp_path):
    """The whole check-and-reserve transaction, across real processes.

    An outcome guard, not a proof. Both children busy-wait to a shared
    wall-clock instant, but the window between `find_existing` and
    `reserve_key` is microseconds and process startup jitter is larger, so this
    still passes with the cross-process lock removed. It is kept because it
    exercises the real two-process path end to end and would catch a coarser
    regression. `test_claim_lock_is_held_across_processes` is the test that
    actually fails without the lock.
    """
    import subprocess
    import sys
    import textwrap
    import time

    start_at = time.time() + 3.0
    script = textwrap.dedent(f"""
        import os, time
        os.environ["DOXOGRAPH_DATA"] = {str(config.data_dir())!r}
        from doxograph import ingest, store
        meta = {{
            "title": "Recovery under steering", "authors": ["Jane Doe"], "year": 2026,
            "abstract": "", "venue": "arXiv", "doi": "",
            "source": {{"kind": "arxiv", "id": "2602.06941", "url": "", "pdf_url": ""}},
        }}
        ingest.fetch_arxiv = lambda i, c: meta
        while time.time() < {start_at!r}:      # busy-wait to the shared instant
            pass
        key, created = ingest.ingest_ref(ingest.Ref("arxiv", "2602.06941", ""))
        print(f"{{key}} {{created}}")
    """)

    procs = [subprocess.Popen([sys.executable, "-c", script],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for _ in range(2)]
    results = [p.communicate(timeout=60) for p in procs]
    outputs = [out.strip() for out, _ in results]

    assert all(p.returncode == 0 for p in procs), results
    keys = {line.split()[0] for line in outputs}
    created = [line.split()[1] for line in outputs]
    assert len(keys) == 1, f"two processes made two papers: {outputs}"
    assert created.count("True") == 1, f"both processes claimed to create it: {outputs}"
    assert len(store.paper_keys()) == 1


def test_a_download_for_a_removed_paper_fails_the_ingest(monkeypatch):
    """`download_pdf` returning False must not be reported as success."""
    meta = {
        "title": "A Study", "authors": ["Jane Doe"], "year": 2026, "abstract": "",
        "venue": "arXiv", "doi": "",
        "source": {"kind": "arxiv", "id": "2602.06941", "url": "", "pdf_url": "https://x/y.pdf"},
    }
    monkeypatch.setattr(ingest, "fetch_arxiv", lambda i, c: meta)
    monkeypatch.setattr(ingest, "download_pdf", lambda url, key, client: False)

    with pytest.raises(ingest.PaperRemoved):
        ingest.ingest_ref(ingest.Ref("arxiv", "2602.06941", ""))


def test_add_reports_a_paper_removed_mid_ingest_without_crashing(monkeypatch, capsys):
    """`_report_missing_pdf` used to raise KeyError on the deleted JSON."""
    def land_then_vanish(ref, client=None):
        return "doe2026study", True      # nothing was ever written

    monkeypatch.setattr(ingest, "ingest_ref", land_then_vanish)
    args = __main__.build_parser().parse_args(["add", "--no-extract", "2602.06941"])
    assert args.func(args) == 1
    assert "removed while it was being added" in capsys.readouterr().err


def recovery_corpus(monkeypatch):
    """An existing paper with no PDF and a recorded download failure."""
    store.save_paper(store.new_paper(
        "doe2026study", title="A Study", notes="PDF download failed: 503",
        source={"kind": "arxiv", "id": "2602.06941", "url": "", "pdf_url": "https://x/y.pdf"},
    ))
    meta = {
        "title": "A Study", "authors": ["Jane Doe"], "year": 2026, "abstract": "",
        "venue": "arXiv", "doi": "",
        "source": {"kind": "arxiv", "id": "2602.06941", "url": "", "pdf_url": "https://x/y.pdf"},
    }
    monkeypatch.setattr(ingest, "fetch_arxiv", lambda i, c: meta)


def test_recovery_keeps_the_note_when_the_retry_fails(monkeypatch):
    """A failed download raises; the note must survive so the paper explains itself."""
    recovery_corpus(monkeypatch)

    def still_down(url, key, client):
        raise httpx.ConnectError("still down")

    monkeypatch.setattr(ingest, "download_pdf", still_down)
    key, created = ingest.ingest_ref(ingest.Ref("arxiv", "2602.06941", ""))
    assert (key, created) == ("doe2026study", False)
    assert store.load_paper("doe2026study")["notes"] == "PDF download failed: 503"


def test_recovery_reports_a_paper_removed_mid_retry(monkeypatch):
    """False means the paper went away, which is different from a failed download."""
    recovery_corpus(monkeypatch)
    monkeypatch.setattr(ingest, "download_pdf", lambda url, key, client: False)
    with pytest.raises(ingest.PaperRemoved):
        ingest.ingest_ref(ingest.Ref("arxiv", "2602.06941", ""))


def test_recovery_clears_the_note_when_the_retry_lands(monkeypatch):
    recovery_corpus(monkeypatch)

    def lands(url, key, client):
        store.pdf_path(key).write_bytes(b"%PDF-1.4\n")
        return True

    monkeypatch.setattr(ingest, "download_pdf", lands)
    ingest.ingest_ref(ingest.Ref("arxiv", "2602.06941", ""))
    assert store.load_paper("doe2026study")["notes"] == ""


# --- removal detected after the PDF landed -------------------------------

def test_removal_after_a_successful_recovery_is_reported(monkeypatch):
    """A KeyError on the post-publication reload means the paper went away."""
    recovery_corpus(monkeypatch)

    def lands_then_vanishes(url, key, client):
        store.pdf_path(key).write_bytes(b"%PDF-1.4\n")
        store.paper_path(key).unlink()      # removed between publish and reload
        return True

    monkeypatch.setattr(ingest, "download_pdf", lands_then_vanishes)
    with pytest.raises(ingest.PaperRemoved):
        ingest.ingest_ref(ingest.Ref("arxiv", "2602.06941", ""))


def test_a_failed_retry_is_still_tolerated(monkeypatch):
    """Only removal is fatal; a download that simply fails leaves the paper."""
    recovery_corpus(monkeypatch)

    def fails(url, key, client):
        raise httpx.ConnectError("still down")

    monkeypatch.setattr(ingest, "download_pdf", fails)
    key, created = ingest.ingest_ref(ingest.Ref("arxiv", "2602.06941", ""))
    assert (key, created) == ("doe2026study", False)
    assert store.load_paper("doe2026study")["notes"] == "PDF download failed: 503"


# --- the web job must say a paper cannot be read -------------------------

def run_ingest_job(ref, monkeypatch, do_extract=False):
    """Drive the server's ingest worker synchronously and return its job."""
    job = {"id": 1, "label": "t", "state": "queued", "detail": "", "key": None}
    monkeypatch.setattr(server, "_prune_jobs", lambda: None)
    server._run_ingest(job, ref, do_extract)
    return job


def test_a_web_ingest_with_no_pdf_is_reported_as_a_failure(monkeypatch):
    """`done` with an empty detail drops the job from the strip entirely."""
    meta = {
        "title": "A Study", "authors": ["Jane Doe"], "year": 2026, "abstract": "",
        "venue": "Journal", "doi": "10.1145/3442188.3445922",
        "source": {"kind": "doi", "id": "10.1145/3442188.3445922", "url": "", "pdf_url": ""},
    }
    monkeypatch.setattr(ingest, "fetch_crossref", lambda doi, client: dict(meta))
    job = run_ingest_job(ingest.Ref("doi", "10.1145/3442188.3445922", ""), monkeypatch)

    assert job["state"] == "error", job
    assert "no PDF stored" in job["detail"]
    assert "Add it again to retry" in job["detail"]


def test_a_web_ingest_that_lands_its_pdf_is_done(monkeypatch):
    meta = {
        "title": "A Study", "authors": ["Jane Doe"], "year": 2026, "abstract": "",
        "venue": "arXiv", "doi": "",
        "source": {"kind": "arxiv", "id": "2602.06941", "url": "", "pdf_url": "https://x/y.pdf"},
    }
    monkeypatch.setattr(ingest, "fetch_arxiv", lambda i, c: dict(meta))

    def lands(url, key, client):
        store.pdf_path(key).write_bytes(b"%PDF-1.4\n")
        return True

    monkeypatch.setattr(ingest, "download_pdf", lands)
    job = run_ingest_job(ingest.Ref("arxiv", "2602.06941", ""), monkeypatch)
    assert job["state"] == "done", job
    assert job["detail"] == ""


def test_a_failed_download_is_reported_with_its_note(monkeypatch):
    meta = {
        "title": "A Study", "authors": ["Jane Doe"], "year": 2026, "abstract": "",
        "venue": "arXiv", "doi": "",
        "source": {"kind": "arxiv", "id": "2602.06941", "url": "", "pdf_url": "https://x/y.pdf"},
    }
    monkeypatch.setattr(ingest, "fetch_arxiv", lambda i, c: dict(meta))

    def fails(url, client):
        raise httpx.ConnectError("503 from the host")

    monkeypatch.setattr(ingest, "fetch_pdf", fails)
    job = run_ingest_job(ingest.Ref("arxiv", "2602.06941", ""), monkeypatch)
    assert job["state"] == "error", job
    assert "503 from the host" in job["detail"]


def test_a_download_is_not_read_back_into_memory(monkeypatch):
    """The signature check must not undo the streaming it follows."""
    read_whole = []
    real_read_bytes = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes",
                        lambda self: (read_whole.append(self.name), real_read_bytes(self))[1])

    class Streamed:
        def stream(self, *args, **kwargs):
            class R:
                headers = {"content-type": "application/pdf"}

                def raise_for_status(self):
                    return None

                def iter_bytes(self, n):
                    yield b"%PDF-1.4\n"
                    for _ in range(4):
                        yield b"x" * n

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False
            return R()

    staged = ingest.fetch_pdf("https://example.org/big.pdf", Streamed())
    try:
        assert staged.stat().st_size > 65536
        assert read_whole == [], "the staged download was read back in full"
    finally:
        staged.unlink(missing_ok=True)


def test_an_upload_is_staged_on_disk_not_held_in_memory(monkeypatch):
    """The worker gets a path; the batch no longer costs one buffer per file."""
    handed = []
    monkeypatch.setattr(server._pool, "submit",
                        lambda fn, job, staged, name, extract_now: handed.append((staged, name)))

    body = b"%PDF-1.4\n" + b"x" * 200_000
    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.post("/api/upload?extract_now=false",
                               files={"files": ("big.pdf", body, "application/pdf")})

    assert response.json() == {"queued": 1}
    staged, name = handed[0]
    assert isinstance(staged, Path), f"the worker was handed {type(staged).__name__}"
    try:
        assert staged.read_bytes() == body
        assert name == "big.pdf"
    finally:
        staged.unlink(missing_ok=True)


def test_a_rejected_upload_leaves_no_staging_file(monkeypatch):
    """`ingest_staged_pdf` owns the file it is given, on every path out."""
    staged = ingest.stage_upload(io.BytesIO(b"<html>not a pdf</html>"), "fake.pdf")
    assert staged.exists()

    with pytest.raises(ValueError, match="is not a PDF"):
        ingest.ingest_staged_pdf(staged, "fake.pdf")

    assert not staged.exists()
    assert list(config.pdfs_dir().glob(".incoming-*")) == []


def test_upload_staging_does_not_run_on_the_event_loop(monkeypatch):
    """A large drop must not stop the page from polling or saving."""
    import asyncio

    where = {}
    real_stage = ingest.stage_upload

    def watched(source, filename):
        try:
            asyncio.get_running_loop()
            where["on_the_loop"] = True
        except RuntimeError:
            where["on_the_loop"] = False
        return real_stage(source, filename)

    monkeypatch.setattr(ingest, "stage_upload", watched)
    monkeypatch.setattr(server._pool, "submit",
                        lambda fn, job, staged, name, extract_now: staged.unlink(missing_ok=True))

    with TestClient(server.app, base_url="http://127.0.0.1:8765") as client:
        response = client.post("/api/upload?extract_now=false",
                               files={"files": ("big.pdf", b"%PDF-1.4\n" + b"x" * 100_000)})

    assert response.json() == {"queued": 1}
    assert where["on_the_loop"] is False, "the copy blocked the event loop"
