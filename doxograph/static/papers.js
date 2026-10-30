'use strict';

// Papers: the library and paper list, a paper's header, overview, and note,
// its chat and context menu, and opening or removing it.

// The last visit is a browser preference, scoped to the workspace. PDF pages
// come from the existing notebook rather than a second copy of reading progress.
let continuation = null;
function continuationState() {
  if (continuation?.workspace === workspaceClient.workspaceId) return continuation;
  let saved = {}, dismissed = false;
  try { saved = JSON.parse(localStorage.getItem(`doxograph-continue:${workspaceClient.workspaceId}`)) || {}; } catch (_) { /* optional */ }
  try { dismissed = localStorage.getItem(`doxograph-continue-dismissed:${workspaceClient.workspaceId}`) === '1'; } catch (_) { /* optional */ }
  // A fresh deep link is a visit. On reload, the library behind a resumed PDF
  // has already been visited and must not displace the more recent reader.
  const reloading = performance.getEntriesByType('navigation')[0]?.type === 'reload';
  continuation = {workspace:workspaceClient.workspaceId, paper:saved.paper || null, claim:saved.claim || null,
    observedPaper:reloading ? V.paper : null, dismissed};
  return continuation;
}

function rememberContinuation(paper, claim = null) {
  const state = continuationState();
  if (state.paper !== paper) state.claim = null;
  state.paper = paper;
  if (claim) state.claim = claim;
  try { localStorage.setItem(`doxograph-continue:${workspaceClient.workspaceId}`, JSON.stringify({paper:state.paper, claim:state.claim})); } catch (_) { /* keep for this session */ }
}

function continuationTargets() {
  const state = continuationState();
  // Only a new paper visit changes the shortcut. A poll while reading a PDF
  // must not replace it with the paper still open behind the reader dialog.
  const visiting = V.view === 'claims' ? V.paper : null;
  if (visiting && (visiting !== state.observedPaper || !state.paper)) rememberContinuation(visiting);
  state.observedPaper = visiting;
  const reading = S.notebook?.reading || [];
  const paper = S.papers.find((p) => p.key === state.paper)
    || S.papers.find((p) => reading.some((r) => r.paper === p.key && r.status === 'reading'));
  const progress = reading.find((r) => r.paper === paper?.key);
  // Finish the last paper first, then continue through the rest of the corpus.
  const rows = paper ? S.claims.filter((c) => c.paper === paper.key) : [];
  const at = rows.findIndex((c) => c.id === state.claim);
  const next = rows.slice(at + 1).concat(rows.slice(0, at + 1)).find((c) => !c.reviewed)
    || S.claims.find((c) => !c.reviewed);
  return {paper, progress, next};
}

function renderContinuation() {
  const {paper, progress, next} = continuationTargets();
  const card = $('continue-reading');
  card.hidden = continuationState().dismissed || (!paper && !next);
  const html = `<h2 id="continue-title">Continue</h2>
    <div class="continue-items">
      ${paper ? `<div class="continue-item">
        <p class="continue-text" title="${esc(paper.title || paper.key)}">${esc(paper.title || paper.key)}</p>
        <div class="row"><button type="button" data-continue="paper">Open paper</button>
        ${paper.has_pdf ? `<button type="button" class="primary" data-tool="reader" data-paper="${esc(paper.key)}">Resume at page ${progress?.page || 1}</button>` : '<span class="hint">No PDF available</span>'}</div></div>` : ''}
      ${next ? `<div class="continue-item">
        <p class="continue-text" title="${esc(next.paper_title || next.paper)}: ${esc(next.text)}">${esc(next.text)}</p>
        <button type="button" data-continue="review">Next unreviewed claim</button></div>`
        : S.claims.length ? '<span class="hint">All claims reviewed.</span>' : ''}
    </div>
    <button type="button" class="continue-dismiss" data-continue="dismiss" aria-label="Dismiss continue where you left off" title="Dismiss">×</button>`;
  // Leave focused shortcuts intact across background refreshes.
  if (card.innerHTML !== html) card.innerHTML = html;
}

$('continue-reading').addEventListener('click', (event) => {
  const button = event.target.closest('[data-continue]');
  if (!button) return;
  if (button.dataset.continue === 'dismiss') {
    continuationState().dismissed = true;
    try { localStorage.setItem(`doxograph-continue-dismissed:${workspaceClient.workspaceId}`, '1'); } catch (_) { /* keep for this session */ }
    renderContinuation();
    return;
  }
  const {paper, next} = continuationTargets();
  const review = button.dataset.continue === 'review';
  const key = review ? next?.paper : paper?.key;
  if (!key) return;
  captureOpenEditor();
  parkNewClaimForNavigation(key);
  parkSynthEditor();
  parkNoteEditor();
  showView('claims');
  V.paper = key; V.tag = null; V.label = null;
  V.q = ''; V.kind = ''; V.unreviewed = false; V.unverified = false; V.editing = null;
  $('q').value = ''; $('kind').value = '';
  $('only-unreviewed').checked = false; $('only-unverified').checked = false;
  selectClaim(review ? next.id : null);
  rememberContinuation(key, review ? next.id : null);
  scheduleTextSearch();
  syncHash(true); renderAll();
  if (review) scrollToSelected();
  else $('main').scrollTop = 0;
});

