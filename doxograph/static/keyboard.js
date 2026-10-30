'use strict';

// Keyboard shortcuts, the command palette, and the shortcuts sheet.

// --- keyboard -------------------------------------------------------------

function selectedRow(rows) {
  return rows.find((row) => row.id === V.selectedId) || rows[0] || null;
}

function moveSelection(rows, step) {
  if (!rows.length) return;
  captureOpenEditor();
  const at = rows.findIndex((row) => row.id === V.selectedId);
  const next = at < 0 ? 0 : Math.min(Math.max(at + step, 0), rows.length - 1);
  selectClaim(rows[next].id);
  renderContent();
  scrollToSelected();
}

// The next claim nobody has reviewed, wrapping round the end: review runs to
// the bottom of the list and then wants the ones passed over on the way.
function selectNextUnreviewed(rows) {
  if (!rows.length) return;
  const at = rows.findIndex((row) => row.id === V.selectedId);
  const ordered = rows.slice(at + 1).concat(rows.slice(0, at + 1));
  const next = ordered.find((row) => !row.reviewed);
  if (!next) {
    toast('Every claim on screen has been reviewed.', { timeout: 2500 });
    return;
  }
  captureOpenEditor();
  selectClaim(next.id);
  renderContent();
  scrollToSelected();
}

function openHelp() {
  closePaperMenu();
  closeSettings();
  if (!$('help').open) $('help').showModal();
}

