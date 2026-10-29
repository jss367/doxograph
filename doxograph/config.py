"""Paths, model settings, and credential resolution."""

from __future__ import annotations

import contextlib
import contextvars
import functools
import json
import logging
import os
import re
import shutil
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
_windows_activity_guard = threading.RLock()
_windows_activity_leases: dict[Path, dict] = {}
_settings_lock = threading.RLock()
_analysis_provider: contextvars.ContextVar[str | None] = contextvars.ContextVar("analysis_provider", default=None)
_codex_model: contextvars.ContextVar[str | None] = contextvars.ContextVar("codex_model", default=None)

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


def analysis_config(settings: dict | None = None) -> AnalysisConfig:
    if current := _analysis.get():
        return current
    settings = _settings() if settings is None else settings
    provider = settings.get("provider", os.environ.get("DOXOGRAPH_PROVIDER", "anthropic"))
    if provider not in PROVIDERS:
        raise ValueError(f"Unsupported AI provider: {provider}")
    profile = settings.get("providers", {}).get(provider, {})
    default = PROVIDERS[provider]
    base_url = "" if provider == "anthropic" else _validated_base_url(
        profile.get("base_url", os.environ.get("DOXOGRAPH_BASE_URL", default["base_url"]))
    )
    return AnalysisConfig(
        provider,
        profile.get("model", os.environ.get("DOXOGRAPH_MODEL", MODEL if provider == "anthropic" else default["model"])),
        base_url,
    )


def analysis_identity() -> list[str]:
    if ai_provider() != "api":
        return [ai_provider(), model(), ""]
    selected = analysis_config()
    return [selected.provider, selected.model, selected.base_url]


@contextlib.contextmanager
def use_analysis():
    """Bind one provider for an operation and its copied worker contexts."""
    token = _analysis.set(analysis_config())
    try:
        yield
    finally:
        _analysis.reset(token)


def analysis_session(operation):
    """Keep requests and provenance on one provider even if Settings changes."""
    @functools.wraps(operation)
    def run(*args, **kwargs):
        with use_analysis():
            return operation(*args, **kwargs)
    return run


def _validated_base_url(base_url: str) -> str:
    """Validate every configuration source before URLs reach state or storage."""
    message = "Enter an HTTP or HTTPS base URL without credentials, query, or fragment"
    base_url = base_url.strip().rstrip("/")
    try:
        parsed = urlsplit(base_url)
        valid = (parsed.scheme in ("http", "https") and parsed.hostname
                 and parsed.username is None and parsed.password is None
                 and not parsed.query and not parsed.fragment)
        parsed.port  # Reject malformed ports with the same secret-free message.
    except ValueError:
        raise ValueError(message) from None
    if not valid:
        raise ValueError(message)
    return base_url


def save_analysis_settings(provider: str, model: str, base_url: str) -> None:
    if provider not in PROVIDERS:
        raise ValueError("Choose a supported AI provider")
    model, base_url = model.strip(), base_url.strip().rstrip("/")
    if not model or len(model) > 200:
        raise ValueError("Enter a model name (at most 200 characters)")
    if provider == "anthropic":
        base_url = ""
    else:
        base_url = _validated_base_url(base_url)
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
    _update_settings(ai_enabled=enabled)


def _update_settings(**changes) -> None:
    with _settings_lock:
        path = base_data_dir() / "settings.json"
        try:
            settings = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            settings = {"ai_enabled": True}
        except (OSError, ValueError):
            settings = {"ai_enabled": False}
        if not isinstance(settings, dict):
            settings = {"ai_enabled": False}
        settings.update(changes)
        _write_json(path, settings)


def ai_provider() -> str:
    bound = _analysis_provider.get()
    if bound is not None:
        return bound
    settings = _settings()
    provider = settings.get("ai_provider") if isinstance(settings, dict) else None
    return provider if provider in ("claude_code", "codex") else "api"


def model() -> str:
    if ai_provider() == "codex":
        return codex_model() or "Codex default"
    return MODEL if ai_provider() == "claude_code" else analysis_config().model


def codex_model(settings: dict | None = None) -> str:
    if (bound := _codex_model.get()) is not None:
        return bound
    settings = _settings() if settings is None else settings
    saved = settings.get("codex_model")
    # An explicitly saved empty string selects automatic model choice, even
    # when the launch environment supplies a legacy override.
    return saved if isinstance(saved, str) else os.environ.get("DOXOGRAPH_CODEX_MODEL", "")


def save_codex_model(model: str) -> None:
    model = model.strip()
    if model and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", model):
        raise ValueError("Enter a model ID of at most 200 characters without spaces, or choose Codex default.")
    _update_settings(codex_model=model)


def bind_model_access(work):
    """Keep a running operation on the provider it started with."""
    @functools.wraps(work)
    def bound(*args, **kwargs):
        with use_model_access():
            return work(*args, **kwargs)
    return bound


@contextlib.contextmanager
def use_model_access():
    """Capture access mode and API/local profile from one saved settings read."""
    settings = _settings()
    access = _analysis_provider.get()
    if access is None:
        saved = settings.get("ai_provider")
        access = saved if saved in ("claude_code", "codex") else "api"
    selected = analysis_config(settings)
    selected_codex_model = codex_model(settings)
    access_token = _analysis_provider.set(access)
    analysis_token = _analysis.set(selected)
    codex_token = _codex_model.set(selected_codex_model)
    try:
        yield
    finally:
        _codex_model.reset(codex_token)
        _analysis.reset(analysis_token)
        _analysis_provider.reset(access_token)


