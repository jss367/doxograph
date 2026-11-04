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

const NOW = Date.parse('Wed, 04 Nov 2026 21:00:00 GMT');
const fixedClient = new Function('Date', `${source('workspace-client.js')}\nreturn createWorkspaceClient;`)(
  { parse: Date.parse, now: () => NOW });
const retryResponse = header => new Response(JSON.stringify({ detail: 'Busy' }), {
  status: 502, headers: { 'Retry-After': header, 'X-Doxograph-Retryable': 'true' },
});

for (const [header, expected] of [
  ['120', 120000], ['0', 0], [' 120 ', 120000],
  ['Wed, 04 Nov 2026 21:02:00 GMT', 120000],
  ['Wednesday, 04-Nov-26 21:02:00 GMT', 120000],
  ['Wed Nov  4 21:02:00 2026', 120000],
  ['Wed, 04 Nov 2026 20:59:59 GMT', 0],
  ['Wed, 04 Nov 2026 21:00:00 GMT', 0],
  ['', undefined], ['   ', undefined], ['not a date', undefined],
  ['Wed, xx Nov 2026 21:02:00 GMT', undefined],
  ['-1', undefined], ['1.5', undefined], ['Infinity', undefined], ['NaN', undefined],
  ['1e3', undefined], ['0x10', undefined], ['2026-11-04', undefined],
  ['9'.repeat(309), undefined], ['9'.repeat(306), undefined],
]) {
  test(`Retry-After ${JSON.stringify(header)} is a finite duration or is ignored`, async () => {
    const client = fixedClient({ fetch: async () => retryResponse(header) });
    await assert.rejects(client.request('/api/ask', { method: 'POST' }), error => {
      assert.equal(error.retryAfter, expected);
      assert.equal(error.retryable, true);
      assert.equal(error.status, 502);
      return true;
    });
  });
}

for (const [header, waits] of [
  ['Wed, 04 Nov 2026 21:02:00 GMT', [120000, 120000, 120000]],
  ['120', [120000, 120000, 120000]],
  ['7', [15000, 30000, 60000]],
  ['Wed, 04 Nov 2026 20:59:59 GMT', [15000, 30000, 60000]],
  ['bad header', [15000, 30000, 60000]],
]) {
  test(`transport and retry loop respect ${JSON.stringify(header)} and the retry limit`, async () => {
    const seen = [], sleeps = [];
    const client = fixedClient({ fetch: async (path, options) => {
      seen.push([path, options]);
      return retryResponse(header);
    } });
    await assert.rejects(ChatRetry.run(() => client.request('/api/ask', { method: 'POST' }),
      { sleep: async ms => sleeps.push(ms) }), /Busy/);
    assert.equal(seen.length, 4);
    assert.deepEqual(sleeps, waits);
  });
}

for (const detail of ['Quota exhausted', 'CLI failed', 'Network disconnected']) {
  test(`${detail} is never retried without the server marker`, async () => {
    let calls = 0;
    const client = fixedClient({ fetch: async () => {
      calls += 1;
      if (detail.startsWith('Network')) throw new TypeError(detail);
      return new Response(JSON.stringify({ detail }), { status: 502, headers: { 'Retry-After': '120' } });
    } });
    await assert.rejects(ChatRetry.run(() => client.request('/api/ask', { method: 'POST' }),
      { sleep: async () => assert.fail('must not wait') }), new RegExp(detail));
    assert.equal(calls, 1);
  });
}

test('long provider delays use bounded timers without shortening the wait', async () => {
  const timers = [], notes = [];
  const retry = new Function('setTimeout', `${source('chat-retry.js')}\nreturn ChatRetry;`)(
    (resolve, ms) => { timers.push(ms); resolve(); });
  let calls = 0;
  const client = fixedClient({ fetch: async () => ++calls === 1 ? retryResponse('2147485')
    : new Response(JSON.stringify({ answer: 'Recovered' })) });
  assert.deepEqual(await retry.run(() => client.request('/api/ask', { method: 'POST' }),
    { onRetry: note => notes.push(note) }), { answer: 'Recovered' });
  assert.deepEqual(timers, [2147483647, 1353]);
  assert.equal(timers.reduce((sum, ms) => sum + ms, 0), 2147485000);
  assert.match(notes[0], /2147485 seconds/);
  assert.equal(calls, 2);
});
