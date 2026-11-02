/* Workspace dataset inventory, including links to the evidence that uses it.
   A view in the main pane. Edits save themselves shortly after typing stops,
   and leaving the view saves at once; a draft that cannot save stays put. */
const Datasets = (() => {
  let client, navigate, select, workspace, items = [], draft = null, saved = null, busy = false, loaded = false, focusName = false, shown = false;
  let inspectedRevision;
  let requested = null, revealed = null;
  // The new draft whose create has gone out at least once.
  let posted = null;
  const el = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
  const roles = {training:'Training', evaluation:'Evaluation', reference:'Reference', 'project-output':'Project output'};
  const fields = ['name','description','source','version','license','access_notes','roles','labels','facet_values','papers','claims'];
  const body = item => Object.fromEntries(fields.map(key => [key, item[key]]));
  const dirty = () => !!draft && (!saved || JSON.stringify(body(draft)) !== JSON.stringify(body(saved)));
  const key = value => facetValueKey(value);
  const button = (action, text, attrs = '') => `<button type="button" data-dataset-action="${action}" ${attrs}>${text}</button>`;
  function status(message, error = false) {
    el('datasets-status').textContent = message;
    el('datasets-status').className = error ? 'warn' : 'hint';
  }
  const autosave = createAutosave({current: () => draft, dirty,
    blocked: () => draft.name.trim() ? '' : 'Give your dataset a name.',
    async write(target) {
      const sent = body(target), first = !saved;
      let item;
      if (first) {
        const retry = posted === target;
        posted = target;
        item = await request('','POST',sent);
        // A create retried after its answer was lost finds the first attempt
        // stored, and the server answers with that record, not this draft.
        // Save the draft over it so edits made since are not dropped.
        if (retry && draft === target && JSON.stringify(body(item)) !== JSON.stringify(sent))
          item = await request('/' + item.id,'PUT',{...sent,revision:item.revision});
      } else item = await request('/' + saved.id,'PUT',{...sent,revision:saved.revision});
      if (draft !== target) return;
      saved = structuredClone(item);
      // Take the stored spelling (a trimmed name, merged labels) unless the
      // writer has typed past it; the fields keep showing what was typed.
      if (JSON.stringify(body(target)) === JSON.stringify(sent)) {
        Object.assign(target, structuredClone(body(item)));
        const labels = el('dataset-labels');
        if (labels && document.activeElement !== labels) labels.value = target.labels.join(', ');
      }
      items = [...items.filter(row => row.id !== item.id), item];
      if (first) { select(item.id); renderActions(); }
      renderList();
    },
    changed: () => progress()});
  function progress() {
    if (draft) status(autosave.describe(), Boolean(autosave.failure));
  }
  async function request(path = '', method = 'GET', data) {
    return client.request('/api/datasets' + path, {method,
      headers:{'Content-Type':'application/json', 'X-Doxograph-Workspace':workspace,
        ...(method === 'POST' ? {'Idempotency-Key':draft.id} : {})},
      ...(data === undefined ? {} : {body:JSON.stringify(data)})});
  }
  function setBusy(value) {
    busy = value;
    el('datasets-sheet').setAttribute('aria-busy', String(value));
    el('datasets-sheet').querySelectorAll('button, input, textarea').forEach(field => field.disabled = value);
    el('dataset-new').disabled = value || !loaded;
  }
  async function run(task) {
    if (busy) return;
    setBusy(true);
    // Actions read the saved revision, so a save under way lands first. A
    // pending one is held while the editor is frozen and resumes after.
    try { await autosave.settle(); await task(); }
    catch (error) { status(error.message, true); }
    finally {
      setBusy(false);
      autosave.resume();
      if (focusName) { el('dataset-name')?.focus(); focusName = false; }
      // Hash navigation can arrive during a load, save, or discard prompt.
      // Drain the latest target once that work releases the editor.
      if (shown && loaded && requested !== revealed) void run(revealRequested);
    }
  }
  function renderList() {
    const query = el('datasets-search').value.toLowerCase().trim();
    const filtered = items.filter(item => fields.filter(f => !['papers','claims'].includes(f))
      .map(f => item[f]).join('\n').toLowerCase().includes(query)).sort((a,b) => a.name.localeCompare(b.name));
    el('datasets-list').innerHTML = filtered.map(item => button('open', `<strong>${esc(item.name)}</strong><small>${esc([item.version, ...item.roles.map(r => roles[r])].filter(Boolean).join(' · ') || 'Dataset')}</small>${item.labels.length ? `<span class="dataset-labels">${item.labels.map(label => `<span class="dataset-label">${esc(label)}</span>`).join('')}</span>` : ''}`,
      `data-id="${item.id}" ${draft?.id === item.id ? 'aria-current="page"' : ''}`)).join('')
      || `<p class="hint">${query ? 'No datasets match your search.' : 'Catalog the datasets your project uses or produces.'}</p>`;
    el('datasets-count').textContent = `${items.length} ${items.length === 1 ? 'dataset' : 'datasets'}`;
  }
  function input(field, label, multiline = false, hint = '', max = 300) {
    const value = field === 'facet_values' ? draft[field].join('\n') : field === 'labels' ? draft[field].join(', ') : draft[field];
    const help = hint ? `aria-describedby="dataset-${field}-help"` : '';
    return `<div class="dataset-field"><label for="dataset-${field}">${label}</label>${multiline
      ? `<textarea id="dataset-${field}" data-field="${field}" rows="${field === 'description' || field === 'access_notes' ? 3 : 2}" maxlength="${max}" ${help}>${esc(value)}</textarea>`
      : `<input id="dataset-${field}" data-field="${field}" maxlength="${max}" value="${esc(value)}" ${field === 'name' ? 'required' : ''} ${help}>`}
      ${hint ? `<small id="dataset-${field}-help" class="hint">${hint}</small>` : ''}${field === 'source' ? '<span id="dataset-source-link"></span>' : ''}</div>`;
  }
  function renderDocument() {
    if (!draft) {
      el('dataset-document').innerHTML = '<div class="datasets-empty"><h3>Your project’s data, in one place</h3><p>Record where each dataset lives, its version and access requirements, and the research it supports.</p><p>Add a dataset to get started.</p></div>';
      return;
    }
    el('dataset-document').innerHTML = `<form id="dataset-form">
      <div class="dataset-actions"></div>
      <div class="dataset-fields">${input('name','Dataset name')}${input('version','Version')}
      ${input('description','Description',true,'',20000)}
      ${input('source','Source URL or local path',false,'Use a local conversation JSON/JSONL file, survey export, or Gloom directory to inspect it in Loupe.',4000)}
      ${input('license','License',false,'',1000)}
      ${input('access_notes','Access notes',true,'Permissions, download instructions, or other requirements.',20000)}</div>
      ${input('labels','Labels',false,'Separate labels with commas. Labels can contain spaces, such as open source or needs review.',60398)}
      <fieldset class="dataset-roles"><legend>Role in this project</legend>${Object.entries(roles).map(([value,label]) => `<label><input type="checkbox" data-role="${value}" ${draft.roles.includes(value) ? 'checked' : ''}> ${label}</label>`).join('')}</fieldset>
      ${input('facet_values','Other dataset names in findings',true,'One per line. The dataset name and these spellings match existing dataset or datasets facet values.',60200)}
      <details class="dataset-link-picker"><summary>Link papers and claims</summary>
        <label for="dataset-link-search">Search your library</label><input type="search" id="dataset-link-search" placeholder="Find a paper or claim…">
        <div id="dataset-link-options"></div>
      </details>
      <section aria-label="Linked research"><h3>Linked research</h3><p class="hint">Includes your links and claims whose dataset facets match this record.</p><div id="dataset-research"></div></section>
      <div id="dataset-saved-version" hidden></div>
    </form>`;
    renderActions(); renderPicker(); renderResearch(); renderSource();
    if (busy) setBusy(true);
  }
  // A new record's first save swaps Cancel for the saved record's actions
  // without rebuilding the fields under the cursor.
  function renderActions() {
    const actions = el('dataset-form')?.querySelector('.dataset-actions');
    if (!actions) return;
    actions.innerHTML = saved ? button('reload','Reload saved version') + button('delete','Delete record') : button('cancel','Discard new dataset');
    if (busy) actions.querySelectorAll('button').forEach(field => field.disabled = true);
  }
  function renderSource() {
    const container = el('dataset-source-link');
    container.replaceChildren();
    if (/^(\/|~\/|file:)/.test(draft.source.trim())) {
      container.innerHTML = button('loupe', 'Open in Loupe');
      return;
    }
    try {
      const url = new URL(draft.source);
      if (!['http:','https:'].includes(url.protocol)) return;
      const link = document.createElement('a');
      link.href = url.href; link.textContent = 'Open source'; link.target = '_blank'; link.rel = 'noopener noreferrer';
      container.append(link);
    } catch (_) { /* Local paths remain available as editable text. */ }
  }
  function openInLoupe() {
    if (busy) return;
    // Reserve the viewer during the click so browsers allow the new window.
    const viewer = window.open('about:blank', '_blank');
    if (viewer) viewer.opener = null;
    run(async () => {
      status('Starting Loupe…');
      try {
        const result = await request('/loupe', 'POST', {source:draft.source});
        const url = new URL(result.url);
        if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || !url.port) throw new Error('Loupe returned an invalid local address.');
        if (viewer && !viewer.closed) viewer.location.replace(url.href);
        status('Loupe is ready. ');
        const link = document.createElement('a');
        link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer'; link.textContent = 'Open viewer';
        el('datasets-status').append(link);
      } catch (error) {
        if (viewer && !viewer.closed) viewer.close();
        throw error;
      }
    });
  }
  function renderSavedVersion(item) {
    if (!item) return '<p>This dataset record has been deleted.</p>';
    const labels = {name:'Dataset name',description:'Description',source:'Source',version:'Version',license:'License',access_notes:'Access notes',
      roles:'Roles',labels:'Labels',facet_values:'Other dataset names',papers:'Linked papers',claims:'Linked claims'};
    return '<dl>' + fields.map(field => {
      let value = item[field];
      if (field === 'roles') value = value.map(role => roles[role]).join(', ');
      else if (field === 'papers') value = value.map(id => client.state.papers.find(p => p.key === id)?.title || `Missing paper: ${id}`).join('\n');
      else if (field === 'claims') value = value.map(ref => client.state.claims.find(c => c.paper === ref.paper && c.id === ref.claim)?.text || `Missing claim: ${ref.claim}`).join('\n');
      else if (Array.isArray(value)) value = value.join('\n');
      return `<dt>${labels[field]}</dt><dd>${esc(value || 'None recorded')}</dd>`;
    }).join('') + '</dl>';
  }
  function showSavedComparison(version) {
    inspectedRevision = version?.revision ?? null;
    const comparison = el('dataset-saved-version');
    comparison.hidden = false;
    comparison.innerHTML = '<h3>Saved version</h3>' + renderSavedVersion(version) + button('replace','Replace draft with saved version');
  }
  function matches(claim) {
    const names = new Set([draft.name, ...draft.facet_values].map(key).filter(Boolean));
    return Object.entries(claim.facets || {}).some(([facet, values]) => ['dataset','datasets'].includes(facet)
      && values.some(value => names.has(key(value))));
  }
  function renderResearch() {
    if (!draft) return;
    const manual = new Set(draft.claims.map(ref => ref.paper + ':' + ref.claim));
    const claims = client.state.claims.filter(claim => manual.has(claim.paper + ':' + claim.id) || matches(claim));
    const papers = new Set([...draft.papers, ...draft.claims.map(ref => ref.paper), ...claims.map(c => c.paper)]);
    const rows = [...papers].map(paper => {
      const source = client.state.papers.find(p => p.key === paper);
      const linked = claims.filter(c => c.paper === paper);
      const removed = draft.claims.filter(ref => ref.paper === paper && !linked.some(c => c.id === ref.claim));
      return `<div class="dataset-source">${source ? button('follow',esc(source.title || paper),`data-paper="${esc(paper)}"`)
        : `<p class="hint">Missing paper: ${esc(paper)}</p>`}
        ${linked.map(claim => button('follow',esc(claim.text),`class="dataset-claim-link" data-paper="${esc(paper)}" data-claim="${esc(claim.id)}"`)).join('')}
        ${removed.map(ref => `<p class="hint">Missing claim: ${esc(ref.claim)}</p>`).join('')}</div>`;
    });
    el('dataset-research').innerHTML = rows.join('') || '<p class="hint">No linked research yet. Link sources above or use a dataset facet on your claims.</p>';
  }
  function renderPicker() {
    const query = el('dataset-link-search').value.toLowerCase().trim();
    const choices = [
      ...client.state.papers.map(p => ({paper:p.key, title:p.title || p.key, kind:'Paper', selected:draft.papers.includes(p.key)})),
      ...client.state.claims.map(c => ({paper:c.paper, claim:c.id, title:c.text, kind:'Claim', selected:draft.claims.some(ref => ref.paper === c.paper && ref.claim === c.id)})),
      ...draft.papers.filter(paper => !client.state.papers.some(p => p.key === paper)).map(paper => ({paper, title:`Missing paper: ${paper}`,kind:'Paper',selected:true})),
      ...draft.claims.filter(ref => !client.state.claims.some(c => c.paper === ref.paper && c.id === ref.claim)).map(ref => ({...ref,title:`Missing claim: ${ref.claim}`,kind:'Claim',selected:true})),
    ];
    const selected = choices.filter(choice => choice.selected);
    const available = choices.filter(choice => !choice.selected && (choice.title + ' ' + choice.paper).toLowerCase().includes(query));
    el('dataset-link-options').innerHTML = [...selected,...available.slice(0,100)].map(choice => `<label><input type="checkbox" data-link-paper="${esc(choice.paper)}" ${choice.claim ? `data-link-claim="${esc(choice.claim)}"` : ''} ${choice.selected ? 'checked' : ''}><span><small>${choice.kind}</small> ${esc(choice.title)}</span></label>`).join('')
      + (available.length > 100 ? '<p class="hint">Search to narrow the remaining results.</p>' : '')
      || '<p class="hint">No matching papers or claims.</p>';
  }
  async function discard() {
    if (await autosave.flush()) return true;
    return confirmDialog(`This dataset couldn’t be saved. ${autosave.failure} Discard your changes?`, {ok:'Discard changes'});
  }
  function choose(item) {
    inspectedRevision = undefined;
    saved = structuredClone(item); draft = structuredClone(item);
    select(item.id); renderList(); renderDocument(); progress();
  }
  function reset() {
    void autosave.settle();
    workspace = client.workspaceId; items = []; draft = saved = null; loaded = false; inspectedRevision = undefined;
    el('datasets-search').value = '';
  }
  // The record a URL names, or else the one last open, or else the first.
  async function reveal(id, current) {
    if (dirty() && (!id || id === draft.id)) { renderList(); progress(); return; }
    if (id && id !== draft?.id && !await discard()) {
      if (current()) select(saved?.id || null);
      return;
    }
    if (!current()) return;
    const item = items.find(item => item.id === (id || draft?.id)) || (!id && [...items].sort((a,b) => a.name.localeCompare(b.name))[0]);
    if (item) choose(item);
    else {
      draft = saved = null; select(null); renderList(); renderDocument();
      status(id ? 'This dataset record no longer exists.' : 'Add a dataset to this workspace.');
    }
  }
  async function revealRequested() {
    if (!shown) return;
    const target = requested;
    try { await reveal(target.id, () => shown && requested === target); }
    finally { revealed = target; }
  }
  async function show(id) {
    // The URL catching up with a record picked here is not a new destination,
    // and must not rebuild the editor under the cursor.
    if (shown && id && id === draft?.id) { requested = revealed = {id}; return; }
    const changed = !shown || requested?.id !== id;
    if (changed) requested = {id};
    if (shown) {
      if (changed && loaded) await run(revealRequested);
      return;
    }
    shown = true;
    el('datasets-sheet').hidden = false;
    if (workspace !== client.workspaceId) reset();
    renderList(); renderDocument();
    if (dirty()) progress(); else status('Loading datasets…');
    await run(async () => {
      items = (await request()).datasets; loaded = true;
      await revealRequested();
    });
  }
  function hide() {
    shown = false;
    el('datasets-sheet').hidden = true;
    void autosave.flush();
  }
  async function action(name, target) {
    if (name === 'new') {
      if (!loaded || !await discard()) return;
      saved = null;
      inspectedRevision = undefined;
      const id = Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2,'0')).join('');
      draft = {id,name:'',description:'',source:'',version:'',license:'',access_notes:'',roles:[],labels:[],facet_values:[],papers:[],claims:[]};
      select(null); renderList(); renderDocument(); status('New dataset. It saves once it has a name.');
      focusName = true;
    } else if (name === 'open') {
      if (draft?.id === target.dataset.id || !await discard()) return;
      choose(items.find(item => item.id === target.dataset.id));
    } else if (name === 'cancel') {
      if (!await confirmDialog('Discard this new dataset?', {ok:'Discard dataset'})) return;
      draft = saved = null; renderDocument(); renderList(); status('');
    } else if (name === 'reload') {
      await autosave.flush();
      const latest = (await request()).datasets;
      const version = latest.find(item => item.id === saved.id);
      // Always leave the draft intact while the reader inspects the disk version.
      showSavedComparison(version);
      items = latest; renderList(); status('Your draft is still in the editor. Inspect the saved version below before replacing it.');
    } else if (name === 'replace') {
      const latest = (await request()).datasets;
      const version = latest.find(item => item.id === saved.id);
      items = latest; renderList();
      if (inspectedRevision === undefined || (version?.revision ?? null) !== inspectedRevision) {
        showSavedComparison(version);
        status('The saved version changed again. Your draft is still here. Inspect the updated comparison before replacing it.');
        return;
      }
      // Replacing is the point here, so the draft is not saved first.
      if (dirty() && !await confirmDialog('Discard unsaved changes to this dataset?', {ok:'Discard changes'})) return;
      if (version) choose(version);
      else { draft = saved = null; select(null); renderList(); renderDocument(); status('This dataset record has been deleted.'); }
    } else if (name === 'delete') {
      if (!await confirmDialog(`Delete the dataset record “${saved.name}”? Its source files and research stay in place.`,{ok:'Delete record'})) return;
      await request('/' + saved.id + '?' + new URLSearchParams({revision:saved.revision}),'DELETE');
      items = items.filter(item => item.id !== saved.id); draft = saved = null;
      select(null); renderList(); renderDocument(); status('Dataset record deleted.');
    } else if (name === 'follow') {
      const {paper,claim} = target.dataset;
      navigate(paper,claim);
    }
  }
  el('datasets-sheet').addEventListener('click', event => {
    const target = event.target.closest('[data-dataset-action]');
    if (!target) return;
    event.preventDefault();
    if (target.dataset.datasetAction === 'loupe') { openInLoupe(); return; }
    run(() => action(target.dataset.datasetAction,target));
  });
  el('datasets-sheet').addEventListener('submit', event => { event.preventDefault(); if (!busy) void autosave.flush(); });
  el('datasets-search').addEventListener('input', renderList);
  el('datasets-sheet').addEventListener('input', event => {
    if (event.target.id === 'dataset-link-search') return renderPicker();
    const field = event.target.dataset.field;
    if (!draft || !field) return;
    draft[field] = field === 'labels' ? event.target.value.split(',').map(normalizeLabel).filter(Boolean)
      : field === 'facet_values' ? event.target.value.split('\n').map(v => v.trim()).filter(Boolean) : event.target.value;
    autosave.schedule();
    if (['name','facet_values'].includes(field)) renderResearch();
    if (field === 'source') renderSource();
  });
  el('datasets-sheet').addEventListener('change', event => {
    if (!draft) return;
    const target = event.target;
    if (target.dataset.role) draft.roles = [...el('datasets-sheet').querySelectorAll('[data-role]:checked')].map(field => field.dataset.role);
    else if (target.dataset.linkPaper) {
      const paper = target.dataset.linkPaper, claim = target.dataset.linkClaim;
      if (claim) {
        draft.claims = draft.claims.filter(ref => !(ref.paper === paper && ref.claim === claim));
        if (target.checked) draft.claims.push({paper,claim});
      } else {
        draft.papers = draft.papers.filter(value => value !== paper);
        if (target.checked) draft.papers.push(paper);
      }
      renderResearch();
    } else return;
    autosave.schedule();
  });
  el('datasets-sheet').addEventListener('keydown', event => {
    // Saving is automatic. The shortcut saves now, or retries a failed save.
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's') { event.preventDefault(); if (!busy) void autosave.flush(); }
  });
  window.addEventListener('beforeunload', event => {
    if (dirty() || busy || autosave.saving) { void autosave.flush(); event.preventDefault(); event.returnValue = ''; }
  });
  return {connect(value, follow, selected) { client = value; navigate = follow; select = selected; }, show, hide, flush: () => autosave.flush(),
    // Reset first: a draft the writer chose to discard must not save on hide.
    resetWorkspace() { reset(); hide(); },
    hasUnsavedChanges:() => dirty() || busy || autosave.saving,
    async beforeWorkspaceSwitch() {
      // A later guard may still refuse. Only committed selection clears drafts.
      return !busy && await discard();
    }};
})();
