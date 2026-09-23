"""Turn a pasted reference or a dropped file into a paper in the store.

Accepts arXiv IDs and URLs, DOIs, direct PDF links, landing pages that
advertise a PDF, and local PDF files.
"""

from __future__ import annotations

import re
import io
import ssl
import os
import hashlib
import shutil
import tempfile
import threading
import time
import unicodedata
import html as html_module
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urljoin

import httpx

from . import config, fingerprint, search, store

class PaperRemoved(RuntimeError):
    """The paper was deleted while it was being ingested.

    Publication returns False in that case. Reporting success anyway would mark
    a job done for a paper that no longer exists, and the CLI would then crash
    trying to read why it has no PDF.
    """


USER_AGENT = "doxograph/0.1 (+https://github.com/jss367/doxograph)"
TIMEOUT = httpx.Timeout(60.0, connect=15.0)

ARXIV_API = "https://export.arxiv.org/api/query"
# arXiv answers 406 rather than 429 when it is queried faster than it likes,
# which is about one request every three seconds. A pasted batch of references
# is ingested several at a time, so the queries have to be spaced out, and a
# refusal has to be retried rather than failing the paper.
ARXIV_INTERVAL = 3.0
# A refusal can outlast a minute even when the request rate was polite, so the
# wait between attempts doubles: 3, 6, 12, 24, 48 seconds, giving up after
# about two minutes rather than after twenty seconds.
ARXIV_ATTEMPTS = 6
ARXIV_BUSY = (406, 429, 503)
_arxiv_gate = threading.Condition()
# The moment the next query may go out. Threads wait on the gate rather than
# sleeping on it, so a refusal can push the moment back while they wait.
_arxiv_next = 0.0

# Each ends where its digits end. A prefix says where an id starts but not
# where it stops, so `arXiv:2301.123456` would otherwise read as the paper
# `2301.12345` with a stray digit after it, and ingest that paper instead of
# saying the reference was not understood.
ARXIV_NEW = r"\d{4}\.\d{4,5}(?:v\d+)?(?!\d)"
ARXIV_OLD = r"[a-z][a-z-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?(?!\d)"
# Legacy Wiley DOIs are SICI codes, which carry a `<page::id>` segment in the
# middle. Angle brackets are otherwise what a DOI is wrapped in, or the tag
# after it in markup, so one is taken only as that segment: opening on a
# digit, with no slash inside, which no closing tag or `<i>` can pass for.
DOI_RE = r"10\.\d{4,9}/(?:[-._;()/:A-Za-z0-9]|<\d[-._;():A-Za-z0-9]*>)+"
# arXiv serves a paper at /abs, /pdf and, for recent submissions, /html. The
# HTML render carries no citation metadata or canonical link, so it can only be
# recognised from its URL.
ARXIV_URL_RE = rf"arxiv\.org/(?:abs|pdf|html)/({ARXIV_NEW}|{ARXIV_OLD})"
# The DOI arXiv mints for every paper. DataCite registers it, not Crossref, so
# it is read as the arXiv ID it spells out rather than looked up as a DOI.
ARXIV_DOI_RE = rf"10\.48550/arxiv\.({ARXIV_NEW}|{ARXIV_OLD})"

def normalize_doi(doi: str) -> str:
    """Trim citation punctuation off a DOI picked up from surrounding prose.

    DOI_RE has to allow `.`, `;`, `(` and `)` because DOIs genuinely contain
    them, so a DOI pasted from a sentence carries the sentence's punctuation
    into the Crossref request. A trailing `)` is dropped only when unbalanced,
    which leaves keys like
    10.1002/(SICI)1097-0258(19980815/30)17:15/16<1661::AID-SIM968>3.0.CO;2-2
    intact.
    """
    doi = doi.strip().strip("<>")
    closers = ".,;:\'\"\u2019\u201d"
    while doi:
        if doi[-1] in closers:
            doi = doi[:-1]
        elif doi[-1] == ")" and doi.count(")") > doi.count("("):
            doi = doi[:-1]
        else:
            break
    return doi


_ARXIV_PATTERNS = [
    re.compile(ARXIV_URL_RE, re.I),
    re.compile(rf"arxiv[:\s]+({ARXIV_NEW}|{ARXIV_OLD})", re.I),
    re.compile(ARXIV_DOI_RE, re.I),
    # A bare ID has nothing ahead of it to say what it is, so it has to be the
    # whole token, give or take the brackets and sentence punctuation a
    # citation wraps it in. The prefixed forms above find it through those.
    re.compile(rf"^[(\[{{\"']*({ARXIV_NEW}|{ARXIV_OLD})[)\]}}\"'.,;:!?]*$"),
]


@dataclass
class Ref:
    kind: str  # arxiv | doi | pdf | page
    value: str
    raw: str
    # A PDF link the landing page advertised. Kept alongside a DOI so it can be
    # used when Crossref has no PDF of its own, instead of being discarded.
    pdf_url: str = ""


def parse_ref(token: str) -> Ref | None:
    token = token.strip().strip("<>,;")
    if not token:
        return None
    for pattern in _ARXIV_PATTERNS:
        match = pattern.search(token)
        if match:
            return Ref("arxiv", match.group(1), token)
    if token.lower().startswith(("doi:", "https://doi.org/", "http://doi.org/")):
        match = re.search(DOI_RE, token)
        if match:
            return Ref("doi", normalize_doi(match.group(0)), token)
    if token.startswith(("http://", "https://")):
        return Ref("pdf" if token.lower().split("?")[0].endswith(".pdf") else "page", token, token)
    # Tolerate wrapping punctuation on both sides; normalize_doi then decides
    # what trailing characters are the DOI's own.
    match = re.search(rf"({DOI_RE})[\s.,;:!?\]\}}>\"']*$", token)
    if match:
        doi = normalize_doi(match.group(1))
        if doi:
            return Ref("doi", doi, token)
    return None


