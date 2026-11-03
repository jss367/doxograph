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
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from . import backups, bib, chats, config, datasets, notebook, research_notes, storage, store

router = APIRouter(prefix='/api/workspace')
_downloads: dict[str, tuple[Path, str, float]] = {}
_download_lock = threading.Lock()
CLAIM_DETAILS = (('evidence', 'Evidence'), ('conditions', 'Conditions'), ('limitations', 'Limitations'),
                 ('evidence_origin', 'Evidence origin'), ('evidence_relationship', 'Replication assessment'), ('note', 'My note'))
DATASET_DETAILS = (('description', 'Description'), ('source', 'Source'), ('version', 'Version'),
                   ('license', 'License'), ('access_notes', 'Access notes'))
README = ('# Portable research workspace\n\nOpen index.md in a Markdown reader. '
          'The papers, notes, datasets, boards, images, PDFs, and references work without a running Doxograph server. '
          'Dataset records contain source references; external dataset files are not copied into this archive. '
          'Original workspace files are under data/. Keep a copy before copying those files into an empty workspace data directory. '
          'The archive excludes installation settings, credential files, active job inputs, and other workspaces. '
          'Links to items outside this workspace still require their original destination. '
          'manifest.json records SHA-256 checksums for the exported files. '
          'Automatic prior document versions remain in the source workspace under backups/.\n')


def portable_links(text: str, notes: dict[str, str], prefix: str = '..') -> str:
    """Rewrite local note, source, and image destinations without fetching URLs."""
    def replace(destination: str) -> str:
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
def _snapshot_locks() -> Iterator[list[str]]:
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
            for name in ('facets', 'vocabulary', 'notebook', 'research-notes', 'datasets', 'context', 'ledger',
                         'agreements', 'tensions', 'syntheses', 'retired-keys', 'chats'):
                locks.enter_context(store.document_lock(name, blocking=False))
            keys = store.paper_keys()
            for key in keys:
                locks.enter_context(store.paper_lock(key, blocking=False))
        except BlockingIOError:
            locks.close()
            if time.monotonic() >= deadline:
                raise HTTPException(409, 'The workspace is busy. Try the archive again after current edits finish.') from None
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
        if store.facet_change_path().exists():
            raise HTTPException(409, 'A facet change needs recovery. Reload the library before exporting.')
        yield keys


@dataclass
class _Archive:
    """An archive being written, with the checksums and skipped paths manifest.json records."""

    archive: zipfile.ZipFile
    root: Path
    files: dict[str, str] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)

    def write(self, name: str, content: str | bytes) -> None:
        data = content.encode('utf-8') if isinstance(content, str) else content
        self.archive.writestr(name, data)
        self.files[name] = hashlib.sha256(data).hexdigest()

    def copy(self, path: Path, name: str) -> None:
        if not path.is_file():
            return
        if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):
            self.skip(path)
            return
        digest = hashlib.sha256()
        with path.open('rb') as stream, self.archive.open(name, 'w', force_zip64=True) as output:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                output.write(chunk)
                digest.update(chunk)
        self.files[name] = digest.hexdigest()

    def skip(self, path: Path) -> None:
        self.skipped.append(path.relative_to(self.root).as_posix())

    def write_manifest(self, header: dict[str, Any]) -> None:
        self.archive.writestr('manifest.json', json.dumps({**header, 'files': self.files, 'skipped': self.skipped}, indent=2))


def build() -> Path:
    root = config.data_dir()
    handle = tempfile.NamedTemporaryFile(prefix='doxograph-workspace-', suffix='.zip', delete=False)
    target = Path(handle.name)
    handle.close()
    header = {'format': 'doxograph-workspace', 'version': 1, 'workspace': config.get_workspace(), 'created': store.now()}
    try:
        with _snapshot_locks() as keys:
            _refuse_symlinks(root, keys)
            keys = [key for key in keys if store.paper_path(key).is_file()]
            notes = research_notes.load()
            saved_datasets = datasets.load()
            saved_notebook = notebook.load()
            note_names = {n['id']: f'{(store.slugify(n["title"]) or "note")[:80]}-{n["id"]}.md' for n in notes}
            with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
                out = _Archive(archive, root)
                _copy_workspace_data(out, keys)
                saved_papers = _write_papers(out, keys, saved_notebook['highlights'], note_names)
                for note in notes:
                    out.write('notes/' + note_names[note['id']], '# ' + notebook.md(note['title']) + '\n\n' + portable_links(note['body'], note_names))
                out.write('datasets.md', _datasets_markdown(saved_datasets, saved_papers))
                out.write('workspace-notes.md', portable_links(saved_notebook['notes'], note_names, '.'))
                _write_boards(out, saved_notebook['boards'], saved_papers)
                out.write('references.bib', '\n\n'.join(bib.entry(p) for p in saved_papers))
                out.write('index.md', _index_markdown(saved_papers, notes, note_names))
                out.write('README.md', README)
                out.write_manifest(header)
        return target
    except BaseException:
        target.unlink(missing_ok=True)
        raise


