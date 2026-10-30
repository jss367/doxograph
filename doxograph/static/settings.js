'use strict';

// Settings: AI access and providers, Codex models, appearance, and the
// shell's disclosure menus.

// --- settings ------------------------------------------------------------

let savingAnalysisSetting = false;
let savingProviderSetting = false;
let savingCodexModel = false;
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
    codex: 'Uses your ChatGPT account through Codex and its plan limits or credits. Run codex login in Terminal first. Includes your global Codex instructions along with extracted PDF text; figures and scanned pages are unavailable. API billing is never used as a fallback.',
  };
  $('ai-access-help').textContent = providerHelp[S.ai_provider] || providerHelp.api;
  const chatAgent = activeChat()?.agent || S.ai_provider;
  $('chat-access-help').textContent = chatAgent === 'codex'
    ? 'ChatGPT receives page-labeled PDF text and your global Codex instructions. Choose Claude to omit that global guidance. Figures and scanned pages are unavailable.'
    : chatAgent === 'api' && S.provider !== 'anthropic'
      ? 'The selected API or local model receives page-labeled PDF text; images are unavailable.'
      : 'Claude receives the PDF, including its page images.';
  $('ai-provider-form').hidden = S.ai_provider !== 'api';
  $('codex-model-form').hidden = S.ai_provider !== 'codex';
  $('codex-model').disabled = savingCodexModel;
  $('codex-custom-model').disabled = savingCodexModel;
  $('codex-model-save').disabled = savingCodexModel || savingProviderSetting;
  $('codex-model-refresh').disabled = loadingCodexModels;
  $('codex-current-model').textContent = `Current model: ${S.codex_model || 'Codex default (automatic)'}`;
  $('chat-send').disabled = !enabled || Boolean(activeChat()?.busy);
  $('ai-action-status').hidden = enabled;
  $('auto-extract').disabled = !enabled;
  $('auto-extract').checked = enabled && readOnArrival;
  $('import-analysis-status').hidden = S.ai_enabled !== false;
  document.querySelectorAll('[data-ai-action]').forEach((button) => {
    button.disabled = !enabled || (button.hasAttribute('data-paper-chat')
      && !S.papers.some(p => p.key === button.dataset.paperChat && p.has_pdf));
  });
  if (typeof syncAgentChatControls === 'function') syncAgentChatControls();
}

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
