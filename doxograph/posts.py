"""Forum posts: LessWrong, the AI Alignment Forum and the EA Forum.

A post has no arXiv ID, no DOI and no PDF, so its page gives `resolve_page`
nothing to go on. The forums do serve every post through a GraphQL API, and a
post read that way is set as a PDF here. From then on it is a paper like any
other: read by the model, its quotes checked against its text, shown in the
viewer.
"""

from __future__ import annotations

import io
import ipaddress
import os
import re
import socket
import tempfile
import zlib
from concurrent.futures import Future
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from queue import Queue
from threading import BoundedSemaphore, Lock, Thread
from time import monotonic
from urllib.parse import urlsplit

import httpx
from fpdf import FPDF
from PIL import Image

from . import config

# The Alignment Forum is a view onto LessWrong's database: an AF post is a
# LessWrong post with the same id, so the two are one source.
SITES = {
    "lesswrong.com": "lesswrong",
    "alignmentforum.org": "lesswrong",
    "forum.effectivealtruism.org": "eaforum",
}
API = {
    "lesswrong": "https://www.lesswrong.com/graphql",
    "eaforum": "https://forum.effectivealtruism.org/graphql",
}
KINDS = frozenset(API)

# `/posts/<id>/<slug>`, or `/s/<sequence>/p/<id>` for a post read inside a
# sequence. Ids are seventeen characters.
POST_PATH = re.compile(r"^/(?:posts|s/[A-Za-z0-9]+/p)/([A-Za-z0-9]{17})(?:/|$)")

QUERY = """
query ($id: String) {
  post(input: {selector: {_id: $id}}) {
    result {
      _id title postedAt pageUrl af
      user { displayName }
      coauthors { displayName }
      contents { html }
    }
  }
}
"""

# Images are what a post shows its results in, so they are worth the fetch,
# but one that is huge or slow is left out rather than holding up the post.
IMAGE_BYTES = 8_000_000
IMAGE_PIXELS = 16_000_000
IMAGE_SECONDS = 30.0
IMAGE_TIMEOUT = httpx.Timeout(20.0, connect=10.0)
IMAGE_DECODE_CHUNK = 65536
POST_IMAGES = 40
POST_IMAGE_BYTES = 32_000_000
POST_IMAGE_SECONDS = 90.0


def parse_url(url: str) -> tuple[str, str] | None:
    """(kind, post id) for a link to a forum post, or None."""
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().removeprefix("www.")
    except ValueError:
        return None
    kind = SITES.get(host)
    if not kind:
        return None
    match = POST_PATH.match(parts.path)
    return (kind, match.group(1)) if match else None


def fetch_post(kind: str, post_id: str, client: httpx.Client) -> dict:
    """The post as the forum's API returns it."""
    response = client.post(API[kind], json={"query": QUERY, "variables": {"id": post_id}})
    response.raise_for_status()
    body = response.json()
    if body.get("errors"):
        raise ValueError(f"{API[kind]} refused post {post_id}: {body['errors'][0].get('message', '')}")
    post = ((body.get("data") or {}).get("post") or {}).get("result")
    if not post:
        raise ValueError(f"{venue_of(kind, False)} has no post {post_id}")
    return post


def venue_of(kind: str, af: bool) -> str:
    if kind == "eaforum":
        return "EA Forum"
    return "AI Alignment Forum" if af else "LessWrong"


def metadata(kind: str, post: dict) -> dict:
    """The paper record for a post, in the shape `fetch_arxiv` returns."""
    posted = post.get("postedAt") or ""
    names = [post.get("user") or {}] + list(post.get("coauthors") or [])
    return {
        "title": re.sub(r"\s+", " ", post.get("title") or "").strip() or post["_id"],
        "authors": [n["displayName"].strip() for n in names if (n or {}).get("displayName")],
        "year": int(posted[:4]) if posted[:4].isdigit() else None,
        "abstract": "",
        "venue": venue_of(kind, bool(post.get("af"))),
        "doi": "",
        "source": {"kind": kind, "id": post["_id"], "url": post.get("pageUrl") or "", "pdf_url": ""},
    }


# --- HTML to blocks -------------------------------------------------------

