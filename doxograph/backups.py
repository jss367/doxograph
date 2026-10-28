"""Local document versions, captured before an atomic replacement."""

import base64
import contextlib
import hashlib
import json
import re
import uuid
from pathlib import Path, PurePosixPath

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from . import config, notebook, records, research_notes, store

router = APIRouter(prefix='/api/workspace/backups')
KEEP_VERSIONS = 20
DOCUMENTS = {'notebook.json', 'research-notes.json', 'tags.yaml', 'ledger.yaml',
             'context.md', 'agreements.json', 'tensions.json', 'syntheses.json', 'retired-keys.json'}


def local(path: Path) -> bool:
    root = config.data_dir().absolute()
    cursor = path.absolute()
    if not cursor.resolve().is_relative_to(root.resolve()):
        return False
    while cursor != root:
        if cursor.is_symlink() or cursor.parent == cursor:
            return False
        cursor = cursor.parent
    return True


def eligible(relative: str) -> bool:
    path = PurePosixPath(relative)
    return relative in DOCUMENTS or (len(path.parts) == 2 and path.parts[0] == 'papers'
        and re.fullmatch(r'[A-Za-z0-9_-]+\.json', path.name) is not None)


def preserve(path: Path, replacement: str) -> None:
    try:
        relative = path.absolute().relative_to(config.data_dir().absolute()).as_posix()
    except ValueError:
        return
    if not eligible(relative) or not path.exists():
        return
    if not local(path):
        raise OSError(f'Refusing to back up a symbolic link: {path}')
    if not path.resolve().is_relative_to(config.data_dir().resolve()):
        raise OSError(f'Backup source is outside this workspace: {path}')
    original = path.read_bytes()
    if original == replacement.encode('utf-8'):
        return
    folder = config.data_dir() / 'backups' / hashlib.sha256(relative.encode()).hexdigest()[:20]
    if not local(folder):
        raise OSError('The backup directory must be inside this workspace')
    identifier = uuid.uuid4().hex
    store.write_json(folder / f'{identifier}.json', {'id': identifier, 'path': relative,
        'created': store.now(), 'sha256': hashlib.sha256(original).hexdigest(),
        'content': base64.b64encode(original).decode('ascii')})
    versions = sorted(folder.glob('*.json'), key=lambda p: p.stat().st_mtime_ns, reverse=True)
    for obsolete in versions[KEEP_VERSIONS:]:
        obsolete.unlink()


def read(path: Path) -> tuple[dict, bytes]:
    try:
        if not local(path):
            raise ValueError('symbolic link')
        saved = json.loads(path.read_text())
        if not all(isinstance(saved.get(k), str) for k in ('id', 'path', 'created', 'sha256', 'content')):
            raise ValueError('invalid backup fields')
        if saved['id'] != path.stem or not eligible(saved['path']):
            raise ValueError('invalid backup identity')
        content = base64.b64decode(saved['content'], validate=True)
        if hashlib.sha256(content).hexdigest() != saved['sha256']:
            raise ValueError('checksum does not match')
        return saved, content
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(409, f'This backup cannot be read: {exc}. The file was left untouched.') from exc


def locate(identifier: str) -> Path:
    if not re.fullmatch(r'[a-f0-9]{32}', identifier):
        raise HTTPException(404, 'Backup not found')
    path = next((config.data_dir() / 'backups').glob(f'*/{identifier}.json'), None)
    if path is None:
        raise HTTPException(404, 'Backup not found')
    return path


def restorable(relative: str) -> bool:
    return relative in ('notebook.json', 'research-notes.json', 'context.md') or relative.startswith('papers/')


@router.get('')
def versions() -> dict:
    items, issues = [], []
    for path in (config.data_dir() / 'backups').glob('*/*.json'):
        try:
            saved, _ = read(path)
            target = config.data_dir() / saved['path']
            expected = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() and local(target) else ''
            items.append({k: saved[k] for k in ('id', 'path', 'created', 'sha256')} | {
                'expected': expected, 'restorable': restorable(saved['path']) and bool(expected)})
        except HTTPException as exc:
            issues.append(str(exc.detail))
    return {'versions': sorted(items, key=lambda v: (v['created'], v['id']), reverse=True),
            'issues': issues, 'retention_per_document': KEEP_VERSIONS}


@router.get('/{identifier}')
def download(identifier: str) -> Response:
    saved, content = read(locate(identifier))
    return Response(content, media_type='application/octet-stream', headers={
        'Content-Disposition': f'attachment; filename="{PurePosixPath(saved["path"]).name}"'})


class Restore(BaseModel):
    expected: str = Field(pattern=r'^[a-f0-9]{64}$')


@router.post('/{identifier}/restore')
def restore(identifier: str, body: Restore) -> dict:
    saved, content = read(locate(identifier))
    relative = saved['path']
    if not restorable(relative):
        raise HTTPException(409, 'Download this metadata version to inspect it before repairing related files together.')
    target = config.data_dir() / relative
    lock = store.paper_lock(target.stem) if relative.startswith('papers/') else store.document_lock(target.stem)
    extraction = store.extraction_lock(target.stem) if relative.startswith('papers/') else contextlib.nullcontext()
    with extraction, store.vocab_lock(), lock:
        if (not local(target)
                or not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != body.expected):
            raise HTTPException(409, 'This document changed or was removed. Reload the backup list before restoring.')
        try:
            text = content.decode('utf-8')
            if relative.startswith('papers/'):
                paper = records.Paper.model_validate_json(text)
                if paper.key != target.stem:
                    raise ValueError('paper identity does not match')
                # Topics can have changed since the backup. Refuse rather than
                # restore a vocabulary or retired identity behind the user's back.
                previous = store.load_paper(target.stem)
                allowed_topics = set(store.tag_names()) | {t for c in previous.get('claims', []) for t in c.get('tags', [])}
                if {t for c in paper.claims for t in c.tags} - allowed_topics:
                    raise ValueError('the backup uses topics no longer in the vocabulary; download it to reconcile first')
                store.ensure_claim_seq(previous)
                restored = paper.model_dump(exclude_unset=True)
                restored['claim_seq'] = max(previous['claim_seq'], restored.get('claim_seq', 0))
                for field in ('pdf_file', 'pdf_upload', 'extraction', 'source', 'error'):
                    if field in previous:
                        restored[field] = previous[field]
                for claim in restored.get('claims', []):
                    store.check_quote(target.stem, claim)
                store.refresh_status(restored)
                store.save_paper(restored)
                return {'restored': relative}
            elif relative == 'notebook.json':
                restored = notebook.Notebook.model_validate_json(text).model_dump()
                restored['revision'] = notebook.load()['revision'] + 1
                text = json.dumps(restored, ensure_ascii=False)
            elif relative == 'research-notes.json':
                restored = research_notes.SavedNotes.model_validate_json(text).model_dump()
                research_notes.remember_revisions(research_notes.load())
                for note in restored:
                    note.update(revision=research_notes.next_revision(note['id'], note['revision']), updated=store.now())
                text = json.dumps(restored, ensure_ascii=False)
        except ValueError as exc:
            raise HTTPException(409, f'This version cannot safely be restored: {exc}') from exc
        store.write_atomic(target, text)  # Preserves the current version before replacing it.
    return {'restored': relative}
