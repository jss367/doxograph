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
    current: () => V.drafts === owner && workspaceClient.workspaceId === workspace
      && writingAutosaves.get(key)?.saver === saver ? session : null,
    dirty: () => V.drafts === owner && dirty(),
    blocked: () => {
      session.waitingForClaimMutation = false;
      if (kind === 'claim') {
        const row = S.claims.find(row => row.id === id);
        if (!row || trash.has('claim:' + id)) return 'This claim is being removed.';
        if (isRemoving(row.paper) || savingClaims.has(id)) {
          session.waitingForClaimMutation = true;
          return isRemoving(row.paper) ? 'This paper is being removed.' : 'Another change is still saving. Try again.';
        }
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
        if (V.drafts !== owner || workspaceClient.workspaceId !== workspace) return;
        const currentRow = S.claims.find(item => item.id === id && item.paper === row.paper);
        if (currentRow === row) Object.assign(currentRow, saved);
        else {
          // A replacement may predate this write or contain a newer edit.
          // The PATCH response cannot order those snapshots, so reread instead
          // of overwriting a newer corpus under its still-current ETag.
          const beforeRead = workspaceClient.pullSequence;
          workspaceClient.invalidateState();
          await pull();
          if (V.drafts !== owner || workspaceClient.workspaceId !== workspace) return;
          // A newer failed read can supersede this read without applying it.
          // Accept only this read or a successful read requested after it.
          if (workspaceClient.appliedPullSequence <= beforeRead) {
            throw new Error('Could not refresh the saved claim. Your writing is kept.');
          }
        }
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
  writingAutosaves.set(key, {owner, saver, session});
  return saver;
}
/** Re-arm a save deferred by a review or another claim mutation. */
function resumeClaimWriting(id) {
  const entry = writingAutosaves.get(JSON.stringify([workspaceClient.workspaceId, 'claim', id]));
  const row = S.claims.find(row => row.id === id);
  if (entry?.owner !== V.drafts || !entry.session.waitingForClaimMutation || savingClaims.has(id)
    || !row || isRemoving(row.paper) || trash.has('claim:' + id)) return;
  entry.session.waitingForClaimMutation = false;
  entry.saver.schedule();
}
/** Identify the current autosave so deletion cannot retire a newer session. */
function writingSessionState(kind, id) {
  const entry = writingAutosaves.get(JSON.stringify([workspaceClient.workspaceId, kind, id]));
  return entry?.owner === V.drafts ? {entry, generation:entry.saver.generation} : null;
}
function retireWritingSession(kind, id, seen) {
  if (!seen) return;
  const key = JSON.stringify([workspaceClient.workspaceId, kind, id]);
  const entry = writingAutosaves.get(key);
  if (entry === seen.entry && entry.owner === V.drafts && entry.saver.generation === seen.generation && !entry.saver.saving) {
    writingAutosaves.delete(key);
  }
}
/** Finish an editor, asking before abandoning a failed save and its retry state. */
async function finishWriting(kind, id, current) {
  const owner = V.drafts, workspace = workspaceClient.workspaceId;
  const key = JSON.stringify([workspace, kind, id]);
  const saver = writingAutosave(kind, id);
  const active = () => V.drafts === owner && workspaceClient.workspaceId === workspace
    && writingAutosaves.get(key)?.saver === saver && current();
  if (await saver.flush()) return active() ? 'saved' : false;
  if (!active()) return false;
  await saver.settle();
  const generation = saver.generation;
  const discard = await confirmDialog('This writing could not be saved. Discard unsaved changes?', {
    note: saver.failure, ok: 'Discard changes',
  });
  if (!discard || !active() || saver.generation !== generation) { saver.resume(); return false; }
  // Retire the saver as well as the draft: a failed request can otherwise
  // leave an unconfirmed write that a later quit or reopened editor retries.
  writingAutosaves.delete(key);
  if (kind === 'claim') {
    delete V.drafts[id]; delete V.quitClaimBases[id]; delete V.unconfirmedClaims[id];
  } else if (kind === 'synthesis') {
    delete V.synthDrafts[id]; delete V.quitSynthBases[id]; delete V.unconfirmedSyntheses[id];
  } else {
    V.researchDraft = null; V.researchBase = storedResearch();
  }
  return 'discarded';
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
