'use strict';

(() => {
  const dialog = $('workspace-backups');
  let owner = null, versions = [], busy = false;
  function download(path, workspace) {
    const anchor = document.createElement('a');
    anchor.href = `${path}?${new URLSearchParams({workspace})}`;
    anchor.download = '';
    document.body.append(anchor); anchor.click(); anchor.remove();
  }
  function title(version) {
    if (version.path === 'notebook.json') return 'Workspace notes and research tools';
    if (version.path === 'research-notes.json') return 'Research notes';
    if (version.path === 'context.md') return 'Research context';
    if (version.path.startsWith('papers/')) {
      const key = version.path.slice(7, -5);
      return S.papers.find(p => p.key === key)?.title || 'Paper: ' + key;
    }
    return 'Research metadata: ' + version.path;
  }
  async function load() {
    const requested = owner;
    const result = await api('/api/workspace/backups', {headers:{'X-Doxograph-Workspace':requested}});
    if (owner !== requested) return;
    versions = result.versions;
    $('backups-status').textContent = result.issues.join(' ');
    $('backup-versions').innerHTML = versions.map(v => `<article class="backup-version">
      <div><strong>${esc(title(v))}</strong><p class="hint">${esc(new Date(v.created).toLocaleString())}</p></div>
      <div class="row"><button type="button" data-backup-download="${esc(v.id)}">Download version</button>
      ${v.restorable ? `<button type="button" data-backup-restore="${esc(v.id)}">Restore version</button>` : ''}</div>
    </article>`).join('') || '<p class="hint">No earlier versions yet. Edits will create them automatically.</p>';
  }
  $('btn-workspace-archive').addEventListener('click', async () => {
    const button = $('btn-workspace-archive'), workspace = workspaceClient.workspaceId;
    button.disabled = true;
    toast('Preparing the workspace archive…');
    try {
      if (!await WorkspaceNotes.flush()) { WorkspaceNotes.reveal(); return; }
      const result = await api('/api/workspace/archive', {method:'POST', headers:{'X-Doxograph-Workspace':workspace}});
      download(result.download, workspace);
    } catch (error) { toast(`Could not prepare the archive: ${error.message}`, {tone:'warn'}); }
    finally { button.disabled = false; }
  });
  $('btn-workspace-backups').addEventListener('click', async () => {
    if (!await WorkspaceNotes.flush()) { WorkspaceNotes.reveal(); return; }
    owner = workspaceClient.workspaceId;
    $('backup-versions').replaceChildren();
    $('backups-status').textContent = 'Loading previous versions…';
    dialog.showModal();
    try { await load(); }
    catch (error) { $('backups-status').textContent = error.message; }
  });
  $('backups-close').addEventListener('click', () => { if (!busy) dialog.close(); });
  dialog.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
  dialog.addEventListener('click', async event => {
    if (busy) return;
    const target = event.target.closest('[data-backup-download], [data-backup-restore]');
    if (!target) return;
    if (owner !== workspaceClient.workspaceId) { dialog.close(); return; }
    const version = versions.find(v => v.id === (target.dataset.backupDownload || target.dataset.backupRestore));
    if (!version) return;
    if (target.dataset.backupDownload) { download(`/api/workspace/backups/${version.id}`, owner); return; }
    if (!canRestoreSavedVersion()) {
      $('backups-status').textContent = 'Save or cancel unsaved edits and let pending changes finish before restoring a saved version.';
      return;
    }
    busy = true;
    try {
      if (!await confirmDialog(`Restore the earlier version of “${title(version)}”? Its current saved version will be kept as a backup.`, {ok:'Restore version'})) return;
      if (!canRestoreSavedVersion()) {
        $('backups-status').textContent = 'The workspace has unsaved or pending changes. Save or cancel them before restoring.';
        return;
      }
      await api(`/api/workspace/backups/${version.id}/restore`, {method:'POST',
        headers:{'Content-Type':'application/json', 'X-Doxograph-Workspace':owner}, body:JSON.stringify({expected:version.expected})});
      workspaceClient.invalidateState();
      await refreshAll();
      await load();
      $('backups-status').textContent = 'Restored. The version replaced is also available here.';
    } catch (error) { $('backups-status').textContent = error.message; }
    finally { busy = false; }
  });
})();
