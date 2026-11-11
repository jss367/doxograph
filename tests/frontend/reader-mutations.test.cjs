const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const vm = require('node:vm');
const turn = () => new Promise(resolve => setImmediate(resolve));
function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}
function reader() {
  const notebooks = new Map(['one', 'two'].map(ws => [ws, {
    revision: 0, highlights: [{ id: 'mark', paper: 'paper', note: 'Original', color: 'yellow' }],
    reading: [{ paper: 'paper', page: 1 }],
  }]));
  const calls = [];
  let intercept = async () => {}, refresh = async () => {};
  const client = {
    workspaceId: 'one', state: {},
    async request(path, options) {
      const ws = options.headers['X-Doxograph-Workspace'];
      calls.push([ws, options.method]);
      if (options.method === 'GET') return structuredClone(notebooks.get(ws));
      const incoming = JSON.parse(options.body);
      await intercept(ws, incoming, options);
      assert.equal(incoming.revision, notebooks.get(ws).revision, 'No same-window stale notebook write');
      incoming.revision++;
      notebooks.set(ws, structuredClone(incoming));
      return incoming;
    },
  };
  const context = vm.createContext({
    document: { addEventListener() {}, querySelector: () => null },
    window: { addEventListener() {} },
    setTimeout: () => 1, clearTimeout() {},
    refreshAll: () => refresh(),
  });
  vm.runInContext(readFileSync(resolve(__dirname, '../../doxograph/static/autosave.js'), 'utf8'), context);
  const source = readFileSync(resolve(__dirname, '../../doxograph/static/research-tools.js'), 'utf8');
  // Expose the actual closures only in this harness; production exports stay private.
  vm.runInContext(source.replace('return {selection:',
    'return {mutateNotebook, markAutosave, setDraft: value => { markEditor = value; }, selection:'), context);
  const tools = vm.runInContext('ResearchTools', context);
  tools.connect(client, {});
  const draft = { id: 'mark', paper: 'paper', workspace: 'one', text: 'First note', saved: 'Original' };
  tools.setDraft(draft);
  return { tools, draft, client, notebooks, calls,
    intercept: fn => { intercept = fn; }, refresh: fn => { refresh = fn; } };
}

for (const action of ['color', 'question', 'page']) {
  test(`note autosave and ${action} serialize and retain newer typing`, async () => {
    const { tools, draft, notebooks, calls, intercept } = reader();
    const gate = deferred();
    let first = true;
    intercept(async () => { if (first) { first = false; await gate.promise; } });
    const saver = tools.markAutosave();
    const saving = saver.flush();
    await turn();
    const mutation = tools.mutateNotebook('one', n => {
      if (action === 'page') n.reading[0].page = 2;
      else n.highlights[0][action] = action === 'color' ? 'green' : true;
    }, { refresh: false });
    draft.text = 'Newer note';
    saver.schedule();
    await turn();
    assert.deepEqual(calls, [['one', 'GET'], ['one', 'PUT']]);
    gate.resolve();
    assert.equal(await saving, true);
    await mutation;
    const n = notebooks.get('one');
    assert.equal(n.highlights[0].note, 'Newer note');
    assert.equal(draft.saved, 'Newer note');
    if (action === 'page') assert.equal(n.reading[0].page, 2);
    else assert.equal(n.highlights[0][action], action === 'color' ? 'green' : true);
    assert.equal(saver.failure, '');
  });
}

test('queued removal cannot be undone by newer note autosave', async () => {
  const { tools, draft, notebooks, intercept } = reader();
  const gate = deferred();
  let first = true;
  intercept(async () => { if (first) { first = false; await gate.promise; } });
  const saver = tools.markAutosave();
  const saving = saver.flush();
  await turn();
  const removal = tools.mutateNotebook('one', n => { n.highlights = []; }, { refresh: false });
  draft.text = 'Writing during removal';
  saver.schedule();
  gate.resolve();
  await removal;
  await saving;
  assert.equal(notebooks.get('one').highlights.length, 0);
  assert.equal(draft.text, 'Writing during removal');
  assert.match(saver.failure, /removed/);
  tools.setDraft(null); // Confirmed removal/discard retires the editor.
  assert.equal(await saver.flush(), true);
  assert.equal(notebooks.get('one').highlights.length, 0);
});

test('failed notebook write does not block the next mutation or cross-workspace work', async () => {
  const { tools, notebooks, intercept } = reader();
  const gate = deferred();
  let first = true;
  intercept(async ws => {
    if (ws === 'one' && first) { first = false; await gate.promise; throw new Error('Offline'); }
  });
  const failed = tools.mutateNotebook('one', n => { n.highlights[0].note = 'Rejected'; }, { refresh: false });
  const rejected = assert.rejects(failed, /Offline/);
  await turn();
  const queued = tools.mutateNotebook('one', n => { n.highlights[0].color = 'green'; }, { refresh: false });
  await tools.mutateNotebook('two', n => { n.highlights[0].note = 'Other workspace'; }, { refresh: false });
  assert.equal(notebooks.get('two').highlights[0].note, 'Other workspace');
  gate.resolve();
  await rejected;
  await queued;
  assert.equal(notebooks.get('one').highlights[0].note, 'Original');
  assert.equal(notebooks.get('one').highlights[0].color, 'green');
});

test('refresh can await subsequent reader progress without queue deadlock', async () => {
  const { tools, notebooks, refresh } = reader();
  refresh(() => tools.mutateNotebook('one', n => { n.reading[0].page = 4; }, { refresh: false, history: false }));
  await tools.mutateNotebook('one', n => { n.highlights[0].color = 'green'; });
  assert.equal(notebooks.get('one').reading[0].page, 4);
  assert.equal(notebooks.get('one').highlights[0].color, 'green');
});

test('switching owners does not publish an old note into the new workspace', async () => {
  const { tools, client, notebooks, intercept } = reader();
  const gate = deferred();
  intercept(() => gate.promise);
  const saving = tools.markAutosave().flush();
  await turn();
  client.workspaceId = 'two';
  const current = { id: 'other' };
  client.state.notebook = current;
  tools.setDraft(null);
  gate.resolve();
  assert.equal(await saving, false);
  assert.equal(client.state.notebook, current);
  assert.equal(notebooks.get('one').highlights[0].note, 'First note');
  assert.equal(notebooks.get('two').highlights[0].note, 'Original');
});
