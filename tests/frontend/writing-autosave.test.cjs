const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const vm = require('node:vm');

function editor(kind) {
  let answer, writes = 0;
  const initial = {context:'Saved research', claims:[]};
  const context = vm.createContext({
    setTimeout, clearTimeout, structuredClone,
    V: {drafts:{claim:{text:'Draft claim'}}, quitClaimBases:{claim:{text:'Saved claim'}}, unconfirmedClaims:{},
      synthDrafts:{topic:'Draft synthesis'}, quitSynthBases:{topic:{text:'Saved synthesis'}}, unconfirmedSyntheses:{},
      researchDraft:{context:'Draft research', claims:[]}, researchBase:initial},
    S: {claims:[{id:'claim',paper:'paper'}]},
    workspaceClient:{workspaceId:'default'},
    document:{addEventListener() {}, querySelectorAll() { return []; }},
    window:{addEventListener() {}},
    isRemoving: () => false, trash:new Set(), savingClaims:new Set(),
    claimRequestBody: value => value, settleDeletes: async () => true,
    beginManualQuitWrite: () => ({}), storedResearch: () => initial,
    differs: (a,b) => JSON.stringify(a) !== JSON.stringify(b),
    api: async () => { writes++; throw Error('Offline'); },
    confirmDialog: () => new Promise(resolve => { answer = resolve; }),
  });
  for (const file of ['autosave.js','writing-autosave.js']) {
    vm.runInContext(readFileSync(resolve(__dirname, '../../doxograph/static/' + file), 'utf8'), context);
  }
  const id = kind === 'claim' ? 'claim' : kind === 'synthesis' ? 'topic' : '';
  const saver = context.writingAutosave(kind, id);
  return {context, saver, id, answer: value => answer(value), writes: () => writes};
}

for (const kind of ['claim','synthesis','research']) {
  for (const discard of [false,true]) {
    test(`${kind}: failed Done ${discard ? 'discards draft and retry state' : 'keeps draft on Cancel'}`, async () => {
      const {context, saver, id, answer, writes} = editor(kind);
      const finishing = context.finishWriting(kind,id,() => true);
      await new Promise(resolve => setImmediate(resolve));
      assert.equal(saver.unconfirmed,true);
      answer(discard);
      assert.equal(await finishing,discard ? 'discarded' : false);
      const draft = kind === 'claim' ? context.V.drafts[id] : kind === 'synthesis' ? context.V.synthDrafts[id] : context.V.researchDraft;
      assert.equal(Boolean(draft),!discard);
      assert.equal(saver.unconfirmed,!discard);
      if (discard) {
        assert.equal(await saver.flush(),true);
        assert.equal(writes(),1);
        assert.notEqual(context.writingAutosave(kind,id),saver);
      }
    });
  }
  test(`${kind}: stale confirmation cannot discard newer writing or another editor`, async () => {
    const {context,saver,id,answer} = editor(kind);
    let current = true;
    const finishing = context.finishWriting(kind,id,() => current);
    await new Promise(resolve => setImmediate(resolve));
    current = false;
    answer(true);
    assert.equal(await finishing,false);
    assert.equal(context.writingAutosave(kind,id),saver);
  });
}

test('discard retires a pending timer without writing an abandoned draft', async () => {
  const {context,saver,id,answer,writes} = editor('claim');
  const finishing = context.finishWriting('claim',id,() => true);
  await new Promise(resolve => setImmediate(resolve));
  saver.schedule();
  answer(true);
  assert.equal(await finishing,false); // Newer input is not covered by this answer.
  await saver.settle();
  assert.equal(writes(),1);
  assert.ok(context.V.drafts[id]);
});
