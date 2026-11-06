'use strict';

// Settings: AI access and providers, Codex models, appearance, and the
// shell's disclosure menus.

// --- settings ------------------------------------------------------------

let savingAnalysisSetting = false;
let savingAutoFindings = false;
let savingProviderSetting = false;
let savingCodexModel = false;
let savingClaudeModel = false;
let loadingCodexModels = false;
let codexModelsLoaded = false;
let codexModels = [];
let codexModelEdited = false;
let readOnArrival = $('auto-extract').checked;
$('auto-extract').addEventListener('change', () => { readOnArrival = $('auto-extract').checked; });

function syncAnalysisControls() {
  const enabled = S.ai_enabled === true;
  const toggle = $('ai-enabled');
  toggle.disabled = savingAnalysisSetting || typeof S.ai_enabled !== 'boolean';
  if (!savingAnalysisSetting) toggle.checked = enabled;
  $('ai-access').disabled = savingProviderSetting || savingCodexModel || !S.ai_provider;
  if (!savingProviderSetting) $('ai-access').value = S.ai_provider || 'api';
  const providerHelp = {
    api: 'Uses the API or local provider configured below.',
    claude_code: 'Uses your signed-in Claude Code account and its plan limits or credits. Run claude auth login in Terminal first. API billing is never used as a fallback.',
    codex: 'Uses your ChatGPT account through Codex and its plan limits or credits. Run codex login in Terminal first. Includes your global Codex instructions along with extracted PDF text. Figures and scanned pages inside PDFs are unavailable; images attached to chats are sent directly. API billing is never used as a fallback.',
  };
  $('ai-access-help').textContent = providerHelp[S.ai_provider] || providerHelp.api;
  $('ai-provider-form').hidden = S.ai_provider !== 'api';
  $('claude-model-form').hidden = S.ai_provider !== 'claude_code';
  $('claude-model').disabled = savingClaudeModel;
  $('claude-custom-model').disabled = savingClaudeModel;
  $('claude-model-save').disabled = savingClaudeModel || savingProviderSetting;
  $('codex-model-form').hidden = S.ai_provider !== 'codex';
  $('codex-model').disabled = savingCodexModel;
  $('codex-custom-model').disabled = savingCodexModel;
  $('codex-model-save').disabled = savingCodexModel || savingProviderSetting;
  $('codex-model-refresh').disabled = loadingCodexModels;
  $('codex-current-model').textContent = `Current model: ${S.codex_model || 'Codex default (automatic)'}`;
  $('ai-action-status').hidden = enabled;
  $('auto-findings').disabled = savingAutoFindings || !enabled || typeof S.auto_findings !== 'boolean';
  if (!savingAutoFindings) $('auto-findings').checked = enabled && S.auto_findings === true;
  $('auto-extract').disabled = !enabled;
  $('auto-extract').checked = enabled && readOnArrival;
  $('import-analysis-status').hidden = S.ai_enabled !== false;
  document.querySelectorAll('[data-ai-action]').forEach((button) => {
    button.disabled = !enabled;
  });
  if (typeof syncAgentChatControls === 'function') syncAgentChatControls();
}

$('auto-findings').addEventListener('change', async () => {
  const enabled = $('auto-findings').checked;
  savingAutoFindings = true;
  syncAnalysisControls();
  $('ai-settings-status').textContent = 'Saving…';
  try {
    const settings = await api('/api/settings', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ auto_findings: enabled }),
    });
    S.auto_findings = settings.auto_findings;
    workspaceClient.invalidateState();
    $('ai-settings-status').textContent = settings.auto_findings ? 'Findings update after reading.' : 'Findings update only when asked.';
  } catch (error) {
    $('ai-settings-status').textContent = `Could not save: ${error.message}`;
  } finally {
    savingAutoFindings = false;
    syncAnalysisControls();
  }
});

