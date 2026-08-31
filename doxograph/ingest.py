"""Turn a pasted reference or a dropped file into a paper in the store.

Accepts arXiv IDs and URLs, DOIs, direct PDF links, landing pages that
advertise a PDF, and local PDF files.
"""

from __future__ import annotations

import re
import os
import tempfile
import html as html_module
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

import httpx

from . import config, store

class PaperRemoved(RuntimeError):
    """The paper was deleted while it was being ingested.

    Publication returns False in that case. Reporting success anyway would mark
    a job done for a paper that no longer exists, and the CLI would then crash
    trying to read its notes.
    """


USER_AGENT = "doxograph/0.1 (+https://github.com/jss367/doxograph)"
TIMEOUT = httpx.Timeout(60.0, connect=15.0)

ARXIV_NEW = r"\d{4}\.\d{4,5}(?:v\d+)?"
ARXIV_OLD = r"[a-z][a-z-]+(?:\.[A-Z]{2})?/\d{7}(?:v\d+)?"
DOI_RE = r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+"

def normalize_doi(doi: str) -> str:
    """Trim citation punctuation off a DOI picked up from surrounding prose.

    DOI_RE has to allow `.`, `;`, `(` and `)` because DOIs genuinely contain
    them, so a DOI pasted from a sentence carries the sentence's punctuation
    into the Crossref request. A trailing `)` is dropped only when unbalanced,
    which leaves keys like 10.1002/(SICI)1097-0258(19980815)17:15 intact.
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
    re.compile(rf"arxiv\.org/(?:abs|pdf)/({ARXIV_NEW}|{ARXIV_OLD})", re.I),
    re.compile(rf"arxiv[:\s]+({ARXIV_NEW}|{ARXIV_OLD})", re.I),
    re.compile(rf"^({ARXIV_NEW}|{ARXIV_OLD})$"),
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
        elif ref.value not in seen:
            seen.add(ref.value)
            refs.append(ref)
    return refs, unknown


# --- metadata sources -----------------------------------------------------

ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"


def fetch_arxiv(arxiv_id: str, client: httpx.Client) -> dict:
    bare = re.sub(r"v\d+$", "", arxiv_id)
    response = client.get(
        "https://export.arxiv.org/api/query",
        params={"id_list": bare, "max_results": 1},
    )
    response.raise_for_status()
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
        "authors": [
            " ".join(p for p in [a.get("given", ""), a.get("family", "")] if p).strip()
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


def resolve_page(url: str, client: httpx.Client) -> Ref:
    """Identify a landing page from its own metadata.

    Deliberately does not search the whole document for an arXiv link: a journal
    page cites other papers in its bibliography and related-articles list, and
    picking one of those would silently ingest and extract the wrong paper.
    Identity comes from citation metadata, the canonical URL, or the head.
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
        match = re.search(rf"arxiv\.org/(?:abs|pdf)/({ARXIV_NEW}|{ARXIV_OLD})", candidate, re.I)
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

    if advertised:
        return Ref("pdf", advertised, url)

    # Last resort, and only within the head, where a bibliography does not reach.
    match = re.search(rf"arxiv\.org/(?:abs|pdf)/({ARXIV_NEW}|{ARXIV_OLD})", head, re.I)
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

def pdf_first_page_text(path: Path, pages: int = 2) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages[:pages])
    except Exception:
        return ""


def guess_from_pdf(path: Path, client: httpx.Client, display_name: str | None = None) -> dict:
    """Try arXiv ID, then DOI, then fall back to the filename as a title.

    `display_name` is the name the file arrived under. The path itself is a
    randomized staging file, so using its stem would put an `mkstemp` suffix in
    the title and citekey and give the same PDF different metadata every upload.
    """
    text = pdf_first_page_text(path)
    match = re.search(rf"arXiv:({ARXIV_NEW}|{ARXIV_OLD})", text, re.I)
    if match:
        try:
            return fetch_arxiv(match.group(1), client)
        except (httpx.HTTPError, ValueError, ET.ParseError):
            pass
    match = re.search(DOI_RE, text)
    if match:
        try:
            return fetch_crossref(normalize_doi(match.group(0)), client)
        except (httpx.HTTPError, KeyError, ValueError):
            pass
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
        if staging.read_bytes()[:5] != b"%PDF-":
            raise ValueError(f"{url} returned {content_type or 'unknown content'} rather than a PDF")
        return staging
    except BaseException:
        staging.unlink(missing_ok=True)
        raise


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
        return True


def download_pdf(url: str, key: str, client: httpx.Client) -> bool:
    """Fetch a PDF and attach it to a paper. False if the paper went away."""
    return publish_pdf(key, fetch_pdf(url, client))


def _client() -> httpx.Client:
    return httpx.Client(timeout=TIMEOUT, headers={"User-Agent": USER_AGENT})


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


def ingest_ref(ref: Ref, client: httpx.Client | None = None) -> tuple[str, bool]:
    """Add one reference. Returns (key, created)."""
    config.ensure_dirs()
    own_client = client is None
    client = client or _client()
    try:
        if ref.kind == "page":
            ref = resolve_page(ref.value, client)

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
                        paper["notes"] = ""
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
        notes = ""
        if pdf_url:
            try:
                if not download_pdf(pdf_url, key, client):
                    raise PaperRemoved(f"{key} was removed while its PDF was being fetched")
            except (httpx.HTTPError, ValueError) as exc:
                notes = f"PDF download failed: {exc}"
        if notes:
            with store.paper_lock(key):
                try:
                    paper = store.load_paper(key)
                except KeyError:
                    raise PaperRemoved(f"{key} was removed while it was being added") from None
                paper["notes"] = notes
                store.save_paper(paper)
        return key, True
    finally:
        if own_client:
            client.close()


def ingest_pdf_bytes(data: bytes, filename: str) -> tuple[str, bool]:
    """Add a dropped PDF. Returns (key, created)."""
    config.ensure_dirs()
    if not data.startswith(b"%PDF-"):
        raise ValueError(f"{filename} is not a PDF")
    # A unique staging path per upload: the upload pool runs three at a time and
    # two files can share a basename, in which case one job would overwrite or
    # unlink the other's staging file mid-read.
    handle, staged = tempfile.mkstemp(
        dir=config.pdfs_dir(), prefix=f".incoming-{store.slugify(filename) or 'upload'}-", suffix=".pdf"
    )
    staging = Path(staged)
    try:
        with os.fdopen(handle, "wb") as fh:
            fh.write(data)
    except BaseException:
        staging.unlink(missing_ok=True)
        raise
    try:
        with _client() as client:
            meta = guess_from_pdf(staging, client, display_name=filename)
            with store.claim_lock():
                existing = find_existing(meta)
                if existing and (meta.get("source") or {}).get("kind") != "file":
                    key, created = existing, False
                    attach = not store.pdf_path(existing).exists()
                else:
                    key = store.reserve_key(
                        store.citekey(meta["title"], meta["authors"], meta["year"]), **meta
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
