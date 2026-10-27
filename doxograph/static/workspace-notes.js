/* The workspace's running overview, kept outside the claim list so browsing
   and background refreshes leave the editor, focus, and caret intact. */
const WorkspaceNotes = (() => {
  let client, draft = null, timer = null, saving = null, reloading = false;
  let scope = {view:'claims', paper:null}, forced = false, expanded = true, keep = false;
  const el = id => document.getElementById(id);
  const changed = () => draft && draft.text !== draft.saved;
  const preferenceKey = () => `doxograph-workspace-notes:${draft.workspace}`;
  function remember() {
    try { localStorage.setItem(preferenceKey(), JSON.stringify({expanded, keep})); } catch (_) { /* optional */ }
  }
  function update() {
    if (!draft) return;
    const panel = el('workspace-notes-panel');
    panel.hidden = !(scope.view === 'claims' && (!scope.paper || keep))
      && !forced && !changed() && !draft.error && !saving;
    const parent = el(scope.view === 'claims' && scope.paper ? 'workspace-notes-reading' : 'workspace-notes-home');
    if (panel.parentElement !== parent) parent.append(panel);
    el('workspace-notes-editor').hidden = !expanded;
    el('workspace-notes-preview').hidden = expanded;
    el('workspace-notes-preview').textContent = draft.text.trim() || 'Capture your questions, working conclusions, and next steps.';
    el('workspace-notes-toggle').textContent = expanded ? 'Collapse notes' : 'Edit notes';
    el('workspace-notes-toggle').setAttribute('aria-expanded', String(expanded));
    el('workspace-notes-keep').checked = keep;
    el('workspace-notes').disabled = reloading;
    const status = el('workspace-notes-status');
    status.textContent = draft.error ? `Couldn’t save. ${draft.error}`
      : reloading ? 'Reloading…' : saving ? 'Saving…' : changed() ? 'Waiting to save…' : 'Saved';
    status.className = draft.error ? 'warn' : 'hint';
    el('workspace-notes-retry').hidden = !draft.error;
    el('workspace-notes-reload').hidden = !draft.error;
    el('workspace-notes-retry').disabled = Boolean(saving || reloading);
    el('workspace-notes-reload').disabled = reloading;
  }
  function sync(nextScope = scope) {
    if (nextScope.view !== scope.view || nextScope.paper !== scope.paper) forced = false;
    scope = nextScope;
    const snapshot = client.state.notebook;
    if (!snapshot || typeof snapshot.notes !== 'string') {
      el('workspace-notes-panel').hidden = true;
      return;
    }
    if (draft?.workspace !== client.workspaceId) {
      clearTimeout(timer);
      draft = {workspace:client.workspaceId, text:snapshot.notes, saved:snapshot.notes, revision:snapshot.revision, error:''};
      expanded = true; keep = false; forced = false;
      try {
        const prefs = JSON.parse(localStorage.getItem(preferenceKey()) || '{}');
        expanded = prefs.expanded !== false; keep = prefs.keep === true;
      } catch (_) { /* defaults work without browser storage */ }
      el('workspace-notes').value = draft.text;
    } else if (!changed() && !saving && !reloading && document.activeElement !== el('workspace-notes')
      && snapshot.revision > draft.revision) {
      // A poll started before our save may finish after it. Only a newer
      // notebook revision may replace clean text, even after focus moves.
      draft.text = draft.saved = snapshot.notes;
      draft.revision = snapshot.revision;
      el('workspace-notes').value = draft.text;
    }
    update();
  }
  async function flush() {
    clearTimeout(timer);
    if (reloading) return false;
    if (saving) return saving;
    if (!changed()) return true;
    const current = draft;
    current.error = '';
    saving = (async () => {
      // New text typed during a request is saved next, to the same workspace.
      while (current.text !== current.saved) {
        const text = current.text;
        try {
          const saved = await client.request('/api/notebook/notes', {method:'PUT',
            headers:{'Content-Type':'application/json', 'X-Doxograph-Workspace':current.workspace},
            body:JSON.stringify({notes:text, previous:current.saved})});
          current.saved = text;
          current.revision = saved.revision;
          client.invalidateState();
        } catch (error) {
          current.error = error.message;
          expanded = true;
          return false;
        }
      }
      return true;
    })().finally(() => { saving = null; update(); });
    update();
    return saving;
  }
  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(flush, 600);
  }
  async function reload() {
    if (reloading) return;
    clearTimeout(timer);
    reloading = true; update();
    try {
      if (saving) await saving;
      if (changed() && !await confirmDialog('Discard unsaved workspace notes and reload the saved version?', {ok:'Discard changes'})) return;
      const saved = await client.request('/api/notebook', {headers:{'X-Doxograph-Workspace':draft.workspace}});
      draft.text = draft.saved = saved.notes;
      draft.revision = saved.revision;
      draft.error = '';
      el('workspace-notes').value = draft.text;
      client.invalidateState();
    } catch (error) { draft.error = error.message; }
    finally {
      reloading = false; update();
      if (changed() && !draft.error) schedule();
    }
  }
  function open() {
    sync();
    if (draft?.workspace !== client.workspaceId || typeof client.state.notebook?.notes !== 'string') return;
    forced = true; expanded = true;
    if (scope.paper && scope.view === 'claims') keep = true;
    remember(); update();
    el('workspace-notes-panel').scrollIntoView({block:'nearest'});
    el('workspace-notes').focus({preventScroll:true});
  }
  document.addEventListener('input', event => {
    if (event.target.id !== 'workspace-notes' || draft?.workspace !== client.workspaceId) return;
    draft.text = event.target.value;
    update(); schedule();
  });
  document.addEventListener('change', event => {
    if (event.target.id !== 'workspace-notes-keep') return;
    keep = event.target.checked;
    if (!keep) forced = false;
    remember(); update();
  });
  document.addEventListener('click', async event => {
    const id = event.target.closest('button')?.id;
    if (id === 'workspace-notes-toggle') {
      if (expanded && !await flush()) return;
      expanded = !expanded; remember(); update();
      if (expanded) el('workspace-notes').focus();
    }
    if (id === 'workspace-notes-retry') await flush();
    if (id === 'workspace-notes-reload') await reload();
  });
  window.addEventListener('beforeunload', event => {
    if (changed() || saving) { event.preventDefault(); event.returnValue = ''; }
  });
  return {connect(value) { client = value; }, sync, open, flush,
    async beforeWorkspaceSwitch() {
      if (await flush()) return true;
      forced = true; expanded = true; update();
      el('workspace-notes-panel').scrollIntoView({block:'nearest'});
      return false;
    }};
})();
