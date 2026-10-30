'use strict';

// Owns transport and corpus identity. Rendering and editor drafts stay with
// the UI; features receive this interface instead of reading app.js globals.
function createWorkspaceClient({ workspaceId = 'default', fetch: send = globalThis.fetch,
  rememberWorkspace = () => {}, onWorkspaceChange = () => {}, readTimeout = 15000 } = {}) {
  const emptyState = () => ({ papers: [], claims: [], tags: [], tag_counts: {}, label_counts: {}, label_keys: {},
    ledger: [], context: '', tensions: [], agreements: [], syntheses: [], kinds: [],
    strengths: [], relations: [], jobs: [], has_key: true });
  let state = emptyState(), workspaces = [], etag = null, epoch = 0;
  let pendingMutations = 0, pullsAsked = 0, pullSettled = 0;

  async function checkResponse(response) {
    if (!response.ok) {
      let detail = response.statusText;
      try { detail = (await response.json()).detail || detail; } catch (_) { /* keep statusText */ }
      if (Array.isArray(detail)) detail = [...new Set(detail.map(issue => issue.msg || 'Invalid input.'))].join(' ');
      throw Object.assign(new Error(detail), { status: response.status });
    }
    return response;
  }

  async function perform(path, options, read) {
    const headers = new Headers(options.headers || {});
    // Capture the destination before the first await. A deferred undo or save
    // may also explicitly name the workspace it originally belonged to.
    if (!headers.has('X-Doxograph-Workspace')) headers.set('X-Doxograph-Workspace', workspaceId);
    const mutating = !['GET', 'HEAD', 'OPTIONS'].includes((options.method || 'GET').toUpperCase());
    if (mutating) pendingMutations += 1;
    try { return await read(await checkResponse(await send(path, { ...options, headers }))); }
    finally { if (mutating) pendingMutations -= 1; }
  }

  const readJSON = async response => response.status === 204 ? null : await response.json();
  const request = (path, options = {}) => perform(path, options, readJSON);
  // Downloads keep their content type and headers, with the same destination
  // capture and error handling as JSON requests.
  const response = (path, options = {}) => perform(path, options, value => value);

  function select(id) {
    if (id !== workspaceId) {
      workspaceId = id;
      epoch += 1;
      etag = null;
      state = emptyState();
    }
    rememberWorkspace(id);
    onWorkspaceChange(id);
  }

  async function loadWorkspaces() {
    const response = await send('/api/workspaces');
    if (!response.ok) throw new Error('Could not load workspaces');
    workspaces = (await response.json()).workspaces || [];
    select(workspaces.some(w => w.id === workspaceId) ? workspaceId : 'default');
  }

  async function pull() {
    const requestedEpoch = epoch, asked = ++pullsAsked;
    const headers = new Headers({ 'X-Doxograph-Workspace': workspaceId });
    if (etag) headers.set('If-None-Match', etag);
    // A stalled request must not keep the poller's overlap guard held forever.
    // Include body reads in the deadline, and abort both requests on failure.
    const controller = new AbortController();
    let timeout;
    const deadline = new Promise((_, reject) => {
      timeout = setTimeout(() => {
        reject(new Error('The library refresh timed out.'));
        controller.abort();
      }, readTimeout);
    });
    let next, jobs;
    try {
      [next, jobs] = await Promise.race([Promise.all([
        (async () => {
          // ETags are managed here. Do not let the web view's HTTP cache serve
          // an older snapshot or import status instead of asking the server.
          const response = await send('/api/state', { headers, signal: controller.signal, cache: 'no-store' });
          return response.status === 304 ? null : {
            state: await readJSON(await checkResponse(response)), etag: response.headers.get('ETag'),
          };
        })(),
        request('/api/jobs', { signal: controller.signal, cache: 'no-store' }),
      ]), deadline]);
    } catch (error) {
      // A newer success or failure owns the sync status. An older failure
      // must not replace it, including after leaving and returning to a workspace.
      if (requestedEpoch !== epoch || asked < pullSettled) {
        return { applied: false, changed: false, registryChanged: false, workspaceChanged: requestedEpoch !== epoch };
      }
      pullSettled = asked;
      throw error;
    } finally {
      clearTimeout(timeout);
      controller.abort();
    }
    // The epoch also rejects a late answer after going A -> B -> A.
    if (requestedEpoch !== epoch || asked < pullSettled) {
      return { applied: false, changed: false, registryChanged: false, workspaceChanged: requestedEpoch !== epoch };
    }
    pullSettled = asked;
    let registryChanged = false;
    if (next) {
      state = { ...next.state, jobs: jobs.jobs || [], job_recovery_issues: jobs.recovery_issues || [] };
      // Commit only when both reads succeeded and this snapshot was applied.
      etag = next.etag;
      if (state.workspace && currentWorkspace()?.name !== state.workspace.name) {
        workspaces = workspaces.map(w => w.id === state.workspace.id ? state.workspace : w);
        registryChanged = true;
      }
    } else { state.jobs = jobs.jobs || []; state.job_recovery_issues = jobs.recovery_issues || []; }
    return { applied: true, changed: Boolean(next), registryChanged };
  }

  const currentWorkspace = () => workspaces.find(w => w.id === workspaceId) || null;
  return Object.freeze({
    get state() { return state; },
    get workspaceId() { return workspaceId; },
    get workspaces() { return workspaces; },
    get pendingMutations() { return pendingMutations; },
    request, response, pull, select, loadWorkspaces, currentWorkspace,
    setWorkspaces(value) { workspaces = value; },
    invalidateState() { etag = null; },
  });
}
