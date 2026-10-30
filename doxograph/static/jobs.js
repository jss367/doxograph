'use strict';

// Background jobs and their notifications, and adding papers by reference
// or by dropping files.

function focusPaperImport() {
  if (readingPaper()) setReadingLibrary(false);
  revealSidebar($('paper-import'));
  $('paper-import').open = true;
  $('refs').focus();
}

const pendingJobActions = new Set();
const jobTimeouts = new Map();

function jobTimeoutPaused(card) {
  return document.hidden || card.matches(':hover') || card.contains(document.activeElement);
}

function syncJobTimeouts() {
  const workspace = workspaceClient.workspaceId;
  const visible = new Set();
  for (const card of $('jobs').querySelectorAll('[data-auto-dismiss]')) {
    const key = card.dataset.jobKey;
    if (!key.startsWith(`${workspace}:`)) continue;
    visible.add(key);
    if (!jobTimeouts.has(key)) {
      jobTimeouts.set(key, { remaining: 8000, timer: null, expired: false });
    }
    const entry = jobTimeouts.get(key);
    entry.card = card;
    if (jobTimeoutPaused(card)) {
      if (entry.timer !== null) {
        clearTimeout(entry.timer);
        entry.remaining = Math.max(0, entry.remaining - (performance.now() - entry.started));
        entry.timer = null;
      }
    } else if (entry.timer === null && !entry.expired) {
      entry.started = performance.now();
      entry.timer = setTimeout(() => {
        entry.timer = null;
        entry.remaining = 0;
        if (workspace !== workspaceClient.workspaceId || !entry.card.isConnected || !jobTimeouts.has(key)) return;
        if (jobTimeoutPaused(entry.card)) return;
        // Use the same persisted dismissal as the close button. On failure,
        // leave the card available for a manual retry instead of looping.
        entry.expired = true;
        entry.card.querySelector('.dismiss-job').click();
      }, entry.remaining);
    }
    card.onmouseenter = card.onmouseleave = syncJobTimeouts;
  }
  // Hidden cards get their full reading time when they next become visible,
  // including after a workspace switch or a backlog of imports clears.
  for (const [key, entry] of jobTimeouts) {
    if (visible.has(key)) continue;
    clearTimeout(entry.timer);
    jobTimeouts.delete(key);
  }
}

for (const type of ['focusin', 'focusout']) {
  $('jobs').addEventListener(type, () => queueMicrotask(syncJobTimeouts));
}
document.addEventListener('visibilitychange', syncJobTimeouts);

