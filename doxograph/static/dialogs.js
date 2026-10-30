'use strict';

// Notices, the shared question dialog, and how label names are compared.

// --- notices and dialogs --------------------------------------------------
//
// Native alert, confirm and prompt block the page, cannot be styled, and hold
// the whole app still while they are up. They also cannot offer anything but
// OK and Cancel, which rules out the one answer a delete wants: Undo. What the
// app has to say goes through a notice; what it has to ask goes through one
// dialog, reused.

function toast(message, { actions = [], timeout = 6000, tone = '', onExpire = null } = {}) {
  const el = document.createElement('div');
  el.className = tone ? `toast ${tone}` : 'toast';
  const text = document.createElement('span');
  text.className = 'msg';
  text.textContent = message;
  el.appendChild(text);
  let timer = null;
  const handle = {
    // `expired` separates running out of being dismissed by an action. A
    // delete's notice commits the delete on the way out, and Undo is the one
    // way of closing it that must not.
    close(expired = false) {
      if (timer) clearTimeout(timer);
      timer = null;
      el.remove();
      if (expired && onExpire) onExpire();
    },
  };
  actions.forEach((action) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.textContent = action.label;
    button.addEventListener('click', () => { handle.close(); action.onClick(); });
    el.appendChild(button);
  });
  const dismiss = document.createElement('button');
  dismiss.type = 'button';
  dismiss.className = 'icon-button';
  dismiss.textContent = '×';
  dismiss.setAttribute('aria-label', `Dismiss: ${message}`);
  dismiss.addEventListener('click', () => handle.close(true));
  el.appendChild(dismiss);
  if (timeout) timer = setTimeout(() => handle.close(true), timeout);
  $('toasts').appendChild(el);
  return handle;
}

let askResolve = null;
let askSubmit = null;
let askBusy = false;

function askDialog({ title, note = '', input = null, labelChoices = [], ok = 'OK', cancel = 'Cancel', onSubmit = null }) {
  // A replacement prompt cancels the previous caller rather than stranding it.
  if (askResolve) settleAsk(null);
  const dialog = $('ask');
  askSubmit = onSubmit;
  dialog.removeAttribute('aria-busy');
  $('ask-title').textContent = title;
  $('ask-note').textContent = note;
  $('ask-note').hidden = !note;
  const field = $('ask-input');
  field.hidden = input === null;
  field.value = input ? (input.value || '') : '';
  field.placeholder = input ? (input.placeholder || '') : '';
  field.disabled = false;
  const choices = $('ask-label-choices');
  choices.hidden = labelChoices.length === 0;
  choices.innerHTML = labelChoices.map((name) => `<button type="button" class="label label-choice"
    data-label="${esc(name)}" style="${labelStyle(name)}" aria-label="${esc(name)}" aria-pressed="false">${esc(name)}</button>`).join('');
  syncAskLabels();
  $('ask-ok').textContent = ok;
  $('ask-cancel').textContent = cancel;
  $('ask-ok').disabled = false;
  $('ask-cancel').disabled = false;
  dialog.showModal();
  if (input) { field.focus(); field.select(); } else $('ask-ok').focus();
  return new Promise((resolve) => { askResolve = resolve; });
}

// Every way out of the dialog lands here. The pending promise is taken first,
// so closing the element does not settle it a second time with a cancel.
function settleAsk(value) {
  const resolve = askResolve;
  askResolve = null;
  askSubmit = null;
  askBusy = false;
  $('ask').removeAttribute('aria-busy');
  if ($('ask').open) $('ask').close();
  if (resolve) resolve(value);
}