let libraryHTML = null;
function renderLibrary() {
  const papers = visiblePapers();
  const topics = new Map();
  for (const claim of S.claims) {
    if (!topics.has(claim.paper)) topics.set(claim.paper, new Set());
    for (const tag of claim.tags || []) topics.get(claim.paper).add(tag);
  }
  const progress = new Map((S.notebook?.reading || []).map(entry => [entry.paper, entry]));
  const statuses = {queued: 'Queued', reading: 'Reading', finished: 'Finished'};
  const found = V.textSearch?.q === V.q.trim() ? V.textSearch : null;
  const hits = new Map((found?.papers || []).map(hit => [hit.key, hit]));
  const cards = papers.map(p => {
    const entry = progress.get(p.key);
    const status = statuses[entry?.status] || 'Unread';
    const tags = [...(topics.get(p.key) || [])].sort((a, b) => a.localeCompare(b));
    const params = new URLSearchParams({ws: workspaceClient.workspaceId, paper: p.key});
    const passages = (hits.get(p.key)?.passages || []).slice(0, 2);
    return `<article class="library-paper" data-library-paper="${esc(p.key)}">
      <div class="library-paper-heading">
        ${ResearchTools.paperCheckbox(p)}
        <h3><a class="library-paper-link" href="#${esc(params.toString())}" data-act="library-open-paper" data-paper="${esc(p.key)}">${esc(p.title || p.key)}</a></h3>
        <span class="reading-status ${esc(entry?.status || 'unread')}">${status}</span>
      </div>
      <p class="library-paper-meta">${esc((p.authors || []).join(', ') || 'Authors unknown')} · ${esc(p.year || 'Year unknown')}${entry?.status === 'reading' && entry.page ? ` · Page ${esc(entry.page)}` : ''}</p>
      <p class="library-paper-summary${p.summary ? '' : ' hint'}">${esc(p.summary || 'No summary yet.')}</p>
      <div class="library-paper-topics" aria-label="Topics">${tags.length
        ? tags.map(tag => `<button type="button" class="tag" data-tag="${esc(tag)}">${esc(tag)}</button>`).join(' ')
        : '<span class="hint">No topics yet</span>'}</div>
      ${passages.map(passage => `<p class="library-paper-passage"><span class="hint">PDF · p. ${esc(passage.page)}</span> …${mark(passage)}…</p>`).join('')}
    </article>`;
  }).join('');
  const searchStatus = !found ? '' : found.loading ? 'Searching paper text…'
    : found.error ? `Could not search paper text: ${found.error}` : '';
  const html = `<section class="paper-library" aria-labelledby="paper-library-title">
    <div class="paper-library-heading"><h2 id="paper-library-title">${V.tag ? esc(V.tag) : 'All papers'}</h2>
      <span class="hint">${papers.length} ${papers.length === 1 ? 'paper' : 'papers'}${papers.length < S.papers.length ? ` of ${S.papers.length}` : ''}</span></div>
    ${V.error ? `<p class="warn">${esc(V.error)}</p>` : ''}
    ${searchStatus ? `<p class="hint" role="status">${esc(searchStatus)}</p>` : ''}
    ${cards || `<p class="empty">${S.papers.length ? 'No papers match these filters.' : 'Your library is empty. Paste an arXiv ID or drop a PDF to add your first paper.'}</p>`}
  </section>`;
  // Keep focused links and checkboxes intact across unchanged background polls.
  if (libraryHTML === html && $('content').querySelector('.paper-library')) return;
  libraryHTML = html;
  const focused = document.activeElement;
  const key = focused.closest('[data-library-paper]')?.dataset.libraryPaper;
  const selector = focused.matches('[data-select-paper]') ? '[data-select-paper]'
    : focused.matches('[data-tag]') ? `[data-tag="${CSS.escape(focused.dataset.tag)}"]` : '.library-paper-link';
  $('content').innerHTML = html;
  if (key) $('content').querySelector(`[data-library-paper="${CSS.escape(key)}"] ${selector}`)?.focus({preventScroll: true});
}

// The server sends papers newest-added first. The other orders are a view
// preference, so they are applied here and remembered per browser, not per
// workspace: the choice is about how the person likes to scan the list.
const PAPER_SORT_KEY = 'doxograph-paper-sort';
const PAPER_SORTS = ['added-desc', 'added-asc', 'year-desc', 'year-asc', 'title'];

function readPaperSort() {
  try {
    const saved = localStorage.getItem(PAPER_SORT_KEY);
    return PAPER_SORTS.includes(saved) ? saved : PAPER_SORTS[0];
  } catch (error) {
    return PAPER_SORTS[0];
  }
}
V.paperSort = readPaperSort();

function sortedPapers(papers, order) {
  const byTitle = (a, b) => (a.title || a.key).localeCompare(b.title || b.key, undefined, { sensitivity: 'base' });
  const byAdded = (a, b) => (a.added || '').localeCompare(b.added || '');
  const list = [...papers];
  switch (order) {
    case 'added-asc': return list.sort((a, b) => byAdded(a, b) || byTitle(a, b));
    case 'title': return list.sort(byTitle);
    // A paper with no year sinks to the bottom in either direction: it is
    // unknown, not ancient or brand new.
    case 'year-desc': return list.sort((a, b) => ((b.year ?? -Infinity) - (a.year ?? -Infinity)) || byTitle(a, b));
    case 'year-asc': return list.sort((a, b) => ((a.year ?? Infinity) - (b.year ?? Infinity)) || byTitle(a, b));
    default: return list.sort((a, b) => byAdded(b, a) || byTitle(a, b));
  }
}

function addedLabel(p) {
  if (!p.added) return '';
  const day = p.added.slice(0, 10);
  return `<time datetime="${esc(p.added)}" title="added ${esc(p.added)}">${esc(day)}</time>`;
}