def _refuse_symlinks(root: Path, keys: list[str]) -> None:
    for name in [*backups.DOCUMENTS, research_notes.REVISION_FILE, 'papers', 'pdfs', 'note-images', 'chats']:
        if (root / name).is_symlink():
            raise HTTPException(409, f'The archive cannot include symbolic links: {name}')
    for key in keys:
        if store.paper_path(key).is_symlink():
            raise HTTPException(409, f'The archive cannot include a symbolic link: papers/{key}.json')


def _copy_workspace_data(out: _Archive, keys: list[str]) -> None:
    """Copy the original workspace files under data/, unchanged."""
    for name in sorted(backups.DOCUMENTS | {research_notes.REVISION_FILE}):
        out.copy(out.root / name, 'data/' + name)
    # An import can reserve a new key after the initial listing.
    # Use exactly the locked set for both raw and readable output.
    for key in keys:
        out.copy(store.paper_path(key), f'data/papers/{key}.json')
        out.copy(store.pdf_path(key), f'data/pdfs/{key}.pdf')
    for path in sorted((out.root / 'note-images').glob('*')):
        if not path.name.startswith('.'):
            out.copy(path, f'data/note-images/{path.name}')
    # Conversations survive removed sources, including in exported workspaces.
    regular = set(chats.files())
    for path in sorted((out.root / 'chats').glob('*.json')):
        if path not in regular:
            out.skip(path)
            continue
        try:
            storage.read_model(path, chats.SavedChat)
        except storage.StorageReadError:
            out.skip(path)
            continue
        out.copy(path, f'data/chats/{path.name}')


def _write_papers(out: _Archive, keys: list[str], highlights: list[dict[str, Any]],
                  note_names: dict[str, str]) -> list[dict[str, Any]]:
    """Write each paper's readable page and return the papers written."""
    saved_papers = []
    for key in keys:
        if store.paper_path(key).is_symlink():
            continue
        paper = store.load_paper(key)
        saved_papers.append(paper)
        marks = [mark for mark in highlights if mark['paper'] == key]
        out.write(f'papers/{key}.md', _paper_markdown(key, paper, marks, note_names))
    return saved_papers


def _paper_markdown(key: str, paper: dict[str, Any], marks: list[dict[str, Any]], note_names: dict[str, str]) -> str:
    lines = ['# ' + notebook.md(paper.get('title') or key), '',
             notebook.md('; '.join(paper.get('authors') or [])) + f' ({paper.get("year") or "year unknown"})', '']
    if store.pdf_path(key).is_file() and not store.pdf_path(key).is_symlink():
        lines.extend([f'[Source PDF](../data/pdfs/{key}.pdf)', ''])
    source = (paper.get('source') or {}).get('url')
    if source:
        lines.extend(['Source: ' + notebook.md(source), ''])
    if paper.get('code'):
        lines.extend(['Code: ' + notebook.md(', '.join(paper['code'])), ''])
    for label, value in (('Summary', paper.get('summary')), ('My paper notes', paper.get('notes'))):
        if value:
            lines.extend(['## ' + label, '', portable_links(value, note_names), ''])
    for claim in paper.get('claims', []):
        lines.extend(_claim_markdown(key, claim))
    if marks:
        lines.extend(['## Highlights and questions', ''])
    for mark in marks:
        lines.extend([f'### {"Figure mark" if mark["kind"] == "figure" else "Passage"} on page {mark["page"]}', '',
            '> ' + notebook.md(mark.get('text')).replace('\n', '\n> '), '',
            portable_links(mark.get('note') or '', note_names), '',
            'Open question: ' + ('yes' if mark['question'] else 'no'), '',
            f'[Open source page](../data/pdfs/{key}.pdf#page={mark["page"]})', ''])
    return '\n'.join(lines)


def _claim_markdown(key: str, claim: dict[str, Any]) -> list[str]:
    lines = ['## ' + notebook.md(claim.get('text')), '',
        '> ' + notebook.md(claim.get('quote')).replace('\n', '\n> '), '',
        'Reference: ' + notebook.md(key + ' / ' + claim['id']), '',
        'Location: ' + notebook.md(claim.get('locator')), '',
        'Human review: ' + ('reviewed' if claim.get('reviewed') else 'unreviewed'), '',
        'Quote match: ' + {True: 'found', False: 'not found', None: 'not checked'}[claim.get('quote_verified')], '']
    for field_name, label in CLAIM_DETAILS:
        if claim.get(field_name):
            lines.extend([label + ': ' + notebook.md(claim[field_name]), ''])
    return lines


