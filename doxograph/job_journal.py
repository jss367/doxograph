"""Durable notifications and retry inputs; resuming always requires a click."""

import dataclasses
import contextlib
import json
import logging
import os
import re
import threading
from pathlib import Path

from . import config, fingerprint, ingest, store

logger = logging.getLogger(__name__)
_paths: dict[int, Path] = {}
_leases: dict[Path, contextlib.AbstractContextManager[None]] = {}
_lease_guard = threading.Lock()
_WINDOWS = os.name == 'nt'


def _hold_owner_lease(folder: Path) -> None:
    # Keep the descriptor alive for the process lifetime, including background
    # workers finishing after the server lifespan exits.
    path = folder / f'.owner-{fingerprint.INSTANCE}.lock'
    with _lease_guard:
        if path not in _leases:
            if path.is_symlink():
                raise OSError('Job ownership markers cannot be symbolic links')
            lease = store._file_lock(path)
            lease.__enter__()
            _leases[path] = lease


def _pid_alive(pid: int) -> bool:
    if _WINDOWS:
        import _winapi
        fingerprint.snapshot()

        try:
            handle = _winapi.OpenProcess(_winapi.SYNCHRONIZE, False, pid)
        except OSError as exc:
            if exc.winerror == 87:  # ERROR_INVALID_PARAMETER: no such process.
                return False
            if exc.winerror == 5:  # ERROR_ACCESS_DENIED: conservatively live.
                return True
            raise
        try:
            return _winapi.WaitForSingleObject(handle, 0) != _winapi.WAIT_OBJECT_0
        finally:
            _winapi.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _owner_alive(data: dict, folder: Path) -> bool:
    if data['version'] == 1:
        # Journals written before instance leases cannot distinguish PID reuse.
        # Query Windows handles instead of sending a console control signal.
        return _pid_alive(data['pid'])
    owner = data.get('owner')
    if not isinstance(owner, str) or not re.fullmatch(r'[a-f0-9]{32}', owner):
        raise ValueError('invalid job owner instance')
    path = folder / f'.owner-{owner}.lock'
    if path.is_symlink():
        raise ValueError('job ownership marker is a symbolic link')
    if not path.exists():
        return False
    try:
        with store._file_lock(path, blocking=False):
            return False
    except BlockingIOError:
        return True


def check_workspace_owners(folder: Path) -> None:
    """Refuse deletion while another live server owns journals in this corpus.

    Its handles cannot be closed here, and on Windows they prevent unlinking.
    This also protects workers running in an older server's lease protocol.
    The caller holds exclusive workspace activity until deletion has finished.
    """
    with _lease_guard:
        for path in (folder / '.jobs').glob('*.json'):
            if path.is_symlink():
                continue
            try:
                data = json.loads(path.read_text())
            except (ValueError, UnicodeError):
                continue  # Malformed notifications do not identify a live owner.
            if (not isinstance(data, dict) or data.get('version') != 1
                    or type(data.get('pid')) is not int or data['pid'] <= 0):
                continue
            try:
                alive = _owner_alive(data, path.parent)
            except PermissionError:
                alive = True  # An inaccessible live process is not a dead owner.
            if alive:
                raise BlockingIOError('A legacy server still owns this workspace')
        for path in (folder / '.jobs').glob('.owner-*.lock'):
            if path in _leases:
                continue
            if path.is_symlink():
                raise ValueError('Job ownership markers cannot be symbolic links')
            with store._file_lock(path, blocking=False):
                pass


def release_workspace_owner(folder: Path) -> None:
    """Close this process's handles after retirement and before corpus removal."""
    with _lease_guard:
        for path in [path for path in _leases if path.parent == folder / '.jobs']:
            _leases.pop(path).__exit__(None, None, None)


def save(job: dict) -> None:
    # Internal workers can also run with an ephemeral context rather than a
    # notification accepted by _new_job (for example from an integration).
    if 'id' not in job:
        return
    path = _paths.setdefault(job['id'], config.data_dir() / '.jobs' / f'{job["id"]}.json')
    if path.is_symlink() or path.parent.is_symlink():
        raise OSError('Job journals must be stored inside their workspace')
    _hold_owner_lease(path.parent)
    source = dict(job.get('_import') or {})
    if source.get('ref') is not None:
        source['ref'] = dataclasses.asdict(source['ref'])
    if source.get('staged') is not None:
        source['staged'] = source['staged'].name
    store.write_json(path, {'version': 2, 'pid': os.getpid(), 'owner': fingerprint.INSTANCE,
        'job': {k: v for k, v in job.items() if not k.startswith('_')}, 'source': source})


def remove(job: dict, *, keep_file: bool = False) -> None:
    path = _paths.pop(job['id'], None)
    if path and not keep_file:
        path.unlink(missing_ok=True)


def recover(issues: list[str] | None = None) -> list[dict]:
    recovered = []
    if (config.data_dir() / '.jobs').is_symlink():
        if issues is not None:
            issues.append('Import journals cannot be recovered from a symbolic link. The files were left in place.')
        return recovered
    for path in sorted((config.data_dir() / '.jobs').glob('*.json')):
        try:
            if path.is_symlink():
                raise ValueError('job journal is a symbolic link')
            data = json.loads(path.read_text())
            if (not isinstance(data, dict) or not isinstance(data.get('job'), dict)
                    or not isinstance(data.get('source', {}), dict)):
                raise ValueError('journal, job, and retry source must be objects')
            if data['version'] not in (1, 2):
                raise ValueError('unsupported job journal version')
            if type(data['pid']) is not int or data['pid'] <= 0:
                raise ValueError('invalid job owner')
            # Another server may still own its requests. Never turn its live
            # operation into a retry in this process.
            if _owner_alive(data, path.parent):
                continue
            job = {k: v for k, v in data['job'].items() if not k.startswith('_')}
            if (type(job['id']) is not int or job['id'] <= 0
                    or path.stem != str(job['id'])
                    or job['workspace'] != config.workspace_id()
                    or job['state'] not in ('queued', 'fetching', 'reading', 'error', 'done')):
                raise ValueError('invalid job identity or state')
            source = data.get('source') or {}
            if source and (type(source.get('extract')) is not bool
                    or ('ref' in source) == ('staged' in source)):
                raise ValueError('invalid retry source')
            if source.get('ref'):
                source['ref'] = ingest.Ref(**source['ref'])
            if source.get('staged'):
                name = source['staged']
                if Path(name).name != name or not name.startswith('.incoming-') or not name.endswith('.pdf'):
                    raise ValueError('invalid retained upload name')
                source['staged'] = config.pdfs_dir() / name
                if (source['staged'].is_symlink() or not source['staged'].resolve().is_relative_to(config.data_dir().resolve())
                        or not source['staged'].is_file()):
                    source = {}
            if source:
                job['_import'] = source
            job['retryable'] = bool(source)
            if job['state'] in ('queued', 'fetching', 'reading'):
                job.update(state='error', interrupted=True,
                    explanation='Doxograph stopped before this operation finished. Resume to try again; an already sent analysis may run again.' if source
                    else 'Doxograph stopped before this operation finished. Start the operation again from its paper or topic.')
            _paths[job['id']] = path
            save(job)
            recovered.append(job)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            logger.warning('Cannot recover job %s; the journal was left in place: %s', path, exc)
            if issues is not None:
                issues.append(f'Cannot recover {path.name}: {exc}. The saved input was left in place.')
    return recovered