def parse_refs(text: str) -> tuple[list[Ref], list[str]]:
    """Split a pasted blob into refs, returning what could not be understood."""
    refs, unknown, seen = [], [], set()
    for token in re.split(r"[\s]+", text or ""):
        if not token.strip():
            continue
        ref = parse_ref(token)
        if ref is None:
            unknown.append(token)
            continue
        # 2602.06941 and 2602.06941v2 are one paper, and pasting both used to
        # fetch it twice. arXiv is the only form with two spellings of one id.
        identity = source_identity(ref_source(ref)) if ref.kind == "arxiv" else (ref.kind, ref.value)
        if identity not in seen:
            seen.add(identity)
            refs.append(ref)
    return refs, unknown


# --- metadata sources -----------------------------------------------------

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"


def _arxiv_turn() -> None:
    """Block until this thread's turn to query arXiv comes round.

    Whoever takes a turn books the next one an interval later, so eight
    references pasted at once go out as eight spaced queries rather than eight
    at once. The wait gives the gate back while it lasts, which is what lets a
    refusal reach the threads that are already queued: each one rechecks the
    booking when it wakes, so a refusal that arrives mid-wait postpones them
    too rather than being stuck behind them.
    """
    global _arxiv_next
    with _arxiv_gate:
        while True:
            wait = _arxiv_next - time.monotonic()
            if wait <= 0:
                break
            _arxiv_gate.wait(wait)
        _arxiv_next = time.monotonic() + ARXIV_INTERVAL


def _arxiv_back_off(seconds: float) -> None:
    """Hold every thread off arXiv, not just the one that was refused.

    A refusal is aimed at the whole address, so a thread that waits alone
    leaves its neighbours querying at the usual cadence and keeping the
    refusal alive. Pushing the shared booking back puts them all on hold, and
    the sleep itself happens in `_arxiv_turn` where it already belongs.
    """
    global _arxiv_next
    with _arxiv_gate:
        _arxiv_next = max(_arxiv_next, time.monotonic() + seconds)
        _arxiv_gate.notify_all()


def query_arxiv(params: dict, client: httpx.Client) -> httpx.Response:
    """Query the arXiv API, waiting out a refusal rather than raising it."""
    for attempt in range(ARXIV_ATTEMPTS):
        _arxiv_turn()
        response = client.get(ARXIV_API, params=params)
        if response.status_code not in ARXIV_BUSY:
            response.raise_for_status()
            return response
        # Every refusal backs the queue off, including the last one. Giving up
        # on this paper is no reason to let the next one in the batch query
        # three seconds later: the final refusal is the strongest sign yet
        # that arXiv is still refusing, so the rest of the batch should wait
        # it out rather than each starting this same retry cycle from scratch.
        _arxiv_back_off(ARXIV_INTERVAL * 2 ** attempt)
    waited = round(ARXIV_INTERVAL * (2 ** (ARXIV_ATTEMPTS - 1) - 1 + ARXIV_ATTEMPTS))
    raise ValueError(
        f"arXiv refused {ARXIV_ATTEMPTS} queries with {response.status_code} "
        f"over {waited} seconds; it is rate-limiting us. Try again shortly.")


def fetch_arxiv(arxiv_id: str, client: httpx.Client) -> dict:
    bare = re.sub(r"v\d+$", "", arxiv_id)
    response = query_arxiv({"id_list": bare, "max_results": 1}, client)
    entry = ET.fromstring(response.text).find(f"{ATOM}entry")
    if entry is None or entry.find(f"{ATOM}title") is None:
        raise ValueError(f"arXiv has no record for {arxiv_id}")
    if entry.find(f"{ATOM}id") is None:
        raise ValueError(f"arXiv returned an empty record for {arxiv_id}")

    def text_of(tag: str) -> str:
        node = entry.find(tag)
        return re.sub(r"\s+", " ", (node.text or "")).strip() if node is not None else ""

    published = text_of(f"{ATOM}published")
    doi_node = entry.find(f"{ARXIV_NS}doi")
    journal_node = entry.find(f"{ARXIV_NS}journal_ref")
    return {
        "title": text_of(f"{ATOM}title"),
        "authors": [
            re.sub(r"\s+", " ", (a.findtext(f"{ATOM}name") or "")).strip()
            for a in entry.findall(f"{ATOM}author")
        ],
        "year": int(published[:4]) if published[:4].isdigit() else None,
        "abstract": text_of(f"{ATOM}summary"),
        "venue": (journal_node.text or "").strip() if journal_node is not None else "arXiv",
        "doi": (doi_node.text or "").strip() if doi_node is not None else "",
        "source": {
            "kind": "arxiv",
            "id": arxiv_id,
            "url": f"https://arxiv.org/abs/{arxiv_id}",
            "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}",
        },
    }


