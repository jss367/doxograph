"""Links to a paper's code: where its repository is, as a list of web addresses."""

import re
from ipaddress import IPv6Address
from urllib.parse import SplitResult, unquote, urlsplit

import idna

MAX_LINKS = 20
MAX_LENGTH = 2000
LinkKey = tuple[str, str | None, int | None, str | None, str | None, str, str, str]


def _identity(parts: SplitResult) -> LinkKey:
    # Scheme/hostname ignore case; credentials, path, query and fragment do not.
    return (parts.scheme.lower(), parts.hostname.lower() if parts.hostname else None, parts.port, parts.username, parts.password,
            parts.path, parts.query, parts.fragment)


def _domain_host(host: str) -> str:
    if re.search(r"%(?![0-9a-fA-F]{2})", host):
        raise ValueError("Malformed host escape")
    host = idna.uts46_remap(unquote(host, errors="strict"), std3_rules=False)
    # Decode before checking: escapes cannot hide forbidden host characters.
    if any(ord(char) <= 32 or ord(char) == 127 or char in "%#/:<>?@[\\]^|" for char in host):
        raise ValueError("Forbidden host character")
    for label in host.split("."):
        if label:
            # Browsers use non-strict UTS46: underscores, empty labels, emoji
            # and edge hyphens are allowed, but bidi/joiner rules still apply.
            idna.check_initial_combiner(label)
            idna.check_bidi(label)
            if any(char in "\u200c\u200d" and not idna.valid_contextj(label, index)
                   for index, char in enumerate(label)):
                raise ValueError("Invalid domain joiner")
    return host


def _valid_host(host: str) -> bool:
    if ":" in host:
        if "%" in host:  # Browsers do not accept IPv6 zone identifiers.
            return False
        IPv6Address(host)
        return True
    host = _domain_host(host)
    if "." not in host:
        return False
    # Browser URLs recognize dotted decimal, octal, hex, and shortened IPv4.
    # An address ending in a number must parse as IPv4, not as a domain name.
    pieces = host.removesuffix(".").split(".")
    if not re.fullmatch(r"[0-9]+|0[xX][0-9a-fA-F]*", pieces[-1]):
        return True
    if len(pieces) > 4:
        return False
    numbers = []
    for piece in pieces:
        base = 10
        if piece.lower().startswith("0x"):
            base, piece = 16, piece[2:]
        elif len(piece) > 1 and piece.startswith("0"):
            base, piece = 8, piece[1:]
        if piece and not re.fullmatch({8:r"[0-7]+", 10:r"[0-9]+", 16:r"[0-9a-fA-F]+"}[base], piece):
            return False
        if not piece and base == 10:
            return False
        numbers.append(int(piece, base) if piece else 0)
    return all(number <= 255 for number in numbers[:-1]) and numbers[-1] < 256 ** (5 - len(numbers))

# Repositories named in the paper's own abstract or arXiv comment. Only hosts
# with at least a namespace and project are recognized. GitLab namespaces may
# be nested; the other supported hosts use owner/repository paths.
_REPOSITORY = re.compile(
    r"(?<![\w.-])(?:https?://)?(?:www\.)?"
    r"(github\.com|gitlab\.com|bitbucket\.org|codeberg\.org)/([\w.-]+(?:/[\w.-]+)+)",
    re.I,
)


def normalize(values: list[str]) -> list[str]:
    """Web addresses in the order given, each once.

    An address without a scheme, as copied from a page ("github.com/a/b"), is
    taken as https. Anything else that is not an http or https address is
    refused rather than stored: the page renders each one as a link, and a
    `javascript:` address there would run when clicked.
    """
    links: dict[LinkKey, str] = {}
    for value in values:
        link = value.strip()
        if not link:
            continue
        if "://" not in link:
            link = "https://" + link
        try:
            parts = urlsplit(link)
            host = parts.hostname or ""
            # Accessing hostname does not validate a numeric/ranged port.
            identity = _identity(parts)
            valid_host = _valid_host(host)
        except ValueError:
            parts, valid_host = None, False
        if (parts is None or parts.scheme.lower() not in ("http", "https") or not valid_host
                or any(c.isspace() for c in link) or len(link) > MAX_LENGTH):
            raise ValueError(f"{value.strip()!r} is not a web address. Code links start with https://.")
        links.setdefault(identity, link)
    if len(links) > MAX_LINKS:
        raise ValueError(f"A paper can have at most {MAX_LINKS} code links.")
    return list(links.values())


def find(text: str) -> list[str]:
    """Repository addresses a paper's own text names, in the order it names them."""
    links: dict[LinkKey, str] = {}
    for host, path in _REPOSITORY.findall(text or ""):
        parts = path.split("/")
        if host.lower() == "gitlab.com":
            # GitLab separates repository views from a nested project with /-/.
            if "-" in parts:
                parts = parts[:parts.index("-")]
        else:
            parts = parts[:2]
        if len(parts) < 2:
            continue
        # A sentence can end on the address, and a clone address ends in `.git`.
        parts[-1] = re.sub(r"\.git$", "", parts[-1].rstrip("."), flags=re.I)
        if parts[-1]:
            link = f"https://{host.lower()}/{'/'.join(parts)}"
            links.setdefault(_identity(urlsplit(link)), link)
    return list(links.values())[:MAX_LINKS]
