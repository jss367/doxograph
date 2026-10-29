const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');

// Load the same dependency-free scripts served to the browser, without a DOM.
function load(file, factory) {
  const source = readFileSync(resolve(__dirname, '../../doxograph/static', file), 'utf8');
  return new Function(`${source}\nreturn ${factory};`)();
}
const createClient = load('workspace-client.js', 'createWorkspaceClient');
const createController = load('workspace-controller.js', 'createWorkspaceController');
const createPoller = load('poller.js', 'createPoller');
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};
const json = (body, etag = 'revision') => new Response(JSON.stringify(body), {
  headers: { 'Content-Type': 'application/json', ETag: etag },
});
const tick = () => new Promise(resolve => setImmediate(resolve));

test('writes retain their captured workspace and release the busy count on failure', async () => {
  const held = deferred(), calls = [];
  const client = createClient({ workspaceId: 'first', fetch: async (path, options) => {
    calls.push(options.headers.get('X-Doxograph-Workspace'));
    return (await held.promise).clone();
  } });
  const saving = client.request('/api/papers/shared', { method: 'PATCH' });
  assert.equal(client.pendingMutations, 1);
  client.select('second');
  const undo = client.request('/api/papers/shared', {
    method: 'DELETE', headers: { 'x-doxograph-workspace': 'first' },
  });
  assert.deepEqual(calls, ['first', 'first']);
  assert.equal(client.pendingMutations, 2);
  held.resolve(new Response(JSON.stringify({ detail: 'Disk full' }), { status: 503 }));
  await assert.rejects(saving, /Disk full/);
  await assert.rejects(undo, /Disk full/);
  assert.equal(client.pendingMutations, 0);
});

test('a failed jobs read cannot commit the ETag of an unseen state', async () => {
  const headers = [];
  let fail = true, unchanged = false;
  const client = createClient({ fetch: async (path, options) => {
    if (path === '/api/jobs') {
      if (fail) throw new Error('Jobs unavailable');
      return json({ jobs: [{ id: 1 }] });
    }
    headers.push(options.headers.get('If-None-Match'));
    return unchanged ? new Response(null, { status: 304 }) : json({ papers: ['saved'] }, 'saved-tag');
  } });
  await assert.rejects(client.pull(), /Jobs unavailable/);
  assert.deepEqual(client.state.papers, []);
  fail = false;
  assert.equal((await client.pull()).changed, true);
  const shown = client.state;
  unchanged = true;
  assert.equal((await client.pull()).changed, false);
  assert.equal(client.state, shown);
  assert.deepEqual(client.state.jobs, [{ id: 1 }]);
  assert.deepEqual(headers, [null, null, 'saved-tag']);
  client.invalidateState();
  await client.pull();
  assert.equal(headers.at(-1), null);
});

for (const stalledPath of ['/api/state', '/api/jobs']) {
  for (const stage of ['headers', 'body']) {
    test(`a stalled ${stalledPath} ${stage} read times out and polling recovers`, async () => {
      let stall = true;
      const signals = [], tags = [];
      const client = createClient({ readTimeout: 20, fetch: async (path, options) => {
        assert.equal(options.cache, 'no-store');
        signals.push(options.signal);
        if (path === '/api/state') tags.push(options.headers.get('If-None-Match'));
        if (stall && path === stalledPath) {
          // Simulate a web view request or body that never settles, even after
          // abort. The deadline still has to release the scheduler's guard.
          if (stage === 'headers') return new Promise(() => {});
          return { ok: true, status: 200, headers: new Headers(), json: () => new Promise(() => {}) };
        }
        return path === '/api/jobs' ? json({ jobs: [{ id: 1, state: 'done' }] })
          : json({ papers: ['monitoringbench'] }, 'saved-tag');
      } });
      const poller = createPoller({ run: () => client.pull() });
      await assert.rejects(poller.once(), /refresh timed out/);
      assert.equal(poller.running, false);
      assert.deepEqual(client.state.papers, []);
      assert.ok(signals.every(signal => signal.aborted));
      stall = false;
      await poller.once();
      assert.deepEqual(client.state.papers, ['monitoringbench']);
      assert.deepEqual(client.state.jobs, [{ id: 1, state: 'done' }]);
      assert.deepEqual(tags, [null, null]);
    });
  }
}

