/* Long-form, reader-owned notes. All rendering libraries are bundled locally.
   A view in the main pane. Writing saves itself shortly after typing stops,
   and leaving the view saves at once; a draft that cannot save stays put. */
const ResearchNotes = (() => {
  let client, navigate, follow, select, shown = false, loaded = false;
  let requested = null, revealed = null;
  const el = id => document.getElementById(id);
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
  let workspace, notes = [], draft = null, saved = null, busy = false, uploading = null, previewTimer;
  let mode = 'split';
  const isDirty = () => !!draft && (!saved || draft.title !== saved.title || draft.body !== saved.body);
  const markdown = NoteWriting.markdown;
  const button = (action, text, attrs = '') => `<button type="button" data-notes-action="${action}" ${attrs}>${text}</button>`;
  function status(text, error = false) {
    el('notes-status').textContent = text;
    el('notes-status').className = error ? 'warn' : 'hint';
  }
  const autosave = createAutosave({current: () => draft, dirty: isDirty,
    blocked: () => draft.title.trim() ? '' : 'Give your note a title.',
    async write(target) {
      const note = await request('/' + target.id, 'PUT', {title:target.title, body:target.body, revision:saved.revision});
      if (draft !== target) return;
      saved = note;
      notes = notes.map(n => n.id === note.id ? note : n);
      renderList();
    },
    changed: () => progress()});
  function progress() {
    if (draft) status(autosave.describe(), Boolean(autosave.failure));
  }
  async function request(path = '', method = 'GET', body) {
    return client.request('/api/research-notes' + path, {method, headers:{'X-Doxograph-Workspace':workspace,
      ...(body instanceof FormData ? {} : {'Content-Type':'application/json'})},
      ...(body === undefined ? {} : {body:body instanceof FormData ? body : JSON.stringify(body)})});
  }
  function setBusy(value) {
    busy = value;
    el('research-notes').setAttribute('aria-busy', String(value));
    el('research-notes').querySelectorAll('button, input, textarea, select').forEach(field => field.disabled = value);
  }
  async function run(task) {
    if (busy) return;
    setBusy(true);
    // Actions read the saved revision, so a save under way lands first. A
    // pending one is held while the editor is frozen and resumes after.
    try { await autosave.settle(); await task(); }
    catch (error) { status(error.message, true); }
    finally {
      setBusy(false);
      autosave.resume();
      // Hash navigation can arrive during a load, save, or discard prompt.
      // Drain the latest target once that work releases the editor.
      if (shown && loaded && requested !== revealed) void run(revealRequested);
    }
  }
  function noteURL(id) {
    return `/?note=${encodeURIComponent(id)}#${new URLSearchParams({ws:workspace})}`;
  }
  function paperURL(key) { return '/#' + new URLSearchParams({ws:workspace, paper:key}); }
  function internalLink(href) {
    try {
      const url = new URL(href, location.href);
      if (url.origin !== location.origin || url.pathname !== '/') return null;
      const hash = new URLSearchParams(url.hash.slice(1));
      if ((hash.get('ws') || 'default') !== workspace) return null;
      if (url.searchParams.has('note')) return {kind:'note', id:url.searchParams.get('note')};
      if (hash.has('note')) return {kind:'note', id:hash.get('note')};
      if (hash.has('paper')) return {kind:'paper', id:hash.get('paper')};
    } catch (_) { /* Plain text remains editable even with malformed links. */ }
    return null;
  }
  function noteLinks(body) {
    const links = [];
    marked.walkTokens(markdown.lexer(body), token => {
      if (token.type === 'link') {
        const link = internalLink(token.href);
        if (link) links.push(link);
      }
    });
    return links;
  }
  function renderList() {
    const query = el('notes-search').value.trim().toLowerCase();
    const filtered = notes.filter(n => (n.title + '\n' + n.body).toLowerCase().includes(query))
      .sort((a,b) => b.updated.localeCompare(a.updated));
    el('notes-list').innerHTML = filtered.map(n => button('open', `<strong>${escape(n.title)}</strong><small>${escape(new Date(n.updated).toLocaleDateString())}</small>`,
      `data-note-id="${n.id}" ${draft?.id === n.id ? 'aria-current="page"' : ''}`)).join('')
      || `<p class="hint">${query ? 'No notes match your search.' : 'Create a note to develop an idea across papers.'}</p>`;
  }
  function renderDocument() {
    if (!draft) {
      el('notes-document').innerHTML = '<div class="notes-empty"><h3>A place to think on the page</h3><p>Develop an argument, work through an equation, or connect findings across papers.</p><p>Write in Markdown with equations, images, tables, and links to your research.</p></div>';
      return;
    }
    el('notes-document').innerHTML = `<div class="note-document-head">
      <label for="note-title" class="sr-only">Note title</label>
      <input id="note-title" maxlength="300" placeholder="Untitled research note" value="${escape(draft.title)}">
      <div class="note-actions">${button('export','Export Markdown')}${button('reload','Reload saved version')}${button('delete','Delete note')}</div>
      <div class="note-toolbar" aria-label="Writing tools">
        ${button('heading','Heading')}${button('bold','Bold')}${button('equation','Equation')}${button('table','Table')}${button('image','Image')}${button('link','Link note or paper')}
        <input id="note-image" type="file" accept="image/png,image/jpeg,image/gif,image/webp" hidden>
        <label class="note-view-label">View <select id="note-view">${['write','split','preview'].map(m => `<option value="${m}" ${mode === m ? 'selected' : ''}>${m[0].toUpperCase()+m.slice(1)}</option>`).join('')}</select></label>
      </div>
      <div id="note-link-picker" hidden></div>
    </div>
    <div class="note-pages" data-mode="${mode}">
      <div class="note-write"><label for="note-body" class="hint">Markdown</label><textarea id="note-body" spellcheck="true" placeholder="Start writing…">${escape(draft.body)}</textarea></div>
      <div class="note-read"><span class="hint">Preview</span><article id="note-preview" class="note-prose" aria-label="Note preview"></article></div>
    </div>
    <div class="note-footer"><span id="note-word-count" class="hint"></span><span class="hint">Saves as you write · Drop or paste images into your note</span></div>
    <details class="note-help"><summary>Formatting guide</summary><p>Use # for headings, **bold**, *italic*, lists, blockquotes, and [text](https://…) for links. Put inline LaTeX between $…$ and display equations between $$…$$. Use the toolbar to insert a table, upload an image, or link a note or paper. Images are stored locally. Raw HTML is shown as text.</p></details>
    <section id="note-backlinks" aria-label="Linked research"></section>`;
    renderPreview();
  }
  function renderPreview() {
    // A redraw still pending from typing would replace the links under a click.
    clearTimeout(previewTimer);
    if (!draft || !el('note-preview')) return;
    const fragment = NoteWriting.render(draft.body, workspace);
    fragment.querySelectorAll('a').forEach(link => {
      const target = internalLink(link.getAttribute('href'));
      if (target) {
        link.removeAttribute('target'); link.removeAttribute('rel');
        link.dataset.notesAction = 'follow'; link.dataset.linkKind = target.kind; link.dataset.linkId = target.id;
        const exists = target.kind === 'note' ? notes.some(n => n.id === target.id) : client.state.papers.some(p => p.key === target.id);
        if (!exists) { link.classList.add('note-missing-link'); link.title = 'This source has been removed.'; }
      } else { link.target = '_blank'; link.rel = 'noopener noreferrer'; }
    });
    el('note-preview').replaceChildren(fragment);
    el('note-word-count').textContent = `${draft.body.trim() ? draft.body.trim().split(/\s+/u).length.toLocaleString() : 0} words`;
    const backlinks = notes.filter(n => n.id !== draft.id && noteLinks(n.body).some(link => link.kind === 'note' && link.id === draft.id));
    const outgoing = [...new Map(noteLinks(draft.body).map(link => [link.kind+':'+link.id, link])).values()];
    el('note-backlinks').innerHTML = `<h3>Linked research</h3><div class="note-linked-items">${outgoing.map(link => {
      const item = link.kind === 'note' ? notes.find(n => n.id === link.id) : client.state.papers.find(p => p.key === link.id);
      return button('follow', escape(item?.title || `Removed ${link.kind}`), `data-link-kind="${link.kind}" data-link-id="${escape(link.id)}"`);
    }).join('') || '<span class="hint">Link a note or paper to connect your research.</span>'}</div>
      <h3>Backlinks <span class="hint">${backlinks.length}</span></h3><div class="note-linked-items">${backlinks.map(n => button('open',escape(n.title),`data-note-id="${n.id}"`)).join('') || '<span class="hint">No other notes link here yet.</span>'}</div>`;
  }
  async function discard() {
    if (await autosave.flush()) return true;
    return confirmDialog(`This research note couldn’t be saved. ${autosave.failure} Discard your changes?`, {ok:'Discard changes'});
  }
  async function choose(id) {
    if (draft?.id === id) return;
    if (!await discard()) return;
    const note = notes.find(n => n.id === id);
    if (!note) throw new Error('This research note no longer exists. Reload the saved version to update the list.');
    saved = structuredClone(note); draft = structuredClone(note);
    select(id); renderList(); renderDocument(); progress();
  }
  function reset() {
    clearTimeout(previewTimer); void autosave.settle();
    workspace = client.workspaceId; notes = []; draft = saved = null; loaded = false;
    el('notes-search').value = '';
  }
  // The note a URL names, or else the one last open, or else the newest.
  async function reveal(id, current) {
    if ((isDirty() || autosave.unconfirmed) && (!id || id === draft.id)) { renderList(); progress(); return; }
    if (id && id !== draft?.id && !await discard()) {
      if (current()) select(saved?.id || null);
      return;
    }
    if (!current()) return;
    const last = notes.some(n => n.id === draft?.id) ? draft.id : null;
    const note = notes.find(n => n.id === (id || last)) || (!id && [...notes].sort((a,b) => b.updated.localeCompare(a.updated))[0]);
    if (note) {
      // Read from disk again: nothing unsaved is waiting here.
      saved = structuredClone(note); draft = structuredClone(note);
      select(note.id); renderList(); renderDocument(); progress();
    } else {
      draft = saved = null; select(null); renderList(); renderDocument();
      status(id ? 'This research note no longer exists.' : 'Notes stay in this workspace and are never sent to a model.');
    }
  }
  async function revealRequested() {
    if (!shown) return;
    const target = requested;
    try { await reveal(target.id, () => shown && requested === target); }
    finally { revealed = target; }
  }
  async function show(id) {
    // The URL catching up with a record picked here is not a new destination,
    // and must not rebuild the editor under the cursor.
    if (shown && id && id === draft?.id) { requested = revealed = {id}; return; }
    const changed = !shown || requested?.id !== id;
    if (changed) requested = {id};
    if (shown) {
      if (changed && loaded) await run(revealRequested);
      return;
    }
    shown = true;
    el('research-notes').hidden = false;
    if (workspace !== client.workspaceId) reset();
    renderList(); renderDocument(); progress();
    await run(async () => {
      notes = (await request()).notes; loaded = true;
      await revealRequested();
    });
  }
  function hide() {
    const wasShown = shown;
    shown = false;
    el('research-notes').hidden = true;
    if (wasShown) void autosave.flush();
  }
  function insert(before, after = '', fallback = '') {
    const field = el('note-body');
    if (mode === 'preview') { mode = 'split'; el('note-view').value = mode; el('research-notes').querySelector('.note-pages').dataset.mode = mode; }
    const start = field.selectionStart, end = field.selectionEnd;
    const selected = field.value.slice(start,end) || fallback;
    field.setRangeText(before + selected + after, start, end, 'end');
    draft.body = field.value;
    autosave.schedule(); renderPreview(); field.focus();
  }
  function renderLinkPicker() {
    const picker = el('note-link-picker');
    picker.hidden = !picker.hidden;
    if (picker.hidden) return;
    picker.innerHTML = `<label for="note-link-search">Find a note or paper</label><input id="note-link-search" type="search" placeholder="Search titles…"><div id="note-link-results"></div>`;
    filterLinks(); el('note-link-search').focus();
  }
  function filterLinks() {
    const query = el('note-link-search').value.toLowerCase();
    el('note-link-results').innerHTML = [...notes.filter(n => n.id !== draft.id).map(n => ({kind:'note',id:n.id,title:n.title})),
      ...client.state.papers.map(p => ({kind:'paper',id:p.key,title:p.title || p.key}))]
      .filter(item => item.title.toLowerCase().includes(query)).slice(0,100)
      .map(item => button('insert-link', `<small>${item.kind === 'note' ? 'Note' : 'Paper'}</small> ${escape(item.title)}`,
        `data-link-kind="${item.kind}" data-link-id="${escape(item.id)}"`)).join('') || '<p class="hint">No matching notes or papers.</p>';
  }
  function upload(file) {
    // A flush waits for this, so the link it inserts is saved with the note.
    uploading = (async () => {
      if (!draft) return;
      if (file.size > 10 * 1024 * 1024) throw new Error('Choose an image smaller than 10 MB.');
      const form = new FormData(); form.append('file', file);
      const result = await request('/images', 'POST', form);
      insert(`\n![${(file.name || 'Figure').replace(/[\[\]\\\r\n]/g,'')}](${result.url})\n`);
    })().finally(() => { uploading = null; });
    return uploading;
  }
  async function action(name, target) {
    if (name === 'open') return choose(target.dataset.noteId);
    if (name === 'new') {
      if (!await discard()) return;
      const title = await askDialog({title:'New research note', input:{value:''}, ok:'Create note'});
      if (!title?.trim()) return;
      const note = await request('', 'POST', {title:title.trim()});
      notes.push(note); draft = saved = null; return choose(note.id);
    }
    if (name === 'reload') {
      const flushed = await autosave.flush();
      // Read first: a network failure must not erase the draft.
      const latest = (await request()).notes;
      // A draft whose write went unanswered can match the old revision while
      // the server holds another, so it is compared like any unsaved draft.
      if (!flushed || isDirty() || autosave.unconfirmed) {
        const version = latest.find(n => n.id === draft.id);
        const comparison = version ? `Saved title: ${version.title}\n\n${version.body}` : 'This note has been deleted.';
        // Keep the writer's draft while they inspect the saved version.
        el('note-link-picker').hidden = false;
        el('note-link-picker').innerHTML = `<h3>Saved version from disk</h3><p class="hint">Your draft stays in the editor. Copy anything you want to keep before replacing it.</p><pre class="note-saved-comparison">${escape(comparison)}</pre>${button('use-saved','Replace draft with saved version')}`;
        return;
      }
      notes = latest; const id = draft?.id; draft = saved = null;
      renderList(); renderDocument(); if (notes.some(n => n.id === id)) await choose(id);
      return;
    }
    if (name === 'use-saved') {
      if (!await confirmDialog('Replace your unsaved writing with the saved version?', {ok:'Replace draft'})) return;
      const latest = (await request()).notes, id = draft.id;
      notes = latest; draft = saved = null;
      renderList(); renderDocument();
      if (notes.some(n => n.id === id)) await choose(id);
      else { select(null); status('The note was deleted in another window.'); }
      return;
    }
    if (name === 'delete') {
      if (!await confirmDialog(`Delete “${draft.title}”? Links to this note will be marked as missing.`, {ok:'Delete note'})) return;
      await request(`/${draft.id}?revision=${saved.revision}`, 'DELETE');
      notes = notes.filter(n => n.id !== draft.id); draft = saved = null;
      select(null); renderList(); renderDocument(); status('Note deleted.'); return;
    }
    if (name === 'export') {
      // Export the current draft so unsaved writing can be recovered on conflict.
      const response = await client.response('/api/research-notes/export', {method:'POST',
        headers:{'Content-Type':'application/json','X-Doxograph-Workspace':workspace},
        body:JSON.stringify({title:draft.title,body:draft.body})});
      if (!response.ok) throw new Error('Could not export this note. Your writing is still in the editor.');
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement('a'); link.href = url; link.download = (draft.title.replace(/[^a-zA-Z0-9_-]+/g,'-').slice(0,100) || 'research-note') + '.md';
      link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); return;
    }
    if (['heading','bold','equation','table'].includes(name)) return insert(...NoteWriting.formats[name]);
    if (name === 'link') return renderLinkPicker();
    if (name === 'insert-link') {
      const {linkKind:kind,linkId:id} = target.dataset;
      const item = kind === 'note' ? notes.find(n => n.id === id) : client.state.papers.find(p => p.key === id);
      const title = (item?.title || id).replace(/[\\\[\]]/g, '\\$&');
      insert(`[${title}](${kind === 'note' ? noteURL(id) : paperURL(id)})`);
      el('note-link-picker').hidden = true; return;
    }
    if (name === 'follow') {
      const {linkKind:kind,linkId:id} = target.dataset;
      if (kind === 'note') return choose(id);
      if (!client.state.papers.some(p => p.key === id)) throw new Error('This paper has been removed.');
      follow(id);
    }
  }
  el('research-notes').addEventListener('click', event => {
    const target = event.target.closest('[data-notes-action]');
    if (!target) return;
    event.preventDefault();
    // Open the picker during the click, before run() disables the file input.
    // The change handler freezes the editor while the selected image uploads.
    if (target.dataset.notesAction === 'image') {
      if (!busy) {
        const picker = el('note-image');
        picker.value = ''; // Re-selecting the same image must fire change, including after an upload failure.
        picker.click();
      }
      return;
    }
    run(() => action(target.dataset.notesAction, target));
  });
  el('notes-search').addEventListener('input', renderList);
  el('research-notes').addEventListener('input', event => {
    if (event.target.id === 'note-link-search') return filterLinks();
    if (!draft) return;
    if (event.target.id === 'note-title') draft.title = event.target.value;
    if (event.target.id === 'note-body') draft.body = event.target.value;
    autosave.schedule(); clearTimeout(previewTimer); previewTimer = setTimeout(renderPreview, 180);
  });
  el('research-notes').addEventListener('change', event => {
    if (event.target.id === 'note-view') {
      mode = event.target.value; el('research-notes').querySelector('.note-pages').dataset.mode = mode;
    }
    if (event.target.id === 'note-image' && event.target.files[0]) run(() => upload(event.target.files[0]));
  });
  el('research-notes').addEventListener('keydown', event => {
    // Saving is automatic. The shortcut saves now, or retries a failed save.
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's') { event.preventDefault(); if (!busy) void autosave.flush(); }
  });
  for (const name of ['dragenter','dragover','dragleave','drop']) el('research-notes').addEventListener(name, event => {
    event.stopPropagation();
    if (name === 'drop' || name === 'dragover') event.preventDefault();
    if (name === 'drop') {
      const file = [...event.dataTransfer.files].find(file => file.type.startsWith('image/'));
      if (file) run(() => upload(file));
    }
  });
  el('research-notes').addEventListener('paste', event => {
    const file = [...(event.clipboardData?.files || [])].find(file => file.type.startsWith('image/'));
    if (file) { event.preventDefault(); event.stopPropagation(); run(() => upload(file)); }
  });
  window.addEventListener('beforeunload', event => {
    if (isDirty() || autosave.saving || autosave.unconfirmed) { void autosave.flush(); event.preventDefault(); event.returnValue = ''; }
  });
  return {connect(value, {open: opening, follow: following, selected}) { client = value; navigate = opening; follow = following; select = selected; },
    show, hide, open: id => navigate(id),
    async flush() {
      if (uploading) await uploading.catch(() => {});
      return autosave.flush();
    },
    // Reset first: a draft the writer chose to discard must not save on hide.
    resetWorkspace() { reset(); hide(); },
    hasUnsavedChanges: () => Boolean(isDirty() || busy || autosave.saving || autosave.unconfirmed),
    async beforeWorkspaceSwitch() {
      // A later guard may still refuse. Only committed selection clears drafts.
      return !busy && await discard();
    }};
})();