// The palette reads the same workspace snapshot as the sidebar. Opening a
// destination clears filters so a previous search cannot hide what was chosen.
const CommandPalette = (() => {
  const dialog = $('command-palette'), query = $('command-query'), list = $('command-results');
  let results = [], active = 0, sourceWorkspace = null, signature = '';
  const normalize = value => String(value || '').normalize('NFD').replace(/\p{M}/gu, '').toLowerCase();

  function library(paper = null, tag = null) {
    captureOpenEditor();
    parkNewClaimForNavigation(paper);
    parkSynthEditor();
    parkNoteEditor();
    showView(paper || tag ? 'claims' : 'library');
    Object.assign(V, {paper, tag, label: null, facet: null, q: '', kind: '', unreviewed: false,
      unverified: false, editing: null, selectedId: null});
    $('q').value = ''; $('kind').value = '';
    $('only-unreviewed').checked = false; $('only-unverified').checked = false;
    dropTextSearch();
    syncHash(true);
    renderAll();
    $('main').scrollTop = 0;
  }

  function catalog() {
    const entries = [];
    const add = (id, kind, title, detail, run, keywords = '') => entries.push({id, kind, title, detail, run,
      search: normalize(`${title} ${detail} ${keywords} ${kind}`), name: normalize(title)});
    S.papers.forEach(p => add(`paper:${p.key}`, 'Paper', p.title || p.key,
      [...(p.authors || []), p.year, p.key].filter(Boolean).join(' · '), () => library(p.key)));
    const topics = new Map(S.tags.map(t => [t.name, t.description || '']));
    Object.keys(S.tag_counts).forEach(name => { if (!topics.has(name)) topics.set(name, ''); });
    topics.forEach((description, name) => add(`topic:${name}`, 'Topic', name, description, () => library(null, name)));
    (S.notebook?.boards || []).forEach(b => add(`board:${b.id}`, 'Board', b.title,
      `${b.entries.length} entries · Evidence board`, () => ResearchTools.openBoard(b.id)));
    add('all', 'Action', 'Show all papers', 'Browse every claim in this workspace', () => library(), 'library home');
    const click = (id, title, keywords = '') => {
      if (!$(id).disabled) add(id, 'Action', title, '', () => {
        revealSidebar($(id));
        $(id).click();
      }, keywords);
    };
    const view = (id, title, next) => add(id, 'Action', title, '', () => {
      if (V.view === next) return;
      showView(next); syncHash(true); renderAll();
      if (next === 'graph') loadCitations();
    });
    view('map', 'Open paper map', 'graph');
    view('tensions', 'Browse tensions', 'tensions');
    view('agreements', 'Browse agreements', 'agreements');
    view('research', 'Edit research context and personal claims', 'research');
    ['questions', 'boards', 'reading', 'marks', 'searches', 'connections', 'compare'].forEach(tool => {
      const titles = {questions: 'Open research questions', boards: 'Open evidence boards', reading: 'Open reading queue', marks: 'Open marks and questions', searches: 'Open saved searches',
        connections: 'Open claim connections', compare: 'Compare selected claims and papers'};
      add(`tool:${tool}`, 'Action', titles[tool], '', () => {
        const target = document.querySelector(`[data-tool="${tool}"]`);
        revealSidebar(target);
        target.click();
      });
    });
    add('import', 'Action', 'Add papers', 'Paste references or drop PDFs', focusPaperImport, 'import');
    add('search', 'Action', 'Search claims and PDF text', '', focusClaimSearch);
    click('btn-workspace-add', 'Create workspace');
    click('btn-export', 'Export HTML');
    click('btn-bib', 'Export BibTeX');
    click('btn-workspace-archive', 'Download portable workspace archive');
    click('btn-workspace-backups', 'Browse previous document versions');
    click('btn-settings', 'Open settings', 'appearance theme color AI');
    click('btn-help', 'Show keyboard shortcuts', 'help');
    click('btn-retag', 'Retag all papers');
    click('btn-tensions', 'Find tensions');
    click('btn-agreements', 'Find agreements');
    click('btn-synth', 'Synthesize topics');
    return entries;
  }

  function score(entry, words, text) {
    if (!words.every(word => entry.search.includes(word))) return -1;
    if (entry.name === text) return 0;
    if (entry.name.startsWith(text)) return 1;
    if (entry.name.includes(text)) return 2;
    return 3;
  }

  function select(index, scroll = true) {
    active = index;
    [...list.children].forEach((row, i) => row.setAttribute('aria-selected', String(i === active)));
    const row = list.children[active];
    if (row) {
      query.setAttribute('aria-activedescendant', row.id);
      if (scroll) row.scrollIntoView({block: 'nearest'});
    } else query.removeAttribute('aria-activedescendant');
  }

  function sync(force = false) {
    if (!dialog.open) return;
    // A workspace can also change through Back while this modal is open.
    if (sourceWorkspace !== workspaceClient.workspaceId || workspaceController.pending || S.workspace?.id !== workspaceClient.workspaceId) {
      dialog.close(); return;
    }
    const entries = catalog();
    const nextSignature = JSON.stringify(entries.map(({id, title, detail}) => [id, title, detail]));
    if (!force && signature === nextSignature) return;
    signature = nextSignature;
    const previous = results[active]?.id;
    const text = normalize(query.value.trim()), words = text.split(/\s+/).filter(Boolean);
    const matched = entries.map(entry => ({...entry, score: score(entry, words, text)})).filter(e => e.score >= 0);
    if (text) matched.sort((a, b) => a.score - b.score || a.title.localeCompare(b.title));
    const counts = {};
    results = matched.filter(e => {
      counts[e.kind] = (counts[e.kind] || 0) + 1;
      return text || e.kind === 'Action' || counts[e.kind] <= 6;
    }).slice(0, 60);
    list.innerHTML = results.map((e, i) => `<li id="command-result-${i}" role="option" data-command="${i}" aria-selected="false">
      <span class="command-kind">${esc(e.kind)}</span><span class="command-label"><strong>${esc(e.title)}</strong>
      ${e.detail ? `<small>${esc(e.detail)}</small>` : ''}</span></li>`).join('');
    $('command-status').textContent = !results.length ? 'No matches. Try a different title, topic, board, or action.'
      : `${results.length < matched.length ? `${results.length} of ${matched.length}` : results.length} results in ${currentWorkspace()?.name || 'this workspace'}${results.length < matched.length ? ' · Type to narrow your search' : ''}`;
    select(force ? 0 : Math.max(0, results.findIndex(e => e.id === previous)), false);
  }

  function open() {
    if (dialog.open) { query.focus(); return; }
    if (document.querySelector('dialog[open]')) return;
    if (workspaceController.pending || S.workspace?.id !== workspaceClient.workspaceId) return;
    closePaperMenu(); closeSettings();
    sourceWorkspace = workspaceClient.workspaceId;
    query.value = ''; signature = '';
    dialog.showModal(); sync(true); query.focus();
  }

  async function activate() {
    const entry = results[active];
    if (!entry) return;
    if (sourceWorkspace !== workspaceClient.workspaceId || workspaceController.pending) { dialog.close(); return; }
    // Recheck the live catalog: a background refresh may have removed a paper,
    // board, topic, or disabled an AI action since it was highlighted.
    const live = catalog().find(e => e.id === entry.id);
    if (!live) { sync(true); return; }
    dialog.close();
    try { await live.run(); }
    catch (error) { toast(`Could not open ${entry.title}: ${error.message}`, {tone: 'warn'}); }
  }

  $('btn-command-palette').addEventListener('click', open);
  $('btn-close-command-palette').addEventListener('click', () => dialog.close());
  query.addEventListener('input', () => sync(true));
  dialog.addEventListener('click', event => {
    const row = event.target.closest('[data-command]');
    if (row) { select(Number(row.dataset.command), false); activate(); }
    if (event.target === dialog) {
      const box = dialog.getBoundingClientRect();
      if (event.clientX < box.left || event.clientX > box.right || event.clientY < box.top || event.clientY > box.bottom) dialog.close();
    }
  });
  list.addEventListener('mousedown', event => event.preventDefault());
  dialog.addEventListener('keydown', event => {
    if (event.isComposing) return;
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      if (results.length) select((active + (event.key === 'ArrowDown' ? 1 : -1) + results.length) % results.length);
    } else if (event.key === 'Enter' && event.target === query) {
      event.preventDefault(); activate();
    }
  });
  document.addEventListener('keydown', event => {
    if ((event.metaKey || event.ctrlKey) && !event.altKey && !event.shiftKey && event.key.toLowerCase() === 'k') {
      if (document.querySelector('dialog[open]') && !dialog.open) return;
      event.preventDefault();
      if (!event.repeat) { if (dialog.open) dialog.close(); else open(); }
    }
  });
  return {sync};
})();

