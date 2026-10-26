"""Paths, model settings, and credential resolution."""

from __future__ import annotations

import contextlib
import contextvars
import functools
import json
import os
import re
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

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

try:
    import fcntl
except ImportError:  # pragma: no cover - not on POSIX
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None

MODEL = os.environ.get("DOXOGRAPH_MODEL", "claude-opus-5")

PROVIDERS = {
    "anthropic": {"label": "Anthropic", "model": "claude-opus-5", "base_url": ""},
    "openai": {"label": "OpenAI", "model": "", "base_url": "https://api.openai.com/v1"},
    "ollama": {"label": "Ollama (local)", "model": "", "base_url": "http://localhost:11434"},
    "openai-compatible": {"label": "OpenAI-compatible", "model": "", "base_url": "http://localhost:1234/v1"},
}
_settings_lock = threading.RLock()


def _settings() -> dict:
    try:
        value = json.loads((base_data_dir() / "settings.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return {"ai_enabled": False}
    return value if isinstance(value, dict) else {"ai_enabled": False}


@dataclass(frozen=True)
class AnalysisConfig:
    provider: str
    model: str
    base_url: str


_analysis: contextvars.ContextVar[AnalysisConfig | None] = contextvars.ContextVar("analysis", default=None)


def analysis_config() -> AnalysisConfig:
    if current := _analysis.get():
        return current
    settings = _settings()
    provider = settings.get("provider", os.environ.get("DOXOGRAPH_PROVIDER", "anthropic"))
    if provider not in PROVIDERS:
        raise ValueError(f"Unsupported AI provider: {provider}")
    profile = settings.get("providers", {}).get(provider, {})
    default = PROVIDERS[provider]
    return AnalysisConfig(
        provider,
        profile.get("model", os.environ.get("DOXOGRAPH_MODEL", MODEL if provider == "anthropic" else default["model"])),
        profile.get("base_url", os.environ.get("DOXOGRAPH_BASE_URL", default["base_url"])),
    )


def analysis_identity() -> list[str]:
    selected = analysis_config()
    return [selected.provider, selected.model, selected.base_url]


def analysis_session(operation):
    """Keep requests and provenance on one provider even if Settings changes."""
    @functools.wraps(operation)
    def run(*args, **kwargs):
        token = _analysis.set(analysis_config())
        try:
            return operation(*args, **kwargs)
        finally:
            _analysis.reset(token)
    return run


def save_analysis_settings(provider: str, model: str, base_url: str) -> None:
    if provider not in PROVIDERS:
        raise ValueError("Choose a supported AI provider")
    model, base_url = model.strip(), base_url.strip().rstrip("/")
    if not model or len(model) > 200:
        raise ValueError("Enter a model name (at most 200 characters)")
    if provider == "anthropic":
        base_url = ""
    else:
        parsed = urlsplit(base_url)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Enter an HTTP or HTTPS base URL without credentials, query, or fragment")
        if provider == "openai" and base_url != PROVIDERS["openai"]["base_url"]:
            raise ValueError("Use OpenAI-compatible for a custom endpoint")
    with _settings_lock:
        settings = _settings()
        settings.setdefault("ai_enabled", True)
        settings["provider"] = provider
        settings.setdefault("providers", {})[provider] = {"model": model, "base_url": base_url}
        _write_json(base_data_dir() / "settings.json", settings)


def analysis_settings() -> dict:
    selected = analysis_config()
    profiles = _settings().get("providers", {})
    choices = {name: {**defaults, **profiles.get(name, {})} for name, defaults in PROVIDERS.items()}
    choices[selected.provider].update(model=selected.model, base_url=selected.base_url)
    return {"provider": selected.provider, "model": selected.model, "base_url": selected.base_url,
            "providers": choices,
            "has_key": api_key(selected.provider) is not None}


# How many model calls a per-topic or per-paper pass keeps in flight. The
# passes are independent per item and the merges tolerate any interleaving,
# so the only reason to hold this down is the API rate limit.
try:
    PASS_WORKERS = max(1, int(os.environ.get("DOXOGRAPH_PASS_WORKERS", "4")))
except ValueError:
    PASS_WORKERS = 4

# Bumped when the extraction schema or prompt changes in a way that makes older
# extractions worth re-running. Stored on each paper so you can find stale ones.
SCHEMA_VERSION = 1

# The same for the cross-paper passes: tensions, agreements and syntheses.
# Each pass skips a topic nothing has changed in, so without this in what they
# compare against, a changed prompt would never reach a topic already answered.
PASS_VERSION = 1

CLAIM_KINDS = ["finding", "method", "definition", "negative", "conjecture"]
CLAIM_STRENGTHS = ["headline", "supporting", "aside"]
LEDGER_RELATIONS = ["supports", "contradicts", "method-for", "refines", "independent"]


def base_data_dir() -> Path:
    return Path(os.environ.get("DOXOGRAPH_DATA", DEFAULT_DATA_DIR)).expanduser()


def ai_enabled() -> bool:
    """Analysis is enabled by default; the preference is shared by all workspaces."""
    try:
        settings = json.loads((base_data_dir() / "settings.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return True
    except (OSError, ValueError):
        return False
    # A damaged preference must not silently re-enable paid analysis.
    return isinstance(settings, dict) and settings.get("ai_enabled") is True


def set_ai_enabled(enabled: bool) -> None:
    with _settings_lock:
        settings = _settings()
        settings["ai_enabled"] = enabled
        _write_json(base_data_dir() / "settings.json", settings)


def require_ai_enabled() -> None:
    if not ai_enabled():
        raise RuntimeError("AI analysis is disabled. Enable it in Settings to run analysis.")


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


@contextlib.contextmanager
def _workspace_file_lock():
    """Serialize registry read-modify-write sequences across processes."""
    path = base_data_dir() / "locks" / "workspaces.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is not None:
        with path.open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    if msvcrt is not None:  # pragma: no cover - Windows only
        with path.open("a+") as handle:
            handle.seek(0)
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    raise RuntimeError("no file-locking primitive available for the workspace registry")


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
    except json.JSONDecodeError as exc:
        raise ValueError(f"workspace registry is not valid JSON: {exc}") from exc
    if not isinstance(loaded, list):
        raise ValueError("workspace registry must contain a JSON list")
    return loaded


def _default_workspace(records: list[dict]) -> dict:
    name = next((item["name"] for item in records
                 if isinstance(item, dict) and item.get("id") == DEFAULT_WORKSPACE_ID
                 and isinstance(item.get("name"), str)), DEFAULT_WORKSPACE_NAME)
    return {"id": DEFAULT_WORKSPACE_ID, "name": name, "created": None}


def list_workspaces() -> list[dict]:
    """List the built-in default workspace followed by user-created ones."""
    saved = _read_workspace_records()
    records = [_default_workspace(saved)]
    seen = {DEFAULT_WORKSPACE_ID}
    for item in saved:
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
    # The default corpus exists independently of the registry. A broken
    # registry can lose its custom display name, but must not block its papers.
    if wanted == DEFAULT_WORKSPACE_ID:
        try:
            records = _read_workspace_records()
        except ValueError:
            records = []
        return _default_workspace(records)
    return next((item for item in list_workspaces() if item["id"] == wanted), None)


def _write_workspace_records(records: list[dict]) -> None:
    _write_json(workspaces_path(), records)


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2)
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


def _clean_workspace_name(name: str) -> str:
    clean = " ".join(name.split())
    if not clean:
        raise ValueError("workspace name cannot be empty")
    if len(clean) > 80:
        raise ValueError("workspace name must be 80 characters or fewer")
    return clean


def rename_workspace(value: str, name: str) -> dict:
    """Change a display name without changing the corpus's id or paths."""
    clean = _clean_workspace_name(name)
    with _workspaces_lock, _workspace_file_lock():
        records = _read_workspace_records()
        workspace = get_workspace(value)
        if workspace is None:
            raise KeyError(value)
        renamed = {**workspace, "name": clean}
        for index, item in enumerate(records):
            if (isinstance(item, dict) and item.get("id") == value
                    and isinstance(item.get("name"), str)):
                records[index] = {**item, "name": clean}
                break
        else:
            # The original corpus needs a registry entry only once renamed.
            records.append(renamed)
        _write_workspace_records(records)
    return renamed


def create_workspace(name: str) -> dict:
    """Create and register an empty workspace with a stable, readable id."""
    clean = _clean_workspace_name(name)
    with _workspaces_lock, _workspace_file_lock():
        records = _read_workspace_records()
        used = {item["id"] for item in list_workspaces()}
        base = _workspace_slug(clean)
        key = base
        suffix = 2
        while True:
            workspace_dir = base_data_dir() / "workspaces" / key
            if key not in used:
                try:
                    workspace_dir.mkdir(parents=True, exist_ok=False)
                    break
                except FileExistsError:
                    pass
            key = f"{base}-{suffix}"
            suffix += 1
        record = {
            "id": key,
            "name": clean,
            "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        try:
            records.append(record)
            _write_workspace_records(records)
        except BaseException:
            # The directory is new and still empty here; avoid leaving an
            # unregistered workspace when writing the small registry fails.
            workspace_dir.rmdir()
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


def text_dir() -> Path:
    """Text extracted from the PDFs, one file per paper."""
    return data_dir() / "text"


def locks_dir() -> Path:
    return data_dir() / "locks"


def export_path() -> Path:
    override = os.environ.get("DOXOGRAPH_EXPORT")
    if override and workspace_id() == DEFAULT_WORKSPACE_ID:
        return Path(override).expanduser()
    return data_dir() / "export" / "doxograph.html"


def ensure_dirs() -> None:
    for d in (papers_dir(), pdfs_dir(), text_dir(), locks_dir(), export_path().parent):
        d.mkdir(parents=True, exist_ok=True)


def api_key(provider: str = "anthropic") -> str | None:
    """Resolve only the selected provider's key from the environment or file.

    Falls back to a shell-style credentials file so the server can start from a
    launcher that does not inherit an exported key. The SDK's own resolution
    (env var, then `ant auth login` profile) still applies to Anthropic when
    this returns None. Local endpoints can operate without a key.
    """
    variable = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY",
                "openai-compatible": "DOXOGRAPH_API_KEY"}.get(provider)
    if variable is None:
        return None
    key = os.environ.get(variable)
    if key:
        return key
    cred = Path(os.environ.get("DOXOGRAPH_CREDENTIALS_FILE", Path.home() / ".credentials")).expanduser()
    if cred.is_file():
        match = re.search(
            rf"^\s*(?:export\s+)?{variable}\s*=\s*[\"']?([^\"'\s]+)",
            cred.read_text(encoding="utf-8", errors="replace"),
            re.MULTILINE,
        )
        if match:
            return match.group(1)
    return None
