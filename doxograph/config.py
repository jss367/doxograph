"""Paths, model settings, and credential resolution."""

from __future__ import annotations

import os
import re
from pathlib import Path

# The corpus lives outside the repository by default: it holds downloaded PDFs
# and in-progress extractions, neither of which belongs in version control.
DEFAULT_DATA_DIR = Path.home() / "doxograph-data"

MODEL = os.environ.get("DOXOGRAPH_MODEL", "claude-opus-5")

# Bumped when the extraction schema or prompt changes in a way that makes older
# extractions worth re-running. Stored on each paper so you can find stale ones.
SCHEMA_VERSION = 1

CLAIM_KINDS = ["finding", "method", "definition", "negative", "conjecture"]
CLAIM_STRENGTHS = ["headline", "supporting", "aside"]
LEDGER_RELATIONS = ["supports", "contradicts", "method-for", "refines", "independent"]


def data_dir() -> Path:
    return Path(os.environ.get("DOXOGRAPH_DATA", DEFAULT_DATA_DIR)).expanduser()


def papers_dir() -> Path:
    return data_dir() / "papers"


def pdfs_dir() -> Path:
    return data_dir() / "pdfs"


def tags_path() -> Path:
    return data_dir() / "tags.yaml"


def ledger_path() -> Path:
    return data_dir() / "ledger.yaml"


def locks_dir() -> Path:
    return data_dir() / "locks"


def export_path() -> Path:
    return Path(os.environ.get("DOXOGRAPH_EXPORT", data_dir() / "export" / "doxograph.html")).expanduser()


def ensure_dirs() -> None:
    for d in (papers_dir(), pdfs_dir(), locks_dir(), export_path().parent):
        d.mkdir(parents=True, exist_ok=True)


def api_key() -> str | None:
    """Resolve an Anthropic API key.

    Falls back to a shell-style credentials file so the server can start from a
    launcher that does not inherit an exported key. The SDK's own resolution
    (env var, then `ant auth login` profile) still applies when this returns
    None, so a missing key here is not necessarily an error.
    """
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    cred = Path(os.environ.get("DOXOGRAPH_CREDENTIALS_FILE", Path.home() / ".credentials")).expanduser()
    if cred.is_file():
        match = re.search(
            r"^\s*(?:export\s+)?ANTHROPIC_API_KEY\s*=\s*[\"']?([^\"'\s]+)",
            cred.read_text(encoding="utf-8", errors="replace"),
            re.MULTILINE,
        )
        if match:
            return match.group(1)
    return None
