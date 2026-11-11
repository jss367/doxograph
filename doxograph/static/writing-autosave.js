/* Existing writing saves without closing or rebuilding its editor. Sessions
   belong to the workspace draft map, so retired workspace timers cannot write. */
const writingAutosaves = new Map();
function writingAutosave(kind, id = '') {
  const key = JSON.stringify([workspaceClient.workspaceId, kind, id]);
  const prior = writingAutosaves.get(key);
  if (prior?.owner === V.drafts) return prior.saver;
  const state = V, owner = V.drafts, workspace = workspaceClient.workspaceId, session = {};
  const draft = () => kind === 'claim' ? state.drafts[id]
    : kind === 'synthesis' ? state.synthDrafts[id] : state.researchDraft;
  const dirty = () => {
    const value = draft();
    if (value === undefined || value === null) return false;
    if (kind === 'claim') {
      const base = state.quitClaimBases[id];
      return !base || Object.keys(base).some(k => JSON.stringify(value[k]) !== JSON.stringify(base[k]));
    }
    if (kind === 'synthesis') return value !== state.quitSynthBases[id]?.text;
    return differs(value, state.researchBase || storedResearch());
  };
  const uncertain = new Set();
  const saver = createAutosave({
    current: () => V.drafts === owner && workspaceClient.workspaceId === workspace ? session : null,
    dirty: () => V.drafts === owner && dirty(),
    blocked: () => {
      if (kind === 'claim') {
        const row = S.claims.find(row => row.id === id);
        if (!row || isRemoving(row.paper) || trash.has('claim:' + id)) return 'This claim is being removed.';
        if (savingClaims.has(id)) return 'Another change is still saving. Try again.';
        if (!draft()?.text) return 'Give the claim some text.';
      }
      if (kind === 'synthesis' && trash.has('synthesis:' + id)) return 'This synthesis is being removed.';
      if (kind === 'research' && V.researchSaving) return 'Wait for the image upload, then try again.';
      return '';
    },
    async write() {
      const value = structuredClone(draft() ?? (kind === 'research' ? state.researchBase || storedResearch() : null));
      if (value === null) return;
      const headers = {'Content-Type':'application/json', 'X-Doxograph-Workspace':workspace};
      if (kind === 'claim') {
        const row = S.claims.find(row => row.id === id);
        const marker = beginManualQuitWrite('claim', id);
        const saved = await api(`/api/papers/${encodeURIComponent(row.paper)}/claims/${encodeURIComponent(id)}`, {
          method:'PATCH', headers, body:JSON.stringify(claimRequestBody(value)),
        });
        if (V.drafts !== owner) return;
        Object.assign(row, saved);
        state.quitClaimBases[id] = value;
        if (state.unconfirmedClaims[id] === marker) delete state.unconfirmedClaims[id];
        if (JSON.stringify(draft()) === JSON.stringify(value)) delete state.drafts[id];
      } else if (kind === 'synthesis') {
        const settled = await settleDeletes();
        if (!settled || V.drafts !== owner) throw new Error('Could not finish pending changes. Your writing is kept.');
        const marker = beginManualQuitWrite('synthesis', id);
        await api(`/api/syntheses/${encodeURIComponent(id)}`, {method:'PATCH', headers, body:JSON.stringify({text:value})});
        if (V.drafts !== owner) return;
        const row = synthesisFor(id); if (row) row.text = value;
        state.quitSynthBases[id] = {text:value};
        if (state.unconfirmedSyntheses[id] === marker) delete state.unconfirmedSyntheses[id];
        if (draft() === value) delete state.synthDrafts[id];
      } else {
        const base = state.researchBase || storedResearch();
        // Acknowledging each field independently also handles a partial failure.
        if (uncertain.has('claims') || JSON.stringify(value.claims) !== JSON.stringify(base.claims)) {
          uncertain.add('claims');
          await api('/api/ledger', {method:'PUT', headers, body:JSON.stringify({claims:value.claims})});
          if (V.drafts !== owner) return;
          uncertain.delete('claims');
          S.ledger = value.claims;
          state.researchBase = {...base, claims:value.claims};
        }
        if (uncertain.has('context') || value.context.trim() !== base.context.trim()) {
          uncertain.add('context');
          await api('/api/context', {method:'PUT', headers, body:JSON.stringify({text:value.context})});
          if (V.drafts !== owner) return;
          uncertain.delete('context');
          S.context = value.context;
          state.researchBase = {...(state.researchBase || base), context:value.context};
        }
        if (draft() && !differs(draft(), value)) state.researchDraft = null;
      }
    },
    changed() {
      if (V.drafts !== owner) return;
      if (!dirty() && !saver.saving && !saver.unconfirmed) {
        if (kind === 'claim') delete state.drafts[id];
        if (kind === 'synthesis') delete state.synthDrafts[id];
      }
      document.querySelectorAll('[data-writing-status]').forEach(el => {
        if (el.dataset.writingStatus === kind && (el.dataset.writingId || '') === id) {
          el.textContent = saver.describe(); el.className = saver.failure ? 'warn' : 'hint';
        }
      });
    },
  });
  writingAutosaves.set(key, {owner, saver});
  return saver;
}
function writingStatus(kind, id = '') {
  const saver = writingAutosave(kind, id);
  return `<span role="status" class="${saver.failure ? 'warn' : 'hint'}" data-writing-status="${kind}" data-writing-id="${esc(id)}">${esc(saver.describe())}</span>`;
}
async function flushWriting() {
  workspaceWritingSnapshot();
  for (const id of Object.keys(V.drafts)) writingAutosave('claim', id);
  for (const id of Object.keys(V.synthDrafts)) writingAutosave('synthesis', id);
  if (V.researchDraft) writingAutosave('research');
  return (await Promise.all([...writingAutosaves.values()].filter(entry => entry.owner === V.drafts).map(entry => entry.saver.flush()))).every(Boolean);
}
function scheduleWriting(event) {
  const scoped = event.target.closest('[data-workspace]');
  if (scoped && scoped.dataset.workspace !== workspaceClient.workspaceId) return;
  const form = event.target.closest('form[data-form]');
  if (form && form.dataset.form !== NEW_CLAIM_ID) {
    const id = form.dataset.form;
    V.drafts[id] = readForm(form);
    writingAutosave('claim', id).schedule();
  }
  if (event.target.matches('textarea[data-synth]')) {
    V.synthDrafts[event.target.dataset.synth] = event.target.value;
    writingAutosave('synthesis', event.target.dataset.synth).schedule();
  }
  if (event.target.closest('#research-form')) {
    captureResearchDraft(); writingAutosave('research').schedule();
  }
}
document.addEventListener('input', scheduleWriting);
document.addEventListener('change', scheduleWriting);
window.addEventListener('beforeunload', event => {
  workspaceWritingSnapshot();
  if (hasQuitWriting() || [...writingAutosaves.values()].some(entry => entry.owner === V.drafts && (entry.saver.saving || entry.saver.unconfirmed))) {
    void flushWriting(); event.preventDefault(); event.returnValue = '';
  }
});

function writingInFlight(kind, id) {
  return [...writingAutosaves.entries()].some(([key, entry]) => entry.owner === V.drafts && entry.saver.saving
    && (!kind || key === JSON.stringify([workspaceClient.workspaceId, kind, id])));
}
function writingUnconfirmed(kind, id) {
  return [...writingAutosaves.entries()].some(([key, entry]) => entry.owner === V.drafts && entry.saver.unconfirmed
    && (!kind || key === JSON.stringify([workspaceClient.workspaceId, kind, id])));
}
