/* Reader-owned workflows. Stored with the active workspace; never sent to a model. */
const ResearchTools = (() => {
  let workspace = null, claims = new Set(), papers = new Set();
  let notebook = null, tab = 'compare', boardId = null, dirty = false, busy = false, connectionDraft = false;
  let modalWorkspace = null, historyData = null, historyPaper = null, historyClaim = null;
  let epoch = 0;
  // The paper open beside its notes in the main pane. Separate from the modal:
  // it stays up while the research tools are opened and closed over it.
  let reader = null, readerEpoch = 0, highlightEditing = null, highlightDraft = '';
  // A page asked for before its paper's reader is up: {paper, workspace, page}.
  // Named, so a reader mounted for another paper meanwhile discards it rather
  // than opening on a page that was meant for somewhere else.
  let pendingPage = null;
  const askPage = (paper, workspace, page) => { pendingPage = {paper, workspace, page}; };
  const COLORS = ['yellow', 'green', 'blue', 'pink'];
  let notes = null, notesTimer = null, notesSaving = null;
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
  const highlightDrafting = () => Boolean(highlightEditing)
    && highlightDraft !== (S.notebook?.highlights || []).find(h => h.id === highlightEditing)?.note;
  const unsaved = () => dirty || connectionDraft || highlightDrafting();
  const refKey = r => JSON.stringify([r.paper, r.claim || r.id]);
  const refs = () => [...claims].map(s => { const [paper, claim] = JSON.parse(s); return {paper, claim}; });
  const resolve = ref => S.claims.find(c => c.paper === ref.paper && c.id === ref.claim);
  const selectedPapers = () => [...new Set([...papers, ...refs().map(r => r.paper)])];
  const button = (action, title, data = '') => `<button type="button" data-tool="${action}" ${data}>${title}</button>`;
  const input = (label, id, value = '') => `<label>${label}<input id="${id}" value="${html(value)}"></label>`;
  const area = (label, id, value = '') => `<label>${label}<textarea id="${id}">${html(value)}</textarea></label>`;
  function remember() {
    try { sessionStorage.setItem(`doxograph-selection:${workspace}`, JSON.stringify({claims:[...claims], papers:[...papers]})); } catch (_) { /* optional */ }
  }
  function sync() {
    if (workspace !== currentWorkspaceId) {
      workspace = currentWorkspaceId;
      claims = new Set(); papers = new Set();
      try {
        const data = JSON.parse(sessionStorage.getItem(`doxograph-selection:${workspace}`) || '{}');
        claims = new Set(data.claims || []); papers = new Set(data.papers || []);
      } catch (_) { /* no saved selection */ }
    }
    const available = new Set(S.claims.map(refKey));
    claims = new Set([...claims].filter(k => available.has(k)));
    papers = new Set([...papers].filter(k => S.papers.some(p => p.key === k)));
    remember();
    el('selection-count').textContent = `${claims.size} claims · ${papers.size} papers selected`;
    document.querySelectorAll('[data-select-claim]').forEach(c => c.checked = claims.has(c.dataset.selectClaim));
    document.querySelectorAll('[data-select-paper]').forEach(c => c.checked = papers.has(c.dataset.selectPaper));
    redrawHighlights();
  }
  function claimCheckbox(row) {
    const key = refKey(row);
    const connections = (S.notebook?.connections || []).filter(c => refKey(c.source) === key || refKey(c.target) === key).length;
    return `<div class="claim-selection"><label><input type="checkbox" data-select-claim="${html(key)}" ${claims.has(key) ? 'checked' : ''}> Select claim</label>
      ${connections ? button('connections', `${connections} manual connection${connections === 1 ? '' : 's'}`) : ''}</div>`;
  }
  function paperCheckbox(p) {
    return `<input type="checkbox" class="paper-selection" aria-label="Select paper: ${html(p.title || p.key)}" data-select-paper="${html(p.key)}" ${papers.has(p.key) ? 'checked' : ''}>`;
  }
  async function request(path = '', method = 'GET', body, ws = modalWorkspace || workspace) {
    return api('/api/notebook' + path, {method, headers:{'Content-Type':'application/json', 'X-Doxograph-Workspace':ws},
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
  async function close() {
    if (busy) return;
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
    const labels = {notes:'Workspace notes', compare:'Compare & organize', boards:'Evidence boards', connections:'Claim connections', reading:'Reading queue', searches:'Saved searches'};
    el('research-tools-title').textContent = tab === 'notes' ? 'Workspace notes' : tab === 'history' ? 'Edit history' : 'Your research tools';
    el('research-tabs').innerHTML = Object.entries(labels).map(([key, label]) => button('tab', label, `data-tab="${key}" ${tab === key ? 'aria-current="page"' : ''}`)).join('')
      + button('reload', 'Reload saved version');
    if (tab === 'compare') renderCompare();
    if (tab === 'notes') renderNotes();
    if (tab === 'boards') renderBoards();
    if (tab === 'connections') renderConnections();
    if (tab === 'reading') renderReading();
    if (tab === 'searches') renderSearches();
    if (tab === 'history') renderHistory();
  }
  function renderNotes() {
    notes = {text:notebook.notes, saved:notebook.notes, error:''};
    const name = workspaces.find(w => w.id === modalWorkspace)?.name || 'this workspace';
    el('research-body').innerHTML = `<div class="workspace-notes">
      <p class="hint">Your notes for ${html(name)}. Changes save automatically.</p>
      <label for="workspace-notes">What are you investigating?</label>
      <textarea id="workspace-notes" placeholder="Capture questions, thoughts, and next steps. Use headings to organize your notes."></textarea>
      <div class="tool-actions"><span id="workspace-notes-status" role="status" aria-live="polite"></span>
        ${button('retry-notes', 'Retry save', 'id="workspace-notes-retry" hidden')}</div></div>`;
    el('workspace-notes').value = notes.text;
    notesStatus();
    el('workspace-notes').focus();
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
        const p = S.papers.find(p => p.key === key);
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
        const p = S.papers.find(p => p.key === r.paper);
        return `<article class="reading-entry" data-reading="${html(r.paper)}"><h3>${html(p?.title || 'Removed paper: '+r.paper)}</h3>
          <div class="tool-actions"><label>Reading status<select data-reading-status="${html(r.paper)}">${['queued','reading','finished'].map(s => `<option ${r.status === s ? 'selected' : ''}>${s}</option>`).join('')}</select></label>
          ${p?.has_pdf ? button('reader',`Resume at page ${r.page}`,`data-paper="${html(r.paper)}"`) : '<span class="hint">No PDF available</span>'}
          ${button('reading-up','↑',`aria-label="Move paper up" ${i === 0 ? 'disabled' : ''}`)}${button('reading-down','↓',`aria-label="Move paper down" ${i === notebook.reading.length-1 ? 'disabled' : ''}`)}${button('reading-remove','Remove from queue')}</div>
          <div class="tool-actions">${r.bookmarks.map(page => button('reader',`Bookmark: page ${page}`,`data-paper="${html(r.paper)}" data-page="${page}"`)).join('')}</div></article>`;
      }).join('')}${notebook.reading.length ? '' : '<p class="empty">Add a paper from its header, or queue a selection of papers.</p>'}`;
  }
  function renderSearches() {
    el('research-body').innerHTML = `<p class="hint">Saved searches remember filters, not a fixed list of results. New matching claims appear automatically.</p>
      ${button('save-search','Save current library filters')}
      ${notebook.searches.map(s => `<article class="reading-entry" data-search="${html(s.id)}"><h3>${html(s.name)}</h3>
        <p class="hint">${html([s.q && `Search: ${s.q}`, s.tag && `Topic: ${s.tag}`, s.label && `Label: ${s.label}`, s.kind, s.unreviewed && 'Unreviewed only', s.unverified && 'Quote not found', s.paper && `Paper: ${S.papers.find(p => p.key === s.paper)?.title || s.paper}`].filter(Boolean).join(' · ') || 'All claims')}</p>
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
    const response = await fetch('/api/notebook/export', {method:'POST', headers:{'Content-Type':'application/json','X-Doxograph-Workspace':modalWorkspace},
      body:JSON.stringify({format,board,claims:refs(),papers:[...papers]})});
    if (!response.ok) { const body = await response.json(); throw new Error(body.detail || 'Export failed'); }
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a'); link.href = url;
    link.download = /filename="([^"]+)"/.exec(response.headers.get('Content-Disposition') || '')?.[1] || `selected-research.${format === 'csv' ? 'csv' : 'md'}`;
    link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
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
    const ws = currentWorkspaceId;
    if (reader && (!paper || reader.key !== readerKey(paper, ws))) await unmountReader();
    el('reader').hidden = !paper;
    if (!paper || reader) return;
    const generation = ++readerEpoch;
    const zoom = savedZoom();
    reader = {key:readerKey(paper, ws), workspace:ws, paper, generation, zoom, scale:1, pages:[], current:1, selection:null, document:null, pdfjs:null, observer:null, resizer:null, progressTimer:null, savedPage:null, wantedPage:null, progressSave:Promise.resolve()};
    el('reader').innerHTML = `<div class="reader-toolbar">${button('previous-page', 'Previous page')}
      <label>Page<input id="reader-page" type="number" min="1" value="1" aria-label="Page number"></label><span id="reader-total"></span>${button('go-page', 'Go')}${button('next-page', 'Next page')}
      <label>Zoom<select id="reader-zoom">${ZOOMS.map(z => `<option value="${z}" ${z === zoom ? 'selected' : ''}>${z === 'fit' ? 'Fit width' : `${Math.round(Number(z) * 100)}%`}</option>`).join('')}</select></label>
      ${button('bookmark', 'Bookmark this page')}<span class="reader-status" id="reader-status"></span></div>
      <div class="pdf-scroll" id="pdf-scroll"><div class="passage-tools" id="passage-tools" hidden>
        ${COLORS.map(c => button('highlight-selection', '', `class="swatch highlight-${c}" data-color="${c}" aria-label="Highlight in ${c}" title="Highlight in ${c}"`)).join('')}
        ${button('capture-selection', 'Capture as claim')}</div></div>
      <p id="reader-hint" class="hint">Loading PDF…</p>`;
    // Buttons on the toolbar must not take the selection away before they act on it.
    el('passage-tools').addEventListener('mousedown', event => event.preventDefault());
    try {
      const pdfjs = await import('/vendor/pdf.min.mjs');
      pdfjs.GlobalWorkerOptions.workerSrc = '/vendor/pdf.worker.min.mjs';
      const doc = await pdfjs.getDocument({url:`/pdf/${encodeURIComponent(paper)}?workspace=${encodeURIComponent(ws)}&inline=1`, isEvalSupported:false, cMapUrl:'/vendor/cmaps/', cMapPacked:true, standardFontDataUrl:'/vendor/standard_fonts/', wasmUrl:'/vendor/wasm/'}).promise;
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
          else if (page.renderedAt) clearPage(page);
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
        reader.scrollTick = requestAnimationFrame(() => { reader.scrollTick = null; trackCurrentPage(); });
      }, {passive:true});
      const progress = (S.notebook?.reading || []).find(r => r.paper === paper);
      reader.savedPage = progress?.page ?? null;
      const asked = pendingPage && pendingPage.paper === paper && pendingPage.workspace === ws ? pendingPage.page : null;
      pendingPage = null;
      goToPage(asked || progress?.page || 1);
      trackCurrentPage();
    } catch (error) {
      if (reader?.generation === generation) el('reader-hint').textContent = `Could not open the PDF: ${error.message}`;
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
    if (gone.document) await gone.document.loadingTask.destroy();
  }
  function sizePage(page) {
    page.el.style.width = `${Math.floor(page.base.w * reader.scale)}px`;
    page.el.style.height = `${Math.floor(page.base.h * reader.scale)}px`;
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
  function renderPage(page) {
    if (!reader || page.rendering || page.renderedAt === reader.scale) return page.rendering || Promise.resolve();
    const generation = reader.generation, scale = reader.scale;
    page.rendering = (async () => {
      const pdfPage = page.pdfPage || (page.pdfPage = await reader.document.getPage(page.number));
      if (reader?.generation !== generation || reader.scale !== scale) return;
      const viewport = pdfPage.getViewport({scale});
      page.base = {w:viewport.width / scale, h:viewport.height / scale};
      const container = page.el;
      container.innerHTML = '<canvas></canvas><div class="highlightLayer"></div><div class="textLayer"></div>';
      container.style.width = `${viewport.width}px`;
      container.style.height = `${viewport.height}px`;
      container.style.setProperty('--total-scale-factor', viewport.scale * (pdfPage.userUnit || 1));
      container.style.setProperty('--scale-round-x', '1px');
      container.style.setProperty('--scale-round-y', '1px');
      const canvas = container.querySelector('canvas');
      const ratio = Math.min(devicePixelRatio || 1, 2);
      canvas.width = Math.floor(viewport.width * ratio); canvas.height = Math.floor(viewport.height * ratio);
      canvas.style.width = `${viewport.width}px`; canvas.style.height = `${viewport.height}px`;
      await pdfPage.render({canvasContext:canvas.getContext('2d'), viewport, transform:ratio === 1 ? null : [ratio, 0, 0, ratio, 0, 0]}).promise;
      if (reader?.generation !== generation || reader.scale !== scale) return;
      const content = page.text || (page.text = await pdfPage.getTextContent());
      if (reader?.generation !== generation || reader.scale !== scale) return;
      await new reader.pdfjs.TextLayer({textContentSource:content, container:container.querySelector('.textLayer'), viewport}).render();
      page.hasText = content.items.some(item => item.str?.trim());
      page.renderedAt = scale;
      container.classList.remove('pending');
      drawPageHighlights(page);
      if (page.number === reader.current) readerHint();
    })().catch(error => { if (reader?.generation === generation) el('reader-hint').textContent = `Could not draw page ${page.number}: ${error.message}`; })
      .finally(() => {
        page.rendering = null;
        if (reader?.generation !== generation) return;
        // The zoom changed under this render: draw it again at the new scale.
        if (page.renderedAt !== reader.scale && pageNear(page)) renderPage(page);
        // Scrolled out of reach while drawing: the observer's leave came too
        // early to release it, so it is released here.
        else if (page.renderedAt && !pageNear(page)) clearPage(page);
      });
    return page.rendering;
  }
  function paperHighlights(paper) {
    return (S.notebook?.highlights || []).filter(h => h.paper === paper);
  }
  function drawPageHighlights(page) {
    const layer = page.el.querySelector('.highlightLayer');
    if (!layer) return;
    const percent = value => `${(value * 100).toFixed(3)}%`;
    layer.innerHTML = paperHighlights(reader.paper).filter(h => h.page === page.number).flatMap(h => h.rects.map(r =>
      `<div class="highlight-${html(h.color)}" data-highlight="${html(h.id)}" style="left:${percent(r.x)};top:${percent(r.y)};width:${percent(r.w)};height:${percent(r.h)}"></div>`)).join('');
  }
  function redrawHighlights() {
    if (!reader) return;
    reader.pages.forEach(page => { if (page.renderedAt) drawPageHighlights(page); });
  }
  function readerHint() {
    const hint = el('reader-hint');
    if (!hint || !reader) return;
    const page = reader.pages[reader.current - 1];
    hint.textContent = page?.renderedAt && !page.hasText
      ? 'This page has no selectable text. You can transcribe a quote and add a claim by hand.'
      : 'Select a passage to highlight it or capture it as a claim.';
  }
  function trackCurrentPage() {
    if (!reader?.pages.length) return;
    const scroller = el('pdf-scroll');
    const line = scroller.scrollTop + scroller.clientHeight * .35;
    const page = reader.pages.find(p => p.el.offsetTop + p.el.offsetHeight > line) || reader.pages[reader.pages.length - 1];
    if (page.number === reader.current && reader.tracked) return;
    reader.current = page.number; reader.tracked = true;
    const field = el('reader-page');
    if (field && document.activeElement !== field) field.value = page.number;
    const progress = (S.notebook?.reading || []).find(r => r.paper === reader.paper);
    const marked = Boolean(progress?.bookmarks.includes(page.number));
    const bookmark = el('reader').querySelector('[data-tool="bookmark"]');
    if (bookmark) bookmark.textContent = marked ? 'Remove bookmark' : 'Bookmark this page';
    readerHint();
    // Where the reading stopped, for the queue: only for a paper that is on it.
    // Compared against the page this reader last saved, not the notebook in
    // `S`: that is refreshed by the poll, and between polls it would still say
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
  function queueProgressSave(active) {
    const {paper, workspace:ws} = active;
    active.progressSave = active.progressSave.then(async () => {
      const number = active.wantedPage;
      if (number === null || number === active.savedPage) return;
      await mutateNotebook(ws, n => { const r = n.reading.find(r => r.paper === paper); if (!r || r.page === number) return false; r.page = number; }, {refresh:false});
      active.savedPage = number;
      // What the queue and the Continue card show until the next poll.
      if (ws === currentWorkspaceId) {
        const local = (S.notebook?.reading || []).find(r => r.paper === paper);
        if (local) local.page = number;
        renderContinuation();
      }
      if (active.wantedPage !== active.savedPage) queueProgressSave(active);
    }).catch(() => { /* a later scroll tries again */ });
  }
  async function goToPage(number, highlight = null) {
    if (!reader?.pages.length) { if (reader) askPage(reader.paper, reader.workspace, number); return; }
    const page = reader.pages[Math.max(1, Math.min(number, reader.pages.length)) - 1];
    el('pdf-scroll').scrollTo({top:Math.max(0, page.el.offsetTop - 8)});
    trackCurrentPage();
    if (!highlight) return;
    await renderPage(page);
    page.el.querySelectorAll(`.highlightLayer [data-highlight="${CSS.escape(highlight)}"]`).forEach(mark => {
      mark.classList.add('flash'); setTimeout(() => mark.classList.remove('flash'), 1600);
    });
  }
  async function setZoom(zoom) {
    if (!reader?.pages.length || !ZOOMS.includes(zoom)) return;
    reader.zoom = zoom;
    try { localStorage.setItem(zoomKey, zoom); } catch (_) { /* preference remains for this page */ }
    const scale = scaleFor(zoom, reader.pages[0].base);
    if (scale === reader.scale) return;
    const scroller = el('pdf-scroll');
    const ratio = scale / reader.scale;
    reader.scale = scale;
    reader.pages.forEach(page => { clearPage(page); sizePage(page); });
    scroller.scrollTop *= ratio;
    reader.tracked = false;
    await Promise.all(reader.pages.filter(pageNear).map(renderPage));
    trackCurrentPage();
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
    if (reader) reader.selection = null;
    const tools = el('passage-tools');
    if (tools) tools.hidden = true;
  }
  function trackSelection() {
    if (!reader) return;
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
    const empty = '<p class="hint">Select a passage in the paper to highlight it, with a note of your own, or to capture it as a claim.</p>';
    return `<section class="highlights"><h3>Highlights${mine.length ? ` <span class="n hint">${mine.length}</span>` : ''}</h3>${mine.length ? mine.map(h => {
      const editing = highlightEditing === h.id;
      return `<article class="highlight-entry" data-highlight="${html(h.id)}">
        <blockquote class="highlight-${html(h.color)}" data-tool="highlight-page" data-page="${h.page}" title="Show this passage in the paper">${html(h.text)}</blockquote>
        ${editing ? `<textarea data-highlight-note="${html(h.id)}" placeholder="Your note on this passage" aria-label="Note on this highlight">${html(highlightDraft)}</textarea>`
          : h.note.trim() ? noteHtml(h.note, 'my note') : ''}
        <div class="hmeta"><span>p. ${h.page}</span><span class="cact">
          ${editing ? button('save-highlight', 'Save note') + button('cancel-highlight', 'Cancel') : button('edit-highlight', h.note.trim() ? 'Edit note' : 'Add note')}
          ${button('capture-highlight', 'Capture as claim')}${button('remove-highlight', 'Remove')}</span></div>
      </article>`;
    }).join('') : empty}</section>`;
  }
  // Whether a highlight note is being written in the rail, which holds off
  // background redraws of it the way a claim editor does.
  function holds() {
    return Boolean(highlightEditing) && paperHighlights(V.paper).some(h => h.id === highlightEditing);
  }
  function goToPaper(paper, claim = null) {
    captureOpenEditor(); parkNewClaimForNavigation(paper);
    V.paper = paper; V.view = 'claims'; V.tag = null; V.label = null;
    V.q = ''; V.kind = ''; V.unreviewed = false; V.unverified = false;
    V.selectedId = claim;
    el('q').value = ''; el('kind').value = ''; el('only-unreviewed').checked = false; el('only-unverified').checked = false;
    scheduleTextSearch(); renderAll(); syncHash(true); scrollToSelected();
  }
  const READER_TOOLS = new Set(['previous-page', 'next-page', 'go-page', 'bookmark', 'reader', 'highlight-selection', 'capture-selection',
    'highlight-page', 'edit-highlight', 'save-highlight', 'cancel-highlight', 'capture-highlight', 'remove-highlight']);
  async function readerAction(name, target) {
    const data = target.dataset;
    if (['previous-page', 'next-page', 'go-page'].includes(name)) {
      if (!reader?.pages.length) return;
      const page = name === 'go-page' ? Number(el('reader-page').value) : reader.current + (name === 'next-page' ? 1 : -1);
      if (!Number.isInteger(page) || page < 1 || page > reader.pages.length) throw new Error('Choose a page within this PDF.');
      await goToPage(page); return;
    }
    if (name === 'highlight-page') {
      const id = target.closest('[data-highlight]')?.dataset.highlight;
      if (reader) { await goToPage(Number(data.page), id); return; }
      // The PDF was put away, or this paper has none any more.
      if (!S.papers.find(p => p.key === V.paper)?.has_pdf) throw new Error('This paper has no PDF to show the passage in.');
      askPage(V.paper, currentWorkspaceId, Number(data.page));
      if (V.pdfHidden) { V.pdfHidden = false; storePdfHidden(); renderContent(); }
      return;
    }
    if (name === 'bookmark') {
      if (!reader?.pages.length) return;
      const {paper, current, workspace:ws} = reader;
      await mutateNotebook(ws, n => {
        let progress = n.reading.find(r => r.paper === paper);
        if (!progress) { progress = {paper, status:'reading', page:current, bookmarks:[]}; n.reading.push(progress); }
        progress.page = current;
        progress.bookmarks = progress.bookmarks.includes(current) ? progress.bookmarks.filter(p => p !== current) : [...progress.bookmarks, current];
      });
      reader.savedPage = current; reader.wantedPage = current;
      reader.tracked = false; trackCurrentPage(); return;
    }
    // Resuming a paper from the queue or the Continue card: it opens in the
    // main pane at that page. Resuming is reading, so a paper not on the queue
    // joins it and a queued one is marked as being read; a finished one is
    // left as it is.
    if (name === 'reader') {
      if (el('research-tools').open) {
        if (!await discardDrafts()) return;
        setBusy(false); await close();
        if (el('research-tools').open) return;
      }
      const paper = data.paper, ws = currentWorkspaceId;
      const saved = await mutateNotebook(ws, n => {
        const entry = n.reading.find(r => r.paper === paper);
        if (!entry) { n.reading.push({paper, status:'reading', page:1, bookmarks:[]}); return; }
        if (entry.status !== 'queued') return false;
        entry.status = 'reading';
      });
      const page = Number(data.page) || saved.reading.find(r => r.paper === paper)?.page || 1;
      if (reader?.paper === paper && reader.workspace === ws && !V.pdfHidden) { await goToPage(page); return; }
      askPage(paper, ws, page);
      if (V.pdfHidden) { V.pdfHidden = false; storePdfHidden(); }
      if (V.paper === paper && V.view === 'claims') renderContent(); else goToPaper(paper);
      return;
    }
    if (name === 'highlight-selection') {
      const passage = reader?.selection;
      if (!passage) throw new Error('Select a passage in the paper first.');
      const {paper, workspace:ws} = reader, color = COLORS.includes(data.color) ? data.color : 'yellow';
      window.getSelection()?.removeAllRanges(); hidePassageTools();
      await mutateNotebook(ws, n => { n.highlights.push({id:uid(), paper, page:passage.page, rects:passage.rects, text:passage.text, color, note:''}); });
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
      if (highlightEditing !== id && highlightDrafting()
        && !await confirmDialog('Discard the note you are writing on another highlight?', {ok:'Discard note'})) return;
      highlightEditing = id; highlightDraft = highlight.note; renderContent(); document.querySelector('[data-highlight-note]')?.focus(); return;
    }
    if (name === 'cancel-highlight') { highlightEditing = null; highlightDraft = ''; renderContent(); return; }
    if (name === 'capture-highlight') {
      // The note as it is being written, when its editor is open: what is on
      // screen, not the saved version the person is in the middle of changing.
      const note = highlightEditing === id ? highlightDraft : highlight.note;
      startClaimFromPassage(highlight.paper, {quote:highlight.text, locator:`p. ${highlight.page}`, note}); return;
    }
    if (name === 'save-highlight') {
      // The rail is outside the modal, so `setBusy` leaves its editor open
      // while the save runs: what is typed meanwhile goes into the draft, and
      // the editor closes only if the draft is still what was saved.
      const note = highlightDraft;
      target.disabled = true;
      try {
        await mutateNotebook(currentWorkspaceId, n => { const h = n.highlights.find(h => h.id === id); if (!h) throw new Error('This highlight was removed in another window.'); h.note = note; });
      } finally { target.disabled = false; }
      if (highlightEditing !== id) return;
      if (highlightDraft !== note) { toast('Saved the note as it was when you clicked Save. What you typed since is still in the editor.'); return; }
      highlightEditing = null; highlightDraft = '';
      renderContent(); return;
    }
    if (name === 'remove-highlight') {
      if (highlight.note && !await confirmDialog('Remove this highlight and its note?', {ok:'Remove highlight'})) return;
      await mutateNotebook(currentWorkspaceId, n => { n.highlights = n.highlights.filter(h => h.id !== id); });
      if (highlightEditing === id) { highlightEditing = null; highlightDraft = ''; }
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
    if (name === 'close') { setBusy(false); await close(); return; }
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
      setBusy(false); await close();
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
        setBusy(false); await close(); captureOpenEditor();
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
    if (name === 'clear-selection') { claims.clear(); papers.clear(); remember(); sync(); return; }
    if (name === 'select-visible') { visibleClaims().forEach(c=>claims.add(refKey(c))); remember(); sync(); return; }
    if (READER_TOOLS.has(name)) { run(() => readerAction(name, target)); return; }
    run(async () => {
      if (!el('research-tools').open) {
        await open(['notes','boards','reading','searches','connections','compare'].includes(name) ? name : 'compare');
        if (['notes','boards','reading','searches','connections','compare'].includes(name)) return;
      }
      await action(name,target);
    });
  }, true);
  document.addEventListener('change', event => {
    const target = event.target;
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
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Enter' && event.target.id === 'reader-page') { event.preventDefault(); run(() => readerAction('go-page', event.target)); }
  });
  document.addEventListener('input', event => {
    const target = event.target;
    if (target.dataset.highlightNote) { highlightDraft = target.value; return; }
    if (!el('research-tools')?.contains(target)) return;
    if (target.id === 'workspace-notes') {
      notes.text = target.value;
      notesStatus();
      clearTimeout(notesTimer);
      notesTimer = setTimeout(saveNotes, 600);
      return;
    }
    if (target.id.startsWith('connection-')) connectionDraft = true;
    if (target.id === 'board-title') { currentBoard().title = target.value; dirty = true; }
    if (target.dataset.entryText) { currentBoard().entries.find(e=>e.id === target.dataset.entryText).text = target.value; dirty = true; }
    if (target.dataset.connectionNote) { notebook.connections.find(c=>c.id === target.dataset.connectionNote).note = target.value; dirty = true; }
  });
  document.addEventListener('DOMContentLoaded', () => {
    el('research-tools').addEventListener('cancel', event => { event.preventDefault(); close(); });
  });
  window.addEventListener('beforeunload', event => { if (unsaved() || notesChanged() || notesSaving) { event.preventDefault(); event.returnValue = ''; } });
  async function beforeWorkspaceSwitch() {
    if (el('research-tools').open) {
      if (busy) { message('Finish the current operation before switching workspaces.', true); return false; }
      await close();
      if (el('research-tools').open) return false;
    }
    // A highlight note being written in the rail is state app.js cannot see;
    // the switch would hide its editor and the next edit would overwrite it.
    if (highlightDrafting()) {
      if (!await confirmDialog('Discard the highlight note you are writing before switching workspaces?', {ok:'Discard note'})) return false;
      highlightEditing = null; highlightDraft = '';
    }
    return true;
  }
  return {sync,claimCheckbox,paperCheckbox,beforeWorkspaceSwitch,mountReader,highlightsHtml,holds,
    openBoard: id => run(() => open('boards', id))};
})();