$('ai-enabled').addEventListener('change', async () => {
  const enabled = $('ai-enabled').checked;
  savingAnalysisSetting = true;
  syncAnalysisControls();
  $('ai-settings-status').textContent = 'Saving…';
  try {
    const settings = await api('/api/settings', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ai_enabled: enabled }),
    });
    S.ai_enabled = settings.ai_enabled;
    workspaceClient.invalidateState();
    $('ai-settings-status').textContent = settings.ai_enabled ? 'AI analysis enabled.' : 'AI analysis disabled.';
  } catch (error) {
    $('ai-settings-status').textContent = `Could not save: ${error.message}`;
  } finally {
    savingAnalysisSetting = false;
    syncAnalysisControls();
    renderStats();
  }
});

$('ai-access').addEventListener('change', async () => {
  const provider = $('ai-access').value;
  savingProviderSetting = true;
  syncAnalysisControls();
  $('ai-settings-status').textContent = 'Saving…';
  try {
    const settings = await api('/api/settings/provider', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ai_provider: provider }),
    });
    S.ai_provider = settings.ai_provider;
    // The open Settings panel already owns the draft, including custom-mode
    // intent. Returning to Codex reveals it without replacing it with saved state.
    if (S.ai_provider === 'codex' && !codexModelsLoaded) loadCodexModels();
    workspaceClient.invalidateState();
    $('ai-settings-status').textContent = 'Model access saved.';
  } catch (error) {
    $('ai-settings-status').textContent = `Could not save: ${error.message}`;
  } finally {
    savingProviderSetting = false;
    syncAnalysisControls();
    renderStats();
  }
});

function selectedClaudeModel() {
  return $('claude-model').value === 'custom' ? $('claude-custom-model').value : $('claude-model').value;
}

function showClaudeModelFields(model = S.claude_code_model || '') {
  const models = S.claude_models || [];
  const custom = Boolean(model && !models.some((item) => item.id === model));
  $('claude-model').innerHTML = models.map((item) => `<option value="${esc(item.id)}">${esc(item.name)} (${esc(item.id)})</option>`).join('')
    + '<option value="custom">Custom model…</option>';
  $('claude-model').value = custom ? 'custom' : model;
  $('claude-custom-model').value = custom ? model : '';
  syncClaudeCustomField();
}

function syncClaudeCustomField() {
  const custom = $('claude-model').value === 'custom';
  $('claude-custom-model-row').hidden = !custom;
  $('claude-custom-model').required = custom;
}

$('claude-model').addEventListener('change', syncClaudeCustomField);
$('claude-model-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  savingClaudeModel = true;
  syncAnalysisControls();
  $('ai-settings-status').textContent = 'Saving…';
  try {
    const settings = await api('/api/settings', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ claude_model: selectedClaudeModel().trim() }),
    });
    Object.assign(S, settings);
    workspaceClient.invalidateState();
    $('ai-settings-status').textContent = 'Model saved. New analysis uses this selection.';
    renderStats();
  } catch (error) {
    $('ai-settings-status').textContent = `Could not save: ${error.message}`;
  } finally {
    savingClaudeModel = false;
    syncAnalysisControls();
  }
});

function selectedCodexModel() {
  return $('codex-model').value === 'custom' ? $('codex-custom-model').value : $('codex-model').value;
}

function showCodexModelFields(model = S.codex_model || '',
  custom = Boolean(model && !codexModels.some((item) => item.id === model))) {
  const picker = $('codex-model');
  picker.innerHTML = '<option value="">Codex default (automatic)</option>'
    + codexModels.map((item) => `<option value="${esc(item.id)}">${esc(item.name)} (${esc(item.id)})</option>`).join('')
    + '<option value="custom">Custom model…</option>';
  picker.value = custom ? 'custom' : model;
  $('codex-custom-model').value = custom ? model : '';
  syncCodexCustomField();
}

function syncCodexCustomField() {
  const custom = $('codex-model').value === 'custom';
  $('codex-custom-model-row').hidden = !custom;
  $('codex-custom-model').required = custom;
}

