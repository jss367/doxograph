"""Paths, model settings, and credential resolution."""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import re
import tempfile
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

# The corpus lives outside the repository by default: it holds downloaded PDFs
# and in-progress extractions, neither of which belongs in version control.
DEFAULT_DATA_DIR = Path.home() / "doxograph-data"
DEFAULT_WORKSPACE_ID = "default"
DEFAULT_WORKSPACE_NAME = "Default workspace"

# A request chooses its corpus with a header (or query parameter). Context-local
# state keeps simultaneous browser requests, and their worker threads, from
# changing one another's paths. Command-line use never sets it and therefore
# continues to address the original corpus at the data-directory root.
_workspace_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "doxograph_workspace", default=DEFAULT_WORKSPACE_ID
)
_workspaces_lock = threading.RLock()

MODEL = os.environ.get("DOXOGRAPH_MODEL", "claude-opus-5")

# Bumped when the extraction schema or prompt changes in a way that makes older
# extractions worth re-running. Stored on each paper so you can find stale ones.
SCHEMA_VERSION = 1

CLAIM_KINDS = ["finding", "method", "definition", "negative", "conjecture"]
CLAIM_STRENGTHS = ["headline", "supporting", "aside"]
LEDGER_RELATIONS = ["supports", "contradicts", "method-for", "refines", "independent"]


def base_data_dir() -> Path:
    return Path(os.environ.get("DOXOGRAPH_DATA", DEFAULT_DATA_DIR)).expanduser()


def workspace_id() -> str:
    return _workspace_id.get()


@contextlib.contextmanager
def use_workspace(value: str):
    """Use one workspace for the current request or background operation."""
    token = _workspace_id.set(value)
    try:
        yield
    finally:
        _workspace_id.reset(token)


def data_dir() -> Path:
    """The selected workspace's corpus directory.

    The default workspace deliberately remains at the historical root so an
    existing install needs no migration and sees all of its papers immediately.
    """
    selected = workspace_id()
    if selected == DEFAULT_WORKSPACE_ID:
        return base_data_dir()
    return base_data_dir() / "workspaces" / selected


def workspaces_path() -> Path:
    return base_data_dir() / "workspaces.json"


def _workspace_slug(name: str) -> str:
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^A-Za-z0-9]+", "-", plain).strip("-").lower()
    return slug or "workspace"


def _read_workspace_records() -> list[dict]:
    path = workspaces_path()
    if not path.exists():
        return []
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return loaded if isinstance(loaded, list) else []


def list_workspaces() -> list[dict]:
    """List the built-in default workspace followed by user-created ones."""
    records = [
        {
            "id": DEFAULT_WORKSPACE_ID,
            "name": DEFAULT_WORKSPACE_NAME,
            "created": None,
        }
    ]
    seen = {DEFAULT_WORKSPACE_ID}
    for item in _read_workspace_records():
        if not isinstance(item, dict):
            continue
        key = item.get("id")
        name = item.get("name")
        if not isinstance(key, str) or not isinstance(name, str) or key in seen:
            continue
        # Only admit plain directory names from the registry. This also makes a
        # hand-edited registry unable to escape the workspace directory.
        if key != _workspace_slug(key):
            continue
        records.append({"id": key, "name": name, "created": item.get("created")})
        seen.add(key)
    return records


def get_workspace(value: str | None = None) -> dict | None:
    wanted = value or workspace_id()
    return next((item for item in list_workspaces() if item["id"] == wanted), None)


def _write_workspace_records(records: list[dict]) -> None:
    path = workspaces_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(records, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def create_workspace(name: str) -> dict:
    """Create and register an empty workspace with a stable, readable id."""
    clean = " ".join(name.split())
    if not clean:
        raise ValueError("workspace name cannot be empty")
    if len(clean) > 80:
        raise ValueError("workspace name must be 80 characters or fewer")
    with _workspaces_lock:
        records = _read_workspace_records()
        used = {item["id"] for item in list_workspaces()}
        base = _workspace_slug(clean)
        key = base
        suffix = 2
        while key in used:
            key = f"{base}-{suffix}"
            suffix += 1
        record = {
            "id": key,
            "name": clean,
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        (base_data_dir() / "workspaces" / key).mkdir(parents=True, exist_ok=False)
        try:
            records.append(record)
            _write_workspace_records(records)
        except BaseException:
            # The directory is new and still empty here; avoid leaving an
            # unregistered workspace when writing the small registry fails.
            (base_data_dir() / "workspaces" / key).rmdir()
            raise
    with use_workspace(key):
        ensure_dirs()
    return record


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
