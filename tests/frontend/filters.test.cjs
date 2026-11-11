const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const vm = require('node:vm');

function filters({ view = 'research', upload = false, writing = '' } = {}) {
  const calls = [];
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {
      addEventListener() {}, querySelector: () => null,
      focus() { calls.push('focus'); },
    });
    return nodes.get(id);
  };
  let form = { value: 'Unfinished research' };
  const context = vm.createContext({
    V: { view, researchSaving: upload, q: 'alignment', tag: 'alignment' },
    $: node,
    writingInFlight(kind, id) {
      assert.equal(kind, 'research');
      assert.equal(id, '');
      return writing === kind;
    },
    captureOpenEditor() { calls.push('capture'); },
    parkNewClaimForNavigation() { calls.push('park'); },
    render() { calls.push('render'); },
    syncHash() { calls.push('hash'); },
    renderAll() { calls.push('renderAll'); form = { ...form }; },
  });
  vm.runInContext(readFileSync(resolve(__dirname, '../../doxograph/static/filters.js'), 'utf8'), context);
  context.dropTextSearch = () => calls.push('dropSearch');
  return { context, calls, form: () => form, node };
}

for (const key of ['q', 'all']) {
  for (const mode of ['autosave', 'upload', 'settled', 'other-writing', 'claims-view']) {
    test(`clearing ${key} during ${mode} preserves only the owned research form`, () => {
      const pending = mode === 'autosave' || mode === 'upload';
      const { context, calls, form, node } = filters({
        view: mode === 'claims-view' ? 'claims' : 'research',
        upload: mode === 'upload',
        writing: mode === 'other-writing' ? 'claim'
          : ['autosave', 'claims-view'].includes(mode) ? 'research' : '',
      });
      const original = form();
      context.clearFilters(key);
      assert.equal(context.V.q, '');
      assert.equal(context.V.tag, key === 'all' ? null : 'alignment');
      assert.equal(node('q').value, '');
      assert.equal(calls[0], 'capture');
      assert.equal(calls.includes('park'), key === 'all');
      assert.ok(calls.includes('dropSearch'));
      assert.equal(calls.at(-1), 'focus');
      if (pending) {
        assert.equal(form(), original);
        assert.equal(form().value, 'Unfinished research');
        assert.ok(calls.includes('render'));
        assert.ok(calls.includes('hash'));
        assert.ok(!calls.includes('renderAll'));
      } else {
        assert.notEqual(form(), original);
        assert.ok(calls.includes('renderAll'));
        assert.ok(!calls.includes('render'));
      }
    });
  }
}