async function loadCodexModels() {
  if (loadingCodexModels) return;
  loadingCodexModels = true;
  $('codex-models-status').textContent = 'Loading models…';
  syncAnalysisControls();
  try {
    const result = await api('/api/settings/codex/models');
    codexModels = result.models;
    codexModelsLoaded = true;
    // Keep a draft typed or selected while discovery was in flight.
    showCodexModelFields(selectedCodexModel(),
      codexModelEdited && $('codex-model').value === 'custom' ? true : undefined);
    $('codex-models-status').textContent = codexModels.length ? '' : 'No models listed. You can enter a custom model ID.';
  } catch (error) {
    $('codex-models-status').textContent = `${error.message} You can still select Codex default or enter a custom model ID.`;
  } finally {
    loadingCodexModels = false;
    syncAnalysisControls();
  }
}

$('codex-model').addEventListener('change', () => {
  codexModelEdited = true;
  syncCodexCustomField();
});
$('codex-custom-model').addEventListener('input', () => { codexModelEdited = true; });
$('codex-model-refresh').addEventListener('click', loadCodexModels);
$('codex-model-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const model = selectedCodexModel().trim();
  savingCodexModel = true;
  syncAnalysisControls();
  $('ai-settings-status').textContent = 'Saving…';
  try {
    const settings = await api('/api/settings', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ codex_model: model }),
    });
    Object.assign(S, settings);
    workspaceClient.invalidateState();
    $('ai-settings-status').textContent = 'Model saved. New analysis uses this selection.';
    renderStats();
  } catch (error) {
    $('ai-settings-status').textContent = `Could not save: ${error.message}`;
  } finally {
    savingCodexModel = false;
    syncAnalysisControls();
  }
});

let providerDrafts = {};

function showProviderFields() {
  const provider = $('ai-provider').value;
  const profile = providerDrafts[provider] || S.providers?.[provider] || {};
  $('ai-model').value = profile.model || '';
  $('ai-model-suggestions').innerHTML = provider === 'anthropic'
    ? (S.claude_models || []).map((item) => `<option value="${esc(item.id)}">${esc(item.name)}</option>`).join('') : '';
  $('ai-base-url').value = profile.base_url || '';
  $('ai-endpoint-row').hidden = ['anthropic', 'openai'].includes(provider);
  $('ai-base-url').required = !['anthropic', 'openai'].includes(provider);
  const help = {
    anthropic: 'Uses ANTHROPIC_API_KEY from your environment or ~/.credentials. Sends PDFs to Anthropic.',
    openai: 'Uses OPENAI_API_KEY from your environment or ~/.credentials. Sends paper text to OpenAI. Choose a model that supports structured output.',
    ollama: 'Start Ollama and download a model first. Paper text goes only to this endpoint. Use a localhost URL and a locally downloaded model to keep analysis on your Mac. Long papers may exceed the local context limit.',
    'openai-compatible': 'For LM Studio or another endpoint supporting chat completions with JSON Schema. Include /v1 in the URL when required. Uses DOXOGRAPH_API_KEY if set; local servers can run without a key. Paper text goes to this endpoint.',
  };
  $('ai-provider-help').textContent = help[provider] || '';
}

let editingProvider = 'anthropic';
$('ai-provider').addEventListener('change', () => {
  providerDrafts[editingProvider] = { model: $('ai-model').value, base_url: $('ai-base-url').value };
  editingProvider = $('ai-provider').value;
  showProviderFields();
});
$('ai-provider-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const button = $('ai-provider-save');
  button.disabled = true;
  $('ai-settings-status').textContent = 'Saving…';
  try {
    const settings = await api('/api/settings', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider: $('ai-provider').value, model: $('ai-model').value,
        base_url: $('ai-base-url').value }),
    });
    Object.assign(S, settings);
    workspaceClient.invalidateState();
    providerDrafts = {};
    $('ai-settings-status').textContent = 'Provider saved. New analysis uses this selection.';
    renderStats();
  } catch (error) {
    $('ai-settings-status').textContent = `Could not save: ${error.message}`;
  } finally {
    button.disabled = false;
  }
});