$('ask-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  if (askBusy) return;
  const value = $('ask-input').hidden ? true : $('ask-input').value;
  if (!askSubmit) { settleAsk(value); return; }
  const owner = askResolve;
  const label = $('ask-ok').textContent;
  askBusy = true;
  $('ask').setAttribute('aria-busy', 'true');
  $('ask-input').disabled = true;
  $('ask-ok').disabled = true;
  $('ask-cancel').disabled = true;
  $('ask-ok').textContent = 'Saving…';
  try {
    // Validation or save failures stay in this dialog with its draft intact.
    const error = await askSubmit(value);
    if (askResolve !== owner) return;
    if (!error) { settleAsk(value); return; }
    $('ask-note').textContent = error;
    $('ask-note').hidden = false;
  } catch (error) {
    if (askResolve !== owner) return;
    $('ask-note').textContent = error.message;
    $('ask-note').hidden = false;
  } finally {
    // A navigation or replacement prompt may have retired this submission.
    if (askResolve === owner) {
      askBusy = false;
      $('ask').removeAttribute('aria-busy');
      $('ask-input').disabled = false;
      $('ask-ok').disabled = false;
      $('ask-cancel').disabled = false;
      $('ask-ok').textContent = label;
      $('ask-input').focus();
    }
  }
});
$('ask-cancel').addEventListener('click', () => settleAsk(null));
$('ask').addEventListener('cancel', (event) => {
  event.preventDefault();
  if (!askBusy) settleAsk(null);   // Escape
});
$('ask').addEventListener('close', () => {
  // close() queues this event. An answer can open the next question before
  // it arrives, so only cancel a question whose dialog is still closed.
  if (!$('ask').open) settleAsk(null);
});

const confirmDialog = (title, { note = '', ok = 'OK' } = {}) =>
  askDialog({ title, note, ok }).then((answer) => answer === true);
const askText = (title, { note = '', placeholder = '', value = '', labelChoices = [] } = {}) =>
  askDialog({ title, note, input: { placeholder, value }, labelChoices });

function normalizeLabel(name) {
  // Match Python str.split(): U+FEFF is preserved; U+001C–001F and U+0085
  // are separators. JavaScript's \s and trim() use a different set.
  return name.replace(/[\p{White_Space}\u001c-\u001f]+/gu, ' ').replace(/^ | $/g, '');
}

// Labels keep the case they were typed in but are one label in any case, as
// `store.label_key` decides on the server. A spelling already on a paper takes
// the server's key, so the page never joins two labels the server keeps apart;
// only a new spelling is folded here.
function labelKey(name) {
  const label = normalizeLabel(name);
  return Object.hasOwn(S.label_keys || {}, label) ? S.label_keys[label] : foldLabel(label);
}

// The server's steps, since JavaScript has no case folding: lowering, raising,
// and lowering again makes Σ, σ, and ς one key, and ß, ẞ, and SS another.
// Dotless ı skips the round trip, which would join it to i.
function foldLabel(label) {
  return label.toLowerCase().split('ı').map((part) => part.toUpperCase().toLowerCase()).join('ı');
}

function compareText(a, b) {
  return a < b ? -1 : a > b ? 1 : 0;
}

// The spelling `labels` uses for `name`, or undefined when it has no such label.
function findLabel(labels, name) {
  const key = labelKey(name);
  return (labels || []).find((label) => labelKey(label) === key);
}

function parseLabels(value) {
  const labels = new Map();
  for (const label of value.split(',').map(normalizeLabel).filter(Boolean)) {
    if (!labels.has(labelKey(label))) labels.set(labelKey(label), label);
  }
  return [...labels.values()];
}

function syncAskLabels() {
  const selected = new Set(parseLabels($('ask-input').value).map(labelKey));
  $('ask-label-choices').querySelectorAll('button').forEach((button) => {
    button.setAttribute('aria-pressed', String(selected.has(labelKey(button.dataset.label))));
  });
}

$('ask-input').addEventListener('input', syncAskLabels);
$('ask-label-choices').addEventListener('click', (event) => {
  const button = event.target.closest('button[data-label]');
  if (!button) return;
  const labels = parseLabels($('ask-input').value);
  const name = button.dataset.label;
  $('ask-input').value = (findLabel(labels, name) !== undefined
    ? labels.filter((label) => labelKey(label) !== labelKey(name)) : [...labels, name]).join(', ');
  syncAskLabels();
});