def fetch_crossref(doi: str, client: httpx.Client) -> dict:
    # Crossref answers 404 for an arXiv DOI. One can still arrive here from a
    # landing page's metadata or a PDF's, and arXiv has the record.
    arxiv = re.fullmatch(ARXIV_DOI_RE, doi, re.I)
    if arxiv:
        return fetch_arxiv(arxiv.group(1), client)
    response = client.get(f"https://api.crossref.org/works/{doi}")
    response.raise_for_status()
    work = response.json()["message"]
    parts = (work.get("published") or work.get("issued") or {}).get("date-parts") or [[None]]
    containers = work.get("container-title") or []
    pdf_url = ""
    for link in work.get("link", []):
        if link.get("content-type") == "application/pdf":
            pdf_url = link.get("URL", "")
            break
    titles = work.get("title") or [""]
    return {
        "title": re.sub(r"\s+", " ", titles[0]).strip(),
        # An institutional author — a collaboration, a working group — is
        # recorded under `name` with no given or family part, and reading only
        # those two left it as a blank author that every citation marker then
        # had to survive.
        "authors": [
            " ".join(p for p in [a.get("given", ""), a.get("family", "")] if p).strip()
            or (a.get("name") or "").strip()
            for a in work.get("author", [])
        ],
        "year": parts[0][0] if parts and parts[0] else None,
        "abstract": re.sub(r"<[^>]+>", "", work.get("abstract", "")).strip(),
        "venue": containers[0] if containers else work.get("type", ""),
        "doi": work.get("DOI", doi),
        "source": {
            "kind": "doi",
            "id": work.get("DOI", doi),
            "url": work.get("URL", f"https://doi.org/{doi}"),
            "pdf_url": pdf_url,
        },
    }


def _meta_content(html: str, *names: str) -> str | None:
    """Read a <meta name=...> value, tolerating attribute order."""
    for name in names:
        for pattern in (
            rf'name=["\']{name}["\']\s+content=["\']([^"\']+)',
            rf'content=["\']([^"\']+)["\']\s+name=["\']{name}["\']',
            rf'property=["\']{name}["\']\s+content=["\']([^"\']+)',
        ):
            match = re.search(pattern, html, re.I)
            if match:
                # Attribute values are HTML-escaped, so a query separator arrives
                # as &amp; and would otherwise be requested literally.
                return html_module.unescape(match.group(1)).strip()
    return None


def _head(html: str) -> str:
    """The document head, where identity metadata lives."""
    match = re.search(r"<head\b.*?</head>", html, re.I | re.S)
    return match.group(0) if match else html[:20_000]


_BIBTEX_START = re.compile(r"@\w+\s*\{")
# The boundary keeps a long run of letters from being retried at every one of
# its positions in search of an `=` that never comes.
_BIBTEX_FIELD = re.compile(r"\b(\w+)\s*=\s*")
_BIBTEX_BARE = re.compile(r"[^,}]*")


def _closing_brace(text: str, i: int, limit: int) -> int | None:
    """Just past the brace that closes one opened before `i`, if before `limit`."""
    depth = 1
    while i < limit:
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
        if not depth:
            return i
    return None


def _closing_quote(text: str, i: int, limit: int) -> int:
    """Where a quoted value opened before `i` ends, or `limit` if it never does.

    A quote inside braces belongs to the value, as in the accent of
    `Schr{\\"o}dinger`, so only one outside every brace closes it.
    """
    depth = 0
    while i < limit:
        char = text[i]
        if char == '"' and not depth:
            return i
        depth += {"{": 1, "}": -1}.get(char, 0)
        i += 1
    return limit


def _bibtex_entries(text: str) -> list[dict[str, str]]:
    """The fields of each BibTeX entry in `text`, lowercased by name.

    An entry is looked for only up to where the next one starts. The page is
    untrusted, and an `@name{` that never closes would otherwise send every
    entry after it scanning to the end of the page, so a page full of them
    would take quadratic time. Bounded like this, each character is read once.
    """
    starts = list(_BIBTEX_START.finditer(text))
    entries = []
    for n, start in enumerate(starts):
        limit = starts[n + 1].start() if n + 1 < len(starts) else len(text)
        end = _closing_brace(text, start.end(), limit)
        if end is not None:
            entries.append(_bibtex_fields(text, start.end(), end - 1))
    return entries


def _bibtex_fields(text: str, i: int, end: int) -> dict[str, str]:
    """The fields between `i` and `end`, each read past its whole value.

    Reading on from where a value ends, rather than from every `name =` in the
    entry, is what keeps `url={https://example.org/?eprint=2510.09023}` from
    also reading as an eprint field naming some other paper.
    """
    fields = {}
    while field := _BIBTEX_FIELD.search(text, i, end):
        # A `%` between fields comments out the rest of its line, and a field
        # commented out that way is often a stale identifier kept for the
        # record. Only the gap before a field is looked in, never a value, so a
        # `%20` in a URL is left alone.
        comment = text.rfind("%", i, field.start())
        if comment >= 0 and "\n" not in text[comment:field.start()]:
            newline = text.find("\n", field.start(), end)
            i = end if newline < 0 else newline
            continue
        i = field.end()
        if text.startswith("{", i, end):
            close = _closing_brace(text, i + 1, end) or end + 1
            value, i = text[i + 1:close - 1], close
        elif text.startswith('"', i, end):
            close = _closing_quote(text, i + 1, end)
            value, i = text[i + 1:close], close + 1
        else:
            bare = _BIBTEX_BARE.match(text, i, end)
            value, i = bare.group(0), bare.end()
        fields.setdefault(field.group(1).lower(), value.strip())
    return fields


def _title_words(title: str) -> str:
    """A title as its words alone, padded so containment is word-aligned.

    An accent is dropped however it is written, so the BibTeX `Schr{\\"o}dinger`
    and the page's `Schrödinger` both read as `schrodinger`. Every other macro
    is dropped too, except the few that print a word of their own, like the
    `LaTeX` of `{\\LaTeX}`.
    """
    title = re.sub(r"\\((?:La|Bib)?TeX)\b", r" \1 ", title)
    title = re.sub(r"""\\(?:["'^`~=.]|[a-zA-Z](?=\{))""", "", title)
    title = re.sub(r"[{}]", "", re.sub(r"\\[a-zA-Z]+", " ", title))
    title = "".join(c for c in unicodedata.normalize("NFKD", title)
                    if not unicodedata.combining(c))
    return " " + " ".join(re.findall(r"[a-z0-9]+", title.lower())) + " "


