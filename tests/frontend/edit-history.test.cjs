const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const load = (file, name) => new Function(`${readFileSync(resolve(__dirname, '../../doxograph/static', file), 'utf8')}\nreturn ${name};`)();
const createHistory = load('edit-history.js', 'createEditHistory');
const createClient = load('workspace-client.js', 'createWorkspaceClient');
const copy = value => structuredClone(value);

function setup() {
  const history = createHistory();
  const notebooks = {one: {revision: 0, notes: '', highlights: [], boards: [], reading: []}, two: {revision: 0, notes: '', highlights: [], boards: [], reading: []}};
  const papers = {one: {key: 'paper', title: 'Original', labels: [], claims: [{id: 'claim', text: 'Original', reviewed: false}]}, two: {key: 'paper', title: 'Other', labels: [], claims: []}};
  let fail = false;
  const client = createClient({workspaceId: 'one', history, fetch: async (path, options) => {
    const ws = options.headers.get('X-Doxograph-Workspace');
    const method = options.method || 'GET';
    const body = options.body ? JSON.parse(options.body) : null;
    if (fail && method !== 'GET') return new Response('{"detail":"Disk full"}', {status: 503});
    let result;
    if (path === '/api/notebook') {
      if (method === 'PUT') {
        if (body.revision !== notebooks[ws].revision) return new Response('{"detail":"Revision conflict"}', {status: 409});
        notebooks[ws] = {...body, revision: body.revision + 1};
      }
      result = notebooks[ws];
    } else if (path === '/api/papers/paper') {
      if (method === 'PATCH') Object.assign(papers[ws], body);
      result = papers[ws];
    } else if (path === '/api/papers/paper/claims/claim') {
      Object.assign(papers[ws].claims[0], body);
      result = papers[ws].claims[0];
    } else { throw new Error(`Unexpected path ${path}`); }
    return new Response(JSON.stringify(result));
  }});
  const notebookEdit = async fn => {
    const data = await client.request('/api/notebook');
    fn(data);
    return client.request('/api/notebook', {method: 'PUT', body: JSON.stringify(data)});
  };
  const patch = data => client.request('/api/papers/paper', {method: 'PATCH', body: JSON.stringify(data)});
  return {history, client, notebooks, papers, notebookEdit, patch, setFail: value => { fail = value; }};
}

test('highlight creation, recoloring, and removal undo and redo in order', async () => {
  const {history, notebooks, notebookEdit} = setup();
  await notebookEdit(n => n.highlights.push({id: 'mark', paper: 'paper', color: 'yellow', note: ''}));
  await notebookEdit(n => { n.highlights[0].color = 'green'; });
  await notebookEdit(n => { n.highlights = []; });
  await history.apply('one');
  assert.equal(notebooks.one.highlights[0].color, 'green');
  await history.apply('one');
  assert.equal(notebooks.one.highlights[0].color, 'yellow');
  await history.apply('one');
  assert.deepEqual(notebooks.one.highlights, []);
  for (let i = 0; i < 3; i++) await history.apply('one', true);
  assert.deepEqual(notebooks.one.highlights, []);
});

test('undo preserves unrelated notebook edits, page progress, and other marks', async () => {
  const {history, notebooks, notebookEdit} = setup();
  notebooks.one.reading.push({paper: 'paper', page: 1, bookmarks: []});
  await notebookEdit(n => n.highlights.push({id: 'mark', paper: 'paper', color: 'yellow', note: ''}));
  notebooks.one.notes = 'Written elsewhere';
  notebooks.one.reading[0].page = 8;
  notebooks.one.highlights.push({id: 'elsewhere', paper: 'paper', color: 'blue', note: ''});
  notebooks.one.revision++;
  await history.apply('one');
  assert.equal(notebooks.one.notes, 'Written elsewhere');
  assert.equal(notebooks.one.reading[0].page, 8);
  assert.deepEqual(notebooks.one.highlights.map(h => h.id), ['elsewhere']);
});

