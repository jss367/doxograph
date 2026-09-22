import contextlib
import ssl
import threading
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


@pytest.mark.parametrize("token,expected", [
    ("2301.12345.", "2301.12345"),
    ("2301.12345,", "2301.12345"),
    ("(2301.12345)", "2301.12345"),
    ("(2301.12345).", "2301.12345"),
    ("[2301.12345v2];", "2301.12345v2"),
    ('"2301.12345"', "2301.12345"),
    ("hep-th/9901001.", "hep-th/9901001"),
])
def test_a_bare_arxiv_id_in_citation_punctuation_is_still_recognized(token, expected):
    """`arXiv:2301.12345.` always was; the bare ID has to be too."""
    refs, unknown = ingest.parse_refs(token)
    assert unknown == []
    assert [(r.kind, r.value) for r in refs] == [("arxiv", expected)]


def test_a_bare_arxiv_id_still_has_to_be_the_whole_token():
    refs, unknown = ingest.parse_refs("2301.12345.pdf x2301.12345")
    assert refs == []
    assert unknown == ["2301.12345.pdf", "x2301.12345"]


@pytest.mark.parametrize("token,expected", [
    ("10.48550/arXiv.2301.12345", "2301.12345"),
    ("doi:10.48550/arXiv.2301.12345", "2301.12345"),
    ("https://doi.org/10.48550/arXiv.2301.12345", "2301.12345"),
    ("https://doi.org/10.48550/ARXIV.2301.12345.", "2301.12345"),
    ("10.48550/arxiv.hep-th/9901001", "hep-th/9901001"),
])
def test_an_arxiv_doi_is_read_as_the_arxiv_id_it_names(token, expected):
    """DataCite registers these, so Crossref answers 404 for every one."""
    refs, unknown = ingest.parse_refs(token)
    assert unknown == []
    assert [(r.kind, r.value) for r in refs] == [("arxiv", expected)]


def test_an_arxiv_doi_and_its_id_are_one_paper():
    refs, _ = ingest.parse_refs("https://doi.org/10.48550/arXiv.2301.12345 2301.12345v1")
    assert [r.value for r in refs] == ["2301.12345"]


def test_an_arxiv_doi_that_reaches_crossref_is_asked_of_arxiv(quick_arxiv):
    """A landing page or a PDF's metadata can name the paper by this DOI too."""
    client = FakeArxivClient(FakeArxivResponse(200, FEED))
    meta = ingest.fetch_crossref("10.48550/arXiv.2607.07916", client)
    assert meta["title"] == "Persona Cartography"
    assert meta["source"]["kind"] == "arxiv"
    assert meta["source"]["id"] == "2607.07916"


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
    monkeypatch.setattr(ingest, "_arxiv_next", 0.0)
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


def test_giving_up_still_holds_off_the_next_paper(quick_arxiv):
    """The last refusal reaches the queue too, even though nobody retries it.

    A pasted batch is ingested several papers at a time. If the refusal that
    exhausts one paper's attempts left the shared booking at the usual three
    seconds, the next paper would query almost immediately after a cooldown
    that had just failed, and work through the same two-minute cycle itself.
    """
    client = FakeArxivClient(*[FakeArxivResponse(406)] * ingest.ARXIV_ATTEMPTS)
    with pytest.raises(ValueError, match="rate-limiting"):
        ingest.fetch_arxiv("2607.07916", client)
    started = time.monotonic()
    ingest._arxiv_turn()
    assert time.monotonic() - started >= quick_arxiv * 16


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


def test_a_refusal_postpones_a_thread_already_waiting(quick_arxiv):
    """A refusal reaches the threads that are already queued for a turn.

    The refused thread is usually not the only one ingesting, so a neighbour
    is typically already waiting its turn when the refusal lands. It has to
    hear about the backoff, rather than going ahead at the usual cadence and
    keeping the refusal alive.
    """
    ingest._arxiv_turn()  # book the next turn, so the waiter has to wait
    took_its_turn = []

    def take_a_turn():
        ingest._arxiv_turn()
        took_its_turn.append(time.monotonic())

    started = time.monotonic()
    waiter = threading.Thread(target=take_a_turn)
    waiter.start()
    time.sleep(quick_arxiv / 3)  # let it settle into the wait
    ingest._arxiv_back_off(quick_arxiv * 10)
    waiter.join(timeout=10)
    assert took_its_turn and took_its_turn[0] - started >= quick_arxiv * 9


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


def _client_hello(context) -> bytes:
    """The bytes the context would put on the wire to open a handshake."""
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    handshake = context.wrap_bio(incoming, outgoing, server_hostname="export.arxiv.org")
    with contextlib.suppress(ssl.SSLWantReadError):
        handshake.do_handshake()
    return outgoing.read()


def test_the_handshake_does_not_offer_http11_alone():
    """arXiv's CDN answers 406 to a handshake whose only ALPN entry is http/1.1.

    httpcore makes that offer on every request unless HTTP/2 is enabled, and it
    makes it on whatever context it is handed, so the control below shows what
    the stock context does with the same call.
    """
    control = httpx.create_ssl_context()
    control.set_alpn_protocols(["http/1.1"])
    assert b"http/1.1" in _client_hello(control)

    ours = ingest._ssl_context()
    ours.set_alpn_protocols(["http/1.1"])
    assert b"http/1.1" not in _client_hello(ours)
