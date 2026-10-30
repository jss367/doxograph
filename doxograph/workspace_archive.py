"""A portable workspace: readable Markdown beside original data and assets."""

import contextlib
import hashlib
import json
import re
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from . import backups, bib, chats, config, notebook, research_notes, storage, store

router = APIRouter(prefix='/api/workspace')
_downloads: dict[str, tuple[Path, str, float]] = {}
_download_lock = threading.Lock()


def portable_links(text: str, notes: dict[str, str], prefix: str = '..') -> str:
    """Rewrite local note, source, and image destinations without fetching URLs."""
    def replace(destination):
        parsed = urlsplit(destination)
        if parsed.scheme or parsed.netloc:
            return destination
        scope = parse_qs(parsed.fragment).get('ws', ['default'])[0]
        if parsed.path == '/' and scope != config.workspace_id():
            return destination
        if parsed.path.startswith('/api/research-notes/images/'):
            name = parsed.path.rsplit('/', 1)[-1]
            if re.fullmatch(r'[a-f0-9]{64}\.(png|jpg|gif|webp)', name):
                return f'{prefix}/data/note-images/{name}'
        params = parse_qs(parsed.query)
        note = params.get('note', [''])[0]
        if note in notes:
            return f'{prefix}/notes/{notes[note]}'
        paper = parse_qs(parsed.fragment).get('paper', [''])[0]
        if re.fullmatch(r'[A-Za-z0-9_-]+', paper):
            return f'{prefix}/papers/{paper}.md'
        pdf = re.fullmatch(r'/pdf/([A-Za-z0-9_-]+)', unquote(parsed.path))
        if pdf:
            return f'{prefix}/data/pdfs/{pdf[1]}.pdf' + (f'#{parsed.fragment}' if parsed.fragment else '')
        return destination
    return research_notes.rewrite_research_links(text, replace)


@contextlib.contextmanager
def _snapshot_locks():
    """Never wait for one legacy lock while holding another.

    Paper keys can alias metadata lock filenames. Releasing the entire partial
    set on contention breaks cross-thread/process cycles without changing the
    filenames used by older clients. A busy workspace fails with a retry message
    rather than hanging an export or indefinitely blocking writers.
    """
    deadline = time.monotonic() + 5
    while True:
        locks = contextlib.ExitStack()
        try:
            for name in ('vocabulary', 'notebook', 'research-notes', 'context', 'ledger',
                         'agreements', 'tensions', 'syntheses', 'retired-keys', 'chats'):
                locks.enter_context(store.document_lock(name, blocking=False))
            keys = store.paper_keys()
            for key in keys:
                locks.enter_context(store.paper_lock(key, blocking=False))
        except BlockingIOError:
            locks.close()
            if time.monotonic() >= deadline:
                raise HTTPException(409, 'The workspace is busy. Try the archive again after current edits finish.')
            time.sleep(0.01)
            continue
        except BaseException:
            locks.close()
            raise
        break
    with locks:
        # Recovering a pending tag rewrite could acquire locks outside this set.
        # Leave that work to normal storage recovery, never start it mid-export.
        if store.tag_change_path().exists():
            raise HTTPException(409, 'A topic change needs recovery. Reload the library before exporting.')
        yield keys