let importWorkspace = null, importPaperCount = 0;
function renderPapers() {
  if (!$('refs').value.trim()) {
    if (importWorkspace !== workspaceClient.workspaceId) $('paper-import').open = S.papers.length === 0;
    else if (!importPaperCount && S.papers.length) $('paper-import').open = false;
  }
  importWorkspace = workspaceClient.workspaceId;
  importPaperCount = S.papers.length;
  const claims = V.view === 'claims';
  const byAdded = V.paperSort.startsWith('added');
  const matched = matchingPapers();
  // The selected paper is listed whether or not it matches, or there would be
  // no way back out of it. It is not counted as a match, though: the count is
  // about the query, and navigation state must not inflate it.
  const keys = new Set(matched.map((p) => p.key));
  const shown = V.paper !== null && !keys.has(V.paper)
    ? S.papers.filter((p) => keys.has(p.key) || p.key === V.paper)
    : matched;
  // The count doubles as the reason the list got shorter: a filtered sidebar
  // with no explanation reads as papers having gone missing.
  const meta = matched.length < S.papers.length
    ? `${matched.length} of ${S.papers.length} match`
    : `${S.papers.length} ${S.papers.length === 1 ? 'paper' : 'papers'}`;
  const all = `<li class="${(claims || V.view === 'library') && V.paper === null ? 'active' : ''}" data-paper="">
    <span class="pt">All papers</span>
    <span class="pm">${meta}</span></li>`;
  $('papers').innerHTML = all + sortedPapers(shown, V.paperSort).map((p) => `
    <li class="${claims && V.paper === p.key ? 'active' : ''}" data-paper="${esc(p.key)}">
      ${ResearchTools.paperCheckbox(p)}<span class="pt"><span class="dot ${esc(p.status)}"></span>${esc(p.title || p.key)}</span>
      <span class="pm">${esc((p.authors || [])[0] ? p.authors[0].split(' ').pop() : '?')}
        ${p.year ? esc(p.year) : ''} · ${p.n_claims} claims · ${esc(paperReadiness(p))}${p.n_unreviewed ? `, ${p.n_unreviewed} new` : ''}${byAdded ? ` · ${addedLabel(p)}` : ''}</span>
      ${(p.labels || []).length ? `<span class="labels" aria-label="Paper labels">${p.labels.map((label) => `
        <button type="button" class="label" data-label="${esc(label)}" style="${labelStyle(label)}"
          aria-pressed="${Boolean(V.label) && labelKey(V.label) === labelKey(label)}" title="Filter by label: ${esc(label)}">${esc(label)}</button>`).join('')}</span>` : ''}
      <button type="button" class="pmenu" data-menu="${esc(p.key)}"
        aria-label="Actions for ${esc(p.title || p.key)}" title="Actions">⋯</button>
    </li>`).join('');
}

function paperHeader(key, reading = false) {
  const p = S.papers.find((x) => x.key === key);
  if (!p) return '';
  const url = (p.source || {}).url;
  const title = url ? `<a href="${esc(url)}" target="_blank" rel="noopener">${esc(p.title || key)}</a>`
                    : esc(p.title || key);
  return readingHeader(p, title, reading);
}

// Both paper layouts share the same reading actions. Analysis and maintenance
// commands stay reachable in a disclosure, with native keyboard navigation.
function readingHeader(p, title, reading) {
  const key = esc(p.key);
  const existing = $('paperhead').querySelector('.paper-actions');
  const open = existing?.dataset.paper === p.key && existing.open;
  const ai = S.ai_enabled === true ? '' : 'disabled';
  const progress = (S.notebook?.reading || []).find(r => r.paper === p.key);
  return `<div class="paperhead reading-head">
    <div class="reading-title"><h2>${title}</h2>
      <p class="pm">${esc((p.authors || []).join(', ') || 'Authors unknown')}${p.year ? ` · ${esc(p.year)}` : ''}${p.venue ? ` · ${esc(p.venue)}` : ''}</p></div>
    <div class="reading-actions" data-paper="${key}">
      <button type="button" class="primary" data-tool="reader" data-paper="${key}" ${p.has_pdf ? '' : 'disabled'}
        ${p.has_pdf ? '' : 'title="Add a PDF to read this paper"'}>Read</button>
      <button type="button" data-paper-chat="${key}" data-ai-action ${p.has_pdf ? ai : 'disabled'}
        ${p.has_pdf ? '' : 'title="Add a PDF to ask about this paper"'}>Ask about this paper</button>
      <button type="button" data-tool="${progress ? 'reading' : 'queue-paper'}" data-paper="${key}">${progress ? 'View reading queue' : 'Add to reading queue'}</button>
      <details class="paper-actions" data-paper="${key}" ${open ? 'open' : ''}>
        <summary>Paper actions</summary>
        <div class="paper-actions-menu">
          <button type="button" data-act="add-claim" data-paper="${key}">Add claim by hand</button>
          ${p.has_pdf ? `<button type="button" data-act="toggle-pdf" data-paper="${key}">${reading ? 'Hide PDF' : 'Show PDF'}</button>
          <button type="button" data-act="verify" data-paper="${key}">Check quotes</button>` : ''}
          ${p.n_unreviewed ? `<button type="button" data-act="review-all" data-paper="${key}">Mark ${p.n_unreviewed} reviewed</button>` : ''}
          <button type="button" data-act="labels" data-paper="${key}">Labels</button>
          <button type="button" data-tool="history" data-paper="${key}">Edit history</button>
          <hr>
          <button type="button" data-act="reextract" data-paper="${key}" data-ai-action ${ai}>Re-read paper</button>
          <button type="button" data-act="retag-one" data-paper="${key}" data-ai-action ${ai}>Retag claims</button>
          ${p.has_pdf ? `<a href="/pdf/${encodeURIComponent(p.key)}?${workspaceQuery()}" target="_blank" rel="noopener">Open original PDF ↗</a>` : ''}
          <button type="button" data-reader-command="btn-export">Export HTML</button>
          <button type="button" data-reader-command="btn-bib">BibTeX</button>
          <button type="button" data-reader-command="btn-help">Keyboard shortcuts</button>
          <hr><button type="button" class="danger" data-act="del-paper" data-paper="${key}">Remove paper</button>
        </div>
      </details>
    </div>
  </div>`;
}

