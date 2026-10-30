'use strict';

// Workspace management and corpus-wide actions: new topics, retagging,
// and exports.

$('workspace').addEventListener('change', (event) => switchWorkspace(event.target.value));

let deletingWorkspace = false;
$('btn-workspace-delete').addEventListener('click', async () => {
  const workspace = currentWorkspace();
  if (!workspace || workspace.id === 'default' || deletingWorkspace || workspaceController.pending) return;
  const confirmed = await confirmDialog(`Delete “${workspace.name}”?`, {
    note: 'This permanently deletes this workspace and all of its papers, PDFs, claims, notes, and exports, including unsaved edits. This cannot be undone. You will return to the default workspace.',
    ok: 'Delete workspace',
  });
  if (!confirmed || workspace.id !== workspaceClient.workspaceId) return;
  if (workspaceClient.pendingMutations) {
    toast('Wait for the current change to finish before deleting this workspace.', { tone: 'warn' });
    return;
  }
  deletingWorkspace = true;
  // Keep editors and navigation still until the destructive request settles.
  // On failure, the current view and all drafts remain available.
  document.body.inert = true;
  const notice = toast('Deleting workspace…', { timeout: 0 });
  try {
    await api(`/api/workspaces/${encodeURIComponent(workspace.id)}`, { method: 'DELETE' });
    workspaceClient.select('default');
    // A fresh page also retires autosave timers, held paper deletions, and
    // feature editors whose data belonged to the removed corpus.
    // Deletion already confirmed discarding these editors' unsaved changes.
    window.addEventListener('beforeunload', event => event.stopImmediatePropagation(), { capture: true, once: true });
    location.replace('/#ws=default');
    location.reload();
  } catch (error) {
    toast(`Could not delete workspace: ${error.message}`, { tone: 'warn' });
  } finally {
    notice.close();
    document.body.inert = false;
    deletingWorkspace = false;
  }
});

$('btn-workspace-rename').addEventListener('click', async () => {
  const workspace = currentWorkspace();
  if (!workspace) return;
  const name = await askDialog({
    title: 'Rename workspace',
    note: 'Your papers, claims, and notes stay in this workspace.',
    input: { value: workspace.name }, ok: 'Rename',
  });
  if (name === null || name === workspace.name) return;
  try {
    const result = await api(`/api/workspaces/${encodeURIComponent(workspace.id)}`, {
      method: 'PATCH', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    workspaceClient.setWorkspaces(result.workspaces);
    renderWorkspacePicker();
    await refresh();
  } catch (error) {
    toast(`Could not rename workspace: ${error.message}`, { tone: 'warn' });
  }
});

$('btn-workspace-add').addEventListener('click', async () => {
  const name = await askText('Name this workspace', {
    note: 'A workspace is an independent corpus: its own papers, topics, and exports.',
    placeholder: 'Consciousness, or Animal locomotion',
  });
  if (!name || !name.trim()) return;
  try {
    const result = await api('/api/workspaces', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name }),
    });
    workspaceClient.setWorkspaces(result.workspaces || workspaceClient.workspaces);
    renderWorkspacePicker();
    await switchWorkspace(result.workspace.id);
  } catch (error) {
    toast(`Could not create workspace: ${error.message}`, { tone: 'warn' });
  }
});

$('btn-tag').addEventListener('click', async () => {
  const name = $('new-tag').value.trim();
  if (!name) return;
  try {
    await api('/api/tags', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, description: '' }),
    });
  } catch (error) {
    // The name stays in the box, as a refused reference list does.
    toast(`Could not add the topic: ${error.message}`, { tone: 'warn' });
    return;
  }
  $('new-tag').value = '';
  await refresh();
});

$('btn-retag').addEventListener('click', async () => {
  const go = await confirmDialog('Reassign topics on every paper?', {
    note: 'Every paper with claims is sent to the model again against the current vocabulary. '
      + 'Claim text you have edited is left alone.',
    ok: 'Retag all',
  });
  if (!go) return;
  const workspace = await settleDeletes();
  if (!workspace) return;   // the delete failed; the pass would read the claim
  try {
    await api('/api/retag', {
      method: 'POST', headers: { 'Content-Type': 'application/json', ...workspace }, body: '{}',
    });
  } catch (error) {
    toast(`Could not retag the papers: ${error.message}`, { tone: 'warn' });
    return;
  }
  await refresh();
});

$('btn-export').addEventListener('click', async () => {
  const selected = currentWorkspace();
  // As with the review undo: the notice can outlive the picker, and Open must
  // hand back the file this export wrote, not whatever the workspace selected
  // by then last exported.
  // The file is written from what is on file, so a delete still waiting out
  // its notice would otherwise be exported as a live claim.
  const headers = await settleDeletes();
  // The delete failed, so the claim is still on file and the export would
  // carry it. The notice about the delete is the one to answer first.
  if (!headers) return;
  const workspace = headers['X-Doxograph-Workspace'] || 'default';
  let result;
  try {
    result = await api('/api/export', {
      method: 'POST', headers: { 'Content-Type': 'application/json', ...headers },
      body: JSON.stringify({ title: selected ? selected.name : 'Doxograph' }),
    });
  } catch (error) {
    toast(`Could not export: ${error.message}`, { tone: 'warn' });
    return;
  }
  // The path on its own is something to go and find. What the reader wants is
  // the file, or the path where they can paste it.
  toast(`Exported to ${result.path}`, {
    timeout: 12000,
    actions: [
      { label: 'Open', onClick: () => window.open(`/export?workspace=${encodeURIComponent(workspace)}`, '_blank') },
      { label: 'Copy path', onClick: () => copyText(result.path, 'Path copied.') },
    ],
  });
});

async function copyText(text, done) {
  try {
    await navigator.clipboard.writeText(text);
    toast(done, { timeout: 2500 });
  } catch (error) {
    toast(`Could not copy: ${error.message}`, { tone: 'warn' });
  }
}

$('btn-bib').addEventListener('click', () => window.open(`/api/bibtex?${workspaceQuery()}`, '_blank'));