def _datasets_markdown(saved_datasets: list[dict[str, Any]], saved_papers: list[dict[str, Any]]) -> str:
    lines = ['# Project datasets', '']
    papers_by_key = {paper['key']: paper for paper in saved_papers}
    for dataset in saved_datasets:
        lines.extend(['## ' + notebook.md(dataset['name']), ''])
        for field_name, label in DATASET_DETAILS:
            if dataset[field_name]:
                lines.extend([label + ': ' + notebook.md(dataset[field_name]), ''])
        lines.extend(['Roles: ' + ', '.join(role.replace('-', ' ') for role in dataset['roles']), ''])
        if dataset['labels']:
            lines.extend(['Labels: ' + notebook.md(', '.join(dataset['labels'])), ''])
        if dataset['facet_values']:
            lines.extend(['Other dataset names: ' + notebook.md(', '.join(dataset['facet_values'])), ''])
        lines.extend(_dataset_sources(dataset, saved_papers, papers_by_key))
        lines.append('')
    return '\n'.join(lines)


def _dataset_sources(dataset: dict[str, Any], saved_papers: list[dict[str, Any]],
                     papers_by_key: dict[str, dict[str, Any]]) -> list[str]:
    """List a dataset's papers and claims, including sources removed since they were linked.

    A claim belongs to the dataset when it was linked by hand or when one of its
    dataset facet values names the dataset or one of its other names.
    """
    names = {store.facet_value_key(value) for value in [dataset['name'], *dataset['facet_values']]}
    references = {(ref['paper'], ref['claim']) for ref in dataset['claims']}
    for saved in saved_papers:
        for claim in saved.get('claims', []):
            if any(store.facet_value_key(value) in names for facet in ('dataset', 'datasets')
                   for value in claim.get('facets', {}).get(facet, [])):
                references.add((saved['key'], claim['id']))
    lines = []
    for key in sorted(set(dataset['papers']) | {paper for paper, _ in references}):
        paper = papers_by_key.get(key)
        if paper:
            lines.append(f'- [{notebook.md(paper.get("title") or key)}](papers/{key}.md)')
        else:
            lines.append('- Missing paper: ' + notebook.md(key))
        for _, claim_id in sorted(ref for ref in references if ref[0] == key):
            claim = next((c for c in (paper or {}).get('claims', []) if c['id'] == claim_id), None)
            lines.append('  - ' + notebook.md(claim['text'] if claim else f'Missing claim: {claim_id}'))
    return lines


def _write_boards(out: _Archive, boards: list[dict[str, Any]], saved_papers: list[dict[str, Any]]) -> None:
    for board in boards:
        response = notebook.export_selection(notebook.Selection(board=board['id']))
        name = f'{(store.slugify(board["title"]) or "board")[:80]}-{hashlib.sha256(board["id"].encode()).hexdigest()[:20]}.md'
        references = {e['reference']['paper'] for e in board['entries'] if e.get('reference')}
        sources = '\n'.join(f'- [{notebook.md(p.get("title") or p["key"])}](../papers/{p["key"]}.md)'
            for p in saved_papers if p['key'] in references)
        out.write('boards/' + name, bytes(response.body).decode('utf-8') + '\n\n## Local sources\n\n' + sources)


def _index_markdown(saved_papers: list[dict[str, Any]], notes: list[dict[str, Any]], note_names: dict[str, str]) -> str:
    return ('# ' + notebook.md(_workspace_name()) + '\n\n'
            + '[Project datasets](datasets.md)\n\n'
            + '\n'.join(f'- [{notebook.md(p.get("title") or p["key"])}](papers/{p["key"]}.md)' for p in saved_papers)
            + '\n\n## Research notes\n\n'
            + '\n'.join(f'- [{notebook.md(n["title"])}](notes/{note_names[n["id"]]})' for n in notes))


def _workspace_name() -> str:
    workspace = config.get_workspace()
    # SelectWorkspace has already refused a request for an unknown workspace.
    assert workspace is not None
    name: str = workspace['name']
    return name


@router.post('/archive')
def prepare_download() -> dict[str, str]:
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
    name = (store.slugify(_workspace_name()) or 'research-workspace') + '.zip'
    return FileResponse(path, filename=name, media_type='application/zip', background=BackgroundTask(path.unlink, missing_ok=True))
