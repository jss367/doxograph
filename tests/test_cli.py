"""The command line reports failure faithfully and streams large files."""

from __future__ import annotations

from pathlib import Path

from doxograph import __main__, extract, ingest, store


# --- the CLI must report failure -----------------------------------------

def test_add_returns_nonzero_when_every_reference_fails(monkeypatch, capsys):
    def boom(ref, client=None):
        raise RuntimeError("arXiv is down")

    monkeypatch.setattr(ingest, "ingest_ref", boom)
    args = __main__.build_parser().parse_args(["add", "--no-extract", "2602.06941"])
    assert args.func(args) == 1
    assert "arXiv is down" in capsys.readouterr().err


def test_add_returns_nonzero_for_an_unreadable_reference(monkeypatch, capsys):
    args = __main__.build_parser().parse_args(["add", "--no-extract", "definitely-not-a-reference"])
    assert args.func(args) == 1
    assert "could not read reference" in capsys.readouterr().err


def test_add_returns_zero_when_the_reference_lands(monkeypatch):
    def land(ref, client=None):
        store.save_paper(store.new_paper("doe2026study"))
        store.pdf_path("doe2026study").write_bytes(b"%PDF-1.4\n")
        return "doe2026study", True

    monkeypatch.setattr(ingest, "ingest_ref", land)
    args = __main__.build_parser().parse_args(["add", "--no-extract", "2602.06941"])
    assert args.func(args) == 0


def test_add_reports_a_paper_that_arrived_without_its_pdf(monkeypatch, capsys):
    def land_without_pdf(ref, client=None):
        paper = store.new_paper("doe2026study")
        paper["notes"] = "PDF download failed: 503"
        store.save_paper(paper)
        return "doe2026study", True

    monkeypatch.setattr(ingest, "ingest_ref", land_without_pdf)
    args = __main__.build_parser().parse_args(["add", "--no-extract", "2602.06941"])
    assert args.func(args) == 1
    err = capsys.readouterr().err
    assert "no PDF stored" in err and "503" in err


def test_add_returns_nonzero_when_extraction_fails(monkeypatch, capsys):
    monkeypatch.setattr(ingest, "ingest_ref", lambda ref, client=None: ("doe2026study", True))
    store.save_paper(store.new_paper("doe2026study"))
    store.pdf_path("doe2026study").write_bytes(b"%PDF-1.4\n")   # extraction needs one

    def boom(key, keep_reviewed=True):
        raise RuntimeError("model refused")

    monkeypatch.setattr(extract, "extract_paper", boom)
    args = __main__.build_parser().parse_args(["add", "2602.06941"])
    assert args.func(args) == 1
    assert "extraction failed" in capsys.readouterr().err


def test_extract_returns_nonzero_when_a_paper_fails(monkeypatch, capsys):
    store.save_paper(store.new_paper("doe2026study"))

    def boom(key, keep_reviewed=True):
        raise RuntimeError("no PDF")

    monkeypatch.setattr(extract, "extract_paper", boom)
    args = __main__.build_parser().parse_args(["extract", "doe2026study"])
    assert args.func(args) == 1
    assert "no PDF" in capsys.readouterr().err


def test_the_cli_does_not_read_a_local_pdf_whole(monkeypatch, tmp_path, capsys):
    """`doxograph add file.pdf` streams it into staging like an upload does."""
    read_whole = []
    real_read_bytes = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes",
                        lambda self: (read_whole.append(self.name), real_read_bytes(self))[1])
    monkeypatch.setattr(ingest, "guess_from_pdf", lambda path, client, display_name=None: {
        "title": "A Local Paper", "authors": [], "year": None, "abstract": "", "venue": "",
        "doi": "", "source": {"kind": "file", "id": display_name, "url": "", "pdf_url": ""}})

    pdf = tmp_path / "local.pdf"
    pdf.write_bytes(b"%PDF-1.4\n" + b"x" * 200_000)

    code = __main__.main(["add", str(pdf), "--no-extract"])

    assert code == 0, capsys.readouterr()
    assert "local.pdf" not in read_whole, "the CLI read the whole PDF into memory"
