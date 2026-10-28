"""Durable notifications and retry inputs; resuming always requires a click."""

import dataclasses
import json
import logging
import os
from pathlib import Path

from . import config, ingest, store

logger = logging.getLogger(__name__)
_paths: dict[int, Path] = {}


def save(job: dict) -> None:
    # Internal workers can also run with an ephemeral context rather than a
    # notification accepted by _new_job (for example from an integration).
    if 'id' not in job:
        return
    path = _paths.setdefault(job['id'], config.data_dir() / '.jobs' / f'{job["id"]}.json')
    if path.is_symlink() or path.parent.is_symlink():
        raise OSError('Job journals must be stored inside their workspace')
    source = dict(job.get('_import') or {})
    if source.get('ref') is not None:
        source['ref'] = dataclasses.asdict(source['ref'])
    if source.get('staged') is not None:
        source['staged'] = source['staged'].name
    store.write_json(path, {'version': 1, 'pid': os.getpid(),
        'job': {k: v for k, v in job.items() if not k.startswith('_')}, 'source': source})


def remove(job: dict) -> None:
    path = _paths.pop(job['id'], None)
    if path:
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
            if data['version'] != 1:
                raise ValueError('unsupported job journal version')
            if type(data['pid']) is not int or data['pid'] <= 0:
                raise ValueError('invalid job owner')
            # Another server may still own its requests. Never turn its live
            # operation into a retry in this process.
            try:
                os.kill(data['pid'], 0)
            except ProcessLookupError:
                pass
            else:
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
