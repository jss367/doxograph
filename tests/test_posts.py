"""Forum posts, which arrive as HTML and are set as PDFs."""

from __future__ import annotations

import io
import json

import httpx
import pytest
from PIL import Image

from doxograph import bib, ingest, posts, search, store

POST_ID = "HhF5kESdtPHku7kim"
LW_URL = f"https://www.lesswrong.com/posts/{POST_ID}/reproducing-steering"


@pytest.mark.parametrize("url,expected", [
    (LW_URL, ("lesswrong", POST_ID)),
    (f"https://lesswrong.com/posts/{POST_ID}", ("lesswrong", POST_ID)),
    (f"https://www.lesswrong.com/s/abcDEF123/p/{POST_ID}", ("lesswrong", POST_ID)),
    (f"https://www.alignmentforum.org/posts/{POST_ID}/slug", ("lesswrong", POST_ID)),
    (f"https://forum.effectivealtruism.org/posts/{POST_ID}/slug?commentId=x", ("eaforum", POST_ID)),
    ("https://www.lesswrong.com/tag/ai", None),
    (f"https://example.com/posts/{POST_ID}", None),
])
def test_a_post_link_is_read_as_the_post_it_names(url, expected):
    assert posts.parse_url(url) == expected


def test_a_post_on_lesswrong_and_the_alignment_forum_is_one_paper():
    refs, unknown = ingest.parse_refs(
        f"{LW_URL} https://www.alignmentforum.org/posts/{POST_ID}/reproducing-steering")
    assert unknown == []
    assert [(r.kind, r.value) for r in refs] == [("lesswrong", POST_ID)]


def _png(color: str) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (400, 200), color).save(out, "PNG")
    return out.getvalue()


HTML = (
    "<p><i>Produced as part of a team.</i></p>"
    "<h1><span>Executive summary</span></h1>"
    '<ul><li value="1"><b>Control vectors</b> matter as much as eval vectors.</li>'
    '<li class="nested-list-item"><ul><li><p>A nested point.</p></li></ul></li></ul>'
    '<ol><li>First step</li><li>Second step</li></ol>'
    '<p>Sampled pairs <span class="math-tex"><span class="mjx-math" aria-label="(x, y)">'
    '<span class="mjx-char">(</span><span class="mjx-char">x</span></span></span> evenly.</p>'
    "<table><tr><th><p>Model</p></th><th>Rate</th></tr>"
    "<tr><td><span>GLM-5</span></td><td>62%</td></tr></table>"
    '<figure><img src="https://images.example/plot.png" alt="a plot">'
    "<figcaption>Figure 1: blackmail rates.</figcaption></figure>"
    '<p><img src="https://images.example/missing.png" alt="lost figure"></p>'
    "<style>.x { color: red }</style><script>alert(1)</script>"
    "<p>Curly “quotes” – dashes – and Ελληνικά survive.</p>"
)

POST = {
    "_id": POST_ID,
    "title": "Reproducing steering\n against evaluation awareness",
    "postedAt": "2026-04-10T10:45:06.860Z",
    "pageUrl": LW_URL,
    "af": False,
    "user": {"displayName": "Thomas Read"},
    "coauthors": [{"displayName": "Joseph Bloom"}],
    "contents": {"html": HTML},
}


@pytest.fixture
def forum():
    calls = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.host == "www.lesswrong.com":
            asked = json.loads(request.content)["variables"]["id"]
            result = POST if asked == POST_ID else None
            return httpx.Response(200, json={"data": {"post": {"result": result}}})
        if request.url.path == "/plot.png":
            return httpx.Response(200, content=_png("navy"))
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(handle))
    yield client, calls
    client.close()


def test_a_post_is_filed_with_its_own_metadata_and_a_pdf_of_its_text(forum):
    client, calls = forum
    key, created = ingest.ingest_ref(ingest.parse_ref(LW_URL), client)
    assert created
    paper = store.load_paper(key)
    assert paper["title"] == "Reproducing steering against evaluation awareness"
    assert paper["authors"] == ["Thomas Read", "Joseph Bloom"]
    assert paper["year"] == 2026
    assert paper["venue"] == "LessWrong"
    assert paper["source"] == {"kind": "lesswrong", "id": POST_ID, "url": LW_URL, "pdf_url": ""}
    assert not paper.get("error")
    assert store.pdf_path(key).read_bytes().startswith(b"%PDF-")

    text = search.paper_text(key)
    flat = " ".join(text.split())
    for expected in [
        "Reproducing steering against evaluation awareness", "Thomas Read, Joseph Bloom",
        "Produced as part of a team.", "Executive summary",
        "Control vectors matter as much as eval vectors.", "A nested point.",
        "1. First step", "2. Second step", "(x, y)", "GLM-5", "62%",
        "Figure 1: blackmail rates.", "[image: lost figure]",
        "Curly “quotes” – dashes – and Ελληνικά survive.",
    ]:
        assert expected in flat, expected
    for absent in ["alert(1)", "color: red", "mjx"]:
        assert absent not in flat
    assert "https://images.example/plot.png" in calls
    leftovers = list(store.pdf_path(key).parent.glob(".render-*"))
    assert leftovers == []


def test_pasting_a_post_again_answers_without_asking_the_forum(forum):
    client, calls = forum
    key, _ = ingest.ingest_ref(ingest.parse_ref(LW_URL), client)
    calls.clear()
    again = ingest.parse_ref(f"https://www.alignmentforum.org/posts/{POST_ID}")
    assert ingest.already_stored(again) == key
    assert ingest.ingest_ref(again, client) == (key, False)
    assert calls == []


def test_a_post_the_forum_does_not_have_says_so(forum):
    client, _ = forum
    with pytest.raises(ValueError, match="LessWrong has no post"):
        ingest.ingest_ref(ingest.parse_ref("https://www.lesswrong.com/posts/AAAAAAAAAAAAAAAAA"), client)
    assert store.paper_keys() == []


def test_a_post_that_cannot_be_set_is_kept_and_says_why(forum, monkeypatch):
    client, _ = forum

    def broken(meta, html, client):
        raise RuntimeError("no room on the page")

    working = posts.render
    monkeypatch.setattr(posts, "render", broken)
    key, created = ingest.ingest_ref(ingest.parse_ref(LW_URL), client)
    assert created and not store.pdf_path(key).exists()
    assert "no room on the page" in store.load_paper(key)["error"]
    # Adding it again retries, as for a download that failed.
    monkeypatch.setattr(posts, "render", working)
    assert ingest.ingest_ref(ingest.parse_ref(LW_URL), client) == (key, False)
    assert store.pdf_path(key).exists()
    assert not store.load_paper(key).get("error")


def test_a_post_is_cited_as_a_post(forum):
    client, _ = forum
    key, _ = ingest.ingest_ref(ingest.parse_ref(LW_URL), client)
    entry = bib.entry(store.load_paper(key))
    assert entry.startswith("@misc{")
    assert "howpublished = {LessWrong}" in entry
    assert "journal" not in entry