@contextlib.contextmanager
def use_ai_provider(provider: str):
    """Use an operation's saved provider without changing installation settings."""
    token = _analysis_provider.set(provider)
    try:
        yield
    finally:
        _analysis_provider.reset(token)


def set_ai_provider(provider: str) -> None:
    if provider not in ("api", "claude_code", "codex"):
        raise ValueError("Choose Anthropic API, Claude subscription, or ChatGPT subscription.")
    _update_settings(ai_provider=provider)


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
def _windows_workspace_activity(path: Path, *, exclusive: bool):
    """Share one byte-range lock across requests and worker threads here.

    Windows locks conflict even between handles in the same process. Retain
    one handle until every shared user has left, including users whose lease
    was acquired by a request and released by a future's completion callback.
    """
    path = path.resolve()
    with _windows_activity_guard:
        lease = _windows_activity_leases.get(path)
        if lease is not None:
            if exclusive or lease["exclusive"]:
                raise BlockingIOError("This workspace is busy")
            lease["users"] += 1
        else:
            handle = path.open("a+")
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                handle.close()
                raise BlockingIOError("This workspace is busy") from exc
            lease = {"handle": handle, "users": 1, "exclusive": exclusive}
            _windows_activity_leases[path] = lease
    try:
        yield
    finally:
        with _windows_activity_guard:
            lease["users"] -= 1
            if not lease["users"]:
                del _windows_activity_leases[path]
                handle = lease["handle"]
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                finally:
                    handle.close()


@contextlib.contextmanager
def workspace_activity(value: str, *, exclusive: bool = False):
    """Nonblocking corpus lease shared by every server using this data root.

    POSIX leases lock the existing corpus directory through a read-only handle,
    keeping healthy read-only libraries browsable. Windows uses counted handles
    on a separate lock file. Every request validates retirement after locking.
    """
    if value == DEFAULT_WORKSPACE_ID:
        yield
        return
    if not value or value != _workspace_slug(value):
        raise KeyError(value)
    if fcntl is not None:
        folder = base_data_dir() / "workspaces" / value
        try:
            descriptor = os.open(folder, os.O_RDONLY)
        except FileNotFoundError:
            # Preserve the registry error instead of disguising damaged
            # registry contents as a missing workspace when no directory exists.
            _read_workspace_records()
            raise KeyError(value)
        try:
            mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
    elif msvcrt is not None:
        # Other Windows servers are conservatively excluded during activity.
        path = base_data_dir() / "locks" / f"workspace-{value}.activity.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        with _windows_workspace_activity(path, exclusive=exclusive):
            yield
    else:  # pragma: no cover
        raise RuntimeError("no file-locking primitive available for workspace activity")


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
        if item.get("deleted"):
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
        # Deleted ids stay retired: an old window or link must never address a
        # different corpus created later with the same display name.
        used = {item["id"] for item in records
                if isinstance(item, dict) and isinstance(item.get("id"), str)}
        used.add(DEFAULT_WORKSPACE_ID)
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


def delete_workspace(value: str) -> bool:
    """Retire a workspace; return whether durable retirement allows cleanup."""
    if value == DEFAULT_WORKSPACE_ID:
        raise ValueError("The default workspace cannot be deleted")
    if not value or value != _workspace_slug(value):
        raise KeyError(value)
    with workspace_activity(value, exclusive=True), _workspaces_lock, _workspace_file_lock():
        from . import job_journal

        records = _read_workspace_records()
        if get_workspace(value) is None:
            raise KeyError(value)
        folder = base_data_dir() / "workspaces" / value
        # Refuse links rather than following a hand-edited filesystem outside
        # the corpus. Commit retirement before touching corpus files: a crash
        # before this commit leaves the live corpus at its original path; a
        # crash afterward leaves an already deleted, permanently retired id.
        if folder.is_symlink() or folder.parent.is_symlink():
            raise ValueError("Workspace directories cannot be symbolic links")
        job_journal.check_workspace_owners(folder)
        retired = [{"id": value, "deleted": True} if isinstance(item, dict) and item.get("id") == value
                   else item for item in records]
        _write_workspace_records(retired)
        # File fsync precedes os.replace in _write_json; persist that directory
        # entry too before destroying any corpus data (as store.write_atomic
        # does for other durable writes on POSIX).
        if os.name == "posix":
            try:
                descriptor = os.open(workspaces_path().parent, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            except OSError:
                # The registry is retired in the running system, but a crash
                # might restore its old entry. Leave the original corpus intact
                # so that even this failure cannot expose partially erased data.
                logging.getLogger(__name__).exception("Could not sync workspace retirement; corpus kept at %s", folder)
                return False
        job_journal.release_workspace_owner(folder)
        if folder.exists():
            try:
                shutil.rmtree(folder)
            except OSError:
                # The retirement already committed. Keep the retired corpus
                # directory on cleanup failure rather than report a false failure
                # or put a partially removed corpus back in the picker.
                logging.getLogger(__name__).exception("Could not remove deleted workspace data at %s", folder)
    return True


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