def build() -> Path:
    root = config.data_dir()
    handle = tempfile.NamedTemporaryFile(prefix='doxograph-workspace-', suffix='.zip', delete=False)
    target = Path(handle.name)
    handle.close()
    manifest = {'format': 'doxograph-workspace', 'version': 1, 'workspace': config.get_workspace(),
                'created': store.now(), 'files': {}, 'skipped': []}
    try:
        with _snapshot_locks() as keys:
            for name in [*backups.DOCUMENTS, research_notes.REVISION_FILE, 'papers', 'pdfs', 'note-images', 'chats']:
                if (root / name).is_symlink():
                    raise HTTPException(409, f'The archive cannot include symbolic links: {name}')
            for key in keys:
                if store.paper_path(key).is_symlink():
                    raise HTTPException(409, f'The archive cannot include a symbolic link: papers/{key}.json')
            keys = [key for key in keys if store.paper_path(key).is_file()]
            notes = research_notes.load()
            saved_notebook = notebook.load()
            note_names = {n['id']: f'{(store.slugify(n["title"]) or "note")[:80]}-{n["id"]}.md' for n in notes}
            with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                def write(name: str, content: str | bytes):
                    data = content.encode('utf-8') if isinstance(content, str) else content
                    archive.writestr(name, data)
                    manifest['files'][name] = hashlib.sha256(data).hexdigest()

                def copy(path: Path, name: str):
                    if not path.is_file():
                        return
                    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                        manifest['skipped'].append(path.relative_to(root).as_posix())
                        return
                    digest = hashlib.sha256()
                    with path.open('rb') as stream, archive.open(name, 'w', force_zip64=True) as output:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                            output.write(chunk)
                            digest.update(chunk)
                    manifest['files'][name] = digest.hexdigest()

                for name in sorted(backups.DOCUMENTS | {research_notes.REVISION_FILE}):
                    copy(root / name, 'data/' + name)
                # An import can reserve a new key after the initial listing.
                # Use exactly the locked set for both raw and readable output.
                for key in keys:
                    copy(store.paper_path(key), f'data/papers/{key}.json')
                    copy(store.pdf_path(key), f'data/pdfs/{key}.pdf')
                for path in sorted((root / 'note-images').glob('*')):
                    if not path.name.startswith('.'):
                        copy(path, f'data/note-images/{path.name}')
                # Paper removal can finish while this waits for paper locks; its
                # conversations go with it, so keep only those of the locked papers.
                regular = set(chats.files())
                for path in sorted((root / 'chats').glob('*.json')):
                    if path not in regular:
                        manifest['skipped'].append(path.relative_to(root).as_posix())
                        continue
                    try:
                        paper = storage.read_model(path, chats.SavedChat).paper
                    except storage.StorageReadError:
                        manifest['skipped'].append(path.relative_to(root).as_posix())
                        continue
                    if paper is None or paper in keys:
                        copy(path, f'data/chats/{path.name}')
                saved_papers = []
                for key in keys:
                    path = store.paper_path(key)
                    if path.is_symlink():
                        continue
                    paper = store.load_paper(key)
                    saved_papers.append(paper)
                    lines = ['# ' + notebook.md(paper.get('title') or key), '',
                             notebook.md('; '.join(paper.get('authors') or [])) + f' ({paper.get("year") or "year unknown"})', '']
                    if store.pdf_path(key).is_file() and not store.pdf_path(key).is_symlink():
                        lines.extend([f'[Source PDF](../data/pdfs/{key}.pdf)', ''])
                    source = (paper.get('source') or {}).get('url')
                    if source:
                        lines.extend(['Source: ' + notebook.md(source), ''])
                    for label, value in (('Summary', paper.get('summary')), ('My paper notes', paper.get('notes'))):
                        if value:
                            lines.extend(['## ' + label, '', portable_links(value, note_names), ''])
                    for claim in paper.get('claims', []):
                        lines.extend(['## ' + notebook.md(claim.get('text')), '',
                            '> ' + notebook.md(claim.get('quote')).replace('\n', '\n> '), '',
                            'Reference: ' + notebook.md(key + ' / ' + claim['id']), '',
                            'Location: ' + notebook.md(claim.get('locator')), '',
                            'Human review: ' + ('reviewed' if claim.get('reviewed') else 'unreviewed'), '',
                            'Quote match: ' + {True: 'found', False: 'not found', None: 'not checked'}[claim.get('quote_verified')], ''])
                        for field, label in (('evidence', 'Evidence'), ('conditions', 'Conditions'), ('limitations', 'Limitations'),
                                             ('evidence_origin', 'Evidence origin'), ('evidence_relationship', 'Replication assessment'), ('note', 'My note')):
                            if claim.get(field):
                                lines.extend([label + ': ' + notebook.md(claim[field]), ''])
                    marks = [mark for mark in saved_notebook['highlights'] if mark['paper'] == key]
                    if marks:
                        lines.extend(['## Highlights and questions', ''])
                    for mark in marks:
                        lines.extend([f'### {"Figure mark" if mark["kind"] == "figure" else "Passage"} on page {mark["page"]}', '',
                            '> ' + notebook.md(mark.get('text')).replace('\n', '\n> '), '',
                            portable_links(mark.get('note') or '', note_names), '',
                            'Open question: ' + ('yes' if mark['question'] else 'no'), '',
                            f'[Open source page](../data/pdfs/{key}.pdf#page={mark["page"]})', ''])
                    write(f'papers/{key}.md', '\n'.join(lines))
                for note in notes:
                    write('notes/' + note_names[note['id']], '# ' + notebook.md(note['title']) + '\n\n' + portable_links(note['body'], note_names))
                write('workspace-notes.md', portable_links(saved_notebook['notes'], note_names, '.'))
                for board in saved_notebook['boards']:
                    response = notebook.export_selection(notebook.Selection(board=board['id']))
                    name = f'{(store.slugify(board["title"]) or "board")[:80]}-{hashlib.sha256(board["id"].encode()).hexdigest()[:20]}.md'
                    references = {e['reference']['paper'] for e in board['entries'] if e.get('reference')}
                    sources = '\n'.join(f'- [{notebook.md(p.get("title") or p["key"])}](../papers/{p["key"]}.md)'
                        for p in saved_papers if p['key'] in references)
                    write('boards/' + name, bytes(response.body).decode('utf-8') + '\n\n## Local sources\n\n' + sources)
                write('references.bib', '\n\n'.join(bib.entry(p) for p in saved_papers))
                write('index.md', '# ' + notebook.md(config.get_workspace()['name']) + '\n\n'
                      + '\n'.join(f'- [{notebook.md(p.get("title") or p["key"])}](papers/{p["key"]}.md)' for p in saved_papers)
                      + '\n\n## Research notes\n\n'
                      + '\n'.join(f'- [{notebook.md(n["title"])}](notes/{note_names[n["id"]]})' for n in notes))
                write('README.md', '# Portable research workspace\n\nOpen index.md in a Markdown reader. '
                    'The papers, notes, boards, images, PDFs, and references work without a running Doxograph server. '
                    'Original workspace files are under data/. Keep a copy before copying those files into an empty workspace data directory. '
                    'The archive excludes installation settings, credential files, active job inputs, and other workspaces. '
                    'Links to items outside this workspace still require their original destination. '
                    'manifest.json records SHA-256 checksums for the exported files. '
                    'Automatic prior document versions remain in the source workspace under backups/.\n')
                archive.writestr('manifest.json', json.dumps(manifest, indent=2))
        return target
    except BaseException:
        target.unlink(missing_ok=True)
        raise


@router.post('/archive')
def prepare_download() -> dict:
    path = build()
    identifier = uuid.uuid4().hex
    with _download_lock:
        for key, (old, _, created) in list(_downloads.items()):
            if time.monotonic() - created > 3600:
                old.unlink(missing_ok=True)
                del _downloads[key]
        _downloads[identifier] = (path, str(config.data_dir().resolve()), time.monotonic())
    return {'download': f'/api/workspace/archive/{identifier}'}


@router.get('/archive/{identifier}')
def download(identifier: str) -> FileResponse:
    with _download_lock:
        item = _downloads.get(identifier)
        if item is None or item[1] != str(config.data_dir().resolve()):
            raise HTTPException(404, 'Workspace archive not found. Prepare a new download.')
        path, _, _ = _downloads.pop(identifier)
    name = (store.slugify(config.get_workspace()['name']) or 'research-workspace') + '.zip'
    return FileResponse(path, filename=name, media_type='application/zip', background=BackgroundTask(path.unlink, missing_ok=True))
