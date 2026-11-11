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
    workspaceClient:{workspaceId:'default', pullSequence:0, appliedPullSequence:0},
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


test('link-button edits schedule the owning form, but not new or retired forms', async () => {
  const {context,saver,id} = editor('claim');
  context.NEW_CLAIM_ID = '__new__';
  context.readForm = () => ({text:'Draft claim',ledger_links:[]});
  const form = {dataset:{form:id,workspace:'default'}};
  const target = {
    dataset:{act:'drop-link'},
    closest: selector => ['[data-workspace]','form[data-form]'].includes(selector) ? form : null,
    matches: () => false,
  };
  context.scheduleWriting({target});
  assert.equal(saver.generation,1);
  assert.equal(context.V.drafts[id].ledger_links.length,0);
  await saver.settle();
  form.dataset.form = context.NEW_CLAIM_ID;
  context.scheduleWriting({target});
  assert.equal(saver.generation,1);
  assert.equal(context.V.drafts.__new__,undefined);
  form.dataset.form = id; form.dataset.workspace = 'retired-workspace';
  context.scheduleWriting({target});
  assert.equal(saver.generation,1);
});


test('a save blocked by a claim mutation resumes only after the last mutation', async () => {
  const {context,saver,id,writes} = editor('claim');
  context.savingClaims.add(id);
  assert.equal(await saver.flush(),false);
  assert.equal(writes(),0);
  context.resumeClaimWriting(id);
  assert.equal(saver.generation,0);
  context.savingClaims.delete(id);
  context.resumeClaimWriting(id);
  assert.equal(saver.generation,1);
  context.resumeClaimWriting(id);
  assert.equal(saver.generation,1);
  await saver.settle();
});

for (const change of ['workspace','owner','discard']) {
  test(`claim mutation completion cannot revive a ${change} session`, async () => {
    const {context,saver,id,answer} = editor('claim');
    context.savingClaims.add(id);
    assert.equal(await saver.flush(),false);
    if (change === 'workspace') context.workspaceClient.workspaceId = 'new-workspace';
    if (change === 'owner') context.V.drafts = {};
    if (change === 'discard') {
      const finishing = context.finishWriting('claim',id,() => true);
      await new Promise(resolve => setImmediate(resolve));
      answer(true);
      assert.equal(await finishing,'discarded');
    }
    context.savingClaims.delete(id);
    context.resumeClaimWriting(id);
    assert.equal(saver.generation,0);
  });
}


test('markSaving resumes the deferred autosave after the last overlapping writer', async () => {
  const {context,saver,id} = editor('claim');
  context.$ = () => ({addEventListener() {}});
  vm.runInContext(readFileSync(resolve(__dirname, '../../doxograph/static/claims.js'), 'utf8'), context);
  context.markSaving(id,true);
  context.markSaving(id,true);
  assert.equal(await saver.flush(),false);
  context.markSaving(id,false);
  assert.equal(saver.generation,0);
  context.markSaving(id,false);
  assert.equal(saver.generation,1);
  await saver.settle();
});


test('failed paper removal resumes a save only after its guard is lifted', async () => {
  const {context,saver,id,writes} = editor('claim');
  let removing = true;
  context.isRemoving = () => removing;
  assert.equal(await saver.flush(),false);
  assert.equal(writes(),0);
  context.resumeClaimWriting(id);
  assert.equal(saver.generation,0);
  removing = false;
  context.resumeClaimWriting(id);
  assert.equal(saver.generation,1);
  await saver.settle();
});

for (const kind of ['claim','synthesis']) {
  for (const newer of [false,true]) {
    test(`${kind}: confirmed deletion retires only ${newer ? 'old' : 'unchanged'} autosave state`, async () => {
      const {context,saver,id} = editor(kind);
      const source = readFileSync(resolve(__dirname, '../../doxograph/static/app.js'), 'utf8');
      vm.runInContext(source.slice(source.indexOf('function manualQuitState('), source.indexOf('function rememberClaimQuitBase(')), context);
      assert.equal(await saver.flush(),false);
      const seen = context.manualQuitState(kind,id);
      if (newer) saver.schedule();
      context.retireManualQuitState(kind,id,seen);
      assert.equal(saver.unconfirmed,newer);
      if (!newer) {
        assert.equal(await saver.flush(),true);
        assert.equal(context.writingSessionState(kind,id),null);
      }
      await saver.settle();
    });
  }
}


