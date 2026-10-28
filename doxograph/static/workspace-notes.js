/* The workspace's running overview, kept outside the claim list so browsing
   and background refreshes leave the editor, focus, and caret intact. */
const WorkspaceNotes = (() => {
  let client, draft = null, timer = null, saving = null, reloading = false, upload = null, previewTimer = null;
  let mode = 'write';
  let scope = {view:'claims', paper:null}, forced = false, expanded = true, keep = false;
  const el = id => document.getElementById(id);
  const changed = () => draft && draft.text !== draft.saved;
  const preferenceKey = () => `doxograph-workspace-notes:${draft.workspace}`;
  function remember() {
    try { localStorage.setItem(preferenceKey(), JSON.stringify({expanded, keep, mode})); } catch (_) { /* optional */ }
  }
  function update() {
    if (!draft) return;
    const panel = el('workspace-notes-panel');
    panel.hidden = !(scope.view === 'claims' && (!scope.paper || keep))
      && !forced && !changed() && !draft.error && !saving && !upload;
    document.querySelector('.research-tools-nav [data-tool="notes"]').setAttribute('aria-expanded', String(!panel.hidden));
    const parent = el(scope.view === 'claims' && scope.paper ? 'workspace-notes-reading' : 'workspace-notes-home');
    if (panel.parentElement !== parent) parent.append(panel);
    el('workspace-notes-editor').hidden = !expanded;
    el('workspace-notes-preview').hidden = expanded;
    el('workspace-notes-preview').textContent = draft.text.trim() || 'Capture your questions, working conclusions, and next steps.';
    // A focusout sync can run between pointerdown and click. Replacing this
    // button's text node then cancels the click in WebKit.
    const toggle = el('workspace-notes-toggle'), label = expanded ? 'Collapse notes' : 'Edit notes';
    if (toggle.textContent !== label) toggle.textContent = label;
    el('workspace-notes-toggle').setAttribute('aria-expanded', String(expanded));
    el('workspace-notes-keep').checked = keep;
    el('workspace-notes').disabled = reloading;
    el('workspace-notes-pages').dataset.mode = mode;
    el('workspace-notes-view').value = mode;
    el('workspace-notes-add-image').disabled = Boolean(upload || reloading);
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
      expanded = true; keep = false; forced = false; mode = 'write';
      el('workspace-notes-image-status').hidden = true;
      try {
        const prefs = JSON.parse(localStorage.getItem(preferenceKey()) || '{}');
        expanded = prefs.expanded !== false; keep = prefs.keep === true;
        if (['write', 'split', 'preview'].includes(prefs.mode)) mode = prefs.mode;
      } catch (_) { /* defaults work without browser storage */ }
      el('workspace-notes').value = draft.text;
      renderPreview();
    } else if (!changed() && !saving && !reloading && document.activeElement !== el('workspace-notes')
      && snapshot.revision > draft.revision) {
      // A poll started before our save may finish after it. Only a newer
      // notebook revision may replace clean text, even after focus moves.
      draft.text = draft.saved = snapshot.notes;
      draft.revision = snapshot.revision;
      el('workspace-notes').value = draft.text;
      renderPreview();
    }
    update();
  }
  async function flush() {
    clearTimeout(timer);
    if (upload) await upload;
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
    })().finally(() => { saving = null; sync(); });
    update();
    return saving;
  }
  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(flush, 600);
    clearTimeout(previewTimer);
    previewTimer = setTimeout(renderPreview, 180);
  }
  function renderPreview() {
    if (draft) el('workspace-notes-rich-preview').replaceChildren(NoteWriting.render(draft.text, draft.workspace));
  }
  function insert(before, after = '', fallback = '') {
    const field = el('workspace-notes');
    if (mode === 'preview') { mode = 'split'; remember(); }
    const start = field.selectionStart, end = field.selectionEnd;
    field.setRangeText(before + (field.value.slice(start, end) || fallback) + after, start, end, 'end');
    draft.text = field.value;
    update(); schedule(); renderPreview(); field.focus();
  }
  async function uploadImage(file) {
    if (upload || reloading || draft?.workspace !== client.workspaceId) return;
    const status = el('workspace-notes-image-status'), current = draft;
    status.hidden = false; status.className = 'hint'; status.textContent = 'Uploading image…';
    upload = (async () => {
      try {
        if (file.size > 10 * 1024 * 1024) throw new Error('Choose an image smaller than 10 MB.');
        const form = new FormData(); form.append('file', file);
        const result = await client.request('/api/research-notes/images', {method:'POST',
          headers:{'X-Doxograph-Workspace':current.workspace}, body:form});
        if (draft !== current) return;
        insert(`\n![${(file.name || 'Figure').replace(/[\[\]\\\r\n]/g, '')}](${result.url})\n`);
        status.textContent = ''; status.hidden = true;
      } catch (error) { status.className = 'warn'; status.textContent = error.message; }
    })().finally(() => { upload = null; update(); });
    update();
    return upload;
  }
  async function reload() {
    if (reloading) return;
    clearTimeout(timer);
    reloading = true; update();
    try {
      if (upload) await upload;
      if (saving) await saving;
      if (changed() && !await confirmDialog('Discard unsaved workspace notes and reload the saved version?', {ok:'Discard changes'})) return;
      const saved = await client.request('/api/notebook', {headers:{'X-Doxograph-Workspace':draft.workspace}});
      draft.text = draft.saved = saved.notes;
      draft.revision = saved.revision;
      draft.error = '';
      el('workspace-notes').value = draft.text;
      renderPreview();
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
    if (mode === 'preview') mode = 'write';
    if (scope.paper && scope.view === 'claims') keep = true;
    remember(); update();
    el('workspace-notes-panel').scrollIntoView({block:'nearest'});
    el('workspace-notes').focus({preventScroll:true});
  }
  function reveal() {
    forced = true; expanded = true; update();
    el('workspace-notes-panel').scrollIntoView({block:'nearest'});
  }
  document.addEventListener('input', event => {
    if (event.target.id !== 'workspace-notes' || draft?.workspace !== client.workspaceId) return;
    draft.text = event.target.value;
    update(); schedule();
  });
  document.addEventListener('focusout', event => {
    // Polls may already have fetched a newer clean note while this field was
    // focused. Reconsider that snapshot even if the next poll returns 304.
    if (event.target.id === 'workspace-notes') queueMicrotask(() => sync());
  });
  document.addEventListener('change', event => {
    if (event.target.id === 'workspace-notes-view') {
      mode = event.target.value; remember(); update(); renderPreview();
      return;
    }
    if (event.target.id === 'workspace-notes-image' && event.target.files[0]) uploadImage(event.target.files[0]);
    if (event.target.id !== 'workspace-notes-keep') return;
    keep = event.target.checked;
    if (!keep) forced = false;
    remember(); update();
  });
  document.addEventListener('click', async event => {
    const format = event.target.closest('[data-workspace-format]');
    if (format && !reloading) insert(...NoteWriting.formats[format.dataset.workspaceFormat]);
    const id = event.target.closest('button')?.id;
    if (id === 'workspace-notes-add-image' && !upload && !reloading) {
      const picker = el('workspace-notes-image'); picker.value = ''; picker.click();
    }
    if (id === 'workspace-notes-toggle') {
      if (expanded) {
        el('workspace-notes').blur(); sync();
        if (!await flush()) return;
      }
      expanded = !expanded;
      if (expanded && mode === 'preview') mode = 'write';
      remember(); update();
      if (expanded) el('workspace-notes').focus();
    }
    if (id === 'workspace-notes-retry') await flush();
    if (id === 'workspace-notes-reload') await reload();
  });
  window.addEventListener('beforeunload', event => {
    if (changed() || saving || upload) { event.preventDefault(); event.returnValue = ''; }
  });
  document.addEventListener('keydown', event => {
    if (event.target.closest?.('#workspace-notes-panel') && (event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's') {
      event.preventDefault(); flush();
    }
  });
  document.addEventListener('DOMContentLoaded', () => {
    const panel = el('workspace-notes-panel');
    for (const name of ['dragenter', 'dragover', 'dragleave', 'drop']) panel.addEventListener(name, event => {
      const image = [...(event.dataTransfer?.items || [])].some(item => item.kind === 'file' && item.type.startsWith('image/'));
      if (!image) return;
      event.stopPropagation(); event.preventDefault();
      if (name === 'drop') {
        const file = [...event.dataTransfer.files].find(file => file.type.startsWith('image/'));
        if (file) uploadImage(file);
      }
    });
    panel.addEventListener('paste', event => {
      const file = [...(event.clipboardData?.files || [])].find(file => file.type.startsWith('image/'));
      if (file) { event.preventDefault(); event.stopPropagation(); uploadImage(file); }
    });
  });
  return {connect(value) { client = value; }, sync, open, flush, reveal,
    hasUnsavedChanges: () => Boolean(changed() || saving || upload || reloading),
    async beforeWorkspaceSwitch() {
      if (await flush()) return true;
      reveal();
      return false;
    }};
})();