@dataclass
class Block:
    style: str = "p"            # p | h1 | h2 | h3 | pre | img
    indent: int = 0
    prefix: str = ""
    runs: list[tuple[str, str, str]] = field(default_factory=list)  # text, B/I, link
    src: str = ""

    def empty(self) -> bool:
        return not "".join(text for text, _, _ in self.runs).strip()


BLOCK_TAGS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "pre",
              "blockquote", "figure", "figcaption", "tr", "section", "hr"}
# Content a reader never sees as text: scripts, styles, inline drawings, and
# the per-glyph markup MathJax lays out a formula with (its source is kept from
# the formula's aria-label instead).
SKIPPED = {"script", "style", "svg", "noscript", "button", "math"}


class _Reader(HTMLParser):
    """Flatten a post's HTML into paragraphs, headings, list items and images.

    Only the structure a reader of the PDF needs survives. Tables become one
    line per row with the cells joined by bars, which keeps every word of a
    results table in the text that quotes are checked against.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[Block] = []
        self.block = Block()
        self.bold = self.italic = 0
        self.links: list[str] = []
        self.lists: list[list] = []      # [ordered, next number] per open list
        self.quotes = 0
        self.cells = 0                   # open table cells
        self.row_has_cell = False
        self.pre = 0
        self.skip: list[str] = []        # the tag being skipped, once per nesting

    # Blocks

    def _indent(self) -> int:
        return len(self.lists) + self.quotes

    def _flush(self) -> None:
        if not self.block.empty() or self.block.style == "img":
            self.blocks.append(self.block)
        self.block = Block(indent=self._indent())

    def _open(self, style: str = "p", prefix: str = "") -> None:
        # A paragraph inside a list item continues the item when the item has
        # no words yet; otherwise it starts a new, unbulleted paragraph under it.
        if self.block.empty() and self.block.style != "img":
            self.block.style = style if style != "p" else self.block.style
            self.block.prefix = prefix or self.block.prefix
            self.block.indent = self._indent()
            return
        self._flush()
        self.block.style, self.block.prefix = style, prefix

    def _style(self) -> str:
        return ("B" if self.bold else "") + ("I" if self.italic else "")

    def _text(self, text: str) -> None:
        link = self.links[-1] if self.links else ""
        self.block.runs.append((text, self._style(), link))

    # Parser callbacks

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self.skip:
            if tag == self.skip[-1]:
                self.skip.append(tag)
            return
        if tag in SKIPPED or tag.startswith("mjx-"):
            self.skip.append(tag)
            return
        classes = (attrs.get("class") or "").split()
        if "mjx-math" in classes:
            # MathJax spells each glyph out in its own span; the label is the
            # formula as one line of text.
            self._text(f" {attrs.get('aria-label') or ''} ")
            self.skip.append(tag)
            return
        if self.cells:
            if tag in ("br", "p", "div", "li"):
                self._text(" ")
            elif tag == "img":
                self._text(f" [image{': ' + attrs['alt'] if attrs.get('alt') else ''}] ")
            self._inline(tag, attrs)
            return
        if tag in ("td", "th"):
            if self.row_has_cell:
                self._text("  |  ")
            self.cells += 1
            self.row_has_cell = True
            if tag == "th":
                self.bold += 1
            return
        if tag in ("ul", "ol"):
            self._flush()
            self.lists.append([tag == "ol", 1])
            self.block.indent = self._indent()
            return
        if tag == "blockquote":
            self._flush()
            self.quotes += 1
            self.block.indent = self._indent()
            return
        if tag == "li":
            if self.lists and self.lists[-1][0]:
                number = attrs.get("value")
                if number and number.isdigit():
                    self.lists[-1][1] = int(number)
                prefix = f"{self.lists[-1][1]}. "
                self.lists[-1][1] += 1
            else:
                prefix = "• "
            self._flush()
            self.block.prefix = prefix
            self.block.indent = max(self._indent() - 1, 0)
            return
        if tag == "img":
            src = attrs.get("src") or ""
            if src.startswith(("http://", "https://")):
                self._flush()
                self.block.style, self.block.src = "img", src
                self.block.runs = [(attrs.get("alt") or "", "", "")]
                self._flush()
            return
        if tag == "br":
            self._text("\n")
            return
        if tag == "hr":
            self._flush()
            return
        if tag == "pre":
            self._open("pre")
            self.pre += 1
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._open("h" + str(min(int(tag[1]), 3)))
            return
        if tag == "tr":
            self._flush()
            self.row_has_cell = False
            return
        if tag == "figcaption":
            self._open()
            self.italic += 1
            return
        if tag in BLOCK_TAGS:
            self._open()
            return
        self._inline(tag, attrs)

    def _inline(self, tag, attrs):
        if tag in ("b", "strong"):
            self.bold += 1
        elif tag in ("i", "em"):
            self.italic += 1
        elif tag == "a":
            href = attrs.get("href") or ""
            self.links.append(href if href.startswith(("http://", "https://")) else "")

    def handle_startendtag(self, tag, attrs):
        # A void element written `<br/>` never gets an end tag; no others do.
        self.handle_starttag(tag, attrs)
        if tag not in ("img", "br", "hr") and not self.skip:
            self.handle_endtag(tag)
        elif self.skip and tag == self.skip[-1]:
            self.skip.pop()

    def handle_endtag(self, tag):
        if self.skip:
            if tag == self.skip[-1]:
                self.skip.pop()
            return
        if tag in ("b", "strong"):
            self.bold = max(self.bold - 1, 0)
        elif tag in ("i", "em"):
            self.italic = max(self.italic - 1, 0)
        elif tag == "a":
            if self.links:
                self.links.pop()
        elif tag in ("td", "th"):
            self.cells = max(self.cells - 1, 0)
            if tag == "th":
                self.bold = max(self.bold - 1, 0)
        elif self.cells:
            return
        elif tag in ("ul", "ol"):
            self._flush()
            if self.lists:
                self.lists.pop()
            self.block.indent = self._indent()
        elif tag == "blockquote":
            self._flush()
            self.quotes = max(self.quotes - 1, 0)
            self.block.indent = self._indent()
        elif tag == "pre":
            self.pre = max(self.pre - 1, 0)
            self._flush()
        elif tag == "figcaption":
            self.italic = max(self.italic - 1, 0)
            self._flush()
        elif tag in BLOCK_TAGS:
            self._flush()

    def handle_data(self, data):
        if self.skip:
            return
        if not self.pre:
            data = re.sub(r"\s+", " ", data)
        if data:
            self._text(data)

    def close(self) -> list[Block]:
        super().close()
        self._flush()
        return self.blocks


def blocks_of(html: str) -> list[Block]:
    reader = _Reader()
    reader.feed(html)
    return reader.close()


# --- Blocks to PDF --------------------------------------------------------

FONTS = Path(__file__).parent / "static" / "vendor" / "standard_fonts"
FACES = {"": "Regular", "B": "Bold", "I": "Italic", "BI": "BoldItalic"}
SIZES = {"h1": 16, "h2": 14, "h3": 12, "p": 10.5, "pre": 9}
INDENT_MM = 6
# A wider face to fall back on for the characters Liberation Sans lacks
# (arrows, math symbols, most scripts other than Latin, Greek and Cyrillic),
# where the system has one.
FALLBACKS = [
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def _clean(text: str) -> str:
    # Control characters other than the line breaks a <br> leaves have no
    # glyph in any font.
    return re.sub("[\\x00-\\x09\\x0b-\\x1f\\x7f\\u200b\\ufeff]", "", text)


class _ImageResolver:
    """A fixed daemon pool: expired DNS calls cannot accumulate more workers.

    Running getaddrinfo calls cannot be cancelled. Their slots remain occupied
    until they finish, and new work fails closed when all slots are occupied.
    No executor context or shutdown waits for a late resolver answer.
    """

    def __init__(self, workers: int = 2):
        self.jobs = Queue(maxsize=workers)
        self.slots = BoundedSemaphore(workers)
        self.threads = [Thread(target=self._run, daemon=True, name="post-image-dns") for _ in range(workers)]
        for thread in self.threads:
            thread.start()

    def _run(self):
        while True:
            job = self.jobs.get()
            if job is None:
                self.jobs.task_done()
                return
            future, resolver, host, port, deadline = job
            try:
                if future.set_running_or_notify_cancel():
                    try:
                        _image_remaining(deadline)
                        result = resolver(host, port, type=socket.SOCK_STREAM)
                    except BaseException as exc:
                        future.set_exception(exc)
                    else:
                        future.set_result(result)
            finally:
                self.slots.release()
                self.jobs.task_done()

    def resolve(self, resolver, host: str, port: int, deadline: float):
        _image_remaining(deadline)
        if not self.slots.acquire(blocking=False):
            raise ValueError("image DNS workers are occupied")
        future = Future()
        try:
            self.jobs.put_nowait((future, resolver, host, port, deadline))
        except BaseException:
            self.slots.release()
            raise
        try:
            return future.result(timeout=_image_remaining(deadline))
        except TimeoutError:
            future.cancel()  # Cancels queued work; running work keeps its slot.
            raise


_image_resolver = None
_image_resolver_lock = Lock()


def _resolve(host: str, port: int, deadline: float):
    global _image_resolver
    with _image_resolver_lock:
        if _image_resolver is None:
            _image_resolver = _ImageResolver()
    # Capture the callable before dispatch, including when tests mock DNS.
    return _image_resolver.resolve(socket.getaddrinfo, host, port, deadline)


@dataclass
class _PostImageBudget:
    """Shared attempt, elapsed-time, received-byte and decoded-byte limits."""

    deadline: float
    images_left: int
    bytes_left: int
    received_left: int
    reason: str = ""

    def available(self) -> bool:
        if monotonic() >= self.deadline:
            self.reason = "post image time budget exhausted"
        elif self.bytes_left <= 0 or self.received_left <= 0:
            self.reason = "post image byte budget exhausted"
        elif self.images_left <= 0:
            self.reason = "post image count budget exhausted"
        return not self.reason

    def begin(self) -> float:
        if not self.available():
            raise ValueError(self.reason)
        self.images_left -= 1
        return min(self.deadline, monotonic() + IMAGE_SECONDS)

    def consume(self, amount: int, *, received: bool = False):
        field_name = "received_left" if received else "bytes_left"
        remaining = getattr(self, field_name)
        setattr(self, field_name, max(0, remaining - amount))
        if amount > remaining:
            self.reason = "post image byte budget exhausted"
            raise ValueError(self.reason)


def _image_targets(url: httpx.URL, deadline: float) -> list[httpx.URL]:
    """Resolve and validate all addresses before returning pinned destinations."""
    if (url.scheme not in {"http", "https"} or not url.host or url.userinfo
            or "%" in url.host or url.host.rstrip(".").lower().endswith((".localhost", ".local"))
            or url.host.rstrip(".").lower() == "localhost"):
        raise ValueError("not a public image URL")
    addresses = _resolve(url.host, url.port or (443 if url.scheme == "https" else 80), deadline)
    if not addresses:
        raise ValueError("image host has no addresses")
    for _, _, _, _, address in addresses:
        ip = ipaddress.ip_address(address[0])
        if (not ip.is_global or ip.is_multicast
                or (isinstance(ip, ipaddress.IPv6Address)
                    and (ip.ipv4_mapped or ip.sixtofour or ip.teredo))):
            raise ValueError("image host has a non-public address")
    # Avoid a second DNS lookup between validation and connecting. Keep the
    # original HTTP Host and TLS name below, so public CDN images still work.
    return [url.copy_with(host=address) for address in dict.fromkeys(a[4][0] for a in addresses)]


def _image_remaining(deadline: float) -> float:
    """Remaining shared budget for DNS, address attempts, redirects and body."""
    remaining = deadline - monotonic()
    if remaining <= 0:
        raise TimeoutError("image download deadline exceeded")
    return remaining


def _image_body(response, deadline: float, budget, received_by_trace: bool) -> bytes | None:
    """Read raw HTTP bytes and bound each decompression allocation first.

    Never use HTTPX's iter_bytes/content decoder here: its decompress call
    creates the full expanded chunk before the caller can check any limit.
    """
    if response.is_stream_consumed:
        # Mock/custom transports may supply an already-buffered decoded body.
        # Do not decode it again or make a second unbounded copy.
        content = memoryview(response.content)
        raw_chunks = (content[i:i + IMAGE_DECODE_CHUNK] for i in range(0, len(content), IMAGE_DECODE_CHUNK))
        encoding = "identity"
    else:
        encoding = response.headers.get("content-encoding", "identity").strip().lower()
        if encoding not in {"", "identity", "gzip", "deflate"}:
            return None  # Unsupported/stacked encodings remain linked placeholders.
        raw_chunks = response.iter_raw()

    data = bytearray()
    decoder = None
    prefix = b""

    def output_limit():
        remaining = IMAGE_BYTES - len(data)
        if budget is not None:
            remaining = min(remaining, budget.bytes_left)
        # A single sentinel byte detects overflow without expanding the rest.
        return max(1, min(IMAGE_DECODE_CHUNK, remaining + 1))

    def append(chunk):
        _image_remaining(deadline)
        if budget is not None:
            budget.consume(len(chunk))
        if len(data) + len(chunk) > IMAGE_BYTES:
            raise ValueError("image decoded byte limit exceeded")
        data.extend(chunk)

    for raw in raw_chunks:
        _image_remaining(deadline)
        if budget is not None and not received_by_trace:
            budget.consume(len(raw), received=True)
        if not raw:
            continue
        if encoding in {"", "identity"}:
            for offset in range(0, len(raw), IMAGE_DECODE_CHUNK):
                append(memoryview(raw)[offset:offset + IMAGE_DECODE_CHUNK])
            continue

        if decoder is not None and decoder.eof:
            if encoding != "gzip":
                return None
            decoder = None  # A gzip body can contain concatenated members.
        if decoder is None:
            if encoding == "deflate":
                # Both zlib-wrapped and raw deflate occur in HTTP. Determine
                # which using at most two bytes, including split headers.
                take = min(2 - len(prefix), len(raw))
                prefix += bytes(raw[:take])
                raw = raw[take:]
                if len(prefix) < 2:
                    continue
                wrapped = ((prefix[0] & 15) == 8 and (prefix[0] >> 4) <= 7
                           and int.from_bytes(prefix, "big") % 31 == 0)
                decoder = zlib.decompressobj(zlib.MAX_WBITS if wrapped else -zlib.MAX_WBITS)
                raw = prefix + bytes(raw)
                prefix = b""
            else:
                decoder = zlib.decompressobj(zlib.MAX_WBITS | 16)

        pending = raw
        while True:
            _image_remaining(deadline)
            limit = output_limit()
            chunk = decoder.decompress(pending, max_length=limit)
            append(chunk)
            if decoder.eof:
                pending = decoder.unused_data
                if pending:
                    if encoding != "gzip":
                        return None
                    decoder = zlib.decompressobj(zlib.MAX_WBITS | 16)
                    continue
                break
            pending = decoder.unconsumed_tail
            if not pending and len(chunk) < limit:
                break
            # Drain buffered output in bounded steps; flush() has no hard
            # output-size cap and must not be used for untrusted bodies.

    _image_remaining(deadline)
    if encoding not in {"", "identity"} and (decoder is None or not decoder.eof):
        return None
    return bytes(data)


class _ImageTrace:
    """Apply the deadline to httpcore socket I/O before response headers exist.

    The trace extension exposes each new TCP/TLS stream. Its reads and writes
    receive the shrinking budget, including reads performed by the HTTP header
    parser and proxy CONNECT parser. The connection belongs to this request
    and is closed afterward; do not reuse an unguarded or multiplexed stream.
    """

    def __init__(self, deadline: float, budget=None, *, hostname=None, target_host=None):
        self.hostname = hostname
        self.target_host = target_host
        self.connected_host = None
        self.deadline = deadline
        self.guarded = False
        self.budget = budget

    def __call__(self, event: str, info: dict):
        operations = ("connect_tcp", "connect_unix_socket", "start_tls")
        if any(event.endswith(f"{operation}.started") for operation in operations):
            remaining = _image_remaining(self.deadline)
            if event == "connection.connect_tcp.started":
                self.connected_host = info["host"]
            elif event == "connection.start_tls.started" and self.hostname is not None:
                # HTTPcore forwards request SNI to an HTTPS proxy's outer TLS.
                # Verify that connection against the proxy's own hostname;
                # only a direct connection to the pinned IP uses image SNI.
                host = self.connected_host
                if isinstance(host, bytes):
                    host = host.decode("ascii")
                if host is not None:
                    info["server_hostname"] = self.hostname if host == self.target_host else host
            elif event == "proxy.start_tls.started" and self.hostname is not None:
                # CONNECT still names the validated IP. Its inner TLS must
                # verify the original image hostname, never the numeric IP.
                info["server_hostname"] = self.hostname
            timeout = info.get("timeout")
            info["timeout"] = remaining if timeout is None else min(timeout, remaining)
        elif any(event.endswith(f"{operation}.complete") for operation in operations):
            stream = info["return_value"]
            try:
                _image_remaining(self.deadline)
                original_read, original_write = stream.read, stream.write

                def bounded_read(max_bytes, timeout=None):
                    remaining = _image_remaining(self.deadline)
                    if self.budget is not None:
                        if self.budget.received_left <= 0:
                            self.budget.consume(1, received=True)
                        max_bytes = min(max_bytes, self.budget.received_left)
                    data = original_read(max_bytes, timeout=remaining if timeout is None else min(timeout, remaining))
                    _image_remaining(self.deadline)
                    if self.budget is not None:
                        self.budget.consume(len(data), received=True)
                    return data

                def bounded_write(data, timeout=None):
                    remaining = _image_remaining(self.deadline)
                    original_write(data, timeout=remaining if timeout is None else min(timeout, remaining))
                    _image_remaining(self.deadline)

                stream.read, stream.write = bounded_read, bounded_write
                self.guarded = True
            except (AttributeError, TypeError) as exc:
                stream.close()
                raise ValueError("image transport cannot enforce a socket deadline") from exc
            except BaseException:
                stream.close()
                raise
        elif event == "http11.receive_response_headers.started":
            _image_remaining(self.deadline)
            if not self.guarded:
                raise ValueError("image transport reused an unguarded connection")
        elif event.startswith("http2.") and event.endswith(".started") and "response_closed" not in event:
            raise ValueError("image deadline requires a dedicated HTTP/1.1 connection")


def _image(src: str, client: httpx.Client, until: float = float("inf"), *,
           budget: _PostImageBudget | None = None) -> bytes | None:
    """Fetch a bounded image, validating and pinning every redirect hop.

    `until` is the shared post deadline; `budget` additionally accounts for
    received/decoded bytes and attempts across images, including failures.
    """
    try:
        deadline = min(budget.begin() if budget is not None else monotonic() + IMAGE_SECONDS, until)
        url = httpx.URL(src)
        for hop in range(6):
            _image_remaining(deadline)
            targets = _image_targets(url, deadline)
            for target in targets:
                remaining = _image_remaining(deadline)
                timeout = httpx.Timeout(
                    min(IMAGE_TIMEOUT.read, remaining),
                    connect=min(IMAGE_TIMEOUT.connect, remaining),
                )
                trace = _ImageTrace(deadline, budget, hostname=url.host, target_host=target.host)
                try:
                    with client.stream(
                        "GET", target, follow_redirects=False, timeout=timeout,
                        headers={"Host": url.netloc.decode("ascii"), "Connection": "close",
                                 "Accept-Encoding": "gzip, deflate"},
                        extensions={"sni_hostname": url.host, "trace": trace},
                    ) as response:
                        _image_remaining(deadline)
                        if response.status_code in {301, 302, 303, 307, 308}:
                            location = response.headers.get("location")
                            if not location or hop == 5:
                                return None
                            url = url.join(location)
                            break
                        response.raise_for_status()
                        return _image_body(response, deadline, budget, trace.guarded)
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    # Keep DNS pinning while allowing another public address
                    # (e.g. IPv4 after an unavailable IPv6 route) to connect.
                    continue
            else:
                return None
    except (httpx.HTTPError, httpx.InvalidURL, ValueError, OSError, zlib.error):
        if budget is not None and monotonic() >= budget.deadline:
            budget.reason = "post image time budget exhausted"
        return None


def render(meta: dict, html: str, client: httpx.Client) -> bytes:
    """The post as a PDF: a title block, then the body."""
    pdf = FPDF(format="letter")
    pdf.set_auto_page_break(True, margin=18)
    pdf.set_margins(20, 18, 20)
    for style, face in FACES.items():
        pdf.add_font("Body", style, FONTS / f"LiberationSans-{face}.ttf")
    fallbacks = []
    for i, path in enumerate(p for p in FALLBACKS if os.path.exists(p)):
        pdf.add_font(f"Fallback{i}", "", path)
        fallbacks.append(f"Fallback{i}")
    if fallbacks:
        pdf.set_fallback_fonts(fallbacks, exact_match=False)
    pdf.set_title(meta["title"])
    pdf.set_author(", ".join(meta["authors"]))
    pdf.add_page()
    width = pdf.epw

    def write(block: Block) -> None:
        size = SIZES.get(block.style, SIZES["p"])
        height = size * 0.5
        left = 20 + block.indent * INDENT_MM
        pdf.set_left_margin(left)
        pdf.set_x(left)
        if block.style.startswith("h"):
            pdf.ln(size * 0.3)
        if block.prefix:
            pdf.set_font("Body", "", size)
            pdf.write(height, block.prefix)
        for text, style, link in block.runs:
            text = _clean(text)
            if not text:
                continue
            pdf.set_font("Body", "B" if block.style.startswith("h") else style, size)
            pdf.write(height, text, link=link)
        pdf.ln(height + (2.5 if block.style != "pre" else 1))

    source = meta["source"]
    byline = ", ".join(meta["authors"])
    when = f"{meta['venue']}, {meta['year']}" if meta.get("year") else meta["venue"]
    write(Block(style="h1", runs=[(meta["title"], "B", "")]))
    header = [(byline + "\n", "", "")] if byline else []
    header.append((when, "I", ""))
    if source.get("url"):
        header.append(("\n" + source["url"], "", source["url"]))
    write(Block(runs=header))

    images_until = monotonic() + POST_IMAGE_SECONDS
    image_budget = _PostImageBudget(images_until, POST_IMAGES, POST_IMAGE_BYTES, POST_IMAGE_BYTES)
    fetched = 0
    for block in blocks_of(html):
        if block.style != "img":
            write(block)
            continue
        data = None
        if fetched < POST_IMAGES and image_budget.available():
            fetched += 1
            data = _image(block.src, client, images_until, budget=image_budget)
        elif not image_budget.reason:
            image_budget.reason = "post image count budget exhausted"
        if not (data and _placed(pdf, data, width)):
            # A placeholder says where the image was.
            alt = block.runs[0][0] if block.runs else ""
            label = f"[image{': ' + alt if alt else ''}]"
            if image_budget.reason:
                label += f" ({image_budget.reason})"
            write(Block(runs=[(label, "I", block.src)]))
    # fpdf2 records unsupported characters while writing, but otherwise only
    # warns and omits them from the PDF. Never publish that loss as a success,
    # including on systems where no broad-script fallback font is available.
    missing = sorted({codepoint for font in pdf.fonts.values() for codepoint in font.missing_glyphs})
    if missing:
        codes = ", ".join(f"U+{codepoint:04X}" for codepoint in missing[:10])
        raise ValueError(f"could not preserve post text: available PDF fonts lack {codes}")
    return bytes(pdf.output())


def _placed(pdf, data: bytes, width: float) -> bool:
    """Draw an image at its own size, shrunk to fit the page.

    False for what Pillow cannot open, an SVG or a truncated file, before
    anything is drawn.
    """
    try:
        with Image.open(io.BytesIO(data)) as image:
            pixels_w, pixels_h = image.size
            if pixels_w <= 0 or pixels_h <= 0 or pixels_w * pixels_h > IMAGE_PIXELS:
                return False
            image.load()
    except Exception:
        return False
    w_mm, h_mm = pixels_w * 0.264583, pixels_h * 0.264583   # at 96 dpi
    scale = min(1.0, width / w_mm, (pdf.eph * 0.8) / h_mm)
    w, h = w_mm * scale, h_mm * scale
    if pdf.will_page_break(h):
        pdf.add_page()
    pdf.set_left_margin(20)
    try:
        pdf.image(io.BytesIO(data), x=20 + (width - w) / 2, w=w, h=h)
    except Exception:
        return False
    pdf.ln(3)
    return True


def render_to_staging(meta: dict, html: str, client: httpx.Client) -> Path:
    """Render a post into its own staging file beside the PDFs and return the path."""
    config.pdfs_dir().mkdir(parents=True, exist_ok=True)
    handle, staged = tempfile.mkstemp(dir=config.pdfs_dir(), prefix=".render-", suffix=".pdf")
    os.close(handle)
    staging = Path(staged)
    try:
        try:
            data = render(meta, html, client)
        except Exception as exc:
            # fpdf2 reports what it cannot lay out with its own exception types;
            # to the caller this is one more way the PDF could not be had.
            raise ValueError(f"could not set {meta['source']['url'] or meta['title']} as a PDF: {exc}") from exc
        staging.write_bytes(data)
        return staging
    except BaseException:
        staging.unlink(missing_ok=True)
        raise
