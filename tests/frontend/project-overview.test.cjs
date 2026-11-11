const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const vm = require('node:vm');

const tick = () => new Promise(resolve => setImmediate(resolve));

function editor() {
  const nodes = new Map(), writes = [];
  function node() {
    return {
      children: [], style: {}, classList: { toggle() {}, remove() {} },
      replaceChildren(...children) { this.children = children; },
      append(child) { this.children.push(child); },
      add(child) { this.append(child); },
      setAttribute() {}, addEventListener() {},
    };
  }
  const get = id => {
    if (!nodes.has(id)) nodes.set(id, node());
    return nodes.get(id);
  };
  let saved = {
    id: 'a'.repeat(32), title: 'Project', body: '', revision: 1,
    project: { image: 'b'.repeat(64) + '.png', sections: [
      { title: 'Section', x: .1, y: .2, width: .3, height: .4, body: '' },
    ] },
  };
  const context = vm.createContext({
    URL, URLSearchParams, structuredClone, setTimeout, clearTimeout,
    location: { search: '?workspace=default' },
    window: { addEventListener() {} },
    document: {
      getElementById: get, createElement: node,
      querySelectorAll: () => [...nodes.values()],
    },
    Option: function (text, value) { return { text, value }; },
    NoteWriting: { render: body => body },
    fetch: async (url, options = {}) => {
      if (options.method === 'PUT') {
        const body = JSON.parse(options.body);
        writes.push(body);
        saved = { ...saved, ...body, revision: saved.revision + 1 };
      }
      const body = options.method === 'PUT' ? saved : url.startsWith('/api/state')
        ? { workspace: { name: 'Default' } } : { notes: [saved] };
      return { ok: true, json: async () => structuredClone(body) };
    },
  });
  vm.runInContext(readFileSync(resolve(__dirname, '../../doxograph/static/autosave.js'), 'utf8'), context);
  vm.runInContext(readFileSync(resolve(__dirname, '../../doxograph/static/project.js'), 'utf8'), context);
  return { get, writes };
}

test('section bounds still update the saved project after repeated saves', async () => {
  const { get, writes } = editor();
  await tick();
  get('regions').children[0].onclick();
  const left = get('bounds').children[0].children[0];
  left.value = '25';
  left.onchange();
  await get('save').onclick();
  assert.equal(writes[0].project.sections[0].x, .25);
  left.value = '35';
  left.onchange();
  await get('save').onclick();
  assert.equal(writes[1].project.sections[0].x, .35);
  assert.equal(writes[1].revision, 2);
});