def _same_title(a: str, b: str) -> bool:
    a, b = _title_words(a), _title_words(b)
    shorter, longer = sorted((a, b), key=len)
    return len(shorter.split()) >= 3 and shorter in longer


_BIBTEX_WHERE = ("url", "journal", "note", "howpublished", "doi")


def _own_bibtex(html: str, page_url: str, pdf_url: str) -> Ref | None:
    """The page's citation of itself, from a BibTeX block titled like the page.

    A project page names its paper in a BibTeX block under "Citation" and in
    nothing a machine reads. The title match is what makes it the page's own:
    a BibTeX entry for any other paper carries that paper's title instead.
    """
    page = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    # A heading names the page only when it is the one heading there is. A
    # publications list gives every paper its own, and each of those would
    # otherwise match that paper's BibTeX and pass the list off as the paper.
    # The document title is read from the head for the same reason, since an
    # inline SVG in the body may carry a <title> of its own.
    h1s = re.findall(r"<h1\b[^>]*>(.*?)</h1>", page, re.I | re.S)
    headings = re.findall(r"<title\b[^>]*>(.*?)</title>", _head(page), re.I | re.S) + (
        h1s if len(h1s) == 1 else [])
    # Each metadata title is a candidate of its own, since a generic
    # citation_title must not hide an og:title that names the paper. A heading
    # may carry inline markup and entities, as in `<em>Role</em> &amp; Intent`,
    # which would otherwise leave `em` and `amp` among its words.
    titles = [t for t in (
        *(_meta_content(html, name) for name in ("citation_title", "og:title", "twitter:title")),
        *(html_module.unescape(re.sub(r"<[^>]+>", " ", h)) for h in headings),
    ) if t]
    text = html_module.unescape(re.sub(r"<[^>]+>", "", page))
    for entry in _bibtex_entries(text):
        if not any(_same_title(entry.get("title", ""), t) for t in titles):
            continue
        eprint = re.sub(r"^arxiv:", "", entry.get("eprint", ""), flags=re.I)
        if re.fullmatch(rf"{ARXIV_NEW}|{ARXIV_OLD}", eprint):
            return Ref("arxiv", eprint, page_url)
        # Only the fields that say where the entry itself is published. An
        # abstract or keywords field may well mention another arXiv paper, and
        # that must not win over the entry's own DOI.
        where = " ".join(entry.get(k, "") for k in _BIBTEX_WHERE)
        for pattern in _ARXIV_PATTERNS[:3]:
            match = pattern.search(where)
            if match:
                return Ref("arxiv", match.group(1), page_url)
        # A DOI may also be given only as the entry's link. There it has to be
        # written as one, since a url or howpublished can link anywhere.
        match = re.search(rf"({DOI_RE})", entry.get("doi", "")) or re.search(
            rf"(?:doi\.org/|\bdoi:\s*)({DOI_RE})",
            " ".join(entry.get(k, "") for k in ("url", "howpublished")), re.I)
        if match:
            return Ref("doi", normalize_doi(match.group(1)), page_url, pdf_url=pdf_url)
    return None


def resolve_page(url: str, client: httpx.Client) -> Ref:
    """Identify a landing page from its own metadata.

    Deliberately does not search the whole document for an arXiv link: a journal
    page cites other papers in its bibliography and related-articles list, and
    picking one of those would silently ingest and extract the wrong paper.
    Identity comes from citation metadata, the canonical URL, a BibTeX block
    titled like the page, or the head.
    """
    response = client.get(url, follow_redirects=True)
    response.raise_for_status()
    if "application/pdf" in response.headers.get("content-type", ""):
        return Ref("pdf", str(response.url), url)
    html = response.text[:400_000]
    head = _head(html)

    arxiv_id = _meta_content(html, "citation_arxiv_id", "citation_technical_report_number")
    if arxiv_id:
        match = re.search(rf"({ARXIV_NEW}|{ARXIV_OLD})", arxiv_id)
        if match:
            return Ref("arxiv", match.group(1), url)

    # The canonical link and og:url name the page itself, unlike any other href.
    canonical = re.search(r'rel=["\']canonical["\']\s+href=["\']([^"\']+)', head, re.I)
    for candidate in (
        _meta_content(html, "og:url"),
        canonical.group(1) if canonical else None,
        str(response.url),
    ):
        if not candidate:
            continue
        match = re.search(ARXIV_URL_RE, candidate, re.I)
        if match:
            return Ref("arxiv", match.group(1), url)

    advertised = _meta_content(html, "citation_pdf_url")
    # The advertised link is often relative; resolve it against the page we
    # actually landed on, after redirects.
    advertised = urljoin(str(response.url), advertised) if advertised else ""

    doi = _meta_content(html, "citation_doi", "dc.identifier.doi")
    if doi:
        match = re.search(DOI_RE, doi)
        if match:
            return Ref("doi", normalize_doi(match.group(0)), url, pdf_url=advertised)

    own = _own_bibtex(html, url, advertised)
    if own:
        return own

    if advertised:
        return Ref("pdf", advertised, url)

    # Last resort, and only within the head, where a bibliography does not reach.
    match = re.search(ARXIV_URL_RE, head, re.I)
    if match:
        return Ref("arxiv", match.group(1), url)
    match = re.search(DOI_RE, head)
    if match:
        return Ref("doi", normalize_doi(match.group(0)), url)
    raise ValueError(
        f"could not identify the paper at {url} from its own metadata; "
        "paste the arXiv ID, the DOI, or a direct PDF link instead"
    )