$('btn-help').addEventListener('click', openHelp);
$('btn-close-help').addEventListener('click', () => $('help').close());
// A click on the backdrop is the element itself: the sheet is something to
// glance at, so anywhere off it puts it away.
$('help').addEventListener('click', (event) => {
  if (event.target === $('help')) $('help').close();
});

document.addEventListener('keydown', async (event) => {
  // A modal owns the keyboard while it is up, Escape included: the dialog
  // closes itself, and the editor branches below must not also fire.
  if (document.querySelector('dialog[open]')) return;
  if (event.key === 'Escape' && document.querySelector('.action-disclosure[open]')) {
    closeActionDisclosures({ restoreFocus: true });
    event.preventDefault();
    return;
  }
  if (event.key === 'Escape' && !$('settings-menu').hidden) {
    closeSettings({ restoreFocus: true });
    return;
  }
  // While the paper menu is open Escape belongs to it. Falling through to the
  // editor branches would also run `cancelEdit` and drop a draft the user
  // only meant to keep by dismissing the menu.
  if (event.key === 'Escape' && !$('ctxmenu').hidden) {
    closePaperMenu({ restoreFocus: true });
    event.preventDefault();
    return;
  }
  // This independent editor owns its keyboard. Escape here must not reach
  // the claim-editor fallback and discard a draft elsewhere in the view.
  if (event.target.closest?.('#workspace-notes-panel')) return;
  if (event.key === 'Escape') {
    const menu = document.querySelector('.paper-actions[open]');
    if (menu) {
      menu.open = false;
      menu.querySelector('summary').focus();
      event.preventDefault();
      return;
    }
  }
  const tag = (event.target.tagName || '').toLowerCase();
  if (['input', 'textarea', 'select'].includes(tag)) {
    if (event.key !== 'Escape') return;
    // In the search box Escape belongs to the search: clear it, or leave it if
    // it is already empty. It is the way back to the whole corpus, and it must
    // not reach past the box and cancel an editor further down the page.
    if (event.target.id === 'q') {
      if (V.q) { $('q').value = ''; clearQuery(); } else $('q').blur();
      return;
    }
    // Only the editor holding the cursor is cancelled. With a claim editor and
    // a synthesis editor open together, cancelling both would drop a draft
    // the user never meant to give up.
    if (event.target.closest('.synth')) {
      if (V.synthEditing && !V.synthSaving) cancelSynthEdit();
    } else if (event.target.closest('.note')) {
      if (V.noteEditing && !V.noteSaving) cancelNoteEdit();
    } else if (V.editing && event.target.closest('form[data-form]')) {
      // And only from inside the claim's own form. Escape in the new-topic
      // box, the add-papers textarea, the reader's page field or a mark's
      // note is a way out of that field, not a verdict on a draft elsewhere.
      cancelEdit();
    }
    return;
  }
  // A held Command, Control or Option is the browser's or the system's: Cmd+R
  // is a reload, not a review, and taking it here would flip a claim's flag on
  // the way out of the page.
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  // The shortcuts nobody can see are the ones nobody uses, so the list is a
  // keystroke away from anywhere.
  if (event.key === '?') { event.preventDefault(); openHelp(); return; }
  if (event.key === '/') {
    // The box filters the claims, so it belongs to that view: reaching for it
    // from the map or the tensions is a way of asking to go back.
    event.preventDefault();
    focusClaimSearch();
    return;
  }
  if (V.view !== 'claims') {
    if (event.key === 'Escape' && V.view !== 'library') { showView('library'); syncHash(true); renderAll(); }
    return;
  }
  const rows = visibleClaims();
  if (event.key === 'j' || event.key === 'ArrowDown') {
    moveSelection(rows, 1);
  } else if (event.key === 'k' || event.key === 'ArrowUp') {
    moveSelection(rows, -1);
  } else if (event.key === 'n') {
    selectNextUnreviewed(rows);
  } else if (event.key === 'e') {
    const row = selectedRow(rows);
    if (row) { setReadingClaims(false); captureOpenEditor(); V.editing = row.id; renderContent(); }
  } else if (event.key === 'r') {
    const row = selectedRow(rows);
    if (!row) return;
    // Marking one reviewed moves to the next, so a paper is reviewed by
    // holding one key rather than alternating between two. Taking a review
    // back does not move: that is a correction, and it is made where it is.
    const marking = !row.reviewed;
    const following = rows[rows.findIndex((other) => other.id === row.id) + 1];
    // The request is slow enough to press j again or click another claim while
    // it runs, and that is the later decision: advancing on top of it would
    // pull the selection back to where this review started.
    const picked = handPicked;
    const took = await toggleReviewed(row);
    if (took && marking && following && handPicked === picked
        && visibleClaims().some((other) => other.id === following.id)) {
      V.selectedId = following.id;
      renderContent();
      scrollToSelected();
    }
  } else if (event.key === 'Escape') {
    // The page's own Escape: from inside the claim's form, or with nothing in
    // particular focused. With focus on some other control — a sidebar
    // button, the divider — Escape is about that control, not a verdict on
    // a draft elsewhere on the page.
    if (event.target === document.body || event.target.closest('form[data-form]')) cancelEdit();
  }
});

function scrollToSelected() {
  const el = document.querySelector('.claim.sel');
  if (el) { setReadingClaims(false); el.scrollIntoView({ block: 'nearest' }); }
}
