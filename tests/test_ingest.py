import time

import httpx
import pytest

from doxograph import ingest


def test_parses_arxiv_forms():
    text = """
    2602.06941
    arXiv:2505.12345v2
    https://arxiv.org/abs/2401.00001
    https://arxiv.org/pdf/2401.00002v1
    https://arxiv.org/html/2401.00003v1
    """
    refs, unknown = ingest.parse_refs(text)
    assert [r.kind for r in refs] == ["arxiv"] * 5
    assert [r.value for r in refs] == [
        "2602.06941", "2505.12345v2", "2401.00001", "2401.00002v1", "2401.00003v1"
    ]
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


def test_one_paper_pasted_under_two_versions_is_fetched_once():
    refs, _ = ingest.parse_refs("2602.06941v1 https://arxiv.org/abs/2602.06941v2 2602.06941")
    assert [r.value for r in refs] == ["2602.06941v1"]


def test_two_papers_are_still_two():
    refs, _ = ingest.parse_refs("2602.06941v1 2602.06942")
    assert [r.value for r in refs] == ["2602.06941v1", "2602.06942"]


# --- arXiv rate limiting --------------------------------------------------
#
# export.arxiv.org answers 406 Not Acceptable, not 429, when it is queried
# faster than it likes. Ingesting a pasted batch runs several fetches at once,
# so a burst used to fail papers that were perfectly fetchable a second later.

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2607.07916v1</id>
    <published>2026-07-10T00:00:00Z</published>
    <title>Persona Cartography</title>
    <summary>An abstract.</summary>
    <author><name>A Researcher</name></author>
  </entry>
</feed>"""


class FakeArxivResponse:
    def __init__(self, status_code: int, text: str = ""):
        self.status_code = status_code
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code}", request=None, response=None)


class FakeArxivClient:
    """Hands out canned responses in order and records when each was asked for."""

    def __init__(self, *responses: FakeArxivResponse):
        self._responses = list(responses)
        self.asked_at: list[float] = []

    def get(self, url, **kwargs):
        self.asked_at.append(time.monotonic())
        return self._responses.pop(0)


@pytest.fixture
def quick_arxiv(monkeypatch):
    """Keep the real spacing logic, at a hundredth of the real interval."""
    monkeypatch.setattr(ingest, "ARXIV_INTERVAL", 0.03)
    monkeypatch.setattr(ingest, "_arxiv_last", 0.0)
    return 0.03


def test_a_rate_limited_query_is_retried(quick_arxiv):
    client = FakeArxivClient(
        FakeArxivResponse(406), FakeArxivResponse(200, FEED))
    meta = ingest.fetch_arxiv("2607.07916", client)
    assert meta["title"] == "Persona Cartography"
    assert len(client.asked_at) == 2


def test_persistent_rate_limiting_says_so(quick_arxiv):
    client = FakeArxivClient(*[FakeArxivResponse(406)] * ingest.ARXIV_ATTEMPTS)
    with pytest.raises(ValueError, match="rate-limiting"):
        ingest.fetch_arxiv("2607.07916", client)
    assert len(client.asked_at) == ingest.ARXIV_ATTEMPTS


def test_a_refusal_waits_longer_each_time(quick_arxiv):
    """The gap after a second refusal is wider than the gap after the first."""
    client = FakeArxivClient(
        FakeArxivResponse(406), FakeArxivResponse(406),
        FakeArxivResponse(406), FakeArxivResponse(200, FEED))
    ingest.fetch_arxiv("2607.07916", client)
    gaps = [b - a for a, b in zip(client.asked_at, client.asked_at[1:])]
    assert gaps[1] > gaps[0] and gaps[2] > gaps[1]


def test_a_refusal_holds_off_the_other_threads(quick_arxiv):
    """Backing off pauses whoever asks next, not only the refused thread."""
    ingest._arxiv_back_off(quick_arxiv * 8)
    started = time.monotonic()
    ingest._arxiv_turn()
    assert time.monotonic() - started >= quick_arxiv * 7


def test_queries_are_spaced_out(quick_arxiv):
    """Two fetches in a row do not go out together."""
    client = FakeArxivClient(
        FakeArxivResponse(200, FEED), FakeArxivResponse(200, FEED))
    ingest.fetch_arxiv("2607.07916", client)
    ingest.fetch_arxiv("2607.07917", client)
    assert client.asked_at[1] - client.asked_at[0] >= quick_arxiv


def test_a_real_failure_still_raises(quick_arxiv):
    """404 is not congestion, so it fails on the first try."""
    client = FakeArxivClient(FakeArxivResponse(404))
    with pytest.raises(httpx.HTTPStatusError):
        ingest.fetch_arxiv("2607.07916", client)
    assert len(client.asked_at) == 1
