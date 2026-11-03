"""Launch the user's local Loupe checkout without copying dataset files.

Hugging Face datasets are the exception: Loupe downloads them into its own
data directory.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

from fastapi import HTTPException


@dataclass
class Session:
    process: subprocess.Popen[bytes]
    signature: tuple[tuple[int, int], ...]
    url: str


_lock = threading.Lock()
# Local sources are keyed by resolved path, Hugging Face sources by repo@revision.
_sessions: dict[Path | str, Session] = {}
_starting: dict[Path | str, threading.Lock] = {}
_launching: set[subprocess.Popen[bytes]] = set()
START_TIMEOUT = 20.0
# Loupe downloads Hugging Face data files before it starts serving.
HF_START_TIMEOUT = 600.0
HF_URL = re.compile(r"(?:https?://(?:www\.)?huggingface\.co/|hf://)datasets/(.*)", re.IGNORECASE)


def hf_dataset(source: str) -> tuple[str, str | None] | None:
    """(repo, revision or None) for a Hugging Face dataset URL, parsed as Loupe's --hf does."""
    url = HF_URL.fullmatch(source.strip())
    if not url:
        return None
    parts = [unquote(part) for part in re.split(r"[?#]", url.group(1))[0].split("/") if part]
    revision = None
    if len(parts) >= 2 and "@" in parts[1]:
        parts[1], _, revision = parts[1].partition("@")
    elif len(parts) >= 4 and parts[2] in ("tree", "blob", "resolve"):
        revision = parts[3]
    if len(parts) < 2 or not all(re.fullmatch(r"[\w.-]+", part) for part in parts[:2]):
        raise HTTPException(422, "Use a Hugging Face dataset URL such as https://huggingface.co/datasets/owner/name.")
    return "/".join(parts[:2]), revision or None


def source_path(source: str) -> Path:
    source = source.strip()
    if source.startswith("file:"):
        try:
            url = urlsplit(source)
        except ValueError:
            raise HTTPException(422, "Use a valid local file:// URL.") from None
        if url.netloc not in ("", "localhost") or url.query or url.fragment:
            raise HTTPException(422, "Use a local file path or a file:// URL on this computer.")
        source = unquote(url.path)
    try:
        path = Path(source).expanduser()
    except RuntimeError:
        raise HTTPException(422, "Cannot resolve this home directory. Use an absolute local dataset path.") from None
    if not source or not path.is_absolute():
        raise HTTPException(422, "Enter an absolute local dataset path (or a path starting with ~/) or a Hugging Face dataset URL. Download other remote datasets before opening them in Loupe.")
    try:
        return path.resolve(strict=True)
    except (OSError, ValueError, RuntimeError) as exc:
        raise HTTPException(422, f"Cannot find the local dataset: {exc}") from None


def dataset_mode(path: Path) -> tuple[Path, str, list[Path]]:
    """Recognize Loupe's formats; leave full record validation to Loupe."""
    if path.name in {"gloom-train.parquet", "gloom-test.parquet"}:
        path = path.parent
    if path.is_dir():
        files = [path / f"gloom-{split}.parquet" for split in ("train", "test")]
        if all(file.is_file() for file in files):
            return path, "--data-dir", files
        raise HTTPException(422, "A Gloom directory must contain both gloom-train.parquet and gloom-test.parquet.")
    if not path.is_file():
        raise HTTPException(422, "Choose a regular dataset file or a Gloom directory.")
    if path.suffix.lower() not in {".json", ".jsonl"}:
        raise HTTPException(422, "Loupe supports conversation JSON/JSONL, loupe.survey.v1 JSON, and Gloom directories. Convert other datasets to one of these formats first.")
    try:
        with path.open(encoding="utf-8") as stream:
            if path.suffix.lower() == ".jsonl":
                first = next((line for line in stream if line.strip()), "")
                data = [json.loads(first)] if first else []
            else:
                data = json.load(stream)
    except (OSError, ValueError) as exc:
        raise HTTPException(422, f"Cannot read this dataset as JSON: {exc}") from None
    if isinstance(data, dict) and data.get("schema") == "loupe.survey.v1":
        return path, "--survey", [path]
    records = data.get("records") if isinstance(data, dict) else data
    if not isinstance(records, list) or any(not isinstance(record, dict) or not isinstance(record.get("messages"), list) for record in records):
        raise HTTPException(422, "Conversation datasets need records with a messages array. Surveys must use the loupe.survey.v1 export format.")
    return path, "--conversations", [path]