# --- PDF text, for files with no metadata anywhere -----------------------

# arXiv prints the ID it assigned down the left margin of the first page, and
# that stamp always carries a version and either a primary class or the posting
# date: `arXiv:2504.01234v1 [cs.CL] 3 Apr 2025`, `arXiv:hep-th/9901001v1 4 Jan
# 1999`. A bare `arXiv:2301.09876` anywhere else on the page is a citation of
# somebody else's work, and taking it as identity files the upload under the
# paper it cites.
ARXIV_STAMP = re.compile(
    rf"arXiv:({ARXIV_NEW}|{ARXIV_OLD})v\d+\s*(?:\[[^\]\n]+\]|\d{{1,2}}\s+\w+\s+\d{{4}})",
    re.I,
)


def pdf_first_page_text(path: Path, pages: int = 2) -> str:
    try:
        from pypdf import PdfReader

        # Beside the import, so what this process loaded is what gets
        # remembered. pypdf arrives only when a PDF does, which can be
        # long after an upgrade that the next reading would have
        # mistaken for the version already in memory.
        fingerprint.snapshot()
    except ImportError:
        return ""
    try:
        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages[:pages])
    except Exception:
        return ""


def pdf_metadata_doi(path: Path) -> str:
    """The DOI a publisher wrote into the file's own metadata, if there is one.

    This is the one DOI that cannot have come from the bibliography, so it is
    taken without further checking. Publishers put it in the XMP packet
    (`prism:doi`, `dc:identifier`) and sometimes in the older document info
    dictionary as well.
    """
    try:
        from pypdf import PdfReader

        # Beside the import, so what this process loaded is what gets
        # remembered. pypdf arrives only when a PDF does, which can be
        # long after an upgrade that the next reading would have
        # mistaken for the version already in memory.
        fingerprint.snapshot()
    except ImportError:
        return ""
    try:
        reader = PdfReader(str(path))
        candidates = [str(v) for v in (reader.metadata or {}).values()]
        xmp = reader.xmp_metadata
        if xmp is not None:
            candidates += [str(v) for v in (getattr(xmp, "dc_identifier", None) or [])]
            candidates.append(str(getattr(xmp, "pdf_keywords", "") or ""))
            candidates.append(xmp.rdf_root.toxml() if xmp.rdf_root is not None else "")
    except Exception:
        return ""
    for value in candidates:
        match = re.search(DOI_RE, value)
        if match:
            return normalize_doi(match.group(0))
    return ""


ABSTRACT_WORD = re.compile(r"\babstract\b", re.I)
# The word set as a heading: capitalized, and first on its line. A sentence
# says "abstract" in lower case, and a line of prose seldom opens on it.
ABSTRACT_HEADING = re.compile(r"^[ \t]*(Abstract|ABSTRACT)\b", re.M)


def front_matter(text: str, title: str = "") -> str:
    """The top of the first page, where the title is printed.

    Bounded by the abstract when there is one, and by a character count
    otherwise. A first page can carry a bibliography, and a paper cited there
    or in the abstract is named alongside its DOI, so a page-wide search would
    accept that pair as the upload's identity.

    `title` is the one being looked for. A title can hold the word itself —
    "Abstract Meaning Representation for Sembanking" — and cutting there would
    leave nothing above the cut to find it in, so the word is passed over
    where it falls inside a printing of that title, and the heading is taken
    to be the next one.

    Only when the next one is set as a heading. The title can be printed as
    a citation of it, and with nothing after that printing but prose, which
    can say "abstract" too, nothing says it is the page's own, so the cut
    stays where the word first falls.
    """
    head = text[:1500]
    abstracts = [match.start() for match in ABSTRACT_WORD.finditer(head)]
    if not abstracts:
        return head[:600]
    printings = _printings(title, head)
    rest = [at for at in abstracts
            if not any(begin <= at < end for begin, end in printings)]
    headings = {match.start(1) for match in ABSTRACT_HEADING.finditer(head)}
    if rest and rest[0] in headings:
        return head[:rest[0]]
    return head[:abstracts[0]]


