/* Reader-owned workflows. Stored with the active workspace; never sent to a model. */
const ResearchTools = (() => {
  let workspace = null, claims = new Set(), papers = new Set();
  let notebook = null, tab = 'compare', boardId = null, dirty = false, busy = false, connectionDraft = false;
  let modalWorkspace = null, reader = null, historyData = null, historyPaper = null, historyClaim = null;
  let epoch = 0, progressSave = null;
  const el = (id) => document.getElementById(id);
  const html = (value) => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
  const uid = () => {
    if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
    // LAN deployments over HTTP lack randomUUID, but support getRandomValues.
    return Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('');
  };
  const promptDialog = (title, value, {ok}) => askDialog({title, input:{value}, ok});
  // Keep note drafts by highlight so scrolling the sidebar does not discard them.
  const noteDrafts = () => reader && tab === 'reader' ? [...reader.noteValues].map(([id, value]) => ({dataset:{highlightNote:id}, value}))
    .filter(field => field.value !== (notebook.highlights.find(h => h.id === field.dataset.highlightNote)?.note ?? field.value)) : [];
  const unsaved = () => dirty || reader?.draft || connectionDraft || noteDrafts().length > 0;
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
  async function request(path = '', method = 'GET', body) {
    return api('/api/notebook' + path, {method, headers:{'Content-Type':'application/json', 'X-Doxograph-Workspace':modalWorkspace || workspace},
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
    // Keep scrolling during a save from restoring an older page from the response.
    if (reader?.views && tab === 'reader') rememberReaderPage();
    dirty = false;
    await refreshAll();
    if (reader?.document && tab === 'reader') { rememberContinuation(reader.paper); renderContinuation(); }
    message('Saved.');
  }
  async function close() {
    if (busy) return;
    await flushReaderProgress();
    if (unsaved() && !await confirmDialog('Discard unsaved changes in research tools?', {ok:'Discard changes'})) return;
    stopReaderViews();
    epoch++;
    if (reader?.document) await reader.document.loadingTask.destroy();
    reader = null; dirty = false; connectionDraft = false;
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
    const labels = {compare:'Compare & organize', boards:'Evidence boards', connections:'Claim connections', reading:'Reading queue', searches:'Saved searches'};
    el('research-tools-title').textContent = tab === 'reader' ? 'Read & capture' : tab === 'history' ? 'Edit history' : 'Your research tools';
    el('research-tabs').innerHTML = Object.entries(labels).map(([key, label]) => button('tab', label, `data-tab="${key}" ${tab === key ? 'aria-current="page"' : ''}`)).join('')
      + button('reload', 'Reload saved version');
    if (tab === 'compare') renderCompare();
    if (tab === 'boards') renderBoards();
    if (tab === 'connections') renderConnections();
    if (tab === 'reading') renderReading();
    if (tab === 'searches') renderSearches();
    if (tab === 'history') renderHistory();
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
          ${p.has_pdf ? button('reader', 'Read & capture', `data-paper="${html(key)}"`) : ''}
          ${button('source', 'Open paper', `data-paper="${html(key)}"`)}${button('unpin-paper', 'Unpin paper', `data-paper="${html(key)}"`)}</article>`;
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
  async function showReader(paper, requestedPage) {
    stopReaderViews();
    if (reader?.document) await reader.document.loadingTask.destroy();
    if (!notebook.reading.some(r => r.paper === paper)) await queue([paper]);
    const progress = notebook.reading.find(r => r.paper === paper);
    if (progress.status === 'queued') { progress.status = 'reading'; await save(); }
    tab = 'reader';
    reader = {paper, page:Number(requestedPage || progress.page), document:null, draft:false, noteValues:new Map()};
    render();
    el('research-body').innerHTML = `<h3>${html(S.papers.find(p => p.key === paper)?.title || paper)}</h3>
      <div class="reader-controls tool-actions">${button('previous-page','Previous page')}
      <label>Page<input id="reader-page" type="number" min="1" value="${reader.page}"></label><span id="reader-total"></span>
      ${button('go-page','Go')}${button('next-page','Next page')}${button('bookmark','Bookmark this page')}
      <label>Zoom<select id="reader-zoom"><option value="1">100%</option><option value="1.3" selected>130%</option><option value="1.6">160%</option><option value="2">200%</option></select></label></div>
      <p id="reader-hint" class="hint">Loading PDF…</p>
      <div class="reader-layout"><div class="pdf-scroll" tabindex="0" role="region" aria-label="PDF pages"><div id="pdf-pages"></div></div>
        <div class="reader-side"><section id="highlight-panel"><h3>Highlights</h3>
          <div class="tool-actions"><label>Color<select id="highlight-color">${['yellow','green','blue','pink'].map(c => `<option value="${c}">${c}</option>`).join('')}</select></label>
          ${button('highlight-selection','Highlight selected passage')}</div><div id="page-highlights"></div></section>
        <form id="capture-form"><h3>Capture a claim</h3><p class="hint">Select text directly on the page, then capture it. Add your interpretation before saving.</p>
          ${button('capture-selection','Capture selected passage')}${area('Verbatim quote','capture-quote')}
          ${input('Page or section','capture-locator')}${area('Claim in your own words','capture-text')}${area('Your note (optional)','capture-note')}
          <button type="submit" class="primary">Save claim</button><p id="capture-status" role="status"></p>
        </form></div></div>`;
    const generation = ++epoch;
    const pdfjs = await import('/vendor/pdf.min.mjs');
    pdfjs.GlobalWorkerOptions.workerSrc = '/vendor/pdf.worker.min.mjs';
    const doc = await pdfjs.getDocument({url:`/pdf/${encodeURIComponent(paper)}?workspace=${encodeURIComponent(modalWorkspace)}&inline=1`, isEvalSupported:false, cMapUrl:'/vendor/cmaps/', cMapPacked:true, standardFontDataUrl:'/vendor/standard_fonts/', wasmUrl:'/vendor/wasm/'}).promise;
    if (generation !== epoch || !el('research-tools').open) { await doc.loadingTask.destroy(); return; }
    reader.document = doc;
    reader.pdfjs = pdfjs;
    reader.page = Math.max(1, Math.min(reader.page, doc.numPages));
    el('reader-total').textContent = `of ${doc.numPages}`;
    el('reader-page').max = doc.numPages;
    // Reserve every page's real dimensions so lazy rendering never shifts the document.
    const active = reader;
    active.pages = await Promise.all(Array.from({length:doc.numPages}, (_, i) => doc.getPage(i + 1)));
    if (reader !== active) return;
    await layoutReader();
    rememberContinuation(reader.paper);
    renderContinuation();
  }
  function stopReaderViews() {
    if (!reader) return;
    reader.observer?.disconnect();
    clearTimeout(reader.progressTimer);
    cancelAnimationFrame(reader.scrollFrame);
    const scroll = el('pdf-pages')?.parentElement;
    scroll?.removeEventListener('scroll', reader.onScroll);
    for (const type of ['wheel','touchmove','keydown']) scroll?.removeEventListener(type, reader.onScrollIntent);
    for (const view of reader.views || []) {
      view.renderTask?.cancel();
      view.textLayer?.cancel();
    }
  }
  async function flushReaderProgress() {
    while (progressSave) await progressSave;
    if (!reader?.progressPending) return;
    const active = reader;
    clearTimeout(active.progressTimer);
    active.progressPending = false;
    progressSave = save();
    try { await progressSave; }
    finally { progressSave = null; }
  }
  function scheduleReaderProgress() {
    const active = reader;
    clearTimeout(active.progressTimer);
    active.progressTimer = setTimeout(() => {
      if (reader !== active || !active.progressPending) return;
      if (busy) { scheduleReaderProgress(); return; }
      // Background progress saves leave text selection and claim editing interactive.
      flushReaderProgress().catch(error => message(error.message, true));
    }, 250);
  }
  function updateReaderControls() {
    const progress = notebook.reading.find(r => r.paper === reader.paper);
    el('reader-page').value = reader.page;
    if (!reader.draft) el('capture-locator').value = `p. ${reader.page}`;
    el('research-body').querySelector('[data-tool="bookmark"]').textContent = progress.bookmarks.includes(reader.page) ? 'Remove bookmark' : 'Bookmark this page';
    el('research-body').querySelector('[data-tool="previous-page"]').disabled = reader.page === 1;
    el('research-body').querySelector('[data-tool="next-page"]').disabled = reader.page === reader.document.numPages;
    const view = reader.views?.[reader.page - 1];
    el('reader-hint').textContent = view?.hasText === false
      ? 'This page has no selectable text. You can transcribe a quote and write a claim by hand.'
      : 'Scroll to read more pages. Select a passage to capture it.';
    if (reader.highlightsPage !== reader.page) renderHighlights();
    rememberReaderPage();
  }
  function rememberReaderPage() {
    const progress = notebook.reading.find(r => r.paper === reader.paper);
    if (progress.page !== reader.page) {
      progress.page = reader.page;
      reader.progressPending = true;
      scheduleReaderProgress();
    }
  }
  async function renderReaderPage(active, view) {
    if (view.rendering || view.rendered) return;
    view.rendering = true;
    const {container, viewport, page} = view;
    const canvas = document.createElement('canvas');
    const layer = document.createElement('div'); layer.className = 'textLayer';
    const highlights = document.createElement('div'); highlights.className = 'highlightLayer';
    container.replaceChildren(canvas, highlights, layer);
    renderHighlightLayer(view);
    const ratio = Math.min(devicePixelRatio || 1, 2);
    canvas.width = Math.floor(viewport.width * ratio); canvas.height = Math.floor(viewport.height * ratio);
    canvas.style.width = `${viewport.width}px`; canvas.style.height = `${viewport.height}px`;
    try {
      view.renderTask = page.render({canvasContext:canvas.getContext('2d'), viewport, transform:ratio === 1 ? null : [ratio,0,0,ratio,0,0]});
      await view.renderTask.promise;
      const content = await page.getTextContent();
      if (reader !== active || !container.isConnected) return;
      view.textLayer = new active.pdfjs.TextLayer({textContentSource:content, container:layer, viewport});
      await view.textLayer.render();
      view.hasText = content.items.some(item => item.str?.trim());
      view.rendered = true;
      if (active.page === page.pageNumber) updateReaderControls();
    } catch (error) {
      if (reader === active && container.isConnected && error.name !== 'RenderingCancelledException' && error.name !== 'AbortException') {
        message(`Could not display page ${page.pageNumber}: ${error.message}`, true);
      }
    } finally {
      view.rendering = false;
      // Keep canvas memory bounded as the reader moves through a long PDF.
      if (!view.nearby && !readerPageSelected(view)) releaseReaderPage(view);
    }
  }
  function readerPageSelected(view) {
    const selection = window.getSelection();
    return selection?.rangeCount > 0 && selection.containsNode(view.container, true);
  }
  function releaseReaderPage(view) {
    if (view.rendering || readerPageSelected(view) || !view.container.hasChildNodes()) return;
    for (const canvas of view.container.querySelectorAll('canvas')) { canvas.width = 0; canvas.height = 0; }
    view.container.replaceChildren();
    view.rendered = false;
    // PDF.js retains operator lists and decoded images after the canvas is removed.
    // cleanup() also waits internally if a newer render is using the same proxy.
    view.page.cleanup();
  }
  async function layoutReader() {
    const active = reader;
    stopReaderViews();
    const scale = Number(el('reader-zoom').value);
    const pages = el('pdf-pages'), scroll = pages.parentElement;
    active.views = active.pages.map(page => {
      const viewport = page.getViewport({scale});
      const container = document.createElement('div');
      container.className = 'pdf-page'; container.dataset.page = page.pageNumber;
      container.setAttribute('role', 'group'); container.setAttribute('aria-label', `Page ${page.pageNumber}`);
      container.style.width = `${viewport.width}px`; container.style.height = `${viewport.height}px`;
      container.style.setProperty('--total-scale-factor', viewport.scale * (page.userUnit || 1));
      container.style.setProperty('--scale-round-x', '1px'); container.style.setProperty('--scale-round-y', '1px');
      return {page, viewport, container};
    });
    pages.replaceChildren(...active.views.map(view => view.container));
    const views = active.views;
    active.observer = new IntersectionObserver(entries => {
      if (reader !== active || active.views !== views) return;
      for (const entry of entries) {
        const view = active.views[Number(entry.target.dataset.page) - 1];
        view.nearby = entry.isIntersecting;
        if (view.nearby) renderReaderPage(active, view);
        else if (!readerPageSelected(view)) releaseReaderPage(view);
      }
    }, {root:scroll, rootMargin:'100% 0px'});
    active.views.forEach(view => active.observer.observe(view.container));
    active.onScroll = () => {
      cancelAnimationFrame(active.scrollFrame);
      active.scrollFrame = requestAnimationFrame(() => {
        if (reader !== active || active.views !== views) return;
        const bounds = scroll.getBoundingClientRect();
        const readingLine = bounds.top + Math.min(scroll.clientHeight * .35, 200);
        const navigation = active.navigation;
        const keepTarget = navigation && Math.abs(scroll.scrollTop - navigation.top) < 2;
        if (!keepTarget) active.navigation = null;
        const maxOffset = scroll.scrollHeight - scroll.clientHeight;
        if (maxOffset <= 2) return; // No scrolling: retain the explicitly selected page.
        const atEnd = scroll.scrollTop >= maxOffset - 2;
        const view = keepTarget ? active.views[navigation.page - 1] : atEnd ? active.views.at(-1)
          : active.views.find(view => view.container.getBoundingClientRect().bottom > readingLine) || active.views.at(-1);
        if (active.page !== view.page.pageNumber) {
          active.page = view.page.pageNumber;
          updateReaderControls();
        }
      });
    };
    active.onScrollIntent = event => {
      if (event.type === 'wheel' && !event.deltaY) return;
      if (event.type === 'keydown' && !['ArrowUp','ArrowDown','PageUp','PageDown','Home','End',' '].includes(event.key)) return;
      active.navigation = null;
      active.onScroll();
    };
    scroll.addEventListener('scroll', active.onScroll, {passive:true});
    for (const type of ['wheel','touchmove','keydown']) scroll.addEventListener(type, active.onScrollIntent, {passive:true});
    await goToReaderPage(active.page);
  }
  async function goToReaderPage(number) {
    const active = reader, view = active.views[number - 1];
    active.page = number;
    const scroll = el('pdf-pages').parentElement;
    scroll.scrollTop += view.container.getBoundingClientRect().top - scroll.getBoundingClientRect().top - scroll.clientTop - 8;
    // Preserve an explicit visible target even when its scroll position is clamped.
    active.navigation = {page:number, top:scroll.scrollTop};
    updateReaderControls();
    view.nearby = true;
    await renderReaderPage(active, view);
  }
  function renderHighlightLayer(view) {
    const layer = view.container.querySelector('.highlightLayer');
    if (!layer) return;
    const here = notebook.highlights.filter(h => h.paper === reader.paper && h.page === view.page.pageNumber);
    const percent = value => `${(value * 100).toFixed(3)}%`;
    layer.innerHTML = here.flatMap(h => h.rects.map(r =>
      `<div class="highlight-${html(h.color)}" style="left:${percent(r.x)};top:${percent(r.y)};width:${percent(r.w)};height:${percent(r.h)}"></div>`)).join('');
  }
  function renderHighlights() {
    const mine = notebook.highlights.filter(h => h.paper === reader.paper);
    const here = mine.filter(h => h.page === reader.page);
    reader.highlightsPage = reader.page;
    for (const view of reader.views || []) renderHighlightLayer(view);
    const drafts = new Map(noteDrafts().map(field => [field.dataset.highlightNote, field.value]));
    const others = [...new Set(mine.map(h => h.page))].filter(page => page !== reader.page).sort((a, b) => a - b);
    el('page-highlights').innerHTML = (here.map(h => `<article class="highlight-entry" data-highlight="${html(h.id)}">
        <blockquote class="highlight-${html(h.color)}">${html(h.text)}</blockquote>
        <label>Your note<textarea data-highlight-note="${html(h.id)}">${html(drafts.get(h.id) ?? h.note)}</textarea></label>
        <div class="tool-actions">${button('save-highlight','Save note')}${button('capture-highlight','Capture as claim')}${button('remove-highlight','Remove highlight')}</div></article>`).join('')
      || '<p class="hint">Select a passage on the page, then highlight it.</p>')
      + (others.length ? `<div class="tool-actions"><span class="hint">Other highlighted pages</span>${others.map(page => button('highlight-page',`Page ${page}`,`data-page="${page}"`)).join('')}</div>` : '');
  }
  // Client rectangles of the selection as fractions of the page, one per line of text.
  function selectionRects(range, container) {
    const box = container.getBoundingClientRect();
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
  async function discardDrafts() {
    if (unsaved() && !await confirmDialog('Discard unsaved changes before leaving this view?',{ok:'Discard changes'})) return false;
    if (dirty) { notebook = await request(); dirty = false; }
    if (reader) reader.draft = false;
    connectionDraft = false;
    return true;
  }
  async function action(name, target) {
    const data = target.dataset;
    if (name === 'close') { setBusy(false); await close(); return; }
    if (name === 'tab' || name === 'reload') {
      if (!await discardDrafts()) return;
      stopReaderViews();
      if (reader?.document) await reader.document.loadingTask.destroy();
      reader = null; epoch++;
      notebook = await request();
      tab = data.tab || (['reader','history'].includes(tab) ? 'compare' : tab); render(); return;
    }
    if (name === 'unpin') { claims.delete(data.ref); remember(); sync(); renderCompare(); return; }
    if (name === 'unpin-paper') { papers.delete(data.paper); remember(); sync(); renderCompare(); return; }
    if (name === 'source') {
      if (!await discardDrafts()) return;
      setBusy(false); await close();
      captureOpenEditor(); parkNewClaimForNavigation(data.paper);
      V.paper = data.paper; V.view = 'claims'; V.tag = null; V.label = null;
      V.q = ''; V.kind = ''; V.unreviewed = false; V.unverified = false;
      V.selectedId = data.claim || null;
      el('q').value = ''; el('kind').value = ''; el('only-unreviewed').checked = false; el('only-unverified').checked = false;
      scheduleTextSearch(); renderAll(); syncHash(true); scrollToSelected(); return;
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
    if (name === 'reader') { if (!await discardDrafts()) return; await showReader(data.paper,data.page); return; }
    if (['previous-page','next-page','go-page','highlight-page'].includes(name)) {
      const page = name === 'go-page' ? Number(el('reader-page').value) : name === 'highlight-page' ? Number(data.page) : reader.page + (name === 'next-page' ? 1 : -1);
      if (!Number.isInteger(page) || page < 1 || page > reader.document.numPages) throw new Error('Choose a page within this PDF.');
      await goToReaderPage(page); return;
    }
    if (name === 'bookmark') {
      const progress = notebook.reading.find(r=>r.paper === reader.paper);
      progress.bookmarks = progress.bookmarks.includes(reader.page) ? progress.bookmarks.filter(p=>p !== reader.page) : [...progress.bookmarks,reader.page];
      await save(); target.textContent = progress.bookmarks.includes(reader.page) ? 'Remove bookmark' : 'Bookmark this page'; return;
    }
    if (name === 'highlight-selection') {
      if (!reader.selection || !reader.rects?.length) throw new Error('Select a passage on the PDF page first.');
      notebook.highlights.push({id:uid(),paper:reader.paper,page:reader.selectionPage,rects:reader.rects,text:reader.selection,color:el('highlight-color').value,note:''});
      reader.selection = ''; reader.rects = []; window.getSelection()?.removeAllRanges();
      await save(); renderHighlights(); return;
    }
    if (['save-highlight','capture-highlight','remove-highlight'].includes(name)) {
      const id = target.closest('[data-highlight]').dataset.highlight, highlight = notebook.highlights.find(h => h.id === id);
      const note = target.closest('[data-highlight]').querySelector('[data-highlight-note]').value;
      if (name === 'capture-highlight') {
        el('capture-quote').value = highlight.text; el('capture-locator').value = `p. ${highlight.page}`;
        if (!el('capture-note').value) el('capture-note').value = note;
        reader.draft = true; el('capture-text').focus(); return;
      }
      if (name === 'save-highlight') highlight.note = note;
      if (name === 'remove-highlight') {
        if ((highlight.note || note) && !await confirmDialog('Remove this highlight and its note?',{ok:'Remove highlight'})) return;
        notebook.highlights = notebook.highlights.filter(h => h.id !== id);
      }
      await save(); renderHighlights(); return;
    }
    if (name === 'capture-selection') {
      if (!reader.selection) throw new Error('Select a passage on the PDF page first.');
      el('capture-quote').value = reader.selection; el('capture-locator').value = reader.selectionLocator;
      reader.draft = true; el('capture-text').focus(); return;
    }
  }
  async function run(task) {
    if (busy) return;
    setBusy(true);
    try { await flushReaderProgress(); await task(); }
    catch (error) { message(error.message,true); if (!el('research-tools').open) toast(error.message,{tone:'warn'}); }
    finally { setBusy(false); if (reader?.progressPending) scheduleReaderProgress(); }
  }
  document.addEventListener('selectionchange', () => {
    if (!reader || tab !== 'reader') return;
    const selection = window.getSelection();
    const pageFor = node => (node?.nodeType === Node.ELEMENT_NODE ? node : node?.parentElement)?.closest('#pdf-pages .pdf-page');
    const start = pageFor(selection?.anchorNode), end = pageFor(selection?.focusNode);
    if (start && end && selection.toString().trim()) {
      reader.selection = selection.toString().trim();
      const first = Math.min(Number(start.dataset.page), Number(end.dataset.page));
      const last = Math.max(Number(start.dataset.page), Number(end.dataset.page));
      reader.selectionLocator = first === last ? `p. ${first}` : `pp. ${first}–${last}`;
      reader.selectionPage = first;
      reader.rects = first === last && selection.rangeCount ? selectionRects(selection.getRangeAt(0), start) : [];
    }
    for (const view of reader.views || []) {
      if (view.rendered && !view.nearby && !readerPageSelected(view)) releaseReaderPage(view);
    }
  });
  document.addEventListener('click', event => {
    // The label too: its text reaching the card would redraw the box away before it ticks.
    if (event.target.closest('[data-select-claim], [data-select-paper], .claim-selection label')) { event.stopPropagation(); return; }
    const target = event.target.closest('[data-tool]');
    if (!target) return;
    event.preventDefault(); event.stopPropagation();
    const name = target.dataset.tool;
    if (name === 'clear-selection') { claims.clear(); papers.clear(); remember(); sync(); return; }
    if (name === 'select-visible') { visibleClaims().forEach(c=>claims.add(refKey(c))); remember(); sync(); return; }
    run(async () => {
      if (!el('research-tools').open) {
        await open(['boards','reading','searches','connections','compare'].includes(name) ? name : 'compare');
        if (['boards','reading','searches','connections','compare'].includes(name)) return;
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
    if (target.id === 'reader-zoom') run(layoutReader);
  });
  document.addEventListener('input', event => {
    const target = event.target;
    if (!el('research-tools')?.contains(target)) return;
    if (target.id.startsWith('connection-')) connectionDraft = true;
    if (target.id === 'board-title') { currentBoard().title = target.value; dirty = true; }
    if (target.dataset.entryText) { currentBoard().entries.find(e=>e.id === target.dataset.entryText).text = target.value; dirty = true; }
    if (target.dataset.connectionNote) { notebook.connections.find(c=>c.id === target.dataset.connectionNote).note = target.value; dirty = true; }
    if (target.dataset.highlightNote && reader) reader.noteValues.set(target.dataset.highlightNote, target.value);
    if (target.closest('#capture-form') && reader) reader.draft = true;
  });
  document.addEventListener('submit', event => {
    if (event.target.id !== 'capture-form') return;
    event.preventDefault();
    run(async () => {
      const text = el('capture-text').value.trim();
      if (!text) throw new Error('Write the claim in your own words before saving.');
      const result = await api(`/api/papers/${encodeURIComponent(reader.paper)}/claims`,{method:'POST',headers:{'Content-Type':'application/json','X-Doxograph-Workspace':modalWorkspace},
        body:JSON.stringify({text,quote:el('capture-quote').value,locator:el('capture-locator').value,note:el('capture-note').value})});
      reader.draft = false; event.target.reset(); el('capture-locator').value = `p. ${reader.page}`;
      await refreshAll(); el('capture-status').textContent = result.quote_verified === false ? 'Claim saved. The quote was not found by the text check; review its wording.' : 'Claim saved with its source.';
    });
  });
  document.addEventListener('DOMContentLoaded', () => {
    el('research-tools').addEventListener('cancel', event => { event.preventDefault(); close(); });
  });
  window.addEventListener('beforeunload', event => { if (unsaved()) { event.preventDefault(); event.returnValue = ''; } });
  async function beforeWorkspaceSwitch() {
    if (!el('research-tools').open) return true;
    if (busy) { message('Finish the current operation before switching workspaces.', true); return false; }
    await close();
    return !el('research-tools').open;
  }
  return {sync,claimCheckbox,paperCheckbox,beforeWorkspaceSwitch,
    openBoard: id => run(() => open('boards', id))};
})();