function paperReadingProgress(key) {
  const progress = (S.notebook?.reading || []).find(r => r.paper === key);
  if (!progress) return 'Not started';
  const status = {queued: 'In reading queue', reading: 'Reading', finished: 'Finished'}[progress.status];
  return `${status}${progress.status !== 'queued' ? ` · Page ${progress.page}` : ''}`;
}

// Page saves can finish while a note editor holds off redraws. Update only the
// progress text, leaving the editor and mounted PDF in place.
function renderPaperReadingProgress() {
  document.querySelectorAll('[data-paper-progress]').forEach(node => {
    node.textContent = paperReadingProgress(node.dataset.paperProgress);
  });
}

function paperOverview(paper) {
  return `<section class="paper-overview" aria-labelledby="paper-overview-title">
    <h3 id="paper-overview-title">Paper overview</h3>
    <div class="paper-reading-progress"><span class="who">Reading progress</span>
      <p data-paper-progress="${esc(paper.key)}">${esc(paperReadingProgress(paper.key))}</p></div>
    ${(paper.labels || []).length ? `<div class="labels">${paper.labels.map(labelChip).join('')}</div>` : ''}
    <div class="paper-summary"><h4>Summary</h4>
      ${paper.summary ? `<p>${esc(paper.summary)}</p>` : '<p class="hint">No summary yet. You can read the paper and add your own notes.</p>'}</div>
    ${paper.relevance ? `<div class="paper-relevance"><h4>Why it matters</h4><p>${esc(paper.relevance)}</p></div>` : ''}
    ${paperNote(paper.key)}
    ${proposedPanel(paper.key)}
  </section>`;
}

// A note is shown as the reader typed it: blank lines make paragraphs and
// nothing else is interpreted. The same in the export.
function paragraphs(text) {
  return String(text || '').split(/\n\s*\n/).filter((p) => p.trim());
}

function noteHtml(text, label, actions = '') {
  return `<div class="note">
    <span class="who">${esc(label)}</span>
    ${paragraphs(text).map((p) => `<p>${esc(p)}</p>`).join('')}
    ${actions}
  </div>`;
}

// The reader's own note on the paper, under the model's summary of it. No
// pass reads it and no pass writes it, which is the point: it is the place
// for what you think that no extraction could have told you.
function paperNote(key) {
  const paper = S.papers.find((x) => x.key === key);
  if (!paper) return '';
  const draft = V.noteDrafts[key];
  // Empty notes are ready to type into. Merely showing the field must not
  // hold background updates or count as an unsaved edit on workspace changes.
  if (draft !== undefined && !isRemoving(key)) V.noteEditing = key;
  if (V.noteEditing === key || (!(paper.notes || '').trim() && !isRemoving(key))) {
    const text = draft !== undefined ? draft : (paper.notes || '');
    // Frozen from state rather than from the elements, so a redraw during the
    // save cannot hand back an editor whose request is still running.
    const busy = V.noteSaving === key ? ' disabled' : '';
    return `<div class="note editing${busy ? ' saving' : ''}">
      <label class="who" for="paper-note-${esc(key)}">my note</label>
      <textarea id="paper-note-${esc(key)}" data-note="${esc(key)}" rows="4"${busy}
        placeholder="What you make of this paper: what to read it for, what it gets wrong, what to do about it.">${esc(text)}</textarea>
      <div class="smeta">
        <span class="hint">Yours. Nothing sends it to a model.</span>
        <span class="cact" style="margin-left:auto">
          <button type="button" data-act="save-note" data-paper="${esc(key)}" class="primary"${busy}>Save</button>
          <button type="button" data-act="cancel-note" data-paper="${esc(key)}"${busy}>Cancel</button>
        </span>
      </div>
    </div>`;
  }
  if (!(paper.notes || '').trim()) return '';
  return noteHtml(paper.notes, 'my note', `<div class="smeta">
    <span class="cact" style="margin-left:auto">
      <button type="button" data-act="edit-note" data-paper="${esc(key)}">edit</button>
    </span></div>`);
}

// Keep what was typed, close the editor — as `parkSynthEditor` does, and for
// the same reason: the header goes with the paper, so leaving it must not
// leave `V.noteEditing` pointing at a textarea nobody can see, which would
// also stop the background poll.
function parkNoteEditor() {
  if (!V.noteEditing) return;
  const field = document.querySelector(`textarea[data-note="${CSS.escape(V.noteEditing)}"]`);
  if (field) V.noteDrafts[V.noteEditing] = field.value;
  V.noteEditing = null;
}

function cancelNoteEdit() {
  // A claim editor open alongside holds its text in the DOM until it is read,
  // and the redraw below rebuilds it from the draft on file.
  captureOpenEditor();
  if (V.noteEditing) delete V.noteDrafts[V.noteEditing];   // discard only this paper's draft
  V.noteEditing = null;
  renderContent();
}