test('binary exports retain their destination, content, and download headers', async () => {
  const held = deferred();
  let destination;
  const client = createClient({ workspaceId: 'research', fetch: async (path, options) => {
    destination = options.headers.get('X-Doxograph-Workspace');
    return held.promise;
  } });
  const exporting = client.response('/api/notebook/export', { method: 'POST' });
  client.select('other');
  assert.equal(client.pendingMutations, 1);
  held.resolve(new Response('Exported research', {
    headers: { 'Content-Disposition': 'attachment; filename="research.md"' },
  }));
  const response = await exporting;
  assert.equal(destination, 'research');
  assert.equal(response.headers.get('Content-Disposition'), 'attachment; filename="research.md"');
  assert.equal(await response.text(), 'Exported research');
  assert.equal(client.pendingMutations, 0);
});

test('an older answer cannot overwrite a newer refresh or its ETag', async () => {
  const old = deferred();
  let reads = 0;
  const sentTags = [];
  const client = createClient({ fetch: async (path, options) => {
    if (path === '/api/jobs') return json({ jobs: [] });
    sentTags.push(options.headers.get('If-None-Match'));
    return ++reads === 1 ? old.promise : json({ papers: ['new'] }, 'new-tag');
  } });
  const first = client.pull();
  await client.pull();
  old.resolve(json({ papers: ['old'] }, 'old-tag'));
  const discarded = await first;
  assert.equal(discarded.applied, false);
  assert.equal(discarded.workspaceChanged, false);
  assert.deepEqual(client.state.papers, ['new']);
  await client.pull();
  assert.equal(sentTags.at(-1), 'new-tag');
});

test('an older failure cannot replace a newer successful refresh', async () => {
  const old = deferred();
  let reads = 0;
  const client = createClient({ fetch: async path => {
    if (path === '/api/jobs') return json({ jobs: [] });
    return ++reads === 1 ? old.promise : json({ papers: ['new'] }, 'new-tag');
  } });
  const first = client.pull();
  await client.pull();
  old.reject(new Error('Old request failed'));
  const discarded = await first;
  assert.equal(discarded.applied, false);
  assert.equal(discarded.workspaceChanged, false);
  assert.deepEqual(client.state.papers, ['new']);
});

test('an older success cannot clear a newer failure or commit its ETag', async () => {
  const old = deferred(), sentTags = [];
  let reads = 0;
  const client = createClient({ fetch: async (path, options) => {
    if (path === '/api/jobs') return json({ jobs: [] });
    sentTags.push(options.headers.get('If-None-Match'));
    if (++reads === 1) return old.promise;
    if (reads === 2) throw new Error('Current request failed');
    return json({ papers: ['recovered'] }, 'new-tag');
  } });
  const first = client.pull();
  await assert.rejects(client.pull(), /Current request failed/);
  old.resolve(json({ papers: ['old'] }, 'old-tag'));
  assert.equal((await first).applied, false);
  assert.deepEqual(client.state.papers, []);
  await client.pull();
  assert.deepEqual(sentTags, [null, null, null]);
  assert.deepEqual(client.state.papers, ['recovered']);
});

test('a failure from before leaving and returning to the same workspace is discarded', async () => {
  const old = deferred();
  const client = createClient({ workspaceId: 'first', fetch: async path =>
    path === '/api/jobs' ? json({ jobs: [] }) : old.promise });
  const first = client.pull();
  client.select('second');
  client.select('first');
  old.reject(new Error('Old workspace request failed'));
  const discarded = await first;
  assert.equal(discarded.applied, false);
  assert.equal(discarded.workspaceChanged, true);
  assert.deepEqual(client.state.papers, []);
});

test('a response from before leaving and returning to the same workspace is discarded', async () => {
  const old = deferred();
  const client = createClient({ workspaceId: 'first', fetch: async path =>
    path === '/api/jobs' ? json({ jobs: [] }) : old.promise });
  const first = client.pull();
  client.select('second');
  client.select('first');
  old.resolve(json({ papers: ['outdated'] }));
  const discarded = await first;
  assert.equal(discarded.applied, false);
  assert.equal(discarded.workspaceChanged, true);
  assert.deepEqual(client.state.papers, []);
});

