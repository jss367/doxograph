/* A fixed set of claim IDs keeps review progress stable as filters stop matching. */
window.FocusedReview = (() => {
  let client;
  const dialog = document.getElementById('focused-review');
  const body = document.getElementById('focus-body');
  let ids = [], workspace = null, current = null, editing = false, busy = false;
  let shown = '', shownPaper = '', generation = 0, loadingTask = null, pdf = null, pdfRender = null, pageNumber = 1;
  const rows = () => {
    const byId = new Map(client.state.claims.map(item => [item.id, item]));
    return ids.map(id => byId.get(id)).filter(Boolean);
  };
  const row = () => rows().find(item => item.id === current);
  const paperSnapshot = item => {
    const paper = client.state.papers.find(p => p.key === item?.paper);
    return JSON.stringify([paper?.title, paper?.authors, paper?.year, paper?.has_pdf]);
  };
  const path = item => `/api/papers/${encodeURIComponent(item.paper)}/claims/${encodeURIComponent(item.id)}`;
  const request = (url, options = {}) => client.request(url, {
    ...options, headers: { 'X-Doxograph-Workspace': workspace, ...options.headers },
  });
  const button = (action, label, disabled = false, primary = false) =>
    `<button type="button" data-focus="${action}" ${disabled ? 'disabled' : ''} ${primary ? 'class="primary"' : ''}>${label}</button>`;

  function parkDraft() {
    if (workspace !== client.workspaceId) return;
    const form = body.querySelector('form[data-form]');
    if (editing && form && current) V.drafts[current] = readForm(form);
  }

  function releasePDF() {
    generation += 1;
    pdfRender?.cancel();
    pdfRender = null;
    // Destruction may reject a pending PDF load/render; their callers also catch it.
    if (loadingTask) loadingTask.destroy().catch(() => {});
    loadingTask = null;
    pdf = null;
  }

  function progress() {
    const items = rows();
    const reviewed = items.filter(item => item.reviewed).length;
    const at = items.findIndex(item => item.id === current);
    const status = document.getElementById('focus-progress');
    if (status) status.textContent = `Claim ${at + 1} of ${items.length} · ${reviewed} reviewed`;
    const meter = document.getElementById('focus-meter');
    if (meter) { meter.max = items.length || 1; meter.value = reviewed; }
    const done = document.getElementById('focus-complete');
    if (done) done.hidden = reviewed !== items.length || !items.length;
  }

  function render() {
    releasePDF();
    const items = rows();
    const item = row();
    shown = JSON.stringify(item);
    shownPaper = paperSnapshot(item);
    if (!item) {
      body.innerHTML = '<p class="empty">These claims are no longer in this workspace. Exit review to choose another set.</p>';
      return;
    }
    selectClaim(item.id);
    const at = items.findIndex(other => other.id === current);
    const paper = client.state.papers.find(p => p.key === item.paper);
    const content = editing
      ? editForm({ ...item, ...V.drafts[item.id] }, 'focus-taglist')
      : `<article class="focus-claim">
          <div class="focus-meta"><span class="kind ${esc(item.kind)}">${esc(item.kind)}</span>
            <span>${item.reviewed ? 'Reviewed' : 'Unreviewed'}</span>
            <span>${esc(item.strength)}</span></div>
          <h3>${esc(item.text)}</h3>
          <p class="hint">${esc((item.tags || []).map(tag => '#' + tag).join(' '))}</p>
          ${evidenceQualityHtml(item)}
          ${item.evidence ? `<h4>Evidence</h4><p>${esc(item.evidence)}</p>` : ''}
          <h4>Quote ${item.quote_verified === false ? '<span class="qflag">not found in PDF</span>' : ''}</h4>
          ${item.quote ? `<blockquote>${esc(item.quote)}</blockquote>` : '<p class="hint">No quote recorded.</p>'}
          <p class="hint">${esc(item.locator || 'No page or section recorded.')}</p>
          ${item.note ? `<h4>My note</h4><p>${esc(item.note)}</p>` : ''}
          ${(item.ledger_links || []).map(link => `<p class="link"><strong>${esc(link.relation)}</strong>
            ${esc(client.state.ledger.find(own => own.id === link.claim)?.text || link.claim)}
            ${esc(link.note || '')}</p>`).join('')}
        </article>`;
    body.innerHTML = `<div class="focus-navigation">
        <div><p id="focus-progress" role="status" aria-live="polite"></p>
          <progress id="focus-meter" aria-label="Claims reviewed"></progress></div>
        ${button('previous', 'Previous claim', at === 0)}
        ${button('next', 'Next claim', at === items.length - 1)}
        ${button('unreviewed', 'Next unreviewed', !items.some(other => !other.reviewed))}
      </div>
      <p id="focus-complete" class="focus-complete" role="status" hidden>All claims in this session are reviewed.</p>
      <p id="focus-error" class="warn" role="alert"></p>
      <div class="focus-layout">
        <section class="focus-details" aria-label="Claim to review" tabindex="-1">
          <h3 class="focus-paper-title">${esc(paper?.title || item.paper)}</h3>
          <p class="hint">${esc((paper?.authors || []).join(', '))} ${esc(paper?.year || '')}</p>
          ${content}
          ${editing ? '<p class="hint">Unsaved edits are kept for this session when you move to another claim or exit.</p>' : `<div class="focus-actions">
            ${button('review', item.reviewed ? (at < items.length - 1 ? 'Reviewed — next claim' : 'Reviewed') : 'Mark reviewed' + (at < items.length - 1 ? ' & next' : ''), item.reviewed && at === items.length - 1, true)}
            ${button('edit', V.drafts[item.id] ? 'Resume edits' : 'Edit claim')}
            ${item.reviewed ? button('unreview', 'Mark unreviewed') : ''}
          </div>`}
        </section>
        <section class="focus-source" aria-label="Source paper">
          <div class="focus-source-heading"><h3>Source paper</h3>
            ${paper?.has_pdf ? `<a href="/pdf/${encodeURIComponent(item.paper)}?workspace=${encodeURIComponent(workspace)}&inline=1" target="_blank" rel="noopener">Open PDF</a>` : ''}</div>
          <div id="focus-passage" role="status">${paper?.has_pdf ? 'Finding the quote in the paper…' : 'No PDF is available for this paper. You can still edit and review its claims.'}</div>
          <div id="focus-pdf-controls" class="focus-pdf-controls" hidden>
            ${button('page-previous', 'Previous page')}
            <label>Page <input id="focus-page" type="number" min="1" aria-label="Source PDF page"></label>
            <span id="focus-pages"></span>${button('page-go', 'Go')}
            ${button('page-next', 'Next page')}
            <label>Zoom <select id="focus-zoom"><option value="fit">Fit width</option><option value="1">100%</option><option value="1.5">150%</option><option value="2">200%</option></select></label>
          </div>
          <div id="focus-pdf" class="focus-pdf" ${paper?.has_pdf ? '' : 'hidden'}></div>
        </section>
      </div>
      <p class="hint focus-shortcuts">j / k: next / previous · r: reviewed & next · e: edit · n: next unreviewed · Esc: exit. Shortcuts pause while typing.</p>`;
    // The shared editor's labels predate explicit associations. Give this copy
    // associations, both for assistive technology and click-to-focus behavior.
    body.querySelectorAll('form label').forEach((label, i) => {
      const field = label.parentElement.querySelector('textarea, select, input');
      if (field && field.type !== 'checkbox') { field.id = `focus-field-${i}`; label.htmlFor = field.id; }
    });
    progress();
    // Replacing the previous claim also removes the focused button. Keep
    // keyboard review inside the dialog rather than dropping focus to body.
    body.querySelector('.focus-details').focus({ preventScroll: true });
    if (paper?.has_pdf) loadSource(item, generation);
  }

  async function loadSource(item, token) {
    let context = null;
    try {
      context = await request(`${path(item)}/quote-context`);
      if (token !== generation) return;
      const passage = document.getElementById('focus-passage');
      if (context.available && context.suggestion) {
        passage.innerHTML = `<p class="hint">${context.found ? 'Quote found' : 'Closest passage'} on page ${context.page}
          ${context.locator_page && context.locator_page !== context.page && !context.repeated ? ` · locator says ${esc(item.locator)}` : ''}</p>
          <p>${esc(context.before)} <mark>${esc(context.suggestion)}</mark> ${esc(context.after)}</p>`;
      } else {
        passage.textContent = context.reason || 'No matching passage found. Check the PDF below.';
      }
    } catch (error) {
      if (token !== generation) return;
      document.getElementById('focus-passage').textContent = `Could not match the quote: ${error.message}. Check the PDF below.`;
    }
    try {
      const pdfjs = await import('/vendor/pdf.min.mjs');
      if (token !== generation) return;
      pdfjs.GlobalWorkerOptions.workerSrc = '/vendor/pdf.worker.min.mjs';
      loadingTask = pdfjs.getDocument({
        url: `/pdf/${encodeURIComponent(item.paper)}?workspace=${encodeURIComponent(workspace)}&inline=1`,
        isEvalSupported: false, cMapUrl: '/vendor/cmaps/', cMapPacked: true,
        standardFontDataUrl: '/vendor/standard_fonts/', wasmUrl: '/vendor/wasm/',
      });
      const documentPDF = await loadingTask.promise;
      if (token !== generation) return;
      pdf = documentPDF;
      pageNumber = Math.min(pdf.numPages, Math.max(1, Number(context?.page || pdfPage(item.locator) || 1)));
      document.getElementById('focus-pdf-controls').hidden = false;
      document.getElementById('focus-pages').textContent = `of ${pdf.numPages}`;
      document.getElementById('focus-page').max = pdf.numPages;
      await renderPage(token);
    } catch (error) {
      if (token === generation) document.getElementById('focus-pdf').textContent = `Could not open the PDF: ${error.message}. Use Open PDF to try it in another window.`;
    }
  }

  async function renderPage(token = generation) {
    if (!pdf) return;
    pdfRender?.cancel();
    const wanted = pageNumber;
    const documentPDF = pdf;
    try {
      const page = await documentPDF.getPage(wanted);
      if (token !== generation || wanted !== pageNumber) return;
      const container = document.getElementById('focus-pdf');
      const zoom = document.getElementById('focus-zoom').value;
      const width = page.getViewport({ scale: 1 }).width;
      const scale = zoom === 'fit' ? Math.max(.2, (container.clientWidth - 20) / width) : Number(zoom);
      const viewport = page.getViewport({ scale });
      const canvas = document.createElement('canvas');
      canvas.setAttribute('aria-label', `Source paper, page ${wanted}. The matched passage is available above.`);
      const ratio = Math.min(devicePixelRatio || 1, 2);
      canvas.width = Math.floor(viewport.width * ratio);
      canvas.height = Math.floor(viewport.height * ratio);
      canvas.style.width = `${viewport.width}px`;
      canvas.style.height = `${viewport.height}px`;
      container.replaceChildren(canvas);
      container.scrollTop = 0;
      document.getElementById('focus-page').value = wanted;
      body.querySelector('[data-focus="page-previous"]').disabled = wanted === 1;
      body.querySelector('[data-focus="page-next"]').disabled = wanted === pdf.numPages;
      pdfRender = page.render({ canvasContext: canvas.getContext('2d'), viewport,
        transform: ratio === 1 ? null : [ratio, 0, 0, ratio, 0, 0] });
      await pdfRender.promise;
    } catch (error) {
      if (token === generation && wanted === pageNumber && error.name !== 'RenderingCancelledException') {
        document.getElementById('focus-pdf').textContent = `Could not render this page: ${error.message}`;
      }
    }
  }

  function move(step, unreviewed = false) {
    parkDraft();
    const items = rows();
    const at = items.findIndex(item => item.id === current);
    const next = unreviewed
      ? items.slice(at + 1).concat(items.slice(0, at + 1)).find(item => !item.reviewed)
      : items[Math.max(0, Math.min(items.length - 1, at + step))];
    if (!next) return;
    current = next.id;
    editing = Boolean(V.drafts[current]);
    render();
  }

  async function write(patch, advance = false) {
    const item = row();
    if (!item || busy || isSaving(item.id) || refuseWhileRemoving(item.paper, 'a review or edit')) return;
    parkDraft();
    busy = true;
    markSaving(item.id, true);
    dialog.setAttribute('aria-busy', 'true');
    const enabled = [...dialog.querySelectorAll('button, input, select, textarea')].filter(field => !field.disabled);
    enabled.forEach(field => { field.disabled = true; });
    try {
      const saved = await request(path(item), { method: 'PATCH',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch) });
      if (workspace !== client.workspaceId) return;
      // A successful write stays successful even if refreshing the list fails.
      Object.assign(item, saved);
      if (editing) delete V.drafts[item.id];
      else if (V.drafts[item.id]) V.drafts[item.id].reviewed = patch.reviewed;
      editing = false;
      try { await refreshAll(); } catch (_) { /* the next poll retries the read */ }
      if (advance) move(1); else render();
    } catch (error) {
      document.getElementById('focus-error').textContent = `Could not save: ${error.message}. Your edits are kept; try again.`;
    } finally {
      busy = false;
      markSaving(item.id, false);
      dialog.removeAttribute('aria-busy');
      enabled.forEach(field => { if (field.isConnected) field.disabled = false; });
      if (workspace !== client.workspaceId) close();
    }
  }

  function close() {
    if (busy) return;
    parkDraft();
    dialog.close();
    releasePDF();
    if (workspace !== client.workspaceId) return;
    renderAll();
    scrollToSelected();
    document.getElementById('btn-focus-review').focus();
  }

  function open() {
    if (dialog.open) return;
    const items = visibleClaims();
    if (!items.length) { toast('No claims match your filters. Choose a paper or change your filters to start reviewing.'); return; }
    captureOpenEditor();
    V.editing = null;
    ids = [...new Set(items.map(item => item.id))];
    workspace = client.workspaceId;
    current = items.some(item => item.id === V.selectedId) ? V.selectedId : items[0].id;
    editing = Boolean(V.drafts[current]);
    dialog.showModal();
    render();
  }

  function sync() {
    if (!dialog.open || busy) return;
    if (workspace !== client.workspaceId) { close(); return; }
    if (!row()) { current = rows()[0]?.id; editing = Boolean(V.drafts[current]); render(); }
    else if (shownPaper !== paperSnapshot(row())) {
      // A download can finish without changing any claim. Refresh the source
      // even during an edit, keeping the draft, cursor, and reading position.
      const fields = [...body.querySelectorAll('input, textarea, select')];
      const focused = fields.indexOf(document.activeElement);
      const start = fields[focused]?.selectionStart;
      const end = fields[focused]?.selectionEnd;
      const scrollTop = body.querySelector('.focus-details').scrollTop;
      parkDraft();
      render();
      const replacement = body.querySelectorAll('input, textarea, select')[focused];
      if (replacement) {
        replacement.focus({ preventScroll: true });
        if (start != null) replacement.setSelectionRange(start, end);
      }
      body.querySelector('.focus-details').scrollTop = scrollTop;
    }
    else if (!editing && shown !== JSON.stringify(row())) render();
    else progress();
  }

  async function action(name) {
    if (busy) return;
    if (name === 'close') close();
    else if (name === 'previous') move(-1);
    else if (name === 'next') move(1);
    else if (name === 'unreviewed') move(1, true);
    else if (name === 'edit') { editing = true; render(); body.querySelector('textarea[name="text"]').focus(); }
    else if (name === 'cancel') { delete V.drafts[current]; editing = false; render(); }
    else if (name === 'review' && !editing) {
      if (row()?.reviewed) move(1); else await write({ reviewed: true }, true);
    } else if (name === 'unreview' && !editing) await write({ reviewed: false });
    else if (name.startsWith('page-') && pdf) {
      const value = name === 'page-go' ? Number(document.getElementById('focus-page').value) : pageNumber + (name === 'page-next' ? 1 : -1);
      if (!Number.isInteger(value) || value < 1 || value > pdf.numPages) return;
      pageNumber = value;
      await renderPage();
    }
  }

  document.getElementById('btn-focus-review').addEventListener('click', open);
  dialog.addEventListener('cancel', event => { event.preventDefault(); close(); });
  dialog.addEventListener('click', event => {
    const target = event.target.closest('button');
    if (!target || target.disabled) return;
    if (target.dataset.act === 'drop-link') target.closest('.linkrow').remove();
    else action(target.dataset.focus || target.dataset.act || '');
  });
  dialog.addEventListener('submit', event => {
    event.preventDefault();
    if (editing && event.target.matches('form[data-form]')) write(readForm(event.target));
  });
  dialog.addEventListener('input', parkDraft);
  dialog.addEventListener('change', event => {
    parkDraft();
    if (event.target.id === 'focus-zoom') renderPage();
  });
  dialog.addEventListener('keydown', event => {
    if (event.ctrlKey || event.metaKey || event.altKey || event.repeat) return;
    if (event.target.id === 'focus-page' && event.key === 'Enter') { event.preventDefault(); action('page-go'); return; }
    if (event.target.closest('input, textarea, select, [contenteditable="true"]')) return;
    const command = { j: 'next', k: 'previous', r: 'review', e: 'edit', n: 'unreviewed' }[event.key];
    if (command) { event.preventDefault(); action(command); }
  });
  window.addEventListener('beforeunload', event => {
    if (dialog.open && editing) { parkDraft(); event.preventDefault(); event.returnValue = ''; }
  });
  return {connect(value) { client = value; },  open, sync };
})();