// --- sidebar buttons ------------------------------------------------------
// Datasets, Benchmarks and People can each be hidden from the sidebar by right-clicking
// them, and brought back from Settings. The pages stay reachable from the
// palette and from links, so hiding only removes the button.
const SIDE_NAV_KEY = 'doxograph-hidden-side-nav';
let hiddenSideNav = new Set();
let sideNavMenuTrigger = null;
let sideNavPointer = false;
let sideNavUnsaved = false;   // a failed save leaves this page's choice newer than the stored one

// Another tab may have changed the choice since this page loaded, so read it
// again before each change rather than overwrite it with a stale copy.
function readSideNav() {
  try {
    const saved = JSON.parse(localStorage.getItem(SIDE_NAV_KEY));
    if (Array.isArray(saved)) hiddenSideNav = new Set(saved);
  } catch (_) { /* keep what this page has */ }
}
readSideNav();

function syncSideNav() {
  const focused = document.activeElement?.closest?.('[data-side-nav]');
  const buttons = [...document.querySelectorAll('[data-side-nav]')];
  for (const button of buttons) button.hidden = hiddenSideNav.has(button.dataset.sideNav);
  document.querySelector('.research-tools-nav').hidden = buttons.every(button => button.hidden);
  for (const box of document.querySelectorAll('input[name="side-nav"]')) box.checked = !hiddenSideNav.has(box.value);
  // A hidden button can't hold focus; keep keyboard users in the sidebar.
  if (focused?.hidden) (document.querySelector('[data-side-nav]:not([hidden])') || $('papers')).focus({ preventScroll: true });
}

function setSideNavHidden(name, hidden) {
  if (!sideNavUnsaved) readSideNav();
  if (hidden) hiddenSideNav.add(name); else hiddenSideNav.delete(name);
  try {
    localStorage.setItem(SIDE_NAV_KEY, JSON.stringify([...hiddenSideNav]));
    sideNavUnsaved = false;
  } catch (_) { sideNavUnsaved = true; /* preference remains for this page */ }
  syncSideNav();
}

syncSideNav();
window.addEventListener('storage', (event) => {
  if (event.key !== SIDE_NAV_KEY) return;
  hiddenSideNav = new Set();
  readSideNav();
  syncSideNav();
});

// A menu opened by the pointer follows a pointerdown; one opened with Shift+F10
// or the Menu key does not. Coordinates can't tell them apart: browsers differ
// on what they report for a keyboard-opened menu.
document.querySelector('.research-tools-nav').addEventListener('pointerdown', () => { sideNavPointer = true; });
// Shift+F10 and the Menu key press a key first, which clears a pointerdown left by an earlier click.
document.querySelector('.research-tools-nav').addEventListener('keydown', () => { sideNavPointer = false; });
document.querySelector('.research-tools-nav').addEventListener('contextmenu', (event) => {
  const byPointer = sideNavPointer;
  sideNavPointer = false;
  const button = event.target.closest('[data-side-nav]');
  if (!button) return;
  event.preventDefault();
  const menu = $('ctxmenu');
  menu.innerHTML = `<li><button type="button" data-act="hide-side-nav" data-side-nav-target="${esc(button.dataset.sideNav)}">Hide from sidebar</button></li>`;
  menu.hidden = false;
  const { width, height } = menu.getBoundingClientRect();
  const x = byPointer ? event.clientX : button.getBoundingClientRect().left;
  const y = byPointer ? event.clientY : button.getBoundingClientRect().bottom;
  menu.style.left = `${Math.max(0, Math.min(x, window.innerWidth - width - 4))}px`;
  menu.style.top = `${Math.max(0, Math.min(y, window.innerHeight - height - 4))}px`;
  // Opened from the keyboard, focus goes into the menu and Escape brings it back.
  sideNavMenuTrigger = byPointer ? null : button;
  if (sideNavMenuTrigger) menu.querySelector('button').focus({ preventScroll: true });
});

