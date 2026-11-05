const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');

const source = readFileSync(resolve(__dirname, '../../doxograph/static/keyboard.js'), 'utf8');
function palette(aiEnabled) {
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) elements.set(id, {
      open: false, disabled: false, value: '', children: [], innerHTML: '', handlers: {},
      addEventListener(name, handler) { this.handlers[name] = handler; },
      setAttribute() {}, removeAttribute() {}, focus() {},
      showModal() { this.open = true; }, close() { this.open = false; },
    });
    return elements.get(id);
  };
  const state = { papers: [], tags: [], tag_counts: {}, ai_enabled: aiEnabled, workspace: { id: 'default' } };
  const forbidden = () => assert.fail('No navigation, provider request, or import may run in this test');
  const command = new Function('$', 'document', 'window', 'S', 'workspaceClient', 'workspaceController',
    'focusPaperImport', 'focusClaimSearch', 'esc', 'currentWorkspace', 'openFindings', 'updateFindings',
    'closePaperMenu', 'closeSettings', 'toast',
    `${source}\nreturn CommandPalette;`)(get, { addEventListener() {}, querySelector: () => null }, { addEventListener() {} }, state,
    { workspaceId: 'default' }, { pending: false }, forbidden, forbidden, value => value,
    () => ({ name: 'Default' }), forbidden, forbidden, () => {}, () => {}, () => {});
  get('btn-command-palette').handlers.click();
  get('command-query').value = 'Update findings';
  return { command, get, state };
}

for (const enabled of [false, undefined, null, 'true', true]) {
  test(`findings update availability requires ai_enabled === true (${JSON.stringify(enabled)})`, () => {
    const { command, get } = palette(enabled);
    command.sync(true);
    assert.equal(get('command-results').innerHTML.includes('Update findings'), enabled === true);
  });
}

test('an update highlighted before AI is disabled cannot activate afterward', async () => {
  const { command, get, state } = palette(true);
  command.sync(true);
  assert.match(get('command-results').innerHTML, /Update findings/);
  state.ai_enabled = false;
  get('command-palette').handlers.keydown({ key: 'Enter', target: get('command-query'), preventDefault() {} });
  await Promise.resolve();
  assert.equal(get('command-palette').open, true);
  assert.doesNotMatch(get('command-results').innerHTML, /Update findings/);
});
