/* Reader-owned workflows. Stored with the active workspace; never sent to a model. */
const ResearchTools = (() => {
  let client;
  let workspace = null, claims = new Set(), papers = new Set();
  let notebook = null, tab = 'compare', boardId = null, dirty = false, busy = false, connectionDraft = false;
  let modalWorkspace = null, historyData = null, historyPaper = null, historyClaim = null, marksFilter = 'all';
  let epoch = 0;
  // The paper open beside its notes in the main pane. Separate from the modal:
  // it stays up while the research tools are opened and closed over it.
  let reader = null, readerEpoch = 0, mountSeq = 0;
  // The editor owns its identity and saved baseline across polls and requests.
  let markEditor = null;
  // A page asked for before its paper's reader is up: {paper, workspace, page,
  // mark}. Named, so a reader mounted for another paper meanwhile discards it
  // rather than opening on a page that was meant for somewhere else.
  let pendingPage = null;
  const askPage = (paper, workspace, page, mark = null) => { pendingPage = {paper, workspace, page, mark}; };
  const COLORS = ['yellow', 'green', 'blue', 'pink'];
  let notes = null, notesTimer = null, notesSaving = null, notesPreviewTimer = null;
  let notesMode = 'split';
  const el = (id) => document.getElementById(id);
  const html = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
  const uid = () => {
    if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
    // LAN deployments over HTTP lack randomUUID, but support getRandomValues.
    return Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('');
  };
  const promptDialog = (title, value, {ok}) => askDialog({title, input:{value}, ok});
  // A highlight note being written in the rail is a draft until Save note. It
  // counts as unsaved once it differs from the note on file, so leaving the
  // page asks first, as it does for a board or a connection being edited.
  const highlightDrafting = () => Boolean(markEditor) && markEditor.text !== markEditor.saved;
  const markEditorOn = paper => markEditor?.workspace === client.workspaceId && markEditor.paper === paper;
  const unsaved = () => dirty || connectionDraft || highlightDrafting();
  const refKey = r => JSON.stringify([r.paper, r.claim || r.id]);
  const refs = () => [...claims].map(s => { const [paper, claim] = JSON.parse(s); return {paper, claim}; });
  const resolve = ref => client.state.claims.find(c => c.paper === ref.paper && c.id === ref.claim);
  const selectedPapers = () => [...new Set([...papers, ...refs().map(r => r.paper)])];
  const button = (action, title, data = '') => `<button type="button" data-tool="${action}" ${data}>${title}</button>`;
  const input = (label, id, value = '') => `<label>${label}<input id="${id}" value="${html(value)}"></label>`;
  const area = (label, id, value = '') => `<label>${label}<textarea id="${id}">${html(value)}</textarea></label>`;
  function remember() {
    try { sessionStorage.setItem(`doxograph-selection:${workspace}`, JSON.stringify({claims:[...claims], papers:[...papers]})); } catch (_) { /* optional */ }
  }
  function sync() {
    if (workspace !== client.workspaceId) {
      workspace = client.workspaceId;
      // The switch has happened: an editor open on a mark of the workspace
      // left is closed here and not before, since a later guard can still
      // refuse the switch after this module has agreed to it.
      markEditor = null;
      claims = new Set(); papers = new Set();
      try {
        const data = JSON.parse(sessionStorage.getItem(`doxograph-selection:${workspace}`) || '{}');
        claims = new Set(data.claims || []); papers = new Set(data.papers || []);
      } catch (_) { /* no saved selection */ }
    }
    // Reconcile the editor against the new corpus without using a missing
    // mark as its saved baseline. Clean editors close; changed text remains
    // editable and copyable until explicitly discarded.
    if (markEditor && client.state.notebook) {
      const removed = !client.state.papers.some(p => p.key === markEditor.paper)
        || !(client.state.notebook.highlights || []).some(h => h.id === markEditor.id && h.paper === markEditor.paper);
      if (removed !== Boolean(markEditor.removed)) {
        if (removed && !highlightDrafting()) markEditor = null;
        else markEditor.removed = removed;
        redrawContent();
      }
    }
    const available = new Set(client.state.claims.map(refKey));
    claims = new Set([...claims].filter(k => available.has(k)));
    papers = new Set([...papers].filter(k => client.state.papers.some(p => p.key === k)));
    remember();
    el('selection-count').textContent = `${claims.size} claims · ${papers.size} papers selected`;
    document.querySelectorAll('[data-select-claim]').forEach(c => c.checked = claims.has(c.dataset.selectClaim));
    document.querySelectorAll('[data-select-paper]').forEach(c => c.checked = papers.has(c.dataset.selectPaper));
    redrawHighlights();
  }
  function claimCheckbox(row) {
    const key = refKey(row);
    const connections = (client.state.notebook?.connections || []).filter(c => refKey(c.source) === key || refKey(c.target) === key).length;
    return `<div class="claim-selection"><label><input type="checkbox" data-select-claim="${html(key)}" ${claims.has(key) ? 'checked' : ''}> Select claim</label>
      ${connections ? button('connections', `${connections} manual connection${connections === 1 ? '' : 's'}`) : ''}</div>`;
  }
  function paperCheckbox(p) {
    return `<input type="checkbox" class="paper-selection" aria-label="Select paper: ${html(p.title || p.key)}" data-select-paper="${html(p.key)}" ${papers.has(p.key) ? 'checked' : ''}>`;
  }
  async function request(path = '', method = 'GET', body, ws = modalWorkspace || workspace) {
    return client.request('/api/notebook' + path, {method, headers:{'Content-Type':'application/json', 'X-Doxograph-Workspace':ws},
      ...(body === undefined ? {} : {body:JSON.stringify(body)})});
  }
  function message(text, error = false) {
    el('research-message').textContent = text;
    el('research-message').className = error ? 'warn' : 'hint';
  }
  function setBusy(value) {
    busy = value;
    if (value) {
      el('research-body').querySelectorAll('input:not(:disabled), textarea:not(:disabled), select:not(:disabled)').forEach(field => { field.dataset.busyDisabled = '1'; field.disabled = true; });
    } else {
      el('research-body').querySelectorAll('[data-busy-disabled]').forEach(field => { field.disabled = false; delete field.dataset.busyDisabled; });
    }
    el('research-tools').classList.toggle('is-busy', value);
    el('research-tools').setAttribute('aria-busy', String(value));
  }
  async function save() {
    dirty = true;
    notebook = await request('', 'PUT', notebook);
    dirty = false;
    await refreshAll();
    message('Saved.');
  }
  function notesChanged() { return notes && notes.text !== notes.saved; }
  function notesStatus() {
    if (!notes || !el('workspace-notes-status')) return;
    el('workspace-notes-status').textContent = notes.error ? `Couldn’t save. ${notes.error}`
      : notesSaving ? 'Saving…' : notesChanged() ? 'Waiting to save…' : 'Saved';
    el('workspace-notes-status').className = notes.error ? 'warn' : 'hint';
    el('workspace-notes-retry').hidden = !notes.error;
  }
  async function saveNotes() {
    clearTimeout(notesTimer);
    if (notesSaving) return notesSaving;
    if (!notesChanged()) return true;
    const draft = notes;
    draft.error = '';
    notesSaving = (async () => {
      // Keep the editor live during saves; send any newer text after this one.
      while (draft.text !== draft.saved) {
        const text = draft.text;
        try {
          notebook = await request('/notes', 'PUT', {notes:text, previous:draft.saved});
          draft.saved = text;
        } catch (error) {
          draft.error = error.message;
          return false;
        }
      }
      return true;
    })().finally(() => { notesSaving = null; notesStatus(); });
    notesStatus();
    return notesSaving;
  }
  async function close({duringOperation = false} = {}) {
    if (busy && !duringOperation) return;
    if (!await saveNotes()) return;
    if ((dirty || connectionDraft) && !await confirmDialog('Discard unsaved changes in research tools?', {ok:'Discard changes'})) return;
    epoch++;
    dirty = false; connectionDraft = false;
    notes = null;
    el('research-tools').close();
  }
  async function open(next = 'compare', requestedBoard = null) {
    sync();
    modalWorkspace = workspace;
    tab = next;
    el('research-tools').showModal();
    el('research-body').innerHTML = '<p class="hint">Loading your research…</p>';
    message('');
    notebook = await request();
    if (requestedBoard !== null) {
      boardId = requestedBoard;
      if (!currentBoard()) throw new Error('That board no longer exists.');
    }
    dirty = false;
    render();
  }
  function citation(row) {
    return `${(row.paper_authors || []).join(', ') || row.paper} · ${row.paper_year || 'year unknown'} · ${row.locator || 'no page recorded'}`;
  }
  function claimSummary(ref) {
    const row = resolve(ref);
    if (!row) return `<p class="warn">Source claim no longer exists: ${html(ref.paper)} / ${html(ref.claim)}</p>`;
    return `<h3>${html(row.text)}</h3><p class="hint">${html(row.paper_title)}<br>${html(citation(row))}</p>
      ${row.quote ? `<blockquote>${html(row.quote)}</blockquote>` : ''}
      ${row.evidence ? `<p><strong>Evidence</strong><br>${html(row.evidence)}</p>` : ''}
      ${row.note ? `<p><strong>My note</strong><br>${html(row.note)}</p>` : ''}
      <p class="hint">${html((row.tags || []).join(' · '))}</p>
      ${button('source', 'Open source', `data-paper="${html(row.paper)}" data-claim="${html(row.id)}"`)}`;
  }
  function render() {
    const labels = {notes:'Workspace notes', compare:'Compare & organize', boards:'Evidence boards', connections:'Claim connections', reading:'Reading queue', marks:'Marks', searches:'Saved searches'};
    el('research-tools-title').textContent = tab === 'notes' ? 'Workspace notes' : tab === 'history' ? 'Edit history' : 'Your research tools';
    el('research-tabs').innerHTML = Object.entries(labels).map(([key, label]) => button('tab', label, `data-tab="${key}" ${tab === key ? 'aria-current="page"' : ''}`)).join('')
      + button('reload', 'Reload saved version');
    if (tab === 'compare') renderCompare();
    if (tab === 'notes') renderNotes();
    if (tab === 'boards') renderBoards();
    if (tab === 'connections') renderConnections();
    if (tab === 'reading') renderReading();
    if (tab === 'marks') renderMarks();
    if (tab === 'searches') renderSearches();
    if (tab === 'history') renderHistory();
  }
  function renderNotes() {
    clearTimeout(notesPreviewTimer);
    notes = {text:notebook.notes, saved:notebook.notes, error:''};
    const name = client.workspaces.find(w => w.id === modalWorkspace)?.name || 'this workspace';
    el('research-body').innerHTML = `<div class="workspace-notes">
      <p class="hint">Your notes for ${html(name)}. Changes save automatically.</p>
      <div class="note-toolbar" aria-label="Workspace writing tools">
        ${['heading','bold','equation','table','link'].map(format => button('format-notes', format[0].toUpperCase() + format.slice(1), `data-format="${format}"`)).join('')}
        ${button('notes-image', 'Image')}
        <input id="workspace-notes-image" type="file" accept="image/png,image/jpeg,image/gif,image/webp" hidden>
        <label class="note-view-label">View <select id="workspace-notes-view">${['write','split','preview'].map(mode => `<option value="${mode}" ${notesMode === mode ? 'selected' : ''}>${mode[0].toUpperCase() + mode.slice(1)}</option>`).join('')}</select></label>
      </div>
      <div class="note-pages" data-mode="${notesMode}">
        <div class="note-write"><label for="workspace-notes">What are you investigating? <span class="hint">Markdown</span></label>
          <textarea id="workspace-notes" spellcheck="true" placeholder="Capture questions, thoughts, and next steps."></textarea></div>
        <div class="note-read"><span class="hint">Preview</span><article id="workspace-notes-preview" class="note-prose" aria-label="Workspace notes preview"></article></div>
      </div>
      <div class="tool-actions"><span id="workspace-notes-status" role="status" aria-live="polite"></span>
        ${button('retry-notes', 'Retry save', 'id="workspace-notes-retry" hidden')}</div>
      <details class="note-help"><summary>Formatting guide</summary><p>Use # for headings, **bold**, *italic*, lists, blockquotes, and [text](https://…) for links. Put inline LaTeX between $…$ and display equations between $$…$$. Use the toolbar to insert a table or image. Upload, drop, or paste images to store them locally. Raw HTML is shown as text.</p></details></div>`;
    el('workspace-notes').value = notes.text;
    notesStatus(); renderNotesPreview();
    if (notesMode !== 'preview') el('workspace-notes').focus();
  }
  function renderNotesPreview() {
    if (!notes || !el('workspace-notes-preview')) return;
    el('workspace-notes-preview').replaceChildren(NoteWriting.render(notes.text, modalWorkspace));
  }
  function editNotes() {
    notes.text = el('workspace-notes').value;
    notesStatus();
    clearTimeout(notesTimer);
    notesTimer = setTimeout(saveNotes, 600);
    clearTimeout(notesPreviewTimer);
    notesPreviewTimer = setTimeout(renderNotesPreview, 180);
  }
  function insertNotes(before, after = '', fallback = '') {
    const field = el('workspace-notes');
    if (notesMode === 'preview') {
      notesMode = 'split'; el('workspace-notes-view').value = notesMode;
      field.closest('.note-pages').dataset.mode = notesMode;
    }
    const start = field.selectionStart, end = field.selectionEnd;
    field.setRangeText(before + (field.value.slice(start, end) || fallback) + after, start, end, 'end');
    editNotes(); renderNotesPreview(); field.focus();
  }
  async function uploadNotesImage(file) {
    if (file.size > 10 * 1024 * 1024) throw new Error('Choose an image smaller than 10 MB.');
    message('');
    const form = new FormData(); form.append('file', file);
    const result = await client.request('/api/research-notes/images', {method:'POST',
      headers:{'X-Doxograph-Workspace':modalWorkspace}, body:form});
    insertNotes(`\n![${(file.name || 'Figure').replace(/[\[\]\\\r\n]/g, '')}](${result.url})\n`);
  }
  function renderCompare() {
    el('research-body').innerHTML = `<p class="hint">Selections stay pinned while you browse other papers or topics. Select claims or papers using their checkboxes.</p>
      <div class="tool-actions">${button('add-board', 'Add claims to board')} ${button('queue-selection', 'Queue selected papers')}
      ${button('export-selection', 'Export Markdown', 'data-format="markdown"')} ${button('export-selection', 'Export CSV', 'data-format="csv"')}</div>
      <fieldset><legend>Bulk organization</legend><div class="tool-actions">
        <label>Change<select id="bulk-field"><option value="tags">Topics on selected claims</option><option value="labels">Labels on selected papers</option></select></label>
        <label>Action<select id="bulk-mode"><option value="add">Add</option><option value="remove">Remove</option><option value="replace">Replace all</option></select></label>
        ${input('Values, separated by commas', 'bulk-values')}${button('bulk', 'Apply to selection')}
      </div><p class="hint">Topics must already exist in the vocabulary. Paper labels also apply to the papers of selected claims.</p></fieldset>
      <div class="comparison">${refs().map(r => `<article class="comparison-card">${claimSummary(r)}${button('unpin', 'Unpin claim', `data-ref="${html(refKey(r))}"`)}</article>`).join('')}
      ${[...papers].map(key => {
        const p = client.state.papers.find(p => p.key === key);
        return `<article class="comparison-card"><h3>${html(p.title)}</h3><p class="hint">${html((p.authors || []).join(', '))} · ${html(p.year)}</p>
          <p>${html(p.summary || 'No summary recorded.')}</p><p>${html(p.notes)}</p>
          ${button('source', p.has_pdf ? 'Read paper' : 'Open paper', `data-paper="${html(key)}"`)}${button('unpin-paper', 'Unpin paper', `data-paper="${html(key)}"`)}</article>`;
      }).join('')}</div>${!claims.size && !papers.size ? '<p class="empty">Select claims or papers in the library to compare them here.</p>' : ''}`;
  }
  function currentBoard() { return notebook.boards.find(b => b.id === boardId); }
  function renderBoards() {
    if (!currentBoard()) boardId = notebook.boards[0]?.id || null;
    const board = currentBoard();
    el('research-body').innerHTML = `<div class="tool-actions"><label>Board<select id="board-picker"><option value="">Choose a board</option>${notebook.boards.map(b => `<option value="${html(b.id)}" ${b.id === boardId ? 'selected' : ''}>${html(b.title)}</option>`).join('')}</select></label>
      ${button('new-board','New board')}${board ? button('delete-board','Delete board') : ''}</div>
      ${board ? `${input('Board title', 'board-title', board.title)}
        <div class="tool-actions">${button('board-add-selection','Add selected claims')}${button('board-heading','Add heading')}${button('board-note','Add commentary')}
        ${button('save-board','Save board')}${button('export-board','Export Markdown','data-format="markdown"')}${button('export-board','Export CSV','data-format="csv"')}</div>
        <div class="board-entries">${board.entries.map((entry,i) => `<article class="board-entry" data-entry="${html(entry.id)}">
          <div class="entry-actions"><span class="hint">${i+1} · ${html(entry.kind === 'note' ? 'commentary' : entry.kind)}</span>
          ${button('entry-up','↑', `aria-label="Move entry ${i+1} up" ${i === 0 ? 'disabled' : ''}`)}${button('entry-down','↓', `aria-label="Move entry ${i+1} down" ${i === board.entries.length-1 ? 'disabled' : ''}`)}${button('entry-remove','Remove')}</div>
          ${entry.kind === 'claim' ? claimSummary(entry.reference) : ''}
          <label>${entry.kind === 'heading' ? 'Heading' : 'Your commentary'}<textarea data-entry-text="${html(entry.id)}">${html(entry.text)}</textarea></label>
        </article>`).join('')}</div>${!board.entries.length ? '<p class="empty">Add selected claims, then arrange headings and commentary around the evidence.</p>' : ''}`
        : '<p class="empty">Create a board for a question, argument, or section you are writing.</p>'}`;
  }
  function renderConnections() {
    const options = refs().map(r => `<option value="${html(refKey(r))}">${html(resolve(r)?.text || r.claim)}</option>`).join('');
    el('research-body').innerHTML = `<p class="hint">Select at least two claims in the library to connect them. “Qualifies” runs from the first claim to the second.</p>
      <fieldset><legend>Connect selected claims</legend>
      <label>First claim<select id="connection-source">${options}</select></label>
      <label>Relationship<select id="connection-relation"><option value="agrees">Agrees with</option><option value="contradicts">Contradicts</option><option value="qualifies">Qualifies</option></select></label>
      <label>Second claim<select id="connection-target">${options}</select></label>
      ${area('Your explanation', 'connection-note')}${button('create-connection','Create connection', claims.size < 2 ? 'disabled' : '')}</fieldset>
      ${notebook.connections.map(c => `<article class="board-entry" data-connection="${html(c.id)}"><strong>${html(c.relation)}</strong>
        <div class="connection-pair"><div>${claimSummary(c.source)}</div><div>${claimSummary(c.target)}</div></div>
        <label>Your explanation<textarea data-connection-note="${html(c.id)}">${html(c.note)}</textarea></label>
        ${button('save-connection','Save explanation')}${button('delete-connection','Remove connection')}</article>`).join('')}`;
    if (el('connection-target')?.options.length > 1) el('connection-target').selectedIndex = 1;
  }
  function renderReading() {
    el('research-body').innerHTML = `<div class="tool-actions">${button('queue-selection','Add selected papers')}</div>
      <p class="hint">Queue order is independent of claim review. Page position and bookmarks are saved as you read.</p>
      ${notebook.reading.map((r,i) => {
        const p = client.state.papers.find(p => p.key === r.paper);
        return `<article class="reading-entry" data-reading="${html(r.paper)}"><h3>${html(p?.title || 'Removed paper: '+r.paper)}</h3>
          <div class="tool-actions"><label>Reading status<select data-reading-status="${html(r.paper)}">${['queued','reading','finished'].map(s => `<option ${r.status === s ? 'selected' : ''}>${s}</option>`).join('')}</select></label>
          ${p?.has_pdf ? button('reader',`Resume at page ${r.page}`,`data-paper="${html(r.paper)}"`) : '<span class="hint">No PDF available</span>'}
          ${button('reading-up','↑',`aria-label="Move paper up" ${i === 0 ? 'disabled' : ''}`)}${button('reading-down','↓',`aria-label="Move paper down" ${i === notebook.reading.length-1 ? 'disabled' : ''}`)}${button('reading-remove','Remove from queue')}</div>
          <div class="tool-actions">${r.bookmarks.map(page => button('reader',`Bookmark: page ${page}`,`data-paper="${html(r.paper)}" data-page="${page}"`)).join('')}</div></article>`;
      }).join('')}${notebook.reading.length ? '' : '<p class="empty">Add a paper from its header, or queue a selection of papers.</p>'}`;
  }
  function markQuote(h) {
    return `<blockquote class="highlight-${html(h.color)}">${h.kind === 'figure' ? '<em>Figure or region</em>' : html(h.text)}</blockquote>`;
  }
  function renderMarks() {
    const marks = notebook.highlights.filter(h => marksFilter === 'all' || h.question);
    const papers = [...new Set(marks.map(h => h.paper))].map(key => ({key, paper:client.state.papers.find(p => p.key === key)}))
      .sort((a, b) => (a.paper?.title || a.key).localeCompare(b.paper?.title || b.key));
    el('research-body').innerHTML = `<p class="hint">Passages and figures you marked while reading, with your notes and questions. Open a mark to return to its page.</p>
      <div class="tool-actions"><label>Show<select id="marks-filter">${[['all','All marks'],['questions','Open questions']].map(([value, label]) => `<option value="${value}" ${marksFilter === value ? 'selected' : ''}>${label}</option>`).join('')}</select></label></div>
      ${papers.map(({key, paper}) => `<section class="marks-paper"><h3>${html(paper?.title || 'Removed paper: '+key)}</h3>
        ${paper?.has_pdf ? `<div class="tool-actions">${button('export-annotated','Export annotated PDF',`data-paper="${html(key)}"`)}</div>` : ''}
        ${marks.filter(h => h.paper === key).sort((a, b) => a.page - b.page || a.rects[0].y - b.rects[0].y).map(h => `<article class="highlight-entry" data-highlight="${html(h.id)}">
          <p class="hint">Page ${h.page} · ${h.kind === 'figure' ? 'figure' : 'passage'}${h.question ? ' · <strong>open question</strong>' : ''}</p>
          ${markQuote(h)}${h.note ? `<p>${html(h.note)}</p>` : ''}
          <div class="tool-actions">${paper?.has_pdf ? button('reader',`Open page ${h.page}`,`data-paper="${html(key)}" data-page="${h.page}" data-mark="${html(h.id)}"`) : '<span class="hint">No PDF available</span>'}
          ${h.question ? button('answer-question','Mark answered') : ''}</div></article>`).join('')}</section>`).join('')}
      ${marks.length ? '' : `<p class="empty">${marksFilter === 'all' ? 'Highlight a passage or mark a figure while reading a paper.' : 'No open questions. Tick “Open question” on a mark to list it here.'}</p>`}`;
  }
  function renderSearches() {
    el('research-body').innerHTML = `<p class="hint">Saved searches remember filters, not a fixed list of results. New matching claims appear automatically.</p>
      ${button('save-search','Save current library filters')}
      ${notebook.searches.map(s => `<article class="reading-entry" data-search="${html(s.id)}"><h3>${html(s.name)}</h3>
        <p class="hint">${html([s.q && `Search: ${s.q}`, s.tag && `Topic: ${s.tag}`, s.label && `Label: ${s.label}`, s.kind, s.unreviewed && 'Unreviewed only', s.unverified && 'Quote not found', s.paper && `Paper: ${client.state.papers.find(p => p.key === s.paper)?.title || s.paper}`].filter(Boolean).join(' · ') || 'All claims')}</p>
        ${button('apply-search','Open search')}${button('rename-search','Rename')}${button('delete-search','Delete')}</article>`).join('')}`;
  }
  async function queue(keys) {
    if (!keys.length) throw new Error('Select at least one paper or claim first.');
    for (const paper of keys) if (!notebook.reading.some(r => r.paper === paper)) notebook.reading.push({paper,status:'queued',page:1,bookmarks:[]});
    await save();
  }
  async function download(format, board = null) {
    if (dirty) await save();
    if (!board && !claims.size && !papers.size) throw new Error('Select claims or papers to export first.');
    const response = await client.response('/api/notebook/export', {method:'POST', headers:{'Content-Type':'application/json','X-Doxograph-Workspace':modalWorkspace},
      body:JSON.stringify({format,board,claims:refs(),papers:[...papers]})});
    await saveFile(response, `selected-research.${format === 'csv' ? 'csv' : 'md'}`);
  }
  async function saveFile(response, fallback) {
    if (!response.ok) { const body = await response.json(); throw new Error(body.detail || 'Export failed'); }
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a'); link.href = url;
    link.download = /filename="([^"]+)"/.exec(response.headers.get('Content-Disposition') || '')?.[1] || fallback;
    link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  async function downloadAnnotated(paper, ws = modalWorkspace) {
    const response = await client.response(`/api/notebook/annotated/${encodeURIComponent(paper)}`, {headers:{'X-Doxograph-Workspace':ws}});
    await saveFile(response, `${paper}-annotated.pdf`);
    const skipped = Number(response.headers.get('X-Doxograph-Skipped-Marks') || 0);
    (el('research-tools').open ? message : text => toast(text))(skipped ? `Annotated copy exported. ${skipped} mark${skipped === 1 ? ' is' : 's are'} past the last page of this PDF and ${skipped === 1 ? 'was' : 'were'} left out.` : 'Annotated copy exported. The original PDF is unchanged.', skipped > 0);
  }
  async function showHistory(paper, claim = null) {
    historyPaper = paper; historyClaim = claim;
    historyData = await request(`/history/${encodeURIComponent(paper)}`);
    tab = 'history'; render();
  }
  function renderHistory() {
    const entries = historyData.entries.filter(e => !historyClaim || e.claim === historyClaim);
    el('research-body').innerHTML = `<p class="hint">Earlier claim wording and paper notes are captured from now on. Restore changes only that claim or note, and keeps the current version in history.</p>
      ${entries.map(e => {
        const current = e.claim ? historyData.claims.find(c => c.id === e.claim) : {notes:historyData.notes};
        const deleted = e.fields === null;
        const fields = deleted ? (current ? ['text', 'quote', 'note'] : []) : Object.keys(e.fields).filter(k => (!current || k !== 'reviewed') && JSON.stringify(e.fields[k]) !== JSON.stringify(current?.[k]));
        return `<article class="board-entry"><h3>${e.claim ? html(e.claim) : 'Paper note'} · ${html(new Date(e.at).toLocaleString())}</h3>
          <div class="history-diff"><div><h4>Earlier version</h4>${deleted ? '<p>Claim deleted</p>' : fields.map(k => `<strong>${html(k)}</strong><pre>${html(typeof e.fields[k] === 'string' ? e.fields[k] : JSON.stringify(e.fields[k],null,2))}</pre>`).join('')}</div>
          <div><h4>Current version</h4>${!current ? '<p class="warn">Claim deleted</p>' : fields.map(k => `<strong>${html(k)}</strong><pre>${html(typeof current[k] === 'string' ? current[k] : JSON.stringify(current[k],null,2))}</pre>`).join('')}</div></div>
          ${button('restore', deleted ? 'Restore deletion' : 'Restore this version',`data-revision="${html(e.id)}" ${fields.length ? '' : 'disabled'}`)}</article>`;
      }).join('')}${entries.length ? '' : '<p class="empty">No earlier versions yet. Future edits will appear here.</p>'}`;
  }
  // --- The paper, read beside its notes -----------------------------------
  // Mounted by app.js whenever the claims view shows one paper with a PDF.
  // Pages are laid out as placeholders at their size and drawn only near the
  // viewport, so a long paper costs what is on screen and not a canvas per page.
  const readerKey = (paper, ws) => `${ws}\u0000${paper}`;
  const zoomKey = 'doxograph-reader-zoom', ZOOMS = ['fit', '1', '1.3', '1.6', '2'];
  function savedZoom() {
    try { const zoom = localStorage.getItem(zoomKey); return ZOOMS.includes(zoom) ? zoom : 'fit'; }
    catch (_) { return 'fit'; }
  }
  // The scale a zoom setting means: 'fit' is whatever fills the column.
  function scaleFor(zoom, base) {
    if (zoom !== 'fit') return Number(zoom);
    const scroller = el('pdf-scroll');
    const width = (scroller?.clientWidth || 700) - 18;
    return Math.max(.3, Math.round(width / base.w * 1000) / 1000);
  }
  async function mountReader(paper) {
    const ws = client.workspaceId;
    // The newest request wins: a mount that waited for the last reader to be
    // torn down must not go on to open its paper if a later render has asked
    // for another paper, or for none, meanwhile.
    const ticket = ++mountSeq;
    if (reader && (!paper || reader.key !== readerKey(paper, ws))) await unmountReader();
    if (ticket !== mountSeq) return;
    el('reader').hidden = !paper;
    if (!paper || reader) return;
    const generation = ++readerEpoch;
    const zoom = savedZoom();
    reader = {key:readerKey(paper, ws), workspace:ws, paper, generation, zoom, scale:1, pages:[], current:1, navigated:false, selection:null, figureMode:false, drag:null, figure:null, focus:null, loadingTask:null, document:null, pdfjs:null, observer:null, resizer:null, progressTimer:null, savedPage:null, wantedPage:null, progressSave:Promise.resolve()};
    el('reader').innerHTML = `<div class="reader-toolbar">${button('previous-page', 'Previous page')}
      <label>Page<input id="reader-page" type="number" min="1" value="1" aria-label="Page number"></label><span id="reader-total"></span>${button('go-page', 'Go')}${button('next-page', 'Next page')}
      <label>Zoom<select id="reader-zoom">${ZOOMS.map(z => `<option value="${z}" ${z === zoom ? 'selected' : ''}>${z === 'fit' ? 'Fit width' : `${Math.round(Number(z) * 100)}%`}</option>`).join('')}</select></label>
      ${button('bookmark', 'Bookmark this page')}${button('figure-mode', 'Mark a figure', 'aria-pressed="false" title="Drag a box around a figure, table, or any region of the page"')}<span class="reader-status" id="reader-status"></span></div>
      <div class="pdf-scroll" id="pdf-scroll"><div class="passage-tools" id="passage-tools" hidden>
        ${COLORS.map(c => button('highlight-selection', '', `class="swatch highlight-${c}" data-color="${c}" aria-label="Highlight in ${c}" title="Highlight in ${c}"`)).join('')}
        ${button('capture-selection', 'Capture as claim')}</div></div>
      <p id="reader-hint" class="hint">Loading PDF…</p>`;
    // Buttons on the toolbar must not take the selection away before they act on it.
    el('passage-tools').addEventListener('mousedown', event => event.preventDefault());
    try {
      const pdfjs = await import('/vendor/pdf.min.mjs');
      if (reader?.generation !== generation) return;
      pdfjs.GlobalWorkerOptions.workerSrc = '/vendor/pdf.worker.min.mjs';
      const task = pdfjs.getDocument({url:`/pdf/${encodeURIComponent(paper)}?workspace=${encodeURIComponent(ws)}&inline=1`, isEvalSupported:false, cMapUrl:'/vendor/cmaps/', cMapPacked:true, standardFontDataUrl:'/vendor/standard_fonts/', wasmUrl:'/vendor/wasm/'});
      reader.loadingTask = task;
      const doc = await task.promise;
      if (reader?.generation !== generation) { await doc.loadingTask.destroy(); return; }
      reader.document = doc; reader.pdfjs = pdfjs;
      const first = await doc.getPage(1);
      if (reader?.generation !== generation) return;
      const base = first.getViewport({scale:1});
      const scroller = el('pdf-scroll');
      reader.scale = scaleFor(zoom, {w:base.width, h:base.height});
      for (let number = 1; number <= doc.numPages; number++) {
        const page = {number, el:document.createElement('div'), base:{w:base.width, h:base.height}, renderedAt:0, rendering:null, pdfPage:number === 1 ? first : null, text:null};
        page.el.className = 'pdf-page pending'; page.el.dataset.page = number;
        sizePage(page);
        scroller.appendChild(page.el);
        reader.pages.push(page);
      }
      el('reader-total').textContent = `of ${doc.numPages}`;
      el('reader-page').max = doc.numPages;
      reader.observer = new IntersectionObserver(entries => {
        if (reader?.generation !== generation) return;
        for (const entry of entries) {
          const page = reader.pages[Number(entry.target.dataset.page) - 1];
          if (entry.isIntersecting) renderPage(page);
          else if (!pageNear(page)) {
            page.error = null;
            if (page.renderedAt) clearPage(page);
          }
        }
      }, {root:scroller, rootMargin:'600px 0px'});
      reader.pages.forEach(page => reader.observer.observe(page.el));
      // Fit width follows the column as the window or the side pane is resized.
      reader.resizer = new ResizeObserver(() => {
        if (reader?.generation !== generation || reader.zoom !== 'fit' || reader.resizeTimer) return;
        reader.resizeTimer = setTimeout(() => { reader.resizeTimer = null; if (reader?.generation === generation) run(() => setZoom('fit')); }, 150);
      });
      reader.resizer.observe(scroller);
      scroller.addEventListener('scroll', () => {
        if (reader?.generation !== generation || reader.scrollTick) return;
        reader.scrollTick = requestAnimationFrame(() => {
          if (reader?.generation !== generation) return;
          reader.scrollTick = null; trackCurrentPage();
          if (!reader.pinned) reader.navigated = true;
        });
      }, {passive:true});
      const progress = (client.state.notebook?.reading || []).find(r => r.paper === paper);
      reader.savedPage = progress?.page ?? null;
      const asked = pendingPage && pendingPage.paper === paper && pendingPage.workspace === ws ? pendingPage : null;
      pendingPage = null;
      goToPage(asked?.page || progress?.page || 1, asked?.mark || null, {initial:true});
      trackCurrentPage();
    } catch (error) {
      if (reader?.generation === generation) {
        reader.openFailed = true;
        el('reader-hint').textContent = `Could not open the PDF: ${error.message} `;
        el('reader-hint').insertAdjacentHTML('beforeend', button('retry-reader', 'Retry PDF'));
      }
    }
  }
  async function unmountReader() {
    const gone = reader;
    reader = null;
    el('reader').hidden = true; el('reader').innerHTML = '';
    if (!gone) return;
    gone.observer?.disconnect(); gone.resizer?.disconnect();
    clearTimeout(gone.progressTimer); clearTimeout(gone.resizeTimer);
    // A page reached within the debounce is still the page the reading
    // stopped on: save it now, for the paper and workspace this reader had,
    // behind any save of its already in flight. Not awaited: the next paper
    // must not wait on it.
    if (gone.wantedPage !== null && gone.wantedPage !== gone.savedPage) queueProgressSave(gone);
    if (gone.loadingTask?.destroy) await gone.loadingTask.destroy();
    else if (gone.document) await gone.document.loadingTask.destroy();
  }
  // The scale a page is drawn at: under Fit width each page fits the column
  // on its own, so a landscape page in a portrait paper does not overflow;
  // a numeric zoom is the same for every page.
  function pageScale(page) {
    return reader.zoom === 'fit' ? scaleFor('fit', page.base) : reader.scale;
  }
  function sizePage(page) {
    const scale = pageScale(page);
    keepingView(() => {
      page.el.style.width = `${Math.floor(page.base.w * scale)}px`;
      page.el.style.height = `${Math.floor(page.base.h * scale)}px`;
    });
  }
  // Where the eye is taken to be: the same line a third of the way down the
  // scroller that names the current page, so the page under it is the one
  // kept in place by every correction below.
  const READING_LINE = .35;
  const readingLine = scroller => scroller.scrollTop + scroller.clientHeight * READING_LINE;
  function pinnedPage(scroller) {
    if (!reader.pinned) return null;
    const page = reader.pages[reader.pinned - 1];
    const expected = Math.min(Math.max(0, page.el.offsetTop - 8), scroller.scrollHeight - scroller.clientHeight);
    if (Math.abs(scroller.scrollTop - expected) <= 2) return page;
    reader.pinned = null;
    return null;
  }
  function viewportAnchor() {
    if (!reader?.pages.length) return null;
    const scroller = el('pdf-scroll');
    const pinned = pinnedPage(scroller);
    if (pinned) return {page:pinned, within:null};
    if (reader.tracked && scroller.scrollHeight > scroller.clientHeight
      && scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 2) return {end:true};
    const line = readingLine(scroller);
    const page = reader.pages.find(p => p.el.offsetTop + p.el.offsetHeight > line) || reader.pages.at(-1);
    return {page, within:(line - page.el.offsetTop) / Math.max(1, page.el.offsetHeight)};
  }
  function restoreViewport(anchor) {
    if (!anchor) return;
    const scroller = el('pdf-scroll');
    scroller.scrollTop = anchor.end ? scroller.scrollHeight - scroller.clientHeight
      : anchor.within === null ? Math.max(0, anchor.page.el.offsetTop - 8)
      : anchor.page.el.offsetTop + anchor.within * anchor.page.el.offsetHeight - scroller.clientHeight * READING_LINE;
  }
  function keepingView(resize) {
    // A navigation pin follows its target's top, even while that target's own
    // placeholder grows. Natural reading follows the reading-line position.
    const anchor = viewportAnchor();
    resize();
    restoreViewport(anchor);
  }
  // A page scrolled far away gives back its canvas and the caches PDF.js keeps
  // behind it (operator lists, decoded images), so a long paper costs what is
  // near the viewport. A page with a selection on it stays: the passage being
  // highlighted must not vanish under the pointer.
  function pageSelected(page) {
    const selection = window.getSelection();
    return Boolean(selection?.rangeCount) && selection.containsNode(page.el, true);
  }
  function clearPage(page) {
    if (page.rendering || pageSelected(page)) return;
    for (const canvas of page.el.querySelectorAll('canvas')) { canvas.width = 0; canvas.height = 0; }
    page.el.innerHTML = ''; page.el.classList.add('pending'); page.renderedAt = 0;
    page.text = null;
    page.pdfPage?.cleanup();
  }
  function pageNear(page) {
    const scroller = el('pdf-scroll');
    if (!scroller) return false;
    const top = page.el.offsetTop, bottom = top + page.el.offsetHeight;
    return bottom > scroller.scrollTop - 600 && top < scroller.scrollTop + scroller.clientHeight + 600;
  }
  function renderPage(page, {retry = false} = {}) {
    if (!reader) return Promise.resolve();
    if (page.rendering) {
      if (retry) page.retryRequested = true;
      return page.rendering;
    }
    if (page.pdfPage && page.renderedAt === pageScale(page)) return Promise.resolve();
    if (page.error && !retry) return Promise.resolve();
    const generation = reader.generation;
    let scale = null, failed = false;
    page.retryRequested = false;
    page.error = null;
    page.rendering = (async () => {
      const pdfPage = page.pdfPage || (page.pdfPage = await reader.document.getPage(page.number));
      if (reader?.generation !== generation) return;
      // The page's own size, known only now: until here it was laid out at
      // the first page's, which is what most papers' pages share.
      const unit = pdfPage.getViewport({scale:1});
      page.base = {w:unit.width, h:unit.height};
      scale = pageScale(page);
      if (page.renderedAt === scale) { sizePage(page); return; }
      const viewport = pdfPage.getViewport({scale});
      const container = page.el;
      container.innerHTML = '<canvas></canvas><div class="highlightLayer"></div><div class="textLayer"></div>';
      keepingView(() => {
        container.style.width = `${viewport.width}px`;
        container.style.height = `${viewport.height}px`;
      });
      container.style.setProperty('--total-scale-factor', viewport.scale * (pdfPage.userUnit || 1));
      container.style.setProperty('--scale-round-x', '1px');
      container.style.setProperty('--scale-round-y', '1px');
      const canvas = container.querySelector('canvas');
      const ratio = Math.min(devicePixelRatio || 1, 2);
      canvas.width = Math.floor(viewport.width * ratio); canvas.height = Math.floor(viewport.height * ratio);
      canvas.style.width = `${viewport.width}px`; canvas.style.height = `${viewport.height}px`;
      await pdfPage.render({canvasContext:canvas.getContext('2d'), viewport, transform:ratio === 1 ? null : [ratio, 0, 0, ratio, 0, 0]}).promise;
      if (reader?.generation !== generation || pageScale(page) !== scale) return;
      const content = page.text || (page.text = await pdfPage.getTextContent());
      if (reader?.generation !== generation || pageScale(page) !== scale) return;
      await new reader.pdfjs.TextLayer({textContentSource:content, container:container.querySelector('.textLayer'), viewport}).render();
      if (reader?.generation !== generation || pageScale(page) !== scale) return;
      page.hasText = content.items.some(item => item.str?.trim());
      page.renderedAt = scale;
      container.classList.remove('pending');
      drawPageHighlights(page);
      if (page.number === reader.current) readerHint();
    })().catch(error => {
      failed = true;
      page.error = error.message;
      if (reader?.generation === generation && page.number === reader.current) readerHint();
    })
      .finally(() => {
        page.rendering = null;
        const retryRequested = page.retryRequested;
        page.retryRequested = false;
        if (reader?.generation !== generation) return;
        // A failed page waits for another explicit navigation or visibility
        // change; it must not be mistaken for a render invalidated by zoom.
        if (failed) {
          clearPage(page);
          // Navigation during a background render requests one retry if it
          // fails. Consume that request before starting so failure cannot loop.
          if (retryRequested && pageNear(page)) return renderPage(page, {retry:true});
          return;
        }
        // The zoom changed under this render: draw it again at the new scale.
        if (page.renderedAt !== pageScale(page) && pageNear(page)) renderPage(page);
        // Scrolled out of reach while drawing: the observer's leave came too
        // early to release it, so it is released here.
        else if (page.renderedAt && !pageNear(page)) clearPage(page);
      });
    return page.rendering;
  }
  function paperHighlights(paper) {
    return (client.state.notebook?.highlights || []).filter(h => h.paper === paper);
  }
  function drawPageHighlights(page) {
    const layer = page.el.querySelector('.highlightLayer');
    if (!layer) return;
    const percent = value => `${(value * 100).toFixed(3)}%`;
    layer.innerHTML = paperHighlights(reader.paper).filter(h => h.page === page.number).flatMap(h => h.rects.map(r =>
      `<div class="highlight-${html(h.color)}${h.kind === 'figure' ? ' figure' : ''}${h.id === reader.focus ? ' focused' : ''}" data-highlight="${html(h.id)}" style="left:${percent(r.x)};top:${percent(r.y)};width:${percent(r.w)};height:${percent(r.h)}"></div>`)).join('');
  }
  // A figure is the box dragged out on the page, in the same fractions as a passage's lines.
  function figureRect(drag, event) {
    const clamp = value => Math.min(1, Math.max(0, value));
    const round = value => Math.round(value * 1e5) / 1e5;
    const [x1, x2] = [drag.x, event.clientX].map(x => clamp((x - drag.box.left) / drag.box.width)).sort((a, b) => a - b);
    const [y1, y2] = [drag.y, event.clientY].map(y => clamp((y - drag.box.top) / drag.box.height)).sort((a, b) => a - b);
    return {x:round(x1), y:round(y1), w:round(x2 - x1), h:round(y2 - y1)};
  }
  function setFigureMode(on) {
    if (!reader) return;
    reader.figureMode = on;
    if (!on) { reader.drag = null; el('reader').querySelector('.figure-draft')?.remove(); hidePassageTools(); }
    el('pdf-scroll')?.classList.toggle('drawing', on);
    const toggle = el('reader').querySelector('[data-tool="figure-mode"]');
    if (toggle) { toggle.textContent = on ? 'Cancel figure mark' : 'Mark a figure'; toggle.setAttribute('aria-pressed', String(on)); }
    if (on) { window.getSelection()?.removeAllRanges(); hidePassageTools(); }
    readerHint();
  }
  // The box just drawn waits for a color: the passage toolbar, under the box,
  // offers the same swatches it offers a selected passage.
  function offerFigure(page, rect, pageEl) {
    reader.figure = {page, rect};
    const tools = el('passage-tools'), scroller = el('pdf-scroll');
    const box = scroller.getBoundingClientRect(), at = pageEl.getBoundingClientRect();
    tools.classList.add('for-figure');
    tools.hidden = false;
    tools.style.top = `${at.top + (rect.y + rect.h) * at.height - box.top + scroller.scrollTop + 6}px`;
    tools.style.left = `${Math.max(4, Math.min(at.left + rect.x * at.width - box.left + scroller.scrollLeft, scroller.clientWidth - tools.offsetWidth - 4))}px`;
  }
  function redrawHighlights() {
    if (!reader) return;
    reader.pages.forEach(page => { if (page.renderedAt) drawPageHighlights(page); });
  }
  function readerHint() {
    const hint = el('reader-hint');
    if (!hint || !reader) return;
    const page = reader.pages[reader.current - 1];
    hint.textContent = page?.error ? `Could not draw page ${page.number}: ${page.error}`
      : reader.figureMode ? 'Drag a box around the figure on the page, then pick a color.'
      : page?.renderedAt && !page.hasText
      ? 'This page has no selectable text. You can transcribe a quote and add a claim by hand.'
      : 'Select a passage to highlight it or capture it as a claim, or mark a figure.';
  }
  function trackCurrentPage(asked = null) {
    if (!reader?.pages.length) return;
    const scroller = el('pdf-scroll');
    // A page gone to by name is the current page even when the scroller cannot
    // reach its top, as with a short last page or a paper that fits whole; it
    // stands until the reader scrolls away from where that landed.
    if (asked) reader.pinned = asked;
    else pinnedPage(scroller);
    const line = readingLine(scroller);
    // Scrolled to the very end, the last page is the one being read even
    // when it is too short to reach the reading line.
    const atEnd = scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 2;
    const page = reader.pinned ? reader.pages[reader.pinned - 1]
      : atEnd ? reader.pages[reader.pages.length - 1]
      : reader.pages.find(p => p.el.offsetTop + p.el.offsetHeight > line) || reader.pages[reader.pages.length - 1];
    if (page.number === reader.current && reader.tracked) return;
    reader.current = page.number; reader.tracked = true;
    // The mark opened from the Marks list stays outlined until the reader moves to another page.
    if (reader.focus && paperHighlights(reader.paper).find(h => h.id === reader.focus)?.page !== page.number) {
      reader.focus = null; redrawHighlights(); focusRailEntry(null);
    }
    const field = el('reader-page');
    if (field && document.activeElement !== field) field.value = page.number;
    const progress = (client.state.notebook?.reading || []).find(r => r.paper === reader.paper);
    const marked = Boolean(progress?.bookmarks.includes(page.number));
    const bookmark = el('reader').querySelector('[data-tool="bookmark"]');
    if (bookmark) bookmark.textContent = marked ? 'Remove bookmark' : 'Bookmark this page';
    readerHint();
    // Where the reading stopped, for the queue: only for a paper that is on it.
    // Compared against the page this reader last saved, not the notebook in
    // `client.state`: that is refreshed by the poll, and between polls it would still say
    // the page the paper opened on, so going back to it would never be saved.
    clearTimeout(reader.progressTimer);
    if (!progress) return;
    reader.wantedPage = page.number;
    if (reader.savedPage !== page.number) {
      const active = reader;
      reader.progressTimer = setTimeout(() => { if (reader === active) queueProgressSave(active); }, 1500);
    }
  }
  // One save after another: two in flight would race for the revision. Each
  // saves the page wanted when its turn comes, and when it lands the reader
  // may have moved on — a save held long enough for a move away and back
  // would otherwise leave the queue on a page nobody is reading — so the
  // wanted page is compared again and another save follows if they differ.
  // Works on the reader it is given rather than the one mounted, so a reader
  // being torn down can still save the page it reached.
  // One chain of saves per paper and workspace, shared by a reader torn down
  // and the reader opened on the same paper after it, so they land in order.
  const progressChains = new Map();
  function queueProgressSave(active) {
    const {paper, workspace:ws, key} = active;
    const chain = (progressChains.get(key) || Promise.resolve()).then(async () => {
      const number = active.wantedPage;
      if (number === null || number === active.savedPage) return;
      // Torn down and replaced by a reader on the same paper: that reader's
      // page is the one to keep, so this save stands down.
      const successor = reader && reader !== active && reader.key === key ? reader : null;
      if (successor) { if (successor.wantedPage !== successor.savedPage) queueProgressSave(successor); return; }
      await mutateNotebook(ws, n => { const r = n.reading.find(r => r.paper === paper); if (!r || r.page === number) return false; r.page = number; }, {refresh:false});
      active.savedPage = number;
      // What the queue and the Continue card show until the next poll.
      if (ws === client.workspaceId) {
        const local = (client.state.notebook?.reading || []).find(r => r.paper === paper);
        if (local) local.page = number;
        renderContinuation();
      }
      // A reader opened on this paper while the save ran started from the
      // page the queue had then; it now knows what the queue says.
      const since = reader && reader !== active && reader.key === key ? reader : null;
      if (since) { since.savedPage = number; if (since.wantedPage !== null && since.wantedPage !== number) queueProgressSave(since); }
      active.retried = false;
      if (active.wantedPage !== active.savedPage) queueProgressSave(active);
    }).catch(async error => {
      // Once more after a moment, since a reader already torn down has no
      // later scroll to try again; then say so rather than lose the page.
      if (!active.retried) { active.retried = true; await new Promise(resolve => setTimeout(resolve, 600)); queueProgressSave(active); return; }
      toast(`Could not save the page you stopped on (page ${active.wantedPage}): ${error.message}`, {tone:'warn'});
    });
    progressChains.set(key, chain);
    active.progressSave = chain;
  }
  async function goToPage(number, highlight = null, {initial = false} = {}) {
    if (!reader?.pages.length) { if (reader) askPage(reader.paper, reader.workspace, number, highlight); return; }
    if (!initial) reader.navigated = true;
    const page = reader.pages[Math.max(1, Math.min(number, reader.pages.length)) - 1];
    el('pdf-scroll').scrollTo({top:Math.max(0, page.el.offsetTop - 8)});
    trackCurrentPage(page.number);
    const active = reader;
    if (highlight) active.focus = highlight;
    await renderPage(page, {retry:true});
    if (!highlight || reader !== active || active.focus !== highlight) return;
    drawPageHighlights(page);
    page.el.querySelectorAll(`.highlightLayer [data-highlight="${CSS.escape(highlight)}"]`).forEach(mark => {
      mark.classList.add('flash'); setTimeout(() => mark.classList.remove('flash'), 1600);
    });
    focusRailEntry(highlight);
  }
  // The rail is drawn by app.js, so the outline follows the mark by hand: on
  // the entry of the mark asked for, and off again when the reader moves on.
  function focusRailEntry(id) {
    document.querySelectorAll('.highlight-entry.focused').forEach(entry => entry.classList.remove('focused'));
    const entry = id && document.querySelector(`.highlight-entry[data-highlight="${CSS.escape(id)}"]`);
    if (entry) { entry.classList.add('focused'); entry.scrollIntoView({block:'nearest'}); }
  }
  async function setZoom(zoom) {
    if (!reader?.pages.length || !ZOOMS.includes(zoom)) return;
    const scale = scaleFor(zoom, reader.pages[0].base);
    const same = zoom === reader.zoom && scale === reader.scale;
    reader.zoom = zoom;
    try { localStorage.setItem(zoomKey, zoom); } catch (_) { /* preference remains for this page */ }
    if (same) return;
    const active = reader, anchor = viewportAnchor();
    reader.scale = scale;
    reader.pages.forEach(page => { clearPage(page); sizePage(page); });
    restoreViewport(anchor);
    reader.tracked = false;
    await Promise.all(active.pages.filter(pageNear).map(page => renderPage(page, {retry:true})));
    if (reader === active) trackCurrentPage();
  }
  // Client rectangles of the selection as fractions of the page, one per line of text.
  function selectionRects(range, pageEl) {
    const box = pageEl.getBoundingClientRect();
    const rects = [...range.getClientRects()]
      .map(r => ({left:Math.max(r.left, box.left), right:Math.min(r.right, box.right), top:Math.max(r.top, box.top), bottom:Math.min(r.bottom, box.bottom)}))
      .filter(r => r.right - r.left > .5 && r.bottom - r.top > .5)
      .sort((a, b) => a.top - b.top || a.left - b.left);
    const lines = [];
    for (const r of rects) {
      // A selection reports both whole spans and their text; overlapping boxes on a line become one.
      const line = lines.find(l => Math.abs(l.top - r.top) < (r.bottom - r.top) / 2 && r.left <= l.right + 2 && r.right >= l.left - 2);
      if (line) Object.assign(line, {left:Math.min(line.left, r.left), right:Math.max(line.right, r.right), top:Math.min(line.top, r.top), bottom:Math.max(line.bottom, r.bottom)});
      else lines.push({...r});
    }
    const round = value => Math.round(value * 1e5) / 1e5;
    return lines.slice(0, 500).map(r => ({x:round((r.left - box.left) / box.width), y:round((r.top - box.top) / box.height),
      w:round((r.right - r.left) / box.width), h:round((r.bottom - r.top) / box.height)})).filter(r => r.w > 0 && r.h > 0);
  }
  function hidePassageTools() {
    if (reader) { reader.selection = null; reader.figure = null; }
    const tools = el('passage-tools');
    if (tools) { tools.hidden = true; tools.classList.remove('for-figure'); }
  }
  function trackSelection() {
    if (!reader || reader.figureMode || reader.figure) return;
    const selection = window.getSelection();
    const layerOf = node => (node && (node.nodeType === 1 ? node : node.parentElement))?.closest('#reader .textLayer') || null;
    const layer = selection && !selection.isCollapsed && selection.rangeCount ? layerOf(selection.anchorNode) : null;
    if (!layer || layerOf(selection.focusNode) !== layer) { hidePassageTools(); return; }
    const pageEl = layer.closest('.pdf-page');
    const range = selection.getRangeAt(0);
    const text = selection.toString().trim(), rects = selectionRects(range, pageEl);
    if (!text || !rects.length) { hidePassageTools(); return; }
    reader.selection = {page:Number(pageEl.dataset.page), text, rects};
    const tools = el('passage-tools'), scroller = el('pdf-scroll');
    tools.classList.remove('for-figure');
    const box = scroller.getBoundingClientRect(), lines = range.getClientRects();
    const last = lines[lines.length - 1] || pageEl.getBoundingClientRect();
    tools.hidden = false;
    tools.style.top = `${last.bottom - box.top + scroller.scrollTop + 6}px`;
    tools.style.left = `${Math.max(4, Math.min(last.left - box.left + scroller.scrollLeft, scroller.clientWidth - tools.offsetWidth - 4))}px`;
  }
  // Read, change, write: the saved notebook rather than a copy held since the
  // modal last opened, so a highlight made here cannot overwrite a board saved
  // there. `fn` returns false to leave the notebook as it is.
  async function mutateNotebook(ws, fn, {refresh = true} = {}) {
    const latest = await request('', 'GET', undefined, ws);
    if (fn(latest) === false) return latest;
    const saved = await request('', 'PUT', latest, ws);
    if (refresh) await refreshAll();
    return saved;
  }
  // The rail's list of the paper's highlights, in page order, drawn by app.js
  // under the paper's note. The note being edited is held here, not in the DOM:
  // a redraw rebuilds the editor from it.
  function highlightsHtml(paper) {
    const mine = paperHighlights(paper).slice().sort((a, b) => a.page - b.page);
    const empty = '<p class="hint">Select a passage in the paper to highlight it, with a note or a question of your own, or to capture it as a claim. Mark a figure by dragging a box around it.</p>';
    const hasPdf = client.state.papers.find(p => p.key === paper)?.has_pdf;
    return `<section class="highlights"><h3>Marks${mine.length ? ` <span class="n hint">${mine.length}</span>` : ''}
      ${mine.length && hasPdf ? `<span class="cact">${button('export-annotated', 'Export annotated PDF', `data-paper="${html(paper)}" title="A copy of the PDF with these marks written in as annotations"`)}</span>` : ''}</h3>${mine.length ? mine.map(h => {
      const editing = markEditorOn(paper) && markEditor.id === h.id, figure = h.kind === 'figure';
      return `<article class="highlight-entry${h.id === reader?.focus ? ' focused' : ''}${h.question ? ' question' : ''}" data-highlight="${html(h.id)}">
        <blockquote class="highlight-${html(h.color)}" data-tool="highlight-page" data-page="${h.page}" title="${figure ? 'Show this figure in the paper' : 'Show this passage in the paper'}">${figure ? '<em>Figure or region</em>' : html(h.text)}</blockquote>
        ${editing ? `<textarea data-highlight-note="${html(h.id)}" placeholder="${figure ? 'Your note or question on this figure' : 'Your note or question on this passage'}" aria-label="Note on this mark">${html(markEditor.text)}</textarea>`
          : h.note.trim() ? noteHtml(h.note, h.question ? 'my question' : 'my note') : ''}
        <div class="hmeta"><span>p. ${h.page}</span>
          <label title="A question to come back to; it is listed under Marks and questions until answered"><input type="checkbox" data-highlight-question="${html(h.id)}" ${h.question ? 'checked' : ''}> Open question</label>
          <span class="cact">
          ${editing ? button('save-highlight', 'Save note') + button('cancel-highlight', 'Cancel') : button('edit-highlight', h.note.trim() ? 'Edit note' : 'Add note')}
          ${figure ? '' : button('capture-highlight', 'Capture as claim')}${button('remove-highlight', 'Remove')}</span></div>
      </article>`;
    }).join('') : empty}</section>`;
  }
  function redrawContent() {
    const field = document.activeElement, claim = field?.closest('form[data-form]');
    const selector = field?.matches('[data-highlight-note]')
      ? `[data-highlight-note="${CSS.escape(field.dataset.highlightNote)}"]`
      : claim && field.name ? `form[data-form="${CSS.escape(claim.dataset.form)}"] [name="${CSS.escape(field.name)}"]` : null;
    const cursor = selector ? {selector, start:field.selectionStart, end:field.selectionEnd} : null;
    captureOpenEditor();
    renderContent();
    const replacement = cursor && document.querySelector(cursor.selector);
    if (replacement) {
      replacement.focus();
      if (cursor.start !== null && cursor.start !== undefined) replacement.setSelectionRange(cursor.start, cursor.end);
    }
  }
  function recoveryHtml() {
    if (!markEditor?.removed || markEditor.workspace !== client.workspaceId) return '';
    return `<section class="highlights"><article class="highlight-entry" data-removed-mark>
      <p class="warn" role="status">This mark was removed in another window. Your unsaved note is kept here.</p>
      <textarea data-highlight-note="${html(markEditor.id)}" aria-label="Note on removed mark">${html(markEditor.text)}</textarea>
      <div class="tool-actions">${button('copy-removed-note', 'Copy note')}${button('discard-removed-note', 'Discard note')}</div>
    </article></section>`;
  }
  // Polls leave a visible editor alone, including one whose mark disappeared.
  function holds() {
    return Boolean(markEditor) && markEditor.workspace === client.workspaceId
      && (markEditor.removed || markEditorOn(V.paper));
  }
  function markDraftOn(paper) { return markEditorOn(paper) && highlightDrafting(); }
  function dropMarkDraft(paper) { if (markEditorOn(paper)) markEditor = null; }
  function goToPaper(paper, claim = null) {
    captureOpenEditor(); parkNewClaimForNavigation(paper);
    V.paper = paper; V.view = 'claims'; V.tag = null; V.label = null;
    V.q = ''; V.kind = ''; V.unreviewed = false; V.unverified = false;
    V.selectedId = claim;
    el('q').value = ''; el('kind').value = ''; el('only-unreviewed').checked = false; el('only-unverified').checked = false;
    scheduleTextSearch(); renderAll(); syncHash(true); scrollToSelected();
  }
  const READER_TOOLS = new Set(['previous-page', 'next-page', 'go-page', 'bookmark', 'reader', 'highlight-selection', 'capture-selection',
    'highlight-page', 'edit-highlight', 'save-highlight', 'cancel-highlight', 'capture-highlight', 'remove-highlight', 'figure-mode', 'export-annotated', 'copy-removed-note', 'discard-removed-note', 'retry-reader']);
  async function readerAction(name, target) {
    const data = target.dataset;
    if (name === 'retry-reader') {
      const active = reader, navigation = navigationVersion();
      if (!active?.openFailed) return;
      await unmountReader();
      if (client.workspaceId === active.workspace && navigationVersion() === navigation
        && V.paper === active.paper && V.view === 'claims' && !V.pdfHidden) await mountReader(active.paper);
      return;
    }
    if (['previous-page', 'next-page', 'go-page'].includes(name)) {
      if (!reader?.pages.length) return;
      const page = name === 'go-page' ? Number(el('reader-page').value) : reader.current + (name === 'next-page' ? 1 : -1);
      if (!Number.isInteger(page) || page < 1 || page > reader.pages.length) throw new Error('Choose a page within this PDF.');
      await goToPage(page); return;
    }
    if (name === 'copy-removed-note') { if (markEditor?.removed) await copyText(markEditor.text, 'Note copied.'); return; }
    if (name === 'discard-removed-note') {
      const draft = markEditor;
      if (draft?.removed && await confirmDialog('Discard this recovered note?', {ok:'Discard note'}) && markEditor === draft) {
        markEditor = null; redrawContent();
      }
      return;
    }
    if (name === 'highlight-page') {
      const id = target.closest('[data-highlight]')?.dataset.highlight;
      if (reader) { await goToPage(Number(data.page), id); return; }
      // The PDF was put away, or this paper has none any more.
      if (!client.state.papers.find(p => p.key === V.paper)?.has_pdf) throw new Error('This paper has no PDF to show the passage in.');
      askPage(V.paper, client.workspaceId, Number(data.page), id || null);
      if (V.pdfHidden) { V.pdfHidden = false; storePdfHidden(); redrawContent(); }
      return;
    }
    if (name === 'bookmark') {
      if (!reader?.pages.length) return;
      const active = reader;
      const {paper, current, workspace:ws} = active;
      await mutateNotebook(ws, n => {
        let progress = n.reading.find(r => r.paper === paper);
        if (!progress) { progress = {paper, status:'reading', page:current, bookmarks:[]}; n.reading.push(progress); }
        progress.page = current;
        progress.bookmarks = progress.bookmarks.includes(current) ? progress.bookmarks.filter(p => p !== current) : [...progress.bookmarks, current];
      });
      // A replacement on the same paper learns what reached disk. Resume
      // that page only if it has not received a newer navigation or scroll.
      if (!reader || reader.key !== active.key) return;
      reader.savedPage = current;
      if (reader !== active && !reader.navigated) { await goToPage(current); return; }
      reader.tracked = false; trackCurrentPage(); return;
    }
    // Resuming a paper from the queue or the Continue card: it opens in the
    // main pane at that page. Resuming is reading, so a paper not on the queue
    // joins it and a queued one is marked as being read; a finished one is
    // left as it is.
    if (name === 'reader') {
      const navigation = navigationVersion(), ws = client.workspaceId;
      if (el('research-tools').open) {
        if (!await discardDrafts()) return;
        await close({duringOperation:true});
        if (el('research-tools').open) return;
      }
      const paper = data.paper;
      const saved = await mutateNotebook(ws, n => {
        const entry = n.reading.find(r => r.paper === paper);
        if (!entry) { n.reading.push({paper, status:'reading', page:1, bookmarks:[]}); return; }
        if (entry.status !== 'queued') return false;
        entry.status = 'reading';
      });
      // Moved on while the request was out: the paper chosen since stands.
      if (client.workspaceId !== ws || navigationVersion() !== navigation) return;
      const page = Number(data.page) || saved.reading.find(r => r.paper === paper)?.page || 1;
      if (reader?.paper === paper && reader.workspace === ws && !V.pdfHidden) { await goToPage(page, data.mark || null); return; }
      askPage(paper, ws, page, data.mark || null);
      if (V.pdfHidden) { V.pdfHidden = false; storePdfHidden(); }
      if (V.paper === paper && V.view === 'claims') redrawContent(); else goToPaper(paper);
      return;
    }
    if (name === 'figure-mode') { setFigureMode(!reader?.figureMode); return; }
    if (name === 'export-annotated') { await downloadAnnotated(data.paper, client.workspaceId); return; }
    if (name === 'highlight-selection') {
      const color = COLORS.includes(data.color) ? data.color : 'yellow';
      const figure = reader?.figure;
      if (figure) {
        const {paper, workspace:ws} = reader;
        hidePassageTools(); setFigureMode(false);
        await mutateNotebook(ws, n => { n.highlights.push({id:uid(), paper, page:figure.page, kind:'figure', rects:[figure.rect], text:'', color, note:'', question:false}); });
        redrawHighlights(); return;
      }
      const passage = reader?.selection;
      if (!passage) throw new Error('Select a passage in the paper first.');
      const {paper, workspace:ws} = reader;
      window.getSelection()?.removeAllRanges(); hidePassageTools();
      await mutateNotebook(ws, n => { n.highlights.push({id:uid(), paper, page:passage.page, kind:'passage', rects:passage.rects, text:passage.text, color, note:'', question:false}); });
      redrawHighlights(); return;
    }
    if (name === 'capture-selection') {
      const passage = reader?.selection;
      if (!passage) throw new Error('Select a passage in the paper first.');
      const paper = reader.paper;
      window.getSelection()?.removeAllRanges(); hidePassageTools();
      startClaimFromPassage(paper, {quote:passage.text, locator:`p. ${passage.page}`}); return;
    }
    const id = target.closest('[data-highlight]')?.dataset.highlight;
    const highlight = paperHighlights(V.paper).find(h => h.id === id);
    if (!highlight) throw new Error('This highlight is no longer here. It may have been removed in another window.');
    if (name === 'edit-highlight') {
      // One note is written at a time; the one half-written is not dropped for
      // another without asking.
      if (markEditor?.id !== id && highlightDrafting()
        && !await confirmDialog('Discard the note you are writing on another highlight?', {ok:'Discard note'})) return;
      markEditor = {id, paper:highlight.paper, workspace:client.workspaceId, saved:highlight.note, text:highlight.note, removed:false}; redrawContent(); document.querySelector('[data-highlight-note]')?.focus(); return;
    }
    if (name === 'cancel-highlight') { markEditor = null; redrawContent(); return; }
    if (name === 'capture-highlight') {
      // The note as it is being written, when its editor is open: what is on
      // screen, not the saved version the person is in the middle of changing.
      const note = markEditor?.id === id ? markEditor.text : highlight.note;
      startClaimFromPassage(highlight.paper, {quote:highlight.text, locator:`p. ${highlight.page}`, note}); return;
    }
    if (name === 'save-highlight') {
      const draft = markEditor;
      if (!draft || draft.id !== id || draft.workspace !== client.workspaceId) return;
      const note = draft.text;
      target.disabled = true;
      try {
        await mutateNotebook(draft.workspace, n => {
          const h = n.highlights.find(h => h.id === id && h.paper === draft.paper);
          if (!h) throw new Error('This highlight was removed in another window.');
          if (h.note !== draft.saved) throw new Error('This note changed in another window. Your draft is kept; copy it before reloading the saved note.');
          h.note = note;
        }, {refresh:false});
        draft.saved = note;
        // Only this exact editor may be completed by its save. New typing
        // remains a draft against the version that actually reached disk.
        if (markEditor === draft && draft.text === note) markEditor = null;
        else if (markEditor === draft) toast('Saved the note as it was when you clicked Save. What you typed since is still in the editor.');
        await refreshAll();
      } finally { target.disabled = false; }
      return;
    }
    if (name === 'remove-highlight') {
      const label = highlight.kind === 'figure' ? 'mark' : 'highlight';
      const writing = markEditor?.id === id && markEditor.text.trim();
      if ((highlight.note || writing) && !await confirmDialog(`Remove this ${label} and its note?`, {ok:`Remove ${label}`})) return;
      // The editor goes once the write has landed and before the refresh,
      // which would otherwise find the mark gone and report a removal made
      // in another window; a write that fails leaves the editor as it was.
      await mutateNotebook(client.workspaceId, n => { n.highlights = n.highlights.filter(h => h.id !== id); }, {refresh:false});
      if (markEditor?.id === id) { markEditor = null; }
      await refreshAll();
      redrawHighlights(); return;
    }
  }
  async function discardDrafts({reloadNotes = false} = {}) {
    clearTimeout(notesTimer);
    if (reloadNotes) { if (notesSaving) await notesSaving; }
    else if (!await saveNotes()) return false;
    if ((dirty || connectionDraft || notesChanged()) && !await confirmDialog('Discard unsaved changes before leaving this view?',{ok:'Discard changes'})) {
      if (notesChanged() && !notes.error) notesTimer = setTimeout(saveNotes, 600);
      return false;
    }
    if (dirty) { notebook = await request(); dirty = false; }
    connectionDraft = false;
    return true;
  }
  async function action(name, target) {
    const data = target.dataset;
    if (name === 'close') { await close({duringOperation:true}); return; }
    if (name === 'retry-notes') { await saveNotes(); return; }
    if (name === 'tab' || name === 'reload') {
      if (!await discardDrafts({reloadNotes:name === 'reload'})) return;
      epoch++;
      notebook = await request();
      notes = null;
      tab = data.tab || (tab === 'history' ? 'compare' : tab); render(); return;
    }
    if (name === 'unpin') { claims.delete(data.ref); remember(); sync(); renderCompare(); return; }
    if (name === 'unpin-paper') { papers.delete(data.paper); remember(); sync(); renderCompare(); return; }
    if (name === 'source') {
      if (!await discardDrafts()) return;
      await close({duringOperation:true}); if (el('research-tools').open) return;
      goToPaper(data.paper, data.claim || null); return;
    }
    if (name === 'bulk') {
      const values = el('bulk-values').value.split(',').map(s=>s.trim()).filter(Boolean);
      const mode = el('bulk-mode').value;
      if (!values.length && mode !== 'replace') throw new Error('Enter at least one value.');
      if (mode === 'replace' && !await confirmDialog('Replace all existing values on this selection?',{ok:'Replace values'})) return;
      const result = await request('/bulk','POST',{papers:el('bulk-field').value === 'labels' ? selectedPapers() : [...papers],claims:refs(),field:el('bulk-field').value,mode,values});
      await refreshAll(); renderCompare(); message(`Updated ${result.changed} items.`); return;
    }
    if (name === 'export-selection') { await download(data.format); return; }
    if (name === 'export-board') { await download(data.format, boardId); return; }
    if (name === 'queue-selection') { await queue(selectedPapers()); tab = 'reading'; render(); return; }
    if (name === 'queue-paper') { await queue([data.paper]); tab = 'reading'; render(); return; }
    if (name === 'new-board') {
      const title = await promptDialog('Name this evidence board', '', {ok:'Create board'});
      if (!title?.trim()) return;
      const board = {id:uid(),title:title.trim(),entries:[]};
      notebook.boards.push(board); boardId = board.id; await save(); renderBoards(); return;
    }
    if (name === 'add-board') { tab = 'boards'; render(); message('Choose or create a board, then add the selected claims.'); return; }
    if (name === 'board-add-selection') {
      if (!claims.size) throw new Error('Select claims in the library first.');
      const board = currentBoard();
      for (const reference of refs()) if (!board.entries.some(e => e.reference && refKey(e.reference) === refKey(reference))) board.entries.push({id:uid(),kind:'claim',reference,text:''});
      await save(); renderBoards(); return;
    }
    if (name === 'board-heading' || name === 'board-note') {
      currentBoard().entries.push({id:uid(),kind:name === 'board-heading' ? 'heading' : 'note',text:'',reference:null}); dirty = true; renderBoards(); return;
    }
    if (name === 'save-board' || name === 'save-connection') { await save(); return; }
    if (name === 'delete-board') {
      if (!await confirmDialog('Delete this evidence board? Source claims will remain in the library.',{ok:'Delete board'})) return;
      notebook.boards = notebook.boards.filter(b=>b.id !== boardId); await save(); renderBoards(); return;
    }
    if (name.startsWith('entry-')) {
      const entries = currentBoard().entries, id = target.closest('[data-entry]').dataset.entry;
      const i = entries.findIndex(e=>e.id === id);
      if (name === 'entry-remove') entries.splice(i,1);
      else { const j = i + (name === 'entry-up' ? -1 : 1); if (j >= 0 && j < entries.length) [entries[i],entries[j]] = [entries[j],entries[i]]; }
      dirty = true; renderBoards(); return;
    }
    if (name === 'create-connection') {
      const from = JSON.parse(el('connection-source').value), to = JSON.parse(el('connection-target').value);
      if (JSON.stringify(from) === JSON.stringify(to)) throw new Error('Choose two different claims.');
      const relation = el('connection-relation').value;
      const existing = notebook.connections.find(c => refKey(c.source) === JSON.stringify(from) && refKey(c.target) === JSON.stringify(to) && c.relation === relation);
      if (existing) existing.note = el('connection-note').value;
      else notebook.connections.push({id:uid(),source:{paper:from[0],claim:from[1]},target:{paper:to[0],claim:to[1]},relation,note:el('connection-note').value});
      await save(); connectionDraft = false; renderConnections(); return;
    }
    if (name === 'delete-connection') {
      const id = target.closest('[data-connection]').dataset.connection;
      if (!await confirmDialog('Remove this manual connection?',{ok:'Remove connection'})) return;
      notebook.connections = notebook.connections.filter(c=>c.id !== id); await save(); renderConnections(); return;
    }
    if (name.startsWith('reading-')) {
      const key = target.closest('[data-reading]').dataset.reading, items = notebook.reading;
      const i = items.findIndex(r=>r.paper === key);
      if (name === 'reading-remove') items.splice(i,1);
      else { const j = i + (name === 'reading-up' ? -1 : 1); if (j >= 0 && j < items.length) [items[i],items[j]] = [items[j],items[i]]; }
      await save(); renderReading(); return;
    }
    if (name === 'save-search') {
      const title = await promptDialog('Name this saved search', '', {ok:'Save search'});
      if (!title?.trim()) return;
      const search = {id:uid(),name:title.trim()};
      for (const field of ['q','paper','tag','label','kind','unreviewed','unverified','group']) search[field] = V[field];
      notebook.searches.push(search); await save(); tab = 'searches'; render(); return;
    }
    if (['apply-search','rename-search','delete-search'].includes(name)) {
      const id = target.closest('[data-search]').dataset.search, search = notebook.searches.find(s=>s.id === id);
      if (name === 'apply-search') {
        if (!await discardDrafts()) return;
        await close({duringOperation:true}); if (el('research-tools').open) return; captureOpenEditor();
        const params = new URLSearchParams();
        if (workspace !== 'default') params.set('ws',workspace);
        for (const field of ['q','paper','tag','label','kind']) if (search[field]) params.set(field, search[field]);
        if (search.unreviewed) params.set('unreviewed','1'); if (search.unverified) params.set('unverified','1');
        if (!search.group) params.set('group','0');
        history.pushState(null,'',`#${params}`); applyHash(); dropMissingFilters(); renderAll(); return;
      }
      if (name === 'rename-search') {
        const title = await promptDialog('Rename saved search', search.name, {ok:'Rename'});
        if (!title?.trim()) return; search.name = title.trim();
      } else notebook.searches = notebook.searches.filter(s=>s.id !== id);
      await save(); renderSearches(); return;
    }
    if (name === 'answer-question') {
      notebook.highlights.find(h => h.id === target.closest('[data-highlight]').dataset.highlight).question = false;
      await save(); renderMarks(); message('Question marked answered. The mark and its note remain.'); return;
    }
    if (name === 'history') { if (!await discardDrafts()) return; await showHistory(data.paper,data.claim); return; }
    if (name === 'restore') {
      const entry = historyData.entries.find(e => e.id === data.revision);
      const prompt = entry?.fields === null ? 'Restore this deletion? The claim will be removed and its wording kept in history.' : 'Restore this earlier version? The current version will remain in history.';
      if (!await confirmDialog(prompt,{ok:entry?.fields === null ? 'Restore deletion' : 'Restore version'})) return;
      const restored = await request(`/history/${encodeURIComponent(historyPaper)}/restore`,'POST',{revision:data.revision,expected:historyData.expected});
      await refreshAll(); await showHistory(historyPaper,historyClaim);
      message(restored.deleted ? 'Claim removed. Its wording remains in history.' : restored.omitted_topics.length ? `Version restored. Topics no longer in the vocabulary were omitted: ${restored.omitted_topics.join(', ')}.` : 'Version restored.'); return;
    }
  }
  async function run(task) {
    if (busy) return;
    setBusy(true);
    try { await task(); }
    catch (error) { message(error.message,true); if (!el('research-tools').open) toast(error.message,{tone:'warn'}); }
    finally { setBusy(false); }
  }
  document.addEventListener('selectionchange', trackSelection);
  document.addEventListener('pointerdown', event => {
    const container = event.target.closest?.('#reader .pdf-page');
    if (!reader?.figureMode || busy || event.button !== 0 || !container || !container.querySelector('.highlightLayer')) return;
    event.preventDefault();
    hidePassageTools();
    container.setPointerCapture(event.pointerId);
    reader.drag = {page:Number(container.dataset.page), container, box:container.getBoundingClientRect(), x:event.clientX, y:event.clientY};
    container.querySelector('.highlightLayer').insertAdjacentHTML('beforeend', '<div class="figure-draft"></div>');
  });
  document.addEventListener('pointermove', event => {
    if (!reader?.drag) return;
    const r = figureRect(reader.drag, event), box = reader.drag.container.querySelector('.figure-draft');
    if (box) Object.assign(box.style, {left:`${r.x * 100}%`, top:`${r.y * 100}%`, width:`${r.w * 100}%`, height:`${r.h * 100}%`});
  });
  document.addEventListener('pointerup', event => {
    if (!reader?.drag) return;
    const {page, container} = reader.drag, rect = figureRect(reader.drag, event);
    reader.drag = null;
    // A click without a drag is not a figure; stay in figure mode for another try.
    if (rect.w < .01 || rect.h < .01) { container.querySelector('.figure-draft')?.remove(); return; }
    offerFigure(page, rect, container);
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && reader?.figureMode && !document.querySelector('dialog[open]')) { hidePassageTools(); setFigureMode(false); }
  });
  document.addEventListener('click', event => {
    // The label too: its text reaching the card would redraw the box away before it ticks.
    if (event.target.closest('[data-select-claim], [data-select-paper], .claim-selection label')) { event.stopPropagation(); return; }
    // A claim's PDF link scrolls the paper already open beside it rather than
    // opening a second copy in a new tab.
    const link = event.target.closest('a.pdflink');
    if (link && reader && link.dataset.paper === reader.paper) {
      event.preventDefault(); event.stopPropagation();
      goToPage(Number(link.dataset.page) || 1); return;
    }
    const target = event.target.closest('[data-tool]');
    if (!target) return;
    event.preventDefault(); event.stopPropagation();
    const name = target.dataset.tool;
    if (name === 'format-notes') {
      if (!busy) insertNotes(...NoteWriting.formats[target.dataset.format]);
      return;
    }
    if (name === 'notes-image') {
      if (!busy) {
        const picker = el('workspace-notes-image');
        picker.value = ''; picker.click();
      }
      return;
    }
    if (name === 'clear-selection') { claims.clear(); papers.clear(); remember(); sync(); return; }
    if (name === 'select-visible') { visibleClaims().forEach(c=>claims.add(refKey(c))); remember(); sync(); return; }
    if (READER_TOOLS.has(name)) { run(() => readerAction(name, target)); return; }
    run(async () => {
      if (!el('research-tools').open) {
        await open(['notes','boards','reading','marks','searches','connections','compare'].includes(name) ? name : 'compare');
        if (['notes','boards','reading','marks','searches','connections','compare'].includes(name)) return;
      }
      await action(name,target);
    });
  }, true);
  document.addEventListener('change', event => {
    const target = event.target;
    if (target.id === 'workspace-notes-view') {
      notesMode = target.value;
      el('workspace-notes').closest('.note-pages').dataset.mode = notesMode;
    }
    if (target.id === 'workspace-notes-image' && target.files[0]) run(() => uploadNotesImage(target.files[0]));
    if (target.matches('[data-select-claim]')) {
      target.checked ? claims.add(target.dataset.selectClaim) : claims.delete(target.dataset.selectClaim); remember(); sync(); return;
    }
    if (target.matches('[data-select-paper]')) {
      target.checked ? papers.add(target.dataset.selectPaper) : papers.delete(target.dataset.selectPaper); remember(); sync(); return;
    }
    if (target.id === 'board-picker') run(async () => {
      const next = target.value;
      if (!await discardDrafts()) { target.value = boardId || ''; return; }
      boardId = next; renderBoards();
    });
    if (target.matches('[data-reading-status]')) run(async () => {
      notebook.reading.find(r=>r.paper === target.dataset.readingStatus).status = target.value;
      await save();
    });
    if (target.id === 'reader-zoom') run(() => setZoom(target.value));
    if (target.id === 'marks-filter') { marksFilter = target.value; renderMarks(); }
    if (target.matches('[data-highlight-question]')) run(async () => {
      const id = target.dataset.highlightQuestion, question = target.checked;
      if (el('research-tools').open) {
        notebook.highlights.find(h => h.id === id).question = question;
        await save(); return;
      }
      await mutateNotebook(client.workspaceId, n => { const h = n.highlights.find(h => h.id === id); if (!h) throw new Error('This mark was removed in another window.'); h.question = question; });
    });
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Enter' && event.target.id === 'reader-page') { event.preventDefault(); run(() => readerAction('go-page', event.target)); }
    if (el('research-tools').open && tab === 'notes' && (event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's') {
      event.preventDefault(); saveNotes();
    }
  });
  document.addEventListener('input', event => {
    const target = event.target;
    if (target.dataset.highlightNote) {
      if (markEditor?.id === target.dataset.highlightNote && markEditor.workspace === client.workspaceId) markEditor.text = target.value;
      return;
    }
    if (!el('research-tools')?.contains(target)) return;
    if (target.id === 'workspace-notes') {
      editNotes();
      return;
    }
    if (target.id.startsWith('connection-')) connectionDraft = true;
    if (target.id === 'board-title') { currentBoard().title = target.value; dirty = true; }
    if (target.dataset.entryText) { currentBoard().entries.find(e=>e.id === target.dataset.entryText).text = target.value; dirty = true; }
    if (target.dataset.connectionNote) { notebook.connections.find(c=>c.id === target.dataset.connectionNote).note = target.value; dirty = true; }
  });
  document.addEventListener('DOMContentLoaded', () => {
    el('research-tools').addEventListener('cancel', event => { event.preventDefault(); close(); });
    for (const name of ['dragenter','dragover','dragleave','drop']) el('research-tools').addEventListener(name, event => {
      if (tab !== 'notes') return;
      event.stopPropagation();
      if (name === 'drop' || name === 'dragover') event.preventDefault();
      if (name === 'drop') {
        const file = [...event.dataTransfer.files].find(file => file.type.startsWith('image/'));
        if (file) run(() => uploadNotesImage(file));
      }
    });
    el('research-tools').addEventListener('paste', event => {
      if (tab !== 'notes') return;
      const file = [...(event.clipboardData?.files || [])].find(file => file.type.startsWith('image/'));
      if (file) { event.preventDefault(); event.stopPropagation(); run(() => uploadNotesImage(file)); }
    });
  });
  window.addEventListener('beforeunload', event => { if (unsaved() || notesChanged() || notesSaving) { event.preventDefault(); event.returnValue = ''; } });
  async function beforeWorkspaceSwitch() {
    // A reader action waiting on the notebook would finish in the workspace
    // it started in and open its paper over the one switched to.
    if (busy) {
      const text = 'Finish the current operation before switching workspaces.';
      if (el('research-tools').open) message(text, true); else toast(text, {tone:'warn'});
      return false;
    }
    if (el('research-tools').open) {
      await close();
      if (el('research-tools').open) return false;
    }
    // A highlight note being written in the rail is state app.js cannot see;
    // the switch would hide its editor and the next edit would overwrite it.
    if (highlightDrafting()) {
      if (!await confirmDialog('Discard the highlight note you are writing before switching workspaces?', {ok:'Discard note'})) return false;
    }
    // Agreed to, not yet done: the editor is closed by sync() once the
    // workspace has actually changed, so a guard after this one refusing the
    // switch leaves the note where it was.
    return true;
  }
  return {connect(value) { client = value; }, sync,claimCheckbox,paperCheckbox,beforeWorkspaceSwitch,mountReader,highlightsHtml,recoveryHtml,holds,markDraftOn,dropMarkDraft,
    openBoard: id => run(() => open('boards', id))};
})();
