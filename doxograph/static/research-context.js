'use strict';

// The research context and the reader's own claims, edited in one form.

function renderResearchNav() {
  const n = (S.ledger || []).length;
  const bits = [];
  bits.push(S.context ? 'context written' : 'no context yet');
  bits.push(n ? `${n} claims of my own` : 'no claims of my own');
  if (V.view !== 'research' && researchFormDirty()) bits.push('unsaved edits');
  $('research-nav').innerHTML = `<li class="${V.view === 'research' ? 'active' : ''}" data-view="research">
    <span class="pt">What I am studying</span>
    <span class="pm">${esc(bits.join(' · '))}</span></li>`;
}

// --- research: the context and the ledger, edited in place -----------------
//
// context.md and ledger.yaml are the two inputs that most shape what
// extraction returns, and both used to need a text editor outside the app.
// One form edits both; Save writes both.

function ledgerRow(claim, i) {
  return `<div class="linkrow" data-ledger-row="${i}">
    <input name="ledger-id" value="${esc(claim.id || '')}" placeholder="L${i + 1}" size="6" aria-label="Claim id">
    <input name="ledger-text" value="${esc(claim.text || '')}" placeholder="One of my own claims, as a sentence" aria-label="Claim text">
    <button type="button" data-act="drop-ledger" title="Remove this claim">×</button>
  </div>`;
}

// The research form's edits live only in the DOM until Save, and every other
// redraw (a filter, a paper click, the topic list) rebuilds `#content`. Read
// the form into `V.researchDraft` before any of that, and draw from the draft
// when there is one, so navigating away and back finds the text as it was.
function captureResearchDraft() {
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
    <h2>What I am studying</h2>
    <p class="ps">The context goes into every model pass and is the main lever on the
      <em>why it is here</em> line. Your own claims are what a paper's claims are linked
      against: supports, contradicts, supplies a method for, refines.</p>
  </div>`;
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
  </form>`;
  $('content').innerHTML = html;
  if (main) main.scrollTop = scrollTop;
}

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
  if (!form.isConnected) return;   // a finished save has already left the view
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
    let warn = form.previousElementSibling;
    if (!warn || !warn.classList.contains('warn')) {
      form.insertAdjacentHTML('beforebegin', '<p class="warn"></p>');
      warn = form.previousElementSibling;
    }
    warn.textContent = V.error;
    return;
  } finally {
    V.researchSaving = false;
    setResearchSaving(form, false);
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

$('research-nav').addEventListener('click', (event) => {
  if (!event.target.closest('[data-view]')) return;
  // Clicking the item already shown is not navigation. Redrawing here would
  // rebuild the form from `S` and throw away whatever has been typed into it.
  if (V.view === 'research') return;
  showView('research');
  syncHash(true);
  renderAll();
});