test('workspace discovery validates the initial destination and updates renamed records', async () => {
  const remembered = [];
  const client = createClient({ workspaceId: 'removed', rememberWorkspace: id => remembered.push(id),
    fetch: async path => path === '/api/workspaces'
      ? json({ workspaces: [{ id: 'default', name: 'Default' }] })
      : path === '/api/jobs' ? json({ jobs: [] })
        : json({ workspace: { id: 'default', name: 'Renamed research' } }),
  });
  await client.loadWorkspaces();
  assert.equal(client.workspaceId, 'default');
  assert.deepEqual(remembered, ['default']);
  assert.equal((await client.pull()).registryChanged, true);
  assert.equal(client.currentWorkspace().name, 'Renamed research');
});

function navigation(client, overrides = {}) {
  return createController(client, {
    prepare: async () => true, showCurrent() {}, canLeave: () => true,
    confirmLeave: async () => true, beforeLeave: async () => {}, enter: async () => {},
    ...overrides,
  });
}

test('switches flush the old workspace first and only the newest queued selection proceeds', async () => {
  const flushing = deferred(), entered = deferred(), destinations = [], requests = [];
  const client = createClient({ fetch: async (path, options) => {
    requests.push(options.headers.get('X-Doxograph-Workspace'));
    return json({});
  } });
  const controller = navigation(client, {
    beforeLeave: () => destinations.length ? Promise.resolve() : flushing.promise,
    enter: async () => { destinations.push(client.workspaceId); if (destinations.length === 1) await entered.promise; },
  });
  const first = controller.switchTo('second');
  await tick();
  assert.ok(controller.pending);
  await client.request('/api/upload', { method: 'POST' });
  assert.deepEqual(requests, ['default']);
  const superseded = controller.switchTo('third');
  const latest = controller.switchTo('fourth');
  await tick();
  flushing.resolve();
  await tick();
  assert.equal(client.workspaceId, 'second');
  entered.resolve();
  await Promise.all([first, superseded, latest]);
  assert.deepEqual(destinations, ['second', 'fourth']);
  assert.equal(controller.pending, null);
});

test('draft refusals and failed loads leave later navigation available', async () => {
  const client = createClient();
  let allow = false, fail = true;
  const controller = navigation(client, {
    confirmLeave: async () => allow,
    enter: async () => { if (fail) throw new Error('Server unavailable'); },
  });
  await controller.switchTo('second');
  assert.equal(client.workspaceId, 'default');
  allow = true;
  await assert.rejects(controller.switchTo('second'), /Server unavailable/);
  assert.equal(controller.pending, null);
  fail = false;
  await controller.switchTo('third');
  assert.equal(client.workspaceId, 'third');
});

test('polling skips hidden pages, prevents overlap, and manual refresh waits for an older round', async () => {
  let scheduled, visible = false, calls = 0, canceled = 0;
  const held = deferred();
  const poller = createPoller({
    run: async () => { calls += 1; if (calls === 1) await held.promise; },
    isVisible: () => visible,
    schedule: callback => { scheduled = callback; return 1; },
    cancel: () => { canceled += 1; },
  });
  poller.start(); poller.start();
  scheduled(); await tick();
  assert.equal(calls, 0);
  visible = true;
  scheduled(); await tick();
  scheduled();
  const manual = poller.afterCurrent();
  assert.equal(calls, 1);
  held.resolve();
  await manual;
  assert.equal(calls, 2);
  assert.equal(poller.completed, 2);
  assert.equal(poller.running, false);
  poller.stop(); poller.stop();
  assert.equal(canceled, 1);
});

test('a failed scheduled poll releases its guard and the following tick recovers', async () => {
  let scheduled, calls = 0;
  const errors = [];
  const poller = createPoller({ run: async () => { if (++calls === 1) throw new Error('Restarting'); },
    schedule: callback => { scheduled = callback; return 1; }, onError: error => errors.push(error.message),
  });
  poller.start();
  scheduled(); await tick();
  assert.deepEqual(errors, ['Restarting']);
  assert.equal(poller.running, false);
  scheduled(); await tick();
  assert.equal(calls, 2);
  assert.equal(poller.completed, 2);
});
