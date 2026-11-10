const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const vm = require('node:vm');

function editor() {
  const nodes = new Map();
  let finish;
  const upload = new Promise(resolve => { finish = resolve; });
  const context = vm.createContext({
    FormData, V: { researchSaving: false, researchDraft: null },
    S: { context: 'Original context', ledger: [{ id: 'L1', text: 'Original claim' }] },
    workspaceClient: { workspaceId: 'default' },
    document: { addEventListener() {}, body: {}, activeElement: null },
    $: id => nodes.get(id), api: () => upload,
  });
  nodes.set('panes', { addEventListener() {} });
  vm.runInContext(readFileSync(resolve(__dirname, '../../doxograph/static/research-context.js'), 'utf8'), context);
  context.setResearchSaving = () => {};
  context.renderResearchPreview = () => {};
  context.readResearchForm = () => ({
    context: nodes.get('research-context').value,
    claims: structuredClone(nodes.get('research-form').claims),
  });
  function show(value = context.S.context, claims = context.S.ledger) {
    nodes.set('research-context', { value, selectionStart: 0, selectionEnd: 8, setSelectionRange() {} });
    nodes.set('research-form', { claims: structuredClone(claims) });
    nodes.set('research-image-status', { textContent: '' });
  }
  show();
  context.V.researchBase = context.storedResearch();
  return { context, nodes, show, finish: () => finish({ url: '/image.png' }) };
}

for (const returned of [false, true]) {
  test(`image completion keeps newer server context and claims (${returned ? 'returned' : 'away'})`, async () => {
    const { context, nodes, show, finish } = editor();
    const pending = context.addResearchImage(new Blob(['image']));
    context.captureResearchDraft(); // An untouched form leaves no parked draft.
    nodes.delete('research-context');
    nodes.delete('research-form');
    context.S.context = 'Newer context from another window';
    context.S.ledger = [{ id: 'L2', text: 'Newer claim' }];
    if (returned) {
      show();
      context.V.researchBase = context.storedResearch();
    }
    finish();
    await pending;
    assert.equal(context.V.researchDraft.context, 'Newer context from another window\n\n![Pasted image](/image.png)\n\n');
    assert.equal(JSON.stringify(context.V.researchDraft.claims), JSON.stringify(context.S.ledger));
    assert.equal(context.V.researchBase.context, context.S.context);
    assert.equal(JSON.stringify(context.V.researchBase.claims), JSON.stringify(context.S.ledger));
    if (returned) assert.equal(nodes.get('research-context').value, context.V.researchDraft.context);
  });
}

test('image completion keeps a parked edited draft and its original insertion selection', async () => {
  const { context, nodes, show, finish } = editor();
  show('Replace this text', [{ id: 'L1', text: 'Edited claim' }]);
  const pending = context.addResearchImage(new Blob(['image']));
  context.captureResearchDraft();
  nodes.delete('research-context');
  nodes.delete('research-form');
  context.S.context = 'Another window';
  finish();
  await pending;
  assert.equal(context.V.researchDraft.context, '\n\n![Pasted image](/image.png)\n\nthis text');
  assert.equal(context.V.researchDraft.claims[0].text, 'Edited claim');
  assert.equal(context.V.researchBase.context, 'Original context');
});
