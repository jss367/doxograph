'use strict';

// The research context and the reader's own claims, edited in one form.

// --- research: the context and the ledger, edited in place -----------------
//
// context.md and ledger.yaml are the two inputs that most shape what
// extraction returns, and both used to need a text editor outside the app.
// One form edits both; Save writes both.

// A saved claim has a link other documents can keep. One not saved yet, or
// under an id not saved yet, has none to give.
function ledgerRow(claim, i) {
  const saved = (S.ledger || []).some((own) => own.id === claim.id);
  return `<div class="linkrow" data-ledger-row="${i}">
    <input name="ledger-id" value="${esc(claim.id || '')}" placeholder="L${i + 1}" size="6" aria-label="Claim id">
    <input name="ledger-text" value="${esc(claim.text || '')}" placeholder="One of my own claims, as a sentence" aria-label="Claim text">
    ${saved ? `<button type="button" data-act="copy-link" data-kind="ledger" data-id="${esc(claim.id)}"
      title="Copy a link to this claim and the evidence bearing on it">link</button>` : ''}
    <button type="button" data-act="drop-ledger" title="Remove this claim">×</button>
  </div>`;
}

// The research form's edits live only in the DOM until Save, and every other
// redraw (a filter, a paper click, the topic list) rebuilds `#content`. Read
// the form into `V.researchDraft` before any of that, and draw from the draft
// when there is one, so navigating away and back finds the text as it was.
function captureResearchDraft() {
  if ($('new-tag')) V.topicDraft = $('new-tag').value;
  if (!$('research-form')) return;
  const current = readResearchForm();
  // A form nobody has typed into is not a draft. It is judged against what
  // it was drawn from, not against `S`: a poll can replace `S` while the form
  // is open, and an untouched form would then look edited, be kept, and
  // later write the old values over the change made elsewhere.
  V.researchDraft = differs(current, V.researchBase || storedResearch()) ? current : null;
}

function storedResearch() {
  return {
    context: S.context || '',
    claims: (S.ledger || []).map((c) => ({ id: c.id || '', text: c.text || '' })),
  };
}

function differs(a, b) {
  return a.context.trim() !== b.context.trim()
    || JSON.stringify(a.claims) !== JSON.stringify(b.claims);
}

function renderResearch() {
  captureResearchDraft();
  const main = $('main');
  const scrollTop = main ? main.scrollTop : 0;
  const draft = V.researchDraft;
  // Drawn from the server, the form remembers what it was drawn from, so a
  // later poll cannot make an untouched form look edited. A draft keeps the
  // base it was typed against.
  if (!draft) V.researchBase = storedResearch();
  const context = draft ? draft.context : (S.context || '');
  const ledger = draft ? draft.claims : (S.ledger || []);
  let html = `<div class="paperhead">
    <h2>Workspace</h2>
    <p class="pm">${esc(S.workspace?.name || '')} · ${S.papers.length} paper${S.papers.length === 1 ? '' : 's'} · ${S.claims.length} claim${S.claims.length === 1 ? '' : 's'}</p>
  </div>
  <div class="workspace-page">
  <h2>What I am studying</h2>
  <p class="ps">The context goes into every model pass and is the main lever on the
    <em>why it is here</em> line. Your own claims are what a paper's claims are linked
    against: supports, contradicts, supplies a method for, refines.</p>`;
  if (V.error) html += `<p class="warn">${esc(V.error)}</p>`;
  html += `<form class="edit research" id="research-form">
    <div><label for="research-context">Research context</label>
      <textarea id="research-context" name="context" rows="8"
        placeholder="What the research is about, and what makes a paper relevant to it.">${esc(context)}</textarea></div>
    <div><label>My own claims</label>
      <div class="links" id="ledger-rows">${ledger.map(ledgerRow).join('')}</div>
      <div class="row"><button type="button" data-act="add-ledger">Add a claim</button></div></div>
    <div class="row right">
      <button type="button" data-act="cancel-research">Cancel</button>
      <button type="submit" class="primary">Save</button>
    </div>
  </form>
  ${workspaceTopics()}
  ${workspaceLabels()}
  <h2>Actions</h2>
  <div class="actions-row">
    <button type="button" data-reader-command="btn-export">Export HTML</button>
    <button type="button" data-reader-command="btn-bib">Export BibTeX</button>
    <button type="button" data-reader-command="btn-workspace-archive">Download archive</button>
    <button type="button" data-reader-command="btn-workspace-backups">Previous versions</button>
    <button type="button" data-reader-command="btn-retag" data-ai-action ${S.ai_enabled === true ? '' : 'disabled'}>Retag all</button>
    <button type="button" data-reader-command="btn-settings">Settings</button>
    <button type="button" data-reader-command="btn-workspace-rename">Rename workspace</button>
    <button type="button" class="danger" data-reader-command="btn-workspace-delete"
      ${$('btn-workspace-delete').disabled ? 'disabled' : ''} title="${esc($('btn-workspace-delete').title)}">Delete workspace</button>
  </div>
  </div>`;
  $('content').innerHTML = html;
  setResearchSaving($('research-form'), V.researchSaving);
  if (main) main.scrollTop = scrollTop;
}