for (const change of ['pull', 'newer-draft', 'removed', 'workspace', 'owner']) {
  test(`claim acknowledgment reconciles the current corpus after ${change}`, async () => {
    const {context, saver, id} = editor('claim');
    const oldRow = context.S.claims[0];
    oldRow.text = 'Saved claim';
    let acknowledge;
    context.api = () => new Promise(resolve => { acknowledge = resolve; });
    let invalidated = false;
    context.workspaceClient.invalidateState = () => { invalidated = true; };
    context.pull = async () => {
      assert.equal(invalidated, true);
      context.workspaceClient.appliedPullSequence = ++context.workspaceClient.pullSequence;
      // The authoritative read, not the delayed PATCH response, replaces S.
      context.S = {claims:change === 'removed' ? [] : [{...oldRow, text:'Draft claim'}]};
    };
    const saving = saver.flush();
    const currentRow = {...oldRow};
    context.S = {claims:change === 'removed' ? [] : [currentRow]};
    if (change === 'newer-draft') context.V.drafts[id] = {text:'Newer writing'};
    if (change === 'workspace') context.workspaceClient.workspaceId = 'other';
    if (change === 'owner') context.V.drafts = {[id]:{text:'Other session'}};
    acknowledge({...oldRow, text:'Draft claim'});
    assert.equal(await saving, !['workspace', 'owner'].includes(change));
    assert.equal(oldRow.text, 'Saved claim');
    if (['pull', 'newer-draft'].includes(change)) {
      assert.equal(currentRow.text, 'Saved claim');
      assert.equal(context.S.claims[0].text, 'Draft claim');
      assert.equal(invalidated, true);
      assert.equal(context.V.quitClaimBases[id].text, 'Draft claim');
      assert.equal(context.V.drafts[id]?.text, change === 'newer-draft' ? 'Newer writing' : undefined);
    } else if (change === 'removed') {
      assert.equal(context.S.claims.length, 0); // Never resurrect a removed row.
    } else {
      assert.equal(currentRow.text, 'Saved claim');
      assert.equal(context.V.quitClaimBases[id].text, 'Saved claim');
      assert.equal(context.V.drafts[id].text, change === 'owner' ? 'Other session' : 'Draft claim');
    }
  });
}


for (const result of ['newer-pull', 'read-failure', 'superseding-failure', 'superseding-success', 'older-read-only', 'workspace-change', 'same-row']) {
  test(`delayed claim acknowledgment respects ${result}`, async () => {
    const {context, saver, id} = editor('claim');
    const oldRow = context.S.claims[0];
    let acknowledge, reads = 0, invalidated = false;
    context.workspaceClient.pullSequence = 2;
    context.workspaceClient.appliedPullSequence = 1;
    context.api = () => new Promise(resolve => { acknowledge = resolve; });
    context.workspaceClient.invalidateState = () => { invalidated = true; };
    context.pull = async () => {
      reads++;
      assert.equal(invalidated, true);
      if (result === 'read-failure') throw Error('Could not refresh');
      if (result === 'superseding-failure') return false;
      if (result === 'older-read-only') {
        context.S = {claims:[{...oldRow, text:'Newer remote writing', note:'Newer remote note'}]};
        context.workspaceClient.appliedPullSequence = 2;
        return false;
      }
      context.workspaceClient.appliedPullSequence = result === 'superseding-success' ? 4 : 3;
      if (result === 'workspace-change') {
        context.workspaceClient.workspaceId = 'other';
        context.V.drafts = {[id]:{text:'Other workspace draft'}};
      }
      context.S = {claims:[{...oldRow, text:'Newer remote writing', note:'Newer remote note'}]};
      return result === 'superseding-success' ? false : true;
    };
    const saving = saver.flush();
    if (result !== 'same-row') context.S = {claims:[{...oldRow, text:'Newer remote writing', note:'Newer remote note'}]};
    acknowledge({...oldRow, text:'Draft claim', note:'Older response note'});
    assert.equal(await saving, ['newer-pull', 'superseding-success', 'same-row'].includes(result));
    assert.equal(reads, result === 'same-row' ? 0 : 1);
    assert.equal(context.S.claims[0].text, result === 'same-row' ? 'Draft claim' : 'Newer remote writing');
    assert.equal(context.S.claims[0].note, result === 'same-row' ? 'Older response note' : 'Newer remote note');
    if (['read-failure', 'superseding-failure', 'older-read-only'].includes(result)) assert.equal(context.V.drafts[id].text, 'Draft claim');
    if (result === 'workspace-change') assert.equal(context.V.drafts[id].text, 'Other workspace draft');
    if (['newer-pull', 'superseding-success'].includes(result)) assert.equal(context.V.drafts[id], undefined);
  });
}
