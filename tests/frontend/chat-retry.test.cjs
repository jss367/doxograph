const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');

const source = file => readFileSync(resolve(__dirname, '../../doxograph/static', file), 'utf8');
const ChatRetry = new Function(`${source('chat-retry.js')}\nreturn ChatRetry;`)();
const createClient = new Function(`${source('workspace-client.js')}\nreturn createWorkspaceClient;`)();
const busy = (retryAfter) => Object.assign(new Error('busy'), { retryable: true, retryAfter });

test('retries a temporary failure, waiting at least as long as the provider asked', async () => {
  const waits = [], notes = [];
  let calls = 0;
  const result = await ChatRetry.run(async () => {
    if (++calls < 3) throw busy(calls === 1 ? 20000 : undefined);
    return 'answer';
  }, { sleep: async ms => waits.push(ms), onRetry: note => notes.push(note) });
  assert.equal(result, 'answer');
  assert.deepEqual(waits, [20000, 30000]);
  assert.match(notes[0], /Retrying in 20 seconds \(attempt 1 of 3\)/);
});

test('gives up after the last delay and rethrows the failure', async () => {
  let calls = 0;
  await assert.rejects(ChatRetry.run(async () => { calls += 1; throw busy(); }, { sleep: async () => {} }), /busy/);
  assert.equal(calls, 4);
});

test('does not retry a failure the server did not mark retryable', async () => {
  let calls = 0;
  await assert.rejects(ChatRetry.run(async () => { calls += 1; throw new Error('lost connection'); }, { sleep: async () => {} }), /lost connection/);
  assert.equal(calls, 1);
});

test('the workspace client carries the retry markers from the response headers', async () => {
  const client = createClient({ fetch: async () => new Response(JSON.stringify({ detail: 'Busy' }), {
    status: 502, headers: { 'Retry-After': '7', 'X-Doxograph-Retryable': 'true' } }) });
  await assert.rejects(client.request('/api/ask', { method: 'POST' }),
    error => error.retryable === true && error.retryAfter === 7000 && error.status === 502);
  const plain = createClient({ fetch: async () => new Response(JSON.stringify({ detail: 'No' }), { status: 502 }) });
  await assert.rejects(plain.request('/api/ask', { method: 'POST' }),
    error => error.retryable === false && error.retryAfter === undefined);
});
