const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const vm = require('node:vm');

function app() {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) {
      const listeners = new Map();
      nodes.set(id, {
        value:'', hidden:false, open:false,
        addEventListener(type, callback) { listeners.set(type, callback); },
        dispatch(type) { return listeners.get(type)?.({preventDefault() {}}); },
        showModal() { this.open = true; },
        // Native close dispatch is deliberately separate from close(), so the
        // test controls exactly when the browser delivers the queued event.
        close() { this.open = false; },
        removeAttribute() {}, setAttribute() {}, focus() {}, select() {},
        querySelectorAll() { return []; },
      });
    }
    return nodes.get(id);
  };
  let finishRead;
  const context = vm.createContext({
    $:node, V:{paperSort:'added-desc'},
    workspaceClient:{workspaceId:'default'}, navigationVersion:() => 1,
    document:{addEventListener() {}}, window:{addEventListener() {}},
    api:() => new Promise(resolve => { finishRead = resolve; }),
    refreshAll:async () => {},
  });
  for (const file of ['dialogs.js', 'papers.js']) {
    vm.runInContext(readFileSync(resolve(__dirname, '../../doxograph/static/' + file), 'utf8'), context);
  }
  return {context, node, finishRead:() => finishRead({paper:{title:'Paper', summary:'Saved'}, revision:'r1'})};
}

for (const repeats of [1, 2]) {
  test(`settled dialog's ${repeats} delayed close event(s) preserve a pending revision opener`, async () => {
    const {context, node, finishRead} = app();
    const previous = context.askDialog({title:'Previous edit', input:{multiline:true, value:'Old'}});
    context.settleAsk('Old');
    assert.equal(await previous, 'Old');
    const opening = context.editPaperText('paper', 'summary');
    const generation = vm.runInContext('askGeneration', context);
    for (let i = 0; i < repeats; i++) node('ask').dispatch('close');
    assert.equal(vm.runInContext('askGeneration', context), generation);
    finishRead();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(node('ask').open, true);
    assert.equal(node('ask-title').textContent, 'Summary for Paper');
    assert.equal(node('ask-lines').hidden, false);
    assert.equal(node('ask-lines').value, 'Saved');
    context.settleAsk(null);
    await opening;
  });
}

test('external native close still cancels an active dialog exactly once', async () => {
  const {context, node} = app();
  const answer = context.askDialog({title:'Active question'});
  const generation = vm.runInContext('askGeneration', context);
  node('ask').close();
  node('ask').dispatch('close');
  assert.equal(await answer, null);
  assert.equal(vm.runInContext('askGeneration', context), generation + 1);
  node('ask').dispatch('close');
  assert.equal(vm.runInContext('askGeneration', context), generation + 1);
});

test('a stale close event cannot cancel a newer open dialog', async () => {
  const {context, node} = app();
  const old = context.askDialog({title:'Old question'});
  context.settleAsk(true);
  assert.equal(await old, true);
  let settled = false;
  const current = context.askDialog({title:'New question'});
  current.then(() => { settled = true; });
  const generation = vm.runInContext('askGeneration', context);
  node('ask').dispatch('close');
  await Promise.resolve();
  assert.equal(settled, false);
  assert.equal(node('ask').open, true);
  assert.equal(vm.runInContext('askGeneration', context), generation);
  node('ask-cancel').dispatch('click');
  assert.equal(await current, null);
});
