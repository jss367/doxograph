"""Links to a paper's code: where its repository is, as a list of web addresses."""

import re
from urllib.parse import SplitResult, urlsplit

MAX_LINKS = 20
MAX_LENGTH = 2000
LinkKey = tuple[str, str | None, int | None, str | None, str | None, str, str, str]


def _identity(parts: SplitResult) -> LinkKey:
    # Scheme/hostname ignore case; credentials, path, query and fragment do not.
    return (parts.scheme.lower(), parts.hostname, parts.port, parts.username, parts.password,
            parts.path, parts.query, parts.fragment)

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
        except ValueError:
            parts, host = None, ""
        if (parts is None or parts.scheme.lower() not in ("http", "https") or "." not in host
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