async function saveNote(key) {
  if (V.noteSaving) {
    // One at a time, as a synthesis is. Another paper's Save is drawn enabled
    // — the freeze is keyed to the paper being written — so it says why
    // rather than doing nothing.
    if (V.noteSaving !== key) toast('Wait for the note being saved to finish.', { tone: 'warn' });
    return;
  }
  // The header stands until the removal comes back, so this is still
  // clickable. The PATCH landing first would report a note saved that the
  // DELETE behind it throws away; landing second it is a 404.
  if (refuseWhileRemoving(key, 'the note')) return;
  const field = document.querySelector(`textarea[data-note="${CSS.escape(key)}"]`);
  const text = field ? field.value : (V.noteDrafts[key] || '');
  V.error = null;
  captureOpenEditor();        // as on cancel: a claim editor's text is only in the DOM
  V.noteDrafts[key] = text;   // the redraw draws the editor from the draft
  V.noteSaving = key;
  renderContent();
  try {
    await api(`/api/papers/${encodeURIComponent(key)}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ notes: text }),
    });
    // Only this paper's editor. The save is slow enough to move to another
    // paper and open its note meanwhile, and closing that one would take the
    // reader out of a note they are still writing.
    if (V.noteEditing === key) V.noteEditing = null;
    delete V.noteDrafts[key];
  } catch (error) {
    V.noteDrafts[key] = text;   // keep what was typed so the save can be retried
    V.error = `Could not save the note: ${error.message}`;
  } finally {
    // In the `finally`, as the synthesis save is: a failure leaves the editor
    // frozen otherwise, with nothing left to hand it back.
    V.noteSaving = null;
    await refreshAllShowingError();
  }
}

// Clicking an existing label edits the same draft as typing; only OK saves it.
async function editLabels(key) {
  const paper = S.papers.find((x) => x.key === key);
  if (!paper) return;
  // The paper's own spelling of a label wins over the sidebar's.
  const inUse = [...(paper.labels || []), ...Object.keys(S.label_counts || {})
    .filter((name) => findLabel(paper.labels, name) === undefined)]
    .sort((a, b) => compareText(labelKey(a), labelKey(b)) || compareText(a, b));
  const answer = await askText(`Labels for ${paper.title || key}`, {
    note: (inUse.length ? 'Click a label to add or remove it. ' : '')
      + 'Type new labels separated by commas. Spaces are allowed within a label.',
    placeholder: 'open source, reading list',
    value: (paper.labels || []).join(', '),
    labelChoices: inUse,
  });
  if (answer === null) return;   // cancelled: not the same as clearing them
  const labels = parseLabels(answer);
  try {
    await api(`/api/papers/${encodeURIComponent(key)}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ labels }),
    });
  } catch (e) {
    toast(`Could not save the labels: ${e.message}`);
    return;
  }
  // `refreshAll` rather than `refresh`: the chips are part of the paper's
  // header, which `render` leaves alone while a claim editor is open, and the
  // labels would not appear until that editor was closed. The draft in it is
  // kept across the rebuild.
  await refreshAll();
}

function paperCache() {
  window.__paperCache = window.__paperCache || {};
  const workspace = workspaceClient.workspaceId || 'default';
  window.__paperCache[workspace] = window.__paperCache[workspace] || {};
  return window.__paperCache[workspace];
}

function proposedPanel(key) {
  const paper = S.papers.find((x) => x.key === key);
  if (!paper || !paper.n_proposed_tags) return '';
  // Keyed by the paper's updated timestamp, so a re-read that replaces the
  // proposals invalidates the cache instead of showing names the server will
  // no longer accept.
  const entry = paperCache()[key];
  const fresh = entry && entry !== 'loading' && entry.updated === paper.updated;
  const proposed = fresh ? entry.proposed_tags : null;
  if (!proposed) {
    loadProposed(key, paper.updated);
    return `<div class="proposed">Loading proposed topics…</div>`;
  }
  if (!proposed.length) return '';
  return `<div class="proposed">
    <div><strong>Proposed topics</strong> — accept the ones worth keeping in the vocabulary.</div>
    ${proposed.map((t) => `<div class="row">
      <span class="pn">${esc(t.name)}</span>
      <span class="hint" style="flex:1">${esc(t.description)}</span>
      <button type="button" data-act="accept-tag" data-paper="${esc(key)}" data-tag="${esc(t.name)}">Accept</button>
      <button type="button" data-act="reject-tag" data-paper="${esc(key)}" data-tag="${esc(t.name)}">Discard</button>
    </div>`).join('')}
  </div>`;
}

async function loadProposed(key, wanted) {
  const cache = paperCache();
  if (cache[key] === 'loading') return;
  cache[key] = 'loading';
  try {
    const paper = await api(`/api/papers/${encodeURIComponent(key)}`);
    cache[key] = { updated: paper.updated, proposed_tags: paper.proposed_tags || [] };
    if (wanted && paper.updated !== wanted) delete cache[key];  // changed again mid-flight
    if (!V.editing) renderContent();
  } catch (e) { delete cache[key]; }
}

function paperReadiness(paper) {
  const active = S.jobs.find(j => j.key === paper.key && ['queued','fetching','reading'].includes(j.state));
  if (active) return active.state === 'reading' ? 'Analyzing' : 'Import in progress';
  if (!paper.has_pdf) return paper.error ? 'PDF unavailable — retry import' : 'No PDF — add a source';
  if (paper.n_unreviewed) return `${paper.n_unreviewed} to review`;
  if (paper.n_claims) return 'Claims reviewed';
  if (paper.has_extraction) return 'No claims found — read manually';
  return S.ai_enabled === true ? 'Ready to read or analyze' : 'Ready to read — AI off';
}

function openPaper(paper) {
  captureOpenEditor();
  closeEditorsNotBelongingTo(paper);
  showView('claims');
  // Papers opened from another view may be outside the current label filter.
  keepLabelFor(paper);
  V.paper = paper; V.tag = null; V.selectedId = null;
  syncHash(true);
  renderAll();
}

