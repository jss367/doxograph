const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const createAutosave = new Function(readFileSync(resolve(__dirname, '../../doxograph/static/autosave.js'), 'utf8') + '\nreturn createAutosave;')();

test('waiting flushes report a failed round without initiating a retry', async () => {
  const target = {};
  let reject, writes = 0, dirty = true;
  const first = new Promise((yes, no) => { reject = no; });
  const saver = createAutosave({current: () => target, dirty: () => dirty,
    write: async () => { writes++; if (writes === 1) await first; dirty = false; }, changed() {}});
  const active = saver.flush(), waiting = saver.flush(), alsoWaiting = saver.flush();
  reject(new Error('lost acknowledgement'));
  assert.deepEqual(await Promise.all([active, waiting, alsoWaiting]), [false, false, false]);
  assert.equal(writes, 1);
  assert.equal(saver.failure, 'lost acknowledgement');
  assert.equal(await saver.flush(), true);
  assert.equal(writes, 2);
  assert.equal(saver.failure, '');
});

test('a waiting flush cannot turn failure into success when the draft looks clean', async () => {
  let reject, dirty = true;
  const write = new Promise((yes, no) => { reject = no; });
  const saver = createAutosave({current: () => 'note', dirty: () => dirty,
    write: () => write, changed() {}});
  const active = saver.flush(), waiting = saver.flush();
  dirty = false;
  reject(new Error('response lost'));
  assert.deepEqual(await Promise.all([active, waiting]), [false, false]);
  assert.equal(saver.failure, 'response lost');
});

test('a draft whose write went unanswered saves again until one is acknowledged', async () => {
  const target = {};
  let writes = 0, dirty = true;
  const saver = createAutosave({current: () => target, dirty: () => dirty,
    write: async () => { writes++; if (writes === 1) throw new Error('response lost'); }, changed() {}});
  assert.equal(await saver.flush(), false);
  // The writer restores the text last known to be stored.
  dirty = false;
  assert.equal(saver.unconfirmed, true);
  assert.notEqual(saver.describe(), 'Saved');
  assert.equal(await saver.flush(), true);
  assert.equal(writes, 2);
  assert.equal(saver.unconfirmed, false);
  assert.equal(saver.describe(), 'Saved');
  assert.equal(await saver.flush(), true);
  assert.equal(writes, 2);
});