function renderJobs() {
  const expanded = new Set([...$('jobs').querySelectorAll('.job-details[open]')]
    .map((details) => details.dataset.jobId));
  const active = (S.jobs || []).filter((j) => j.state !== 'done' || j.detail || j.kind === 'import');
  // Successful imports remain available to open without crowding out failures.
  active.sort((a, b) => Number(a.state === 'done') - Number(b.state === 'done'));
  const importStates = { queued: 'Waiting to import…', fetching: 'Importing…', reading: 'Analyzing…',
                         done: 'Imported', error: 'Import failed' };
  const next = document.createElement('div');
  next.innerHTML = (S.job_recovery_issues || []).map(issue => `<div class="job error"><span class="lbl">Previous imports</span><p class="job-explanation">${esc(issue)}</p></div>`).join('') + active.slice(0, 6).map((j) => {
    const disabled = pendingJobActions.has(`${workspaceClient.workspaceId}:${j.id}`) ? 'disabled' : '';
    const explained = Boolean(j.explanation);
    const label = S.papers.find((paper) => paper.key === j.key)?.title || j.label;
    const status = j.kind === 'import' ? (importStates[j.state] || j.state) : j.state;
    return `
    <div class="job ${j.state === 'error' ? 'error' : ''}" data-job-key="${esc(workspaceClient.workspaceId)}:${j.id}"
      ${j.state === 'done' && j.kind === 'import' ? 'data-auto-dismiss' : ''}>
      <span class="lbl" title="${esc(label)}">${esc(label)}</span>
      <span class="st">${esc(status)}${j.detail && !explained ? ': ' + esc(j.detail) : ''}</span>
      ${explained ? `<p class="job-explanation">${esc(j.explanation)}</p>` : ''}
      ${explained && j.state === 'error' ? `
        <details class="job-details" data-job-id="${j.id}" ${expanded.has(String(j.id)) ? 'open' : ''}>
          <summary>Technical details</summary>${esc(j.detail)}</details>` : ''}
      ${j.state === 'error' && j.retryable ? `<div class="job-actions">
        <button type="button" class="retry-job" data-job-id="${j.id}" ${disabled}>${j.interrupted ? 'Resume import' : 'Retry'}</button></div>` : ''}
      ${j.state === 'done' && j.kind === 'import' && j.key ? `<div class="job-actions">
        <button type="button" class="open-job" data-job-id="${j.id}" data-paper="${esc(j.key)}" ${disabled}>Open paper</button></div>` : ''}
      ${['done', 'error'].includes(j.state) ? `<button type="button" class="dismiss-job" data-job-id="${j.id}"
        aria-label="Dismiss notification for ${esc(label)}" title="Dismiss notification" ${disabled}>×</button>` : ''}
    </div>`;
  }).join('');
  // Keep unchanged cards in place so polling and other jobs completing do not
  // interrupt keyboard focus or the hover that pauses a successful import.
  const container = $('jobs');
  const existing = new Map([...container.children].filter(card => card.dataset.jobKey)
    .map(card => [card.dataset.jobKey, card]));
  const cards = [...next.children].map(card => {
    const old = existing.get(card.dataset.jobKey);
    return old && old.outerHTML === card.outerHTML ? old : card;
  });
  for (const card of [...container.children]) {
    if (!cards.includes(card)) card.remove();
  }
  cards.forEach((card, index) => {
    if (container.children[index] !== card) container.insertBefore(card, container.children[index] || null);
  });
  syncJobTimeouts();
}

$('jobs').addEventListener('click', async (event) => {
  const button = event.target.closest('.dismiss-job, .retry-job, .open-job');
  if (!button || button.disabled) return;
  const workspace = workspaceClient.workspaceId;
  const actionKey = `${workspace}:${button.dataset.jobId}`;
  // Selection changes before the destination read retires the previous DOM.
  if (button.closest('.job').dataset.jobKey !== actionKey) return;
  if (pendingJobActions.has(actionKey)) return;
  pendingJobActions.add(actionKey);
  button.disabled = true;
  const retry = button.classList.contains('retry-job');
  const open = button.classList.contains('open-job');
  try {
    if (open) {
      if (S.papers.some((paper) => paper.key === button.dataset.paper)) openPaper(button.dataset.paper);
      else toast('This paper is no longer in this workspace.', { tone: 'warn' });
    }
    await api(`/api/jobs/${button.dataset.jobId}${retry ? '/retry' : ''}`, {
      method: retry ? 'POST' : 'DELETE', headers: { 'X-Doxograph-Workspace': workspace },
    });
    await refresh();
  } catch (error) {
    toast(`Could not ${retry ? 'retry import' : 'dismiss notification'}: ${error.message}`, { tone: 'warn' });
  } finally {
    pendingJobActions.delete(actionKey);
    if (workspace === workspaceClient.workspaceId) renderJobs();
  }
});

let addingReferences = false;