def _squash_title(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def _printings(title: str, text: str) -> list[tuple[int, int]]:
    """Where `title` is printed in `text`, read as `_squash_title` reads it:
    on its letters and digits, whatever an extraction put between them."""
    letters = _squash_title(title)
    if not letters:
        return []
    pattern = "[^A-Za-z0-9]*".join(map(re.escape, letters))
    return [match.span() for match in re.finditer(pattern, text, re.I)]


def title_is_in_the_front_matter(title: str, text: str) -> bool:
    """Does the record we just fetched name the paper this page belongs to?

    A DOI printed on a first page is usually the paper's own, but it can be one
    it cites, and the two look alike. Fetching the record and looking for its
    title above the abstract tells them apart: the paper's own title is printed
    there, a cited paper's is not. Compared on letters and digits alone,
    because extraction inserts line breaks and turns ligatures into anything.
    """
    needle = _squash_title(title)
    haystack = _squash_title(front_matter(text, title))
    if len(needle) < 12:            # too short to be evidence either way
        return False
    return needle[:60] in haystack


def guess_from_pdf(path: Path, client: httpx.Client, display_name: str | None = None) -> dict:
    """Try arXiv ID, then DOI, then fall back to the filename as a title.

    Both identifiers have to come from the file's own front matter rather than
    from anything it cites, because a wrong match here does not merely mislabel
    the upload — it files the PDF under the cited paper's record, or merges it
    into that record if it is already in the corpus.

    `display_name` is the name the file arrived under. The path itself is a
    randomized staging file, so using its stem would put an `mkstemp` suffix in
    the title and citekey and give the same PDF different metadata every upload.
    """
    # One page only. Page two is where the references start, and a reference
    # list is full of other papers' DOIs.
    text = pdf_first_page_text(path, pages=1)
    match = ARXIV_STAMP.search(text)
    if match:
        try:
            return fetch_arxiv(match.group(1), client)
        except (httpx.HTTPError, ValueError, ET.ParseError):
            pass
    embedded = pdf_metadata_doi(path)
    if embedded:
        try:
            return fetch_crossref(embedded, client)
        except (httpx.HTTPError, KeyError, ValueError, ET.ParseError):
            pass
    # A DOI printed on the page is only accepted if the record it resolves to
    # is titled like this page. Front matter and citations both print DOIs in
    # the same form, and taking the wrong one files the upload under the paper
    # it cites.
    for match in re.finditer(DOI_RE, text):
        try:
            meta = fetch_crossref(normalize_doi(match.group(0)), client)
        except (httpx.HTTPError, KeyError, ValueError, ET.ParseError):
            continue
        if title_is_in_the_front_matter(meta.get("title", ""), text):
            return meta
    name = Path(display_name or path.name).name
    title = re.sub(r"[_-]+", " ", Path(name).stem).strip()
    return {
        "title": title or name,
        "authors": [],
        "year": None,
        "abstract": "",
        "venue": "",
        "doi": "",
        "source": {"kind": "file", "id": name, "url": "", "pdf_url": ""},
    }


# --- downloading ----------------------------------------------------------

def fetch_pdf(url: str, client: httpx.Client) -> Path:
    """Download a PDF to its own staging file and return that path."""
    config.pdfs_dir().mkdir(parents=True, exist_ok=True)
    handle, staged = tempfile.mkstemp(dir=config.pdfs_dir(), prefix=".download-", suffix=".pdf")
    # Close the descriptor straight away and reopen by path. Opening the stream
    # or checking its status can fail before any write, and a descriptor still
    # owned by `mkstemp` at that point leaks for the life of the server.
    os.close(handle)
    staging = Path(staged)
    try:
        with client.stream("GET", url, follow_redirects=True) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            with staging.open("wb") as fh:
                for chunk in response.iter_bytes(65536):
                    fh.write(chunk)
        # The signature only. `read_bytes` here would pull a streamed download
        # back into memory in full, which is what the chunked loop above avoids.
        with staging.open("rb") as fh:
            signature = fh.read(5)
        if signature != b"%PDF-":
            raise ValueError(f"{url} returned {content_type or 'unknown content'} rather than a PDF")
        return staging
    except BaseException:
        staging.unlink(missing_ok=True)
        raise


def pdf_digest(path: Path) -> str:
    """A content hash of a PDF, read in chunks rather than into memory."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pdf_fingerprint(path: Path) -> dict:
    """What identifies a PDF on disk: the stat that says whether it changed,
    and the hash that says which paper it is.

    The same three fields `extract.upload_pdf` keeps for the same reason.
    """
    st = path.stat()
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "sha256": pdf_digest(path)}


def stored_digest(key: str) -> str:
    """The hash of a paper's PDF, read against the file and written down.

    A recorded hash is trusted only while the file it was taken from is
    unchanged. The corpus is a plain directory and a paper can be replaced in
    it by hand — `search.paper_source` and `extract.upload_pdf` both guard the
    same case — and a stale hash would file a re-drop of the replacement as a
    second paper. Size and mtime decide; a replacement that matches the old
    file in both is the limit of a stat, here as there.

    Papers stored before any of this are hashed on first ask rather than left
    unmatchable, which costs one pass over each PDF, once.

    A paper with no PDF keeps whatever it recorded: an upload reserves its key
    carrying the hash before it publishes, and that reservation is what a
    second drop of the same file in that moment has to see.
    """
    with store.paper_lock(key):
        try:
            paper = store.load_paper(key)
        except store.VANISHED:
            return ""
        recorded = paper.get("pdf_file") or {}
        path = store.pdf_path(key)
        try:
            st = path.stat()
        except OSError:
            return recorded.get("sha256") or ""
        if (recorded.get("size"), recorded.get("mtime_ns")) == (st.st_size, st.st_mtime_ns):
            return recorded.get("sha256") or ""
        fresh = pdf_fingerprint(path)
        paper["pdf_file"] = fresh
        store.save_paper(paper)
        return fresh["sha256"]


def find_by_digest(digest: str) -> str | None:
    """The paper whose PDF is this exact file, or None.

    The identity of last resort. A PDF that names neither an arXiv ID nor a
    DOI has only its filename to go on, and two unrelated files can arrive
    under one name, so the bytes are the only thing that can say "this is the
    paper you already have".
    """
    if not digest:
        return None
    for key in store.paper_keys():
        if stored_digest(key) == digest:
            return key
    return None


def settled_digest(digest: str) -> str | None:
    """The paper this hash is already here as, PDF and all, or None.

    A paper still missing its PDF is not an answer, for the same reason it is
    not one in `already_stored`: the upload is what attaches it.
    """
    settled = find_by_digest(digest)
    return settled if settled and store.pdf_path(settled).exists() else None


def already_uploaded(path: Path) -> str | None:
    """The paper this file is already here as, PDF and all, or None.

    The counterpart of `already_stored` for a dropped file: it costs a read of
    the file rather than parsing it and asking Crossref about what it finds.
    """
    return settled_digest(pdf_digest(path))


def publish_pdf(key: str, staging: Path) -> bool:
    """Move a staged PDF into place, only if the paper still exists.

    Publishing outside the paper's lock could recreate a `<key>.pdf` for a paper
    that was removed while the download was running, leaving an orphan file.
    """
    with store.paper_lock(key):
        if not store.paper_path(key).exists():
            staging.unlink(missing_ok=True)
            return False
        os.replace(staging, store.pdf_path(key))
        # The text stored for the old PDF describes a paper that is no longer
        # there. `os.replace` carries the staged file's mtime, which can be
        # older than that text, so the freshness check alone would go on
        # serving it and quotes would be checked against the wrong paper.
        store.text_path(key).unlink(missing_ok=True)
        # Read the new text before letting go of the lock. Two publishes of one
        # key can interleave otherwise: the first reads its PDF, the second
        # replaces it and stores its text, and the first then writes the text
        # of a paper that is no longer there. The lock is this paper's alone,
        # and the parse is the price of the text being right.
        search.cache_text(key)
        # Write down what landed while still under the paper's lock, so the
        # same file dropped again is recognised as this paper. Every PDF
        # reaches the corpus through here, downloads and uploads alike.
        paper = store.load_paper(key)
        paper["pdf_file"] = pdf_fingerprint(store.pdf_path(key))
        store.save_paper(paper)
    return True


def download_pdf(url: str, key: str, client: httpx.Client) -> bool:
    """Fetch a PDF and attach it to a paper. False if the paper went away."""
    return publish_pdf(key, fetch_pdf(url, client))


def _ssl_context() -> ssl.SSLContext:
    """A TLS context that does not announce HTTP/1.1 as the only protocol it speaks.

    arXiv sits behind a CDN that answers 406 with an empty body to any
    handshake whose ALPN list is exactly ["http/1.1"] — a shape no browser and
    no `curl` produces, so it reads as a bot. httpcore sets that list on
    whatever context it is handed, so the only way to stop announcing it is to
    make the call it uses do nothing. Announcing no protocol at all is what
    `urllib` does, and the CDN serves it.
    """
    context = httpx.create_ssl_context()
    context.set_alpn_protocols = lambda protocols: None
    return context


def _client() -> httpx.Client:
    return httpx.Client(
        timeout=TIMEOUT, verify=_ssl_context(), headers={"User-Agent": USER_AGENT})


def source_identity(source: dict) -> tuple[str, str] | None:
    """A comparable identity for a source, with arXiv versions collapsed.

    `fetch_arxiv` already treats 2602.06941, v1 and v2 as one record, so
    deduplication has to as well or an updated preprint becomes a second paper.
    """
    kind = (source or {}).get("kind") or ""
    value = ((source or {}).get("id") or "").strip()
    if not value:
        return None
    if kind == "arxiv":
        return ("arxiv", re.sub(r"v\d+$", "", value, flags=re.I).lower())
    return (kind, value.lower())


def find_existing(meta: dict) -> str | None:
    """Match on arXiv ID or DOI so re-pasting a reference does not duplicate it."""
    identity = source_identity(meta.get("source") or {})
    doi = normalize_doi(meta.get("doi") or "").lower()
    for paper in store.all_papers():
        if identity and source_identity(paper.get("source") or {}) == identity:
            return paper["key"]
        if doi and normalize_doi(paper.get("doi") or "").lower() == doi:
            return paper["key"]
    return None


def ref_source(ref: Ref) -> dict:
    """The `source` this reference would be stored under, before any lookup.

    An arXiv ID, a DOI and a direct PDF link each state an identity by
    themselves. A landing page states nothing until `resolve_page` has turned
    it into one of those three, so it has no source here.
    """
    kind = {"arxiv": "arxiv", "doi": "doi", "pdf": "url"}.get(ref.kind)
    return {"kind": kind, "id": ref.value} if kind else {}


def find_existing_ref(ref: Ref) -> str | None:
    """Match a reference against the corpus with no lookup at all."""
    source = ref_source(ref)
    if not source:
        return None
    return find_existing({"source": source,
                          "doi": ref.value if ref.kind == "doi" else ""})


def already_stored(ref: Ref) -> str | None:
    """The paper this reference is already here as, PDF and all, or None.

    This is the quick answer to "I have this one": it costs a read of the
    corpus rather than a call to arXiv or Crossref, and those calls are what
    make an Add take seconds — arXiv queries are spaced three seconds apart.
    A paper whose PDF never landed is deliberately not an answer, since
    `ingest_ref` retries the download for exactly that case and answering
    here would leave it unrecoverable.
    """
    existing = find_existing_ref(ref)
    return existing if existing and store.pdf_path(existing).exists() else None


def ingest_ref(ref: Ref, client: httpx.Client | None = None) -> tuple[str, bool]:
    """Add one reference. Returns (key, created)."""
    config.ensure_dirs()
    own_client = client is None
    client = client or _client()
    try:
        if ref.kind == "page":
            ref = resolve_page(ref.value, client)

        # Re-pasting a reference is the common case and the reference names the
        # paper, so answer before querying arXiv or Crossref for metadata that
        # is already on file. The check under `claim_lock` further down still
        # stands: it is what two simultaneous adds of one new paper need, and
        # what catches a paper whose PDF has yet to arrive.
        settled = already_stored(ref)
        if settled:
            return settled, False

        if ref.kind == "arxiv":
            meta = fetch_arxiv(ref.value, client)
        elif ref.kind == "doi":
            meta = fetch_crossref(ref.value, client)
            # Crossref often has no PDF for a paywalled or hybrid journal, but
            # the page we came from told us where one is.
            if ref.pdf_url and not (meta.get("source") or {}).get("pdf_url"):
                meta["source"]["pdf_url"] = ref.pdf_url
        elif ref.kind == "pdf":
            meta = {
                "title": Path(ref.value.split("?")[0]).stem.replace("-", " ").replace("_", " "),
                "authors": [], "year": None, "abstract": "", "venue": "", "doi": "",
                "source": {"kind": "url", "id": ref.value, "url": ref.value, "pdf_url": ref.value},
            }
        else:
            raise ValueError(f"cannot ingest a {ref.kind} reference")

        with store.claim_lock():
            existing = find_existing(meta)
        if existing:
            # A transient download failure leaves a paper with no PDF, and every
            # later Add would return here without retrying. Recover it instead.
            pdf_url = (meta.get("source") or {}).get("pdf_url")
            if pdf_url and not store.pdf_path(existing).exists():
                try:
                    published = download_pdf(pdf_url, existing, client)
                except (httpx.HTTPError, ValueError):
                    published = None   # a failed retry is fine; the paper is here
                if published is False:
                    raise PaperRemoved(
                        f"{existing} was removed while its PDF was being recovered")
                if published:
                    with store.paper_lock(existing):
                        try:
                            paper = store.load_paper(existing)
                        except KeyError:
                            # Removed after the PDF landed. A swallowed KeyError
                            # here would report a successful ingest for a paper
                            # that no longer exists.
                            raise PaperRemoved(
                                f"{existing} was removed while its PDF was being recovered"
                            ) from None
                        paper["error"] = ""
                        store.save_paper(paper)
            return existing, False

        with store.claim_lock():
            # Re-check under the lock: the recovery path above released it.
            existing = find_existing(meta)
            if existing:
                return existing, False
            # The reservation carries the identity, so a second request for the
            # same paper finds it while this one is still fetching the PDF.
            key = store.reserve_key(store.citekey(meta["title"], meta["authors"], meta["year"]), **meta)

        pdf_url = (meta.get("source") or {}).get("pdf_url")
        failure = ""
        if pdf_url:
            try:
                if not download_pdf(pdf_url, key, client):
                    raise PaperRemoved(f"{key} was removed while its PDF was being fetched")
            except (httpx.HTTPError, ValueError) as exc:
                failure = f"PDF download failed: {exc}"
        if failure:
            with store.paper_lock(key):
                try:
                    paper = store.load_paper(key)
                except KeyError:
                    raise PaperRemoved(f"{key} was removed while it was being added") from None
                paper["error"] = failure
                store.save_paper(paper)
        return key, True
    finally:
        if own_client:
            client.close()


STAGING_SLUG_BYTES = 100


def stage_upload(source: BinaryIO, filename: str) -> Path:
    """Copy an incoming file to its own staging path, in chunks.

    A unique staging path per upload: the upload pool runs three at a time and
    two files can share a basename, in which case one job would overwrite or
    unlink the other's staging file mid-read.
    """
    config.ensure_dirs()
    # A file name is capped at 255 bytes, and the name a file was dropped under
    # can be longer than that once the prefix and random part are added. The
    # slug is ASCII, so cutting its characters cuts its bytes.
    slug = store.slugify(filename)[:STAGING_SLUG_BYTES].rstrip("-")
    handle, staged = tempfile.mkstemp(
        dir=config.pdfs_dir(), prefix=f".incoming-{slug or 'upload'}-", suffix=".pdf"
    )
    staging = Path(staged)
    try:
        with os.fdopen(handle, "wb") as fh:
            shutil.copyfileobj(source, fh, 65536)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise
    return staging


def ingest_pdf_bytes(data: bytes, filename: str) -> tuple[str, bool]:
    """Add a dropped PDF held in memory. Returns (key, created)."""
    return ingest_staged_pdf(stage_upload(io.BytesIO(data), filename), filename)


def ingest_staged_pdf(staging: Path, filename: str) -> tuple[str, bool]:
    """Add an upload already written to `staging`, and take ownership of it.

    The file is removed on every path out, including the failures, so a
    rejected or duplicate upload leaves nothing behind in the PDF directory.
    """
    try:
        with staging.open("rb") as fh:
            if fh.read(5) != b"%PDF-":
                raise ValueError(f"{filename} is not a PDF")
        # The same file again is the same paper, whatever it says inside, so
        # answer before parsing it or asking Crossref about it.
        digest = pdf_digest(staging)
        settled = settled_digest(digest)
        if settled:
            return settled, False
        with _client() as client:
            meta = guess_from_pdf(staging, client, display_name=filename)
            with store.claim_lock():
                # A `file` source is nothing but the name the upload arrived
                # under, and two unrelated PDFs can share a name, so it is
                # never an identity. The bytes are one: dropping the same
                # unidentifiable file twice used to make a second paper.
                named = (meta.get("source") or {}).get("kind") != "file"
                existing = (find_existing(meta) if named else None) or find_by_digest(digest)
                if existing:
                    key, created = existing, False
                    attach = not store.pdf_path(existing).exists()
                else:
                    # The hash goes into the reservation, so a second drop of
                    # this file recognises it while this one is still being
                    # published — the same reason the identity goes in. The
                    # stat is left out: there is no file to stat yet, and
                    # `publish_pdf` records the whole fingerprint when there is.
                    key = store.reserve_key(
                        store.citekey(meta["title"], meta["authors"], meta["year"]),
                        pdf_file={"sha256": digest}, **meta
                    )
                    created, attach = True, True

            # Publish through the same locked, atomic helper the downloads use,
            # so a concurrent Remove cannot be followed by an orphan PDF and a
            # half-copied file is never visible as the paper's PDF.
            if attach and not publish_pdf(key, staging):
                raise PaperRemoved(f"{key} was removed while its PDF was being added")
            return key, created
    finally:
        staging.unlink(missing_ok=True)
