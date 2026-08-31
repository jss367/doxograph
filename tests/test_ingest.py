from doxograph import ingest


def test_parses_arxiv_forms():
    text = """
    2602.06941
    arXiv:2505.12345v2
    https://arxiv.org/abs/2401.00001
    https://arxiv.org/pdf/2401.00002v1
    """
    refs, unknown = ingest.parse_refs(text)
    assert [r.kind for r in refs] == ["arxiv"] * 4
    assert [r.value for r in refs] == ["2602.06941", "2505.12345v2", "2401.00001", "2401.00002v1"]
    assert unknown == []


def test_parses_old_style_arxiv_ids():
    refs, _ = ingest.parse_refs("https://arxiv.org/abs/cs/0112017")
    assert refs[0].kind == "arxiv"
    assert refs[0].value == "cs/0112017"


def test_parses_dois_and_urls():
    refs, unknown = ingest.parse_refs(
        "10.1038/s41586-021-03819-2 https://doi.org/10.1145/3442188.3445922 "
        "https://example.org/paper.pdf https://example.org/landing"
    )
    assert [r.kind for r in refs] == ["doi", "doi", "pdf", "page"]
    assert unknown == []


def test_flags_unreadable_tokens():
    refs, unknown = ingest.parse_refs("see the paper by Smith")
    assert refs == []
    assert unknown == ["see", "the", "paper", "by", "Smith"]


def test_deduplicates_within_one_paste():
    refs, _ = ingest.parse_refs("2602.06941 https://arxiv.org/abs/2602.06941")
    assert len(refs) == 1