async function addReferences() {
  if (addingReferences) return;
  const text = $('refs').value.trim();
  if (!text) return;
  const workspace = workspaceClient.workspaceId;
  addingReferences = true;
  $('btn-add').disabled = true;
  $('btn-add').textContent = 'Adding…';
  $('import-status').hidden = false;
  $('import-status').textContent = 'Submitting references…';
  let result;
  try {
    result = await api('/api/ingest', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text, extract: $('auto-extract').checked }),
    });
  } catch (error) {
    // What was pasted stays in the box either way: a refused request is a
    // reason to try again, not a reason to lose the list.
    toast(`Could not add these references: ${error.message}`, { tone: 'warn' });
    $('import-status').textContent = `Could not add these references: ${error.message}`;
    return;
  } finally {
    addingReferences = false;
    $('btn-add').disabled = false;
    $('btn-add').textContent = 'Add';
  }
  // A request can finish after a workspace switch. Its notices belong to the
  // workspace it was submitted in, as do the references it would clear.
  if (workspace !== workspaceClient.workspaceId) return;
  const unknown = result.unknown || [];
  if ($('refs').value.trim() === text) $('refs').value = unknown.join('\n');
  $('import-status').hidden = !result.queued;
  if (result.queued) $('import-status').textContent =
    `Import started for ${result.queued} ${result.queued === 1 ? 'paper' : 'papers'}. Progress appears in the notifications.`;
  // Which lines failed, not how many: a count leaves the reader to work out
  // for themselves which of what they pasted is the problem.
  showRefWarning(unknown);
  reportKnown(result);
  try {
    await refresh();
  } catch (error) {
    toast(`References submitted, but the library could not refresh: ${error.message}`, { tone: 'warn' });
  }
}

// References the server recognised without fetching anything. They never
// become jobs, so the strip never mentions them and this is the only place
// they are reported.
function reportKnown(result) {
  const known = (result && result.known) || [];
  if (!known.length) return;
  const rest = known.length > 1 ? `, and ${known.length - 1} more` : '';
  toast(`Already in the corpus: ${known[0].key}${rest}.`);
}

function showRefWarning(unknown) {
  const warning = $('ref-warn');
  warning.hidden = !unknown.length;
  if (!unknown.length) return;
  const shown = unknown.slice(0, 3).map((line) => `“${line}”`).join(', ');
  const rest = unknown.length > 3 ? `, and ${unknown.length - 3} more` : '';
  warning.textContent = `Nothing identifies ${shown}${rest}. `
    + 'They are still in the box; paste an arXiv ID, a DOI, or a direct PDF link.';
}

$('btn-add').addEventListener('click', addReferences);

// The box is where references are typed, so it takes the usual way of saying
// "done": the button is a long way from the cursor otherwise.
$('refs').addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) {
    event.preventDefault();
    addReferences();
  }
});

// --- drag and drop --------------------------------------------------------

// A text drag over a field is the browser's: a sentence selected in the reader
// and dragged into the quote box must land there, not be read as papers to
// add. Files are the page's wherever they are dropped.
const TEXT_INPUT_TYPES = new Set(['', 'text', 'search', 'url', 'email', 'tel', 'password', 'number']);

function dropBelongsToField(event) {
  const types = [...(event.dataTransfer?.types || [])];
  if (types.includes('Files')) return false;
  const field = event.target.closest?.('input, textarea, [contenteditable]:not([contenteditable="false"])');
  if (!field) return false;
  // A checkbox, a range slider or a file picker takes no text, so a drop on
  // one is a drop on the page.
  return field.tagName !== 'INPUT' || TEXT_INPUT_TYPES.has((field.getAttribute('type') || '').toLowerCase());
}

['dragenter', 'dragover'].forEach((type) => document.addEventListener(type, (event) => {
  if (dropBelongsToField(event)) return;
  event.preventDefault();
  document.body.classList.add('dragging');
}));
['dragleave', 'drop'].forEach((type) => document.addEventListener(type, (event) => {
  if (type === 'dragleave' && event.relatedTarget) return;
  document.body.classList.remove('dragging');
}));

document.addEventListener('drop', async (event) => {
  if (dropBelongsToField(event)) return;
  event.preventDefault();
  const files = [...(event.dataTransfer.files || [])].filter((f) => f.type === 'application/pdf'
    || f.name.toLowerCase().endsWith('.pdf'));
  const text = event.dataTransfer.getData('text/plain');
  try {
    if (files.length) {
      const body = new FormData();
      files.forEach((file) => body.append('files', file, file.name));
      await api(`/api/upload?extract_now=${$('auto-extract').checked}`, { method: 'POST', body });
    } else if (text && text.trim()) {
      reportKnown(await api('/api/ingest', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, extract: $('auto-extract').checked }),
      }));
    }
  } catch (error) {
    toast(`Could not add what was dropped: ${error.message}`, { tone: 'warn' });
  }
  await refresh();
});