// Shared by the Remove button in the paper header and the right-click menu in
// the paper list, so the menu can remove a paper that is not the one on screen.
async function removePaper(paper) {
  const p = S.papers.find((x) => x.key === paper);
  // The one delete with no undo: a removed paper's key is retired for good, so
  // adding it again would make a different paper. Hence a question first.
  const draft = ResearchTools.markDraftOn(paper);
  const go = await confirmDialog(`Remove ${p ? p.title || paper : paper} and its claims?`, {
    note: 'The PDF and every claim on it go with it, and the key is never reissued.'
      + (draft ? ' The note you are writing on one of its marks goes too.' : ''),
    ok: 'Remove',
  });
  if (!go) return;
  // The question is a wait like any other, and the header and the paper list
  // stay live across it: a second Remove could have been answered first, and
  // this one would ask the server for a paper that is already going.
  if (refuseWhileRemoving(paper, 'a second removal')) return;
  // A write already on its way cannot be recalled. It carries no claim id this
  // could freeze — a re-read, a quote check, a retag and an accepted topic all
  // write the paper itself — and whichever way it races the DELETE it ends
  // badly: landing first it is thrown away under a page that reported it
  // saved, landing second it is a 404 over a removal the reader did ask for.
  // The guard below stops the next one; this is the one already in flight.
  if (workspaceClient.pendingMutations) {
    toast('Wait for the change in flight to finish, then remove the paper.', { tone: 'warn' });
    return;
  }
  // Nothing else may be written to this paper from here on, and "here" is
  // before the flush below rather than after it: the flush ends in a read of
  // the whole corpus, long enough for a claim to be saved, a re-read started
  // or a topic accepted, and the guard is no use to the DELETE if it goes up
  // only once that window has closed. It is dropped in the `finally` at the
  // end, so it covers the whole attempt and not just the request.
  removingPapers.add(paper);
  // Frozen claim ids, filled in once the flush has settled and the corpus is
  // known. Declared out here so the `finally` can hand them back on the way
  // out of a flush that failed too.
  let frozen = [];
  try {
    // A claim of this paper still waiting out its notice has to go first. Left
    // waiting, its Undo would have nothing to restore and its request would
    // arrive under a paper that no longer exists, failing in the reader's face
    // over a deletion they got what they asked for. Only this paper's, though:
    // a claim of another paper, or a synthesis, was promised its own eight
    // seconds and removing something else is no reason to take them away.
    //
    // That flush ends in a read, and nothing counts a read as a change in
    // flight, so the picker can move in the gap before the paper's own request
    // goes. It names the workspace it was asked in: the same paper imported
    // into two corpora has the same key in both, and removing the wrong one is
    // not something an undo could fix.
    const headers = await settleDeletes((entry) => entry.paper === paper);
    // That claim's delete failed. Removing the paper would take it anyway,
    // under a notice that said the claim could not be deleted — so nothing
    // happens. The `finally` drops the guard, handing the paper back to the
    // reader who still has it on screen.
    if (!headers) return;
    const workspace = headers['X-Doxograph-Workspace'];
    // The paper's cards stay on screen, and clickable, until this request comes
    // back and the redraw takes them away. A claim deleted in that window is
    // held for its own eight seconds under a paper that is already gone: the
    // flush above has finished, so nothing sends it early, its Undo would have
    // nothing to restore, and letting the notice run out sends a DELETE under a
    // paper the server no longer has — a failure notice over a deletion the
    // reader did ask for. They are frozen for the length of the removal, the
    // same freeze a save or a bulk review puts on the claims it is writing.
    //
    // Worked out after the flush rather than before it, because the flush ends
    // in a read and the claims the paper has once that lands are the ones the
    // DELETE is about to take. Nothing can be clicked in between — there is no
    // await between the read and this freeze — and the guard above held the
    // paper for the whole of the flush itself.
    frozen = S.claims.filter((c) => c.paper === paper).map((c) => c.id);
    // A claim being written by hand is not among them: it has no id on the
    // server, only the `__new__` the form is drawn under. Frozen by that name
    // when it belongs to this paper, so the form goes read-only like the rest
    // rather than staying open over a paper that is going. The set below is what
    // actually refuses the save — the draft can be parked and restored by moving
    // between papers, and a form restored after this was worked out would not be
    // covered by it.
    if (V.newClaim && V.newClaim.paper === paper) frozen.push(NEW_CLAIM_ID);
    frozen.forEach((id) => markSaving(id, true));
    try {
      await api(`/api/papers/${encodeURIComponent(paper)}`, { method: 'DELETE', headers });
    } catch (error) {
      // The paper stays, cards and all, so the reader is told rather than left
      // with a request that failed in silence.
      toast(`Could not remove the paper: ${error.message}`, { tone: 'warn' });
      return;
    }
    // Moved on meanwhile: the view belongs to another corpus now, and the drafts
    // and selection this would tidy up went with `resetWorkspaceView`. A note
    // being written there on a mark of the same key is that corpus's own.
    if (workspaceClient.workspaceId !== workspace) {
      await refreshAll();
      return;
    }
    // The note being written on one of its marks goes with the paper, and only
    // now: a removal refused or failed above leaves the paper, and the note.
    ResearchTools.dropMarkDraft(paper);
    // Close an editor that belonged to the deleted paper, so its form is not
    // captured as a draft for a claim that no longer exists. An editor on some
    // other paper's claim stays open, which is why this ends in `refreshAll`:
    // `refresh` would leave the claim list alone while an editor is open and
    // the deleted paper's cards would stay on screen with buttons that 404.
    S.claims.filter((c) => c.paper === paper).forEach((c) => delete V.drafts[c.id]);
    if (V.editing && V.editing !== NEW_CLAIM_ID) {
      const row = S.claims.find((c) => c.id === V.editing);
      if (!row || row.paper === paper) V.editing = null;
    }
    if (V.newClaim && V.newClaim.paper === paper) {
      V.newClaim = null;
      if (V.editing === NEW_CLAIM_ID) V.editing = null;
    }
    delete V.parkedNewClaims[paper];
    // The note draft goes the same way as the claim drafts. The key is retired
    // for good, so a draft kept under it can never be reopened or cancelled —
    // and every entry in `noteDrafts` counts as an unsaved edit, so one left
    // behind would ask about discarding it on every later workspace switch.
    if (V.noteEditing === paper) V.noteEditing = null;
    delete V.noteDrafts[paper];
    // Removing a paper from the list while reading another one keeps that one
    // open; only a paper that was itself on screen falls back to "All papers".
    if (V.paper === paper) V.paper = null;
    const selected = S.claims.find((c) => c.id === V.selectedId);
    if (!selected || selected.paper === paper) V.selectedId = null;
    V.error = null;
    await refreshAll();
  } finally {
    // In a `finally` so a removal that fails — or one called off by a held
    // delete that would not go — does not leave the paper frozen with nothing
    // left to unfreeze it: the cards are still there and the reader has to be
    // able to work on them again.
    frozen.forEach((id) => markSaving(id, false));
    removingPapers.delete(paper);
  }
}