test('conflicts and failed undo keep the entry retryable', async () => {
  const {history, notebooks, notebookEdit, setFail} = setup();
  await notebookEdit(n => n.highlights.push({id: 'mark', color: 'yellow'}));
  notebooks.one.highlights[0].color = 'blue';
  await assert.rejects(history.apply('one'), /changed since/);
  notebooks.one.highlights[0].color = 'yellow';
  setFail(true);
  await assert.rejects(history.apply('one'), /Disk full/);
  setFail(false);
  assert.equal(await history.apply('one'), true);
  assert.deepEqual(notebooks.one.highlights, []);
});

test('paper and claim edits share order with notebook edits and stay in their workspace', async () => {
  const {history, client, papers, notebooks, notebookEdit, patch} = setup();
  await patch({title: 'Corrected', labels: ['reading']});
  await notebookEdit(n => n.highlights.push({id: 'mark', color: 'yellow'}));
  await client.request('/api/papers/paper/claims/claim', {method: 'PATCH', body: JSON.stringify({reviewed: true})});
  client.select('two');
  assert.equal(await history.apply('two'), false);
  await history.apply('one');
  assert.equal(papers.one.claims[0].reviewed, false);
  assert.equal(notebooks.one.highlights.length, 1);
  await history.apply('one');
  await history.apply('one');
  assert.equal(papers.one.title, 'Original');
  assert.deepEqual(papers.one.labels, []);
  assert.equal(papers.two.title, 'Other');
});

test('failed saves and no-op saves do not consume history; new edits clear redo', async () => {
  const {history, patch, setFail, papers} = setup();
  await patch({title: 'Changed'});
  await patch({title: 'Changed'});
  setFail(true);
  await assert.rejects(patch({title: 'Failed'}));
  setFail(false);
  await history.apply('one');
  assert.equal(papers.one.title, 'Original');
  await patch({title: 'New direction'});
  assert.equal(await history.apply('one', true), false);
});

test('automatic reading writes do not replace the latest user edit', async () => {
  const {history, client, notebookEdit, notebooks} = setup();
  await notebookEdit(n => n.highlights.push({id: 'mark', color: 'yellow'}));
  const n = await client.request('/api/notebook');
  n.reading.push({paper: 'paper', page: 2});
  await client.request('/api/notebook', {method: 'PUT', history: false, body: JSON.stringify(n)});
  await history.apply('one');
  assert.deepEqual(notebooks.one.highlights, []);
  assert.equal(notebooks.one.reading[0].page, 2);
});

test('bookmarks undo after later page progress without moving the reader backward', async () => {
  const {history, notebookEdit, notebooks} = setup();
  notebooks.one.reading.push({paper: 'paper', page: 1, bookmarks: []});
  await notebookEdit(n => { n.reading[0].page = 3; n.reading[0].bookmarks.push(3); });
  notebooks.one.reading[0].page = 8;
  notebooks.one.revision++;
  await history.apply('one');
  assert.deepEqual(notebooks.one.reading[0], {paper: 'paper', page: 8, bookmarks: []});
  await history.apply('one', true);
  assert.deepEqual(notebooks.one.reading[0], {paper: 'paper', page: 8, bookmarks: [3]});
});

test('collection order can be restored without losing another window’s new board', async () => {
  const {history, notebookEdit, notebooks} = setup();
  notebooks.one.boards = [{id: 'first', title: 'First'}, {id: 'second', title: 'Second'}];
  await notebookEdit(n => n.boards.reverse());
  notebooks.one.boards.push({id: 'third', title: 'Third'});
  notebooks.one.revision++;
  await history.apply('one');
  assert.deepEqual(notebooks.one.boards.map(b => b.id), ['first', 'second', 'third']);
});

test('unsupported latest writes do not silently undo an older action', async () => {
  const history = createHistory();
  let undone = false;
  history.add('one', {undo: async () => { undone = true; }});
  await history.request('/api/ingest', {method: 'POST', body: '{}'}, async () => ({queued: 1}), 'one');
  await assert.rejects(history.apply('one'), /latest action cannot be undone/);
  assert.equal(undone, false);
});
