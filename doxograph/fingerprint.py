"""Which code this process is running, and whether the disk still agrees.

The macOS launcher adopts a Doxograph that is already answering on a port
rather than starting a second one. That is the right default and it is how an
orphan becomes permanent: a server that outlived a crash or a force quit is
adopted by every launch afterwards and upgraded by none. The release number in
`/api/health` catches one that has outlived a release and misses one that has
outlived a morning, because `Info.plist` and `doxograph/__init__.py` move
together and only on a release.

So this answers the finer question, which is also the useful one: would
restarting this server change what it runs.

Two halves, by different means. The package's own `.py` files are digested by
contents, because there are a few dozen of them and a checkout is rewritten by
every branch switch and rebase — mostly back to bytes it already held, and a
check that flags those is a check people learn to dismiss. Everything imported
from outside the package is digested by size and time, because its contents run
to tens of megabytes and installing always rewrites files.

`static/` is in neither. It is handed to `FileResponse` per request, so editing
`app.js` under a running server reaches the next reload with nothing stale left
behind.
"""

from __future__ import annotations

import hashlib
import os
import sys
import threading
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent


def source_fingerprint(package: Path = PACKAGE) -> str:
    """A digest of the Python this package is made of, as it is on disk now.

    Only `.py` files outside `static/` count. They are read once, when the
    process imports them, and never looked at again, so a server goes on running
    whatever they said at that moment however far the checkout moves afterwards.
    Everything under `static/` is handed to `FileResponse` per request whatever
    its extension, so a `.py` served from there is as live as `app.js` and as
    little evidence that a running server has gone stale.

    Paths go in beside contents, so a file that is deleted or renamed moves the
    digest as much as an edited one does.

    A package that cannot be read answers with an empty string, and so does one
    with no Python in it at all — `rglob` walks a missing directory without
    complaining, and the digest of nothing is a perfectly ordinary-looking hex
    string that would compare equal to the next reading of nothing. Callers read
    an empty answer as no answer rather than as no change.
    """
    try:
        digest = hashlib.sha256()
        counted = 0
        for path in sorted(package.rglob("*.py")):
            parts = path.relative_to(package).parts
            if "__pycache__" in parts or parts[0] == "static":
                continue
            digest.update(str(path.relative_to(package)).encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
            counted += 1
        return digest.hexdigest() if counted else ""
    except OSError:
        return ""


def imported_files(modules: dict | None = None) -> tuple[str, ...]:
    """The files behind everything imported from outside this package.

    FastAPI, Pydantic, HTTPX and what they pull in are code this server runs as
    surely as its own is, and `pip install -e .` upgrades them without touching
    a single `doxograph` source file.

    The package's own modules are left out because `source_fingerprint` has them
    already, and has them by contents rather than by size and time.
    """
    own = f"{__package__}."
    return tuple(sorted({
        module.__file__
        for name, module in list((sys.modules if modules is None else modules).items())
        if getattr(module, "__file__", None)
        and name != __package__ and not name.startswith(own)
    }))


def _reading(path: str) -> str:
    """The size and time of one file, or that it has gone.

    Contents would be the same question asked more honestly and far too slowly.
    Installing rewrites files, so size and mtime catch what there is to catch.
    The price is that touching a file in site-packages without changing it reads
    as a change — something pip does and a person does not.

    A file that has gone is a change, not an unreadable one. Its module is still
    loaded in this process; it is the disk that no longer has it.
    """
    try:
        stat = os.stat(path)
    except OSError:
        return "gone"
    return f"{stat.st_size}:{stat.st_mtime_ns}"


_lock = threading.Lock()
#: What each dependency file read when this process first saw it. Written once
#: per path and never revised, so it goes on saying what was there when the
#: module behind it was loaded.
_as_loaded: dict[str, str] = {}


def _record(readings: dict[str, str]) -> dict[str, str]:
    """Remember any path not recorded yet, and answer with what is remembered.

    One critical section over one set of paths, because a lazy import landing on
    another thread between two walks would leave a path in one half and not the
    other. `/api/code` is asked at launch, and an ingestion worker reaching for
    pypdf at that moment is an ordinary Tuesday.
    """
    with _lock:
        for path, reading in readings.items():
            _as_loaded.setdefault(path, reading)
        return {path: _as_loaded[path] for path in readings}


def snapshot(modules: dict | None = None) -> None:
    """Remember every dependency file not recorded yet, as it is right now.

    Called once when `server` finishes importing, and again beside each import
    that happens later than that — `serve()` reaches for Uvicorn after this
    module is long done, and the PDF readers reach for pypdf only when a PDF
    arrives. Recording a file at its import is the whole point: record it later
    and an upgrade that landed in between is remembered as the original, and the
    server reads as current forever while running code nobody has on disk.

    Doing this from the import site rather than from an import hook is a choice
    about blast radius. A `sys.meta_path` finder would catch every lazy import
    including a library's own, and a bug in one breaks every import in the
    process. The imports that matter here are countable, and a test counts them.
    """
    _record({path: _reading(path) for path in imported_files(modules)})


def dependency_state(modules: dict | None = None) -> tuple[str, str]:
    """The dependency half, as this process loaded it and as it is on disk now.

    Both sides are over the same set of files — one walk, not two — and that set
    is whatever has been imported by now. A file nobody has recorded is recorded
    here, as it is, which is right when it was imported since the last snapshot
    and wrong when it was replaced since. That is why the snapshots are placed
    where the window is microseconds wide.
    """
    current = {path: _reading(path) for path in imported_files(modules)}
    return _digest(_record(current)), _digest(current)


def _digest(readings: dict[str, str]) -> str:
    if not readings:
        return ""
    digest = hashlib.sha256()
    for path in sorted(readings):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(readings[path].encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def combine(source: str, dependencies: str) -> str:
    """The two halves as the one answer the launcher compares.

    An unreadable package is reported as no answer at all, not as a package that
    happens to match. The launcher refuses to adopt on a mismatch, so a digest it
    cannot trace back to real files has to be unusable rather than merely
    different — otherwise the half that did answer would decide it alone.
    """
    if not source:
        return ""
    return hashlib.sha256(f"{source}:{dependencies}".encode("ascii")).hexdigest()


#: Taken while this process was importing its own modules, which is the code it
#: will be running until it exits.
SOURCE_AT_START = source_fingerprint()


def report() -> tuple[str, str]:
    """What this server is running, and what is on disk, as two digests.

    They differ exactly when something has been edited, pulled or reinstalled
    underneath a process that had already loaded it.
    """
    as_loaded, on_disk = dependency_state()
    return combine(SOURCE_AT_START, as_loaded), combine(source_fingerprint(), on_disk)