document.addEventListener('click', event => {
  const command = event.target.closest('[data-reader-command]');
  if (command) $(command.dataset.readerCommand).click();
  document.querySelectorAll('.paper-actions[open]').forEach(menu => {
    if (!menu.contains(event.target) || event.target.closest('button, a')) {
      menu.open = false;
      if (menu.contains(document.activeElement)) menu.querySelector('summary').focus();
    }
  });
});

$('papers').addEventListener('click', (event) => {
  const label = event.target.closest('[data-label]');
  if (label) {
    chooseLabel(label.dataset.label);
    return;
  }
  // The same menu the right-click opens, for the people who never try one.
  const menuButton = event.target.closest('[data-menu]');
  if (menuButton) {
    const box = menuButton.getBoundingClientRect();
    openPaperMenu(menuButton.dataset.menu, box.left, box.bottom + 4, menuButton);
    return;
  }
  const li = event.target.closest('[data-paper]');
  if (!li) return;
  const next = li.dataset.paper || null;
  const view = next ? 'claims' : 'library';
  // Clicking the paper already selected is not navigation. It used to run the
  // whole abandon path anyway, which threw away a new claim being written.
  // From the tensions view it is navigation: back to that paper's claims.
  if (next === V.paper && V.view === view) return;
  if (next === V.paper) { showView(view); syncHash(true); renderAll(); return; }
  // Keep an existing claim's edits, but still abandon a new unsaved claim:
  // that one was never persisted and belongs to the paper being left. A
  // synthesis being edited is parked too: `render` skips the list while one
  // is open, and its topic heading is not shown under a paper anyway.
  captureOpenEditor();
  parkNewClaimForNavigation(next);
  parkSynthEditor();
  // The note editor belongs to the paper being left, and `render` below skips
  // the list while one is open, so it would never be drawn away.
  parkNoteEditor();
  showView(view);
  V.paper = next;
  V.selectedId = null;
  V.editing = null;
  syncHash(true);   // going to a paper is navigation: Back comes back here
  render();   // the editors are closed here, so render redraws the list anyway
});

// Each paper keeps its saved conversations in its workspace, and each conversation its own agent.
let paperChatKey = null;
const paperChats = ChatThreads.create({
  prefix: 'chat',
  agents: () => ['api', 'claude_code', 'codex'],
  render(chat) {
    $('chat-messages').innerHTML = chat.messages.map((m) =>
      `<div class="note"><span class="who">${m.role === 'user' ? 'You' : esc(m.assistant || 'Claude')}</span>
        ${paragraphs(m.content).map((p) => `<p>${esc(p)}</p>`).join('')}
        <div class="chat-attachments">${ChatThreads.attachmentsHTML(m.attachments || [])}</div></div>`).join('');
    $('chat-question').value = chat.draft;
    $('chat-question').disabled = chat.busy;
    $('chat-status').textContent = chat.busy ? 'Reading the paper…' : chat.error || paperChats.issues.join(' ');
    syncAnalysisControls();
  },
});
const activeChat = () => paperChats.active;
document.addEventListener('click', (event) => {
  const button = event.target.closest('[data-paper-chat]');
  if (!button || button.disabled) return;
  paperChatKey = button.dataset.paperChat;
  $('chat-paper-title').textContent = (S.papers.find((p) => p.key === paperChatKey) || {}).title || paperChatKey;
  paperChats.open(workspaceClient.workspaceId, paperChatKey);
  $('paper-chat').showModal();
  $('chat-question').focus();
});
// Chat boxes send on Enter and take Shift+Enter as a new line, like other chat apps.
// Enter that confirms an IME composition belongs to the composition, not the form;
// WebKit reports that Enter with isComposing false and keyCode 229. A held key sends once.
function sendOnEnter(field) {
  field.addEventListener('keydown', (event) => {
    if (event.key !== 'Enter' || event.shiftKey || event.repeat) return;
    if (event.isComposing || event.keyCode === 229) return;
    event.preventDefault();
    field.form.requestSubmit();
  });
}
sendOnEnter($('chat-question'));
$('chat-question').addEventListener('input', () => { if (activeChat()) activeChat().draft = $('chat-question').value; });
$('chat-close').addEventListener('click', () => $('paper-chat').close());
$('chat-form').addEventListener('submit', (event) => {
  event.preventDefault();
  if (S.ai_enabled !== true) return;
  const key = paperChatKey, workspace = paperChats.workspace;
  paperChats.ask(async (chat, messages) => {
    const result = await api(`/api/papers/${encodeURIComponent(key)}/chat`, {
      method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Doxograph-Workspace': workspace },
      body: JSON.stringify({ messages, agent: chat.agent }),
    });
    return { content: result.answer, assistant: result.assistant || 'Claude', model: result.model || '' };
  });
});