$('ctxmenu').addEventListener('keydown', (event) => {
  if (event.key !== 'Escape' || !sideNavMenuTrigger || !$('ctxmenu').querySelector('[data-act="hide-side-nav"]')) return;
  const trigger = sideNavMenuTrigger;
  sideNavMenuTrigger = null;
  closePaperMenu();
  trigger.focus({ preventScroll: true });
  event.preventDefault();
  event.stopPropagation();   // the menu was ours; no editor behind it should see this Escape
});

$('ctxmenu').addEventListener('click', (event) => {
  const button = event.target.closest('[data-act="hide-side-nav"]');
  if (!button) return;
  // Hand focus back to the button first, so hiding it moves focus on in the sidebar.
  sideNavMenuTrigger?.focus({ preventScroll: true });
  sideNavMenuTrigger = null;
  closePaperMenu();
  setSideNavHidden(button.dataset.sideNavTarget, true);
});

function syncThemeControls() {
  const appearance = document.querySelector(`input[name="appearance"][value="${themeSettings.appearance}"]`);
  const colors = document.querySelector(`input[name="color-theme"][value="${themeSettings.colors}"]`);
  if (appearance) appearance.checked = true;
  if (colors) colors.checked = true;
}

function closeSettings({ restoreFocus = false } = {}) {
  $('settings-menu').hidden = true;
  $('btn-settings').setAttribute('aria-expanded', 'false');
  if (restoreFocus) $('btn-settings').focus();
}

function openSettings() {
  closePaperMenu();
  closeActionDisclosures();
  syncThemeControls();
  syncAnalysisControls();
  providerDrafts = {};
  editingProvider = S.provider || 'anthropic';
  $('ai-provider').value = editingProvider;
  showProviderFields();
  showClaudeModelFields();
  codexModelEdited = false;
  showCodexModelFields();
  if (S.ai_provider === 'codex' && !codexModelsLoaded) loadCodexModels();
  $('settings-menu').hidden = false;
  $('btn-settings').setAttribute('aria-expanded', 'true');
}

$('btn-settings').addEventListener('click', () => {
  if ($('settings-menu').hidden) openSettings(); else closeSettings();
});
$('btn-close-settings').addEventListener('click', () => closeSettings({ restoreFocus: true }));
$('settings-menu').addEventListener('change', (event) => {
  if (event.target.name === 'appearance') {
    applyThemeSettings({ ...themeSettings, appearance: event.target.value }, true);
  } else if (event.target.name === 'color-theme') {
    applyThemeSettings({ ...themeSettings, colors: event.target.value }, true);
  } else if (event.target.name === 'side-nav') {
    setSideNavHidden(event.target.value, !event.target.checked);
  }
});
$('btn-reset-theme').addEventListener('click', () => {
  applyThemeSettings({ ...DEFAULT_THEME }, true);
  syncThemeControls();
});

document.addEventListener('mousedown', (event) => {
  if (!$('settings-menu').contains(event.target) && !$('btn-settings').contains(event.target)) closeSettings();
});

// Shell disclosures share dismissal behavior without consuming Escape in an
// editor behind them. Native details keeps keyboard and expanded state intact.
function closeActionDisclosures({ except = null, restoreFocus = false } = {}) {
  for (const menu of document.querySelectorAll('.action-disclosure[open]')) {
    if (menu === except) continue;
    if (restoreFocus && menu.contains(document.activeElement)) menu.querySelector('summary').focus();
    menu.open = false;
  }
}
document.addEventListener('mousedown', (event) => {
  closeActionDisclosures({ except: event.target.closest('.action-disclosure') });
});
$('workspace-actions').addEventListener('click', (event) => {
  if (event.target.closest('button')) closeActionDisclosures({ restoreFocus: true });
});
$('btn-close-filters').addEventListener('click', () => closeActionDisclosures({ restoreFocus: true }));
document.querySelectorAll('.action-disclosure').forEach(menu => {
  menu.addEventListener('toggle', () => {
    if (menu.open) {
      closeActionDisclosures({ except: menu });
      closeSettings();
    }
  });
});