// The topic vocabulary with its use, in the vocabulary's order; a topic in
// use that the vocabulary does not list is marked, as in the sidebar. A click
// opens the library on the topic.
function workspaceTopics() {
  return `<h2>Topics</h2><div id="workspace-topic-list">${workspaceTopicList()}</div>
    <div class="row topic-add"><input id="new-tag" value="${esc(V.topicDraft || '')}" placeholder="new topic" aria-label="New topic">
    <button type="button" id="btn-tag">Add topic</button></div>`;
}

function topicNameDraft() {
  return $('new-tag')?.value ?? V.topicDraft ?? '';
}

// Refresh confirmed vocabulary without replacing the research form, its
// focus or a topic name being typed while an earlier request finishes.
function syncWorkspaceTopics() {
  const list = $('workspace-topic-list');
  if (list) list.innerHTML = workspaceTopicList();
}

function workspaceTopicList() {
  const counts = S.tag_counts || {};
  const declared = (S.tags || []).map((t) => t.name);
  const names = [...declared, ...Object.keys(counts).filter((name) => !declared.includes(name)).sort()];
  if (!names.length) return '<p class="hint">No topics yet. The vocabulary is written as papers are read.</p>';
  return `<ul class="topic-list">${names.map((name) => {
    const description = (S.tags.find((t) => t.name === name) || {}).description || '';
    return `<li data-open-topic="${esc(name)}" title="${esc(description)}">${esc(name)}${declared.includes(name) ? '' : ' *'}<span class="n">${counts[name] || 0}</span></li>`;
  }).join('')}</ul>`;
}

function workspaceLabels() {
  const entries = Object.entries(S.label_counts || {});
  if (!entries.length) return '';
  return `<h2>Labels</h2><ul class="label-list">${entries.map(([name, count]) => `<li>${labelChip(name)} <span class="hint">${count}</span></li>`).join('')}</ul>`;
}

// A topic on the Workspace page opens the library's claims on that topic,
// the way the command palette does, leaving the research form behind with
// its draft kept.
document.addEventListener('click', (event) => {
  const topic = event.target.closest('[data-open-topic]');
  if (!topic) return;
  captureResearchDraft();
  // Settle the complete destination before its one history entry is written.
  followToPaper(null, null, topic.dataset.openTopic);
});

// Whether the research form on screen differs from what the server holds. The
// form is the only place its edits live until Save, so leaving the workspace
// with it dirty is leaving a draft behind.
function researchFormDirty() {
  if ($('research-form')) return differs(readResearchForm(), V.researchBase || storedResearch());
  return Boolean(V.researchDraft);
}

function readResearchForm() {
  const form = $('research-form');
  const claims = [...form.querySelectorAll('[data-ledger-row]')].map((row) => ({
    id: row.querySelector('[name="ledger-id"]').value.trim(),
    text: row.querySelector('[name="ledger-text"]').value.trim(),
  })).filter((c) => c.id || c.text);
  return { context: form.querySelector('[name="context"]').value, claims };
}

function setResearchSaving(form, busy) {
  if (!form?.isConnected) return;   // a finished save has already left the view
  form.classList.toggle('saving', busy);
  form.querySelectorAll('input, textarea, button').forEach((field) => { field.disabled = busy; });
}

async function saveResearch() {
  if (V.researchSaving) return;   // a save is in flight: one ledger/context pair at a time
  const form = $('research-form');
  const { context, claims } = readResearchForm();
  V.error = null;
  V.researchSaving = true;
  setResearchSaving(form, true);
  // Only what was edited is written, judged against what the form was drawn
  // from: a field left alone must not carry the value the form opened with
  // over a change made elsewhere while it was open.
  const base = V.researchBase || storedResearch();
  const ledgerChanged = JSON.stringify(claims) !== JSON.stringify(base.claims);
  const contextChanged = context.trim() !== base.context.trim();
  try {
    // The ledger goes first: it is the one of the two the server can refuse
    // (a missing or repeated id), so nothing is written unless both will be.
    if (ledgerChanged) {
      await api('/api/ledger', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ claims }),
      });
    }
    if (contextChanged) {
      await api('/api/context', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text: context }),
      });
    }
  } catch (error) {
    // Show the error over the form as typed; redrawing from S would put the
    // saved values back and lose the edit that just failed.
    V.error = `Could not save: ${error.message}`;
    const currentForm = $('research-form') || form;
    let warn = currentForm.previousElementSibling;
    if (!warn || !warn.classList.contains('warn')) {
      currentForm.insertAdjacentHTML('beforebegin', '<p class="warn"></p>');
      warn = currentForm.previousElementSibling;
    }
    warn.textContent = V.error;
    return;
  } finally {
    V.researchSaving = false;
    setResearchSaving(form, false);
    if ($('research-form') !== form) setResearchSaving($('research-form'), false);
  }
  showView('claims');       // captures the form on the way out, so clear after
  V.researchDraft = null;
  V.researchBase = null;
  syncHash(true);           // as with Cancel: leaving the form is a move
  await refreshAll();
}

$('panes').addEventListener('submit', async (event) => {
  if (event.target.id !== 'research-form') return;
  event.preventDefault();
  await saveResearch();
});