// --- paper context menu --------------------------------------------------

let paperMenuTrigger = null;
let paperMenuScroll = null;

function restorePaperMenuFocus(trigger) {
  if (!trigger || trigger.workspace !== workspaceClient.workspaceId) return;
  $('papers').querySelector(`[data-menu="${CSS.escape(trigger.paper)}"]`)?.focus({ preventScroll: true });
}

function openPaperMenu(paper, x, y, trigger = null) {
  // Keep the identity rather than the node: polling and label saves rebuild
  // the list while the menu or its editor is open.
  paperMenuTrigger = trigger ? { paper, workspace: workspaceClient.workspaceId } : null;
  paperMenuScroll = new Map([$('side'), $('papers')].map(node =>
    [node, { top: node.scrollTop, left: node.scrollLeft }]));
  const p = S.papers.find((row) => row.key === paper);
  const menu = $('ctxmenu');
  menu.innerHTML = `
    <li class="mh">${esc(p ? p.title || paper : paper)}</li>
    ${p && p.has_pdf ? `<li><button type="button" data-act="open-pdf" data-paper="${esc(paper)}">Open the PDF</button></li>` : ''}
    <li><button type="button" data-act="labels" data-paper="${esc(paper)}">${p?.labels?.length ? 'Edit labels…' : 'Add labels…'}</button></li>
    <li><button type="button" data-act="del-paper" data-paper="${esc(paper)}">Remove paper</button></li>`;
  menu.hidden = false;
  // Measure after showing, then keep the whole menu inside the window.
  const { width, height } = menu.getBoundingClientRect();
  menu.style.left = `${Math.max(0, Math.min(x, window.innerWidth - width - 4))}px`;
  menu.style.top = `${Math.max(0, Math.min(y, window.innerHeight - height - 4))}px`;
  if (trigger) menu.querySelector('button')?.focus({ preventScroll: true });
}

function closePaperMenu({ restoreFocus = false } = {}) {
  $('ctxmenu').hidden = true;
  const trigger = paperMenuTrigger;
  paperMenuTrigger = null;
  paperMenuScroll = null;
  if (restoreFocus) restorePaperMenuFocus(trigger);
  return trigger;
}

$('papers').addEventListener('contextmenu', (event) => {
  const li = event.target.closest('[data-paper]');
  if (!li || !li.dataset.paper) return;   // "All papers" has no paper actions
  event.preventDefault();
  openPaperMenu(li.dataset.paper, event.clientX, event.clientY, li.querySelector('[data-menu]'));
});

$('ctxmenu').addEventListener('click', async (event) => {
  const button = event.target.closest('[data-act]');
  if (!button) return;
  const trigger = closePaperMenu({ restoreFocus: true });
  const paper = button.dataset.paper;
  if (button.dataset.act === 'labels') {
    try {
      await editLabels(paper);
    } finally {
      // A save replaces the restored button. Recover focus only if it fell
      // back to the document, preserving any new focus chosen during the save.
      if (document.activeElement === document.body) restorePaperMenuFocus(trigger);
    }
  }
  // The paper stays in the list until its DELETE comes back, so the menu can
  // be opened on it again and asked to remove it a second time.
  if (button.dataset.act === 'del-paper') {
    if (refuseWhileRemoving(paper, 'a second removal')) return;
    await removePaper(paper);
  }
  if (button.dataset.act === 'open-pdf') {
    window.open(`/pdf/${encodeURIComponent(paper)}?${workspaceQuery()}&inline=1`, '_blank');
  }
});

$('ctxmenu').addEventListener('keydown', (event) => {
  if (!['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) return;
  const buttons = [...$('ctxmenu').querySelectorAll('button')];
  if (!buttons.length) return;
  const current = buttons.indexOf(document.activeElement);
  const index = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1
    : (current + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length;
  event.preventDefault();
  event.stopPropagation();
  buttons[index].focus({ preventScroll: true });
});

// A press anywhere outside the menu dismisses it. Right-clicking another paper
// also lands here first, then the contextmenu handler above reopens it there.
document.addEventListener('mousedown', (event) => {
  if (!$('ctxmenu').contains(event.target)) closePaperMenu();
});
// Escape is handled in the keyboard section below, ahead of the editor keys,
// so closing the menu does not also cancel an open editor.
// Paper lists scroll independently; scroll does not bubble from descendants.
$('side').addEventListener('scroll', event => {
  if ($('ctxmenu').hidden) return;
  // Opening by pointer can follow a scroll in the same frame. Ignore that
  // queued event when its position was already accounted for on opening.
  const start = paperMenuScroll?.get(event.target);
  if (start && start.top === event.target.scrollTop && start.left === event.target.scrollLeft) return;
  closePaperMenu({ restoreFocus: true });
}, { capture: true });
window.addEventListener('resize', () => closePaperMenu({ restoreFocus: true }));

$('paper-sort').addEventListener('change', (e) => {
  V.paperSort = PAPER_SORTS.includes(e.target.value) ? e.target.value : PAPER_SORTS[0];
  try { localStorage.setItem(PAPER_SORT_KEY, V.paperSort); } catch (error) { /* preference remains for this page */ }
  renderPapers();
  if (V.view === 'library') renderContent();
});
$('paper-sort').value = V.paperSort;

for (const [id, view] of [['btn-library-view', 'library'], ['btn-claims-view', 'claims']]) {
  $(id).addEventListener('click', () => {
    showView(view);
    V.selectedId = null;
    syncHash(true);
    renderAll();
  });
}
