"""Links to a paper's code: where its repository is, as a list of web addresses."""

import re
from urllib.parse import urlsplit

MAX_LINKS = 20
MAX_LENGTH = 2000

# Repositories named in the paper's own abstract or arXiv comment. Only hosts
# whose first two path segments are an owner and a repository are recognized,
# so a link to a user, an organization, or a project page is not taken for one.
_REPOSITORY = re.compile(
    r"(?<![\w.-])(?:https?://)?(?:www\.)?"
    r"(github\.com|gitlab\.com|bitbucket\.org|codeberg\.org)/([\w.-]+)/([\w.-]+)",
    re.I,
)


def normalize(values: list[str]) -> list[str]:
    """Web addresses in the order given, each once.

    An address without a scheme, as copied from a page ("github.com/a/b"), is
    taken as https. Anything else that is not an http or https address is
    refused rather than stored: the page renders each one as a link, and a
    `javascript:` address there would run when clicked.
    """
    links: dict[str, str] = {}
    for value in values:
        link = value.strip()
        if not link:
            continue
        if "://" not in link:
            link = "https://" + link
        try:
            parts = urlsplit(link)
            host = parts.hostname or ""
        except ValueError:
            parts, host = None, ""
        if (parts is None or parts.scheme.lower() not in ("http", "https") or "." not in host
                or any(c.isspace() for c in link) or len(link) > MAX_LENGTH):
            raise ValueError(f"{value.strip()!r} is not a web address. Code links start with https://.")
        links.setdefault(link.rstrip("/").lower(), link)
    if len(links) > MAX_LINKS:
        raise ValueError(f"A paper can have at most {MAX_LINKS} code links.")
    return list(links.values())


def find(text: str) -> list[str]:
    """Repository addresses a paper's own text names, in the order it names them."""
    links: dict[str, str] = {}
    for host, owner, repository in _REPOSITORY.findall(text or ""):
        # A sentence can end on the address, and a clone address ends in `.git`.
        repository = re.sub(r"\.git$", "", repository.rstrip("."), flags=re.I)
        if repository:
            link = f"https://{host.lower()}/{owner}/{repository}"
            links.setdefault(link.lower(), link)
    return list(links.values())[:MAX_LINKS]