def installation(hub: bool = False) -> tuple[Path, str]:
    directory = Path(os.environ.get("DOXOGRAPH_LOUPE_DIR", "~/git/loupe")).expanduser().resolve()
    if not (directory / "server.py").is_file():
        raise HTTPException(503, "Loupe was not found. Install it at ~/git/loupe or set DOXOGRAPH_LOUPE_DIR to its checkout, then restart Doxograph.")
    if hub and not (directory / "hf.py").is_file():
        raise HTTPException(503, "This Loupe checkout cannot open Hugging Face datasets. Update it to 8b68b4a or newer, then try again.")
    python = os.environ.get("DOXOGRAPH_LOUPE_PYTHON")
    if not python:
        venv = directory / ".venv" / "bin" / "python"
        python = str(venv) if venv.is_file() else shutil.which("python3")
    if not python:
        raise HTTPException(503, "Python 3 was not found. Set DOXOGRAPH_LOUPE_PYTHON to Loupe's Python interpreter, then restart Doxograph.")
    return directory, python


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def close() -> None:
    with _lock:
        # Viewers still starting are stopped too; their launch then reports failure.
        for process in [*(session.process for session in _sessions.values()), *_launching]:
            _stop(process)
        _sessions.clear()


atexit.register(close)


def open_dataset(source: str) -> str:
    if os.name != "posix" or not hasattr(os, "pread"):
        raise HTTPException(501, "Opening datasets in Loupe from Doxograph is supported on macOS and Linux. On this platform, start Loupe separately and use its URL as the dataset source.")
    hub = hf_dataset(source)
    key: Path | str
    signature: tuple[tuple[int, int], ...]
    if hub:
        repo, revision = hub
        # A running viewer keeps the commit it resolved; restarting Doxograph picks up a moved branch.
        key, signature, timeout = f"hf:{repo}@{revision or 'main'}", (), HF_START_TIMEOUT
        arguments = ["--hf", repo] + (["--revision", revision] if revision else [])
    else:
        try:
            path, mode, files = dataset_mode(source_path(source))
            signature = tuple((file.stat().st_mtime_ns, file.stat().st_size) for file in files)
        except OSError as exc:
            raise HTTPException(422, f"Cannot read the local dataset: {exc}") from None
        key, timeout, arguments = path, START_TIMEOUT, [mode, str(path)]
    directory, python = installation(bool(hub))
    # Loupe notebooks are browser-origin scoped. Keep the same source on the
    # same port after restarts; never silently attach to an occupied port.
    port = 40000 + int(hashlib.sha256(str(key).encode()).hexdigest()[:8], 16) % 20000
    url = f"http://127.0.0.1:{port}"
    # Starts lock per source, so a long download does not hold up other datasets.
    with _lock:
        starting = _starting.setdefault(key, threading.Lock())
    with starting:
        with _lock:
            previous = _sessions.pop(key, None)
            if previous and previous.process.poll() is None and previous.signature == signature:
                _sessions[key] = previous
                return previous.url
        if previous:
            _stop(previous.process)
        with tempfile.TemporaryFile() as output:
            try:
                with _lock:
                    process = subprocess.Popen(
                        [python, "-u", str(Path(__file__).with_name("loupe_runner.py")),
                         str(directory / "server.py"), *arguments, "--port", str(port)],
                        cwd=directory, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                    )
                    _launching.add(process)
            except OSError as exc:
                raise HTTPException(503, f"Could not start Loupe: {exc}") from None
            started = False
            try:
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    # pread leaves the child's shared output offset untouched.
                    offset = max(0, os.fstat(output.fileno()).st_size - 8192)
                    message = os.pread(output.fileno(), 8192, offset).decode("utf-8", "replace")
                    if process.poll() is not None:
                        raise HTTPException(422, f"Loupe could not open this dataset. Check its format, Loupe's Python dependencies, and whether port {port} is already in use. {message[-2000:].strip()}")
                    if f"Loupe: {url}\n" in message:
                        with _lock:
                            _launching.discard(process)
                            _sessions[key] = Session(process, signature, url)
                        started = True
                        return url
                    time.sleep(0.05)
                if hub:
                    raise HTTPException(504, "Loupe took too long to download and load this dataset. It is intended for small collections; choose a smaller dataset.")
                raise HTTPException(504, "Loupe took too long to load this dataset. It is intended for small collections; try a smaller export.")
            finally:
                if not started:
                    with _lock:
                        _launching.discard(process)
                    _stop(process)
