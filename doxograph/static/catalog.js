/* A workspace catalog, such as the datasets or the benchmarks, including links
   to the evidence that uses each record. A view in the main pane. Edits save
   themselves shortly after typing stops, and leaving the view saves at once; a
   draft that cannot save stays put. `kind` names the catalog and what differs
   between catalogs; Datasets and Benchmarks below are the two there are. */
function createCatalog(kind) {
  const {one, many, One, Many} = kind;
  let client, navigate, select, workspace, items = [], draft = null, saved = null, busy = false, loaded = false, focusName = false, shown = false;
  // A paper panel's current read and its observer of confirmed cache changes.
  let reading = null, afterChange = null, cacheVisit = {}, notifiedItems = items, cacheOrder = 0, appliedRead = 0;
  function notifyCache(visit) {
    if (visit !== cacheVisit || workspace !== client.workspaceId || notifiedItems === items) return;
    notifiedItems = items;
    afterChange?.();
  }
  // Reads may overlap across the editor and paper panel. Failed reads do not
  // retire older successful ones; acknowledged mutations retire every prior read.
  async function readItems(current = () => true) {
    const visit = cacheVisit, order = ++cacheOrder;
    const latest = (await request())[many];
    if (visit !== cacheVisit || workspace !== client.workspaceId || !current()) return false;
    if (order >= appliedRead) { items = latest; loaded = true; appliedRead = order; }
    return true;
  }
  function mutateItems(change, visit) {
    if (visit !== cacheVisit || workspace !== client.workspaceId) return false;
    items = change(items); appliedRead = ++cacheOrder;
    return true;
  }
  let inspectedRevision;
  // Each record's linked research, gathered for the table being drawn.
  let researched = new WeakMap();
  let requested = null, revealed = null;
  // The new draft whose create has gone out, and each body it sent.
  let posted = null;
  // A draft whose record another window changed before this one heard back.
  let conflicted = null;
  const conflict = `This ${one} changed in another window. Your edits are still here. Reload the saved version before saving.`;
  // The draft the reader agreed to discard when a workspace switch began.
  let discarding = null;
  const writing = () => draft && (dirty() || autosave.unconfirmed || autosave.saving)
    ? JSON.stringify([draft?.id, body(draft), autosave.generation, autosave.unconfirmed]) : null;
  const el = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
  // The roles a record can play in the project, for a catalog that records them.
  const roles = kind.roles || {};
  const fields = ['name','description','source','version','license','access_notes',...(kind.roles ? ['roles'] : []),'labels','facet_values','papers','claims','custom_fields'];
  // A custom field row left entirely blank is not part of the record yet.
  const body = item => ({...Object.fromEntries(fields.map(key => [key, item[key]])),
    custom_fields:item.custom_fields.filter(field => field.name || field.value)});
  const dirty = () => !!draft && (!saved || JSON.stringify(body(draft)) !== JSON.stringify(body(saved)));
  const unique = (values, by = value => value) => values.filter((value, i) => values.findIndex(other => by(other) === by(value)) === i);
  // Python's str.strip(). JavaScript's trim() also drops U+FEFF and keeps
  // U+001C–001F and U+0085.
  const strip = value => value.replace(/^[\p{White_Space}\u001c-\u001f]+|[\p{White_Space}\u001c-\u001f]+$/gu, '');
  // Python orders strings by code point, JavaScript by UTF-16 unit.
  const byCodePoint = (a, b) => {
    const x = [...a], y = [...b];
    for (let i = 0; i < Math.min(x.length, y.length); i++)
      if (x[i] !== y[i]) return x[i].codePointAt(0) - y[i].codePointAt(0);
    return x.length - y.length;
  };
  // A body as the server stores it: the name and facet names stripped and
  // repeats dropped, and labels as `store.normalize_labels` leaves them, with
  // spacing collapsed, one spelling per label, sorted by key and then spelling.
  const stored = item => JSON.stringify({...body(item), name:strip(item.name),
    labels:unique(item.labels.map(normalizeLabel).filter(Boolean), labelKey)
      .sort((a, b) => byCodePoint(labelKey(a), labelKey(b)) || byCodePoint(a, b)),
    facet_values:unique(item.facet_values.map(strip))});
  const key = value => facetValueKey(value);
  // The table's optional columns. The name column is always shown.
  const columns = {version:'Version', license:'License', source:'Source', ...(kind.roles ? {roles:'Roles'} : {}), labels:'Labels',
    description:'Description', access_notes:'Access notes', facet_values:`Other ${one} names`,
    papers:'Linked papers', claims:'Linked claims', created:'Added', updated:'Last updated'};
  // The layout, columns, and sort are kept between visits.
  const TABLE_KEY = `doxograph-${one}-table`;
  let table = {view:'list', columns:kind.columns, sort:'name', descending:false};
  try {
    const kept = JSON.parse(localStorage.getItem(TABLE_KEY)) || {};
    if (['list','table'].includes(kept.view)) table.view = kept.view;
    if (Array.isArray(kept.columns)) table.columns = Object.keys(columns).filter(column => kept.columns.includes(column));
    // A direction without its column would reverse the name order unasked.
    if (['name', ...table.columns].includes(kept.sort)) Object.assign(table, {sort:kept.sort, descending:kept.descending === true});
  } catch (_) { /* optional */ }
  const remember = () => { try { localStorage.setItem(TABLE_KEY, JSON.stringify(table)); } catch (_) { /* preference remains for this page */ } };
  const button = (action, text, attrs = '') => `<button type="button" data-catalog-action="${action}" ${attrs}>${text}</button>`;
  function status(message, error = false) {
    el(many + '-status').textContent = message;
    el(many + '-status').className = error ? 'warn' : 'hint';
  }
  const autosave = createAutosave({current: () => draft, dirty,
    blocked: () => conflicted === draft ? conflict : !draft.name.trim() ? `Give your ${one} a name.` : customProblem(),
    async write(target) {
      const visit = cacheVisit;
      const sent = body(target), first = !saved;
      let item;
      if (first) {
        if (posted?.target !== target) posted = {target, bodies:[]};
        const retry = posted.bodies.length > 0;
        posted.bodies.push(sent);
        item = await request('','POST',sent);
        // A create retried after its answer was lost finds the record already
        // stored, and the server answers with it, not this draft. Save the
        // draft over it only while it is still what this draft created.
        if (retry && draft === target && stored(item) !== stored(sent)) {
          if (!posted.bodies.some(earlier => stored(earlier) === stored(item))) {
            // Another window changed it since. Keep the draft, as any other
            // conflict does, and hold saves until the writer reloads.
            saved = structuredClone(item); conflicted = target;
            if (!mutateItems(rows => [...rows.filter(row => row.id !== item.id), item], visit)) return;
            select(item.id); renderActions(); renderList();
            notifyCache(visit);
            throw new Error(conflict);
          }
          item = await request('/' + item.id,'PUT',{...sent,revision:item.revision});
        }
      } else item = await request('/' + saved.id,'PUT',{...sent,revision:saved.revision});
      if (draft !== target) return;
      saved = structuredClone(item);
      // Take the stored spelling (a trimmed name, merged labels) unless the
      // writer has typed past it; the fields keep showing what was typed.
      if (JSON.stringify(body(target)) === JSON.stringify(sent)) {
        Object.assign(target, structuredClone(body(item)));
        const labels = el(one + '-labels');
        if (labels && document.activeElement !== labels) labels.value = target.labels.join(', ');
      }
      if (!mutateItems(rows => [...rows.filter(row => row.id !== item.id), item], visit)) return;
      if (first) { select(item.id); renderActions(); }
      renderList();
      notifyCache(visit);
    },
    changed: () => progress()});
  function customProblem() {
    const named = body(draft).custom_fields;
    if (named.some(field => !field.name)) return 'Name each custom field.';
    if (new Set(named.map(field => foldLabel(normalizeLabel(field.name)))).size !== named.length) return 'Give each custom field a different name.';
    return '';
  }
  function progress() {
    if (draft) status(autosave.describe(), Boolean(autosave.failure));
  }
  async function request(path = '', method = 'GET', data) {
    return client.request(`/api/${many}` + path, {method,
      headers:{'Content-Type':'application/json', 'X-Doxograph-Workspace':workspace,
        ...(method === 'POST' && path === '' ? {'Idempotency-Key':draft.id} : {})},
      ...(data === undefined ? {} : {body:JSON.stringify(data)})});
  }
  function setBusy(value) {
    busy = value;
    el(many + '-sheet').setAttribute('aria-busy', String(value));
    el(many + '-sheet').querySelectorAll('button, input, textarea').forEach(field => field.disabled = value);
    el(one + '-new').disabled = value || !loaded;
  }
  async function run(task) {
    if (busy) return;
    const visit = cacheVisit;
    setBusy(true);
    // Actions read the saved revision, so a save under way lands first. A
    // pending one is held while the editor is frozen and resumes after.
    try { await autosave.settle(); await task(); }
    catch (error) { status(error.message, true); }
    finally {
      setBusy(false);
      autosave.resume();
      // Actions can confirm a delete, discard, reload or replacement even when
      // the reader has moved to a paper. Publish only this visit's cache facts.
      notifyCache(visit);
      if (focusName) { el(one + '-name')?.focus(); focusName = false; }
      // Hash navigation can arrive during a load, save, or discard prompt.
      // Drain the latest target once that work releases the editor.
      if (shown && loaded && requested !== revealed) void run(revealRequested);
    }
  }
  function renderList() {
    const query = el(many + '-search').value.toLowerCase().trim();
    const filtered = items.filter(item => fields.filter(f => !['papers','claims'].includes(f))
      .map(f => f === 'custom_fields' ? item[f].map(field => field.name + ' ' + field.value).join('\n') : item[f]).join('\n').toLowerCase().includes(query)).sort((a,b) => a.name.localeCompare(b.name));
    const empty = `<p class="hint">${query ? `No ${many} match your search.` : kind.text.listEmpty}</p>`;
    el(many + '-list').innerHTML = filtered.map(item => button('open', `<strong>${esc(item.name)}</strong><small>${esc([item.version, ...(item.roles || []).map(r => roles[r])].filter(Boolean).join(' · ') || One)}</small>${labelChips(item)}`,
      `data-id="${item.id}" ${draft?.id === item.id ? 'aria-current="page"' : ''}`)).join('') || empty;
    // Only the visible layout holds the records, so each name appears once on the page.
    el(many + '-table').innerHTML = table.view === 'table' ? renderTable(filtered) || empty : '';
    el(many + '-count').textContent = `${items.length} ${items.length === 1 ? one : many}`;
    const named = loaded ? unnamed() : [];
    el(one + '-from-facets').hidden = !named.length;
    el(one + '-from-facets').textContent = `Add ${named.length === 1 ? kind.a : `${named.length} ${many}`} named in claims`;
  }
  // The names claims give in this catalog's facets that no record covers yet,
  // the most used first, each spelled as most claims spell it.
  function unnamed() {
    const covered = new Set(items.flatMap(item => [item.name, ...item.facet_values]).map(key));
    const found = new Map();
    for (const facet of kind.facets) {
      for (const [value, count] of Object.entries(facetEntry(client.state.facet_counts, facet) || {})) {
        const name = key(value);
        if (!name || covered.has(name)) continue;
        const seen = found.get(name);
        found.set(name, {value:seen?.value ?? value, count:(seen?.count ?? 0) + count});
      }
    }
    return [...found.values()].sort((a, b) => b.count - a.count || a.value.localeCompare(b.value)).map(entry => entry.value);
  }
  const labelChips = item => item.labels.length ? `<span class="catalog-labels">${item.labels.map(label => `<span class="catalog-label">${esc(label)}</span>`).join('')}</span>` : '';
  function cell(item, column) {
    const value = item[column];
    // Counts match the editor's Linked research, which is costly to gather,
    // so each render gathers it once per record.
    if (column === 'papers' || column === 'claims') {
      if (!researched.has(item)) researched.set(item, research(item));
      const linked = researched.get(item);
      return column === 'papers' ? linked.papers.length : linked.claims.length + linked.missing.length;
    }
    if (column === 'roles') return value.map(role => roles[role]).join(', ');
    if (column === 'created' || column === 'updated') return value ? new Date(value).toLocaleDateString() : '';
    return Array.isArray(value) ? value.join(', ') : value;
  }
  // Empty values sort last in either direction; ties fall back to the name.
  function compare(a, b) {
    const sortable = item => ['created','updated'].includes(table.sort) ? item[table.sort] || '' : cell(item, table.sort);
    const x = sortable(a), y = sortable(b);
    if ((x === '') !== (y === '')) return x === '' ? 1 : -1;
    const order = typeof x === 'number' ? x - y : String(x).localeCompare(String(y), undefined, {numeric:true, sensitivity:'base'});
    return (table.descending ? -order : order) || a.name.localeCompare(b.name);
  }
  function renderTable(rows) {
    if (!rows.length) return '';
    researched = new WeakMap();
    const header = column => {
      const sorted = table.sort === column;
      return `<th scope="col" aria-sort="${sorted ? (table.descending ? 'descending' : 'ascending') : 'none'}">${button('sort',
        `${column === 'name' ? 'Name' : columns[column]}<span aria-hidden="true">${sorted ? (table.descending ? ' ↓' : ' ↑') : ''}</span>`, `data-column="${column}"`)}</th>`;
    };
    return `<table><thead><tr>${['name', ...table.columns].map(header).join('')}</tr></thead><tbody>${[...rows].sort(compare).map(item =>
      `<tr data-catalog-action="open" data-id="${item.id}" ${draft?.id === item.id ? 'aria-current="true"' : ''}><th scope="row">${button('open', esc(item.name), `data-id="${item.id}"`)}</th>${table.columns.map(column =>
        `<td class="catalog-cell-${column}">${column === 'labels' ? labelChips(item) : ['description','access_notes'].includes(column) ? `<span class="catalog-clamp">${esc(cell(item, column))}</span>` : esc(cell(item, column))}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
  }
  function showView(view) {
    table.view = view;
    el(many + '-sheet').classList.toggle('table-view', view === 'table');
    el(many + '-sheet').querySelectorAll('[data-view]').forEach(choice => choice.setAttribute('aria-pressed', String(choice.dataset.view === view)));
    // Like the rows, the column choices exist only while the table shows, so
    // their names never collide with the editor's fields.
    el(one + '-column-options').innerHTML = view === 'table' ? Object.entries(columns).map(([column, label]) =>
      `<label><input type="checkbox" data-table-column="${column}" ${table.columns.includes(column) ? 'checked' : ''}> ${label}</label>`).join('') : '';
  }
  function setView(view) {
    if (table.view === view) return;
    showView(view); remember(); renderList();
    select(saved?.id ?? null);
  }
  function sortBy(column) {
    table.descending = table.sort === column && !table.descending;
    table.sort = column;
    remember(); renderList();
    el(many + '-table').querySelector(`[data-column="${column}"]`)?.focus();
  }
  function input(field, label, multiline = false, hint = '', max = 300) {
    const value = field === 'facet_values' ? draft[field].join('\n') : field === 'labels' ? draft[field].join(', ') : draft[field];
    const help = hint ? `aria-describedby="${one}-${field}-help"` : '';
    return `<div class="catalog-field"><label for="${one}-${field}">${label}</label>${multiline
      ? `<textarea id="${one}-${field}" data-field="${field}" rows="${field === 'description' || field === 'access_notes' ? 3 : 2}" maxlength="${max}" ${help}>${esc(value)}</textarea>`
      : `<input id="${one}-${field}" data-field="${field}" maxlength="${max}" value="${esc(value)}" ${field === 'name' ? 'required' : ''} ${help}>`}
      ${hint ? `<small id="${one}-${field}-help" class="hint">${hint}</small>` : ''}${field === 'source' ? `<span id="${one}-source-link" class="catalog-source-link"></span>` : ''}</div>`;
  }
  function customRow(field = {name:'', value:''}) {
    return `<div class="catalog-custom-field"><input data-custom="name" aria-label="Field name" placeholder="Field name, such as Model" maxlength="100" list="${one}-field-names" value="${esc(field.name)}">
      <input data-custom="value" aria-label="${esc(field.name || 'Field value')}" maxlength="4000" value="${esc(field.value)}">
      <button type="button" data-custom-action="remove" aria-label="Remove ${esc(field.name || 'field')}">Remove</button></div>`;
  }
  // Names used on other records, so a field like Model is spelled one way.
  function customNames() {
    const names = unique(items.flatMap(item => item.custom_fields.map(field => field.name)), name => foldLabel(normalizeLabel(name)));
    return names.sort(byCodePoint).map(name => `<option value="${esc(name)}"></option>`).join('');
  }
  function readCustomFields() {
    draft.custom_fields = [...el(one + '-custom-list').children].map(row => ({
      name:strip(row.querySelector('[data-custom="name"]').value), value:row.querySelector('[data-custom="value"]').value}));
  }
  function renderDocument() {
    if (!draft) {
      el(one + '-document').innerHTML = `<div class="catalog-empty">${kind.text.welcome}<p>Add ${kind.a} to get started.</p></div>`;
      return;
    }
    el(one + '-document').innerHTML = `<form id="${one}-form">
      <div class="catalog-actions"></div>
      <div class="catalog-fields">${input('name',`${One} name`)}${input('version','Version')}
      ${input('description','Description',true,'',20000)}
      ${input('source','Source URL or local path',false,kind.text.source,4000)}
      ${input('license','License',false,'',1000)}
      ${input('access_notes','Access notes',true,'Permissions, download instructions, or other requirements.',20000)}</div>
      <fieldset class="catalog-custom-fields"><legend>Custom fields</legend>
        <p class="hint">${kind.text.custom}</p>
        <div id="${one}-custom-list">${draft.custom_fields.map(customRow).join('')}</div>
        <button type="button" data-custom-action="add">Add field</button>
        <datalist id="${one}-field-names">${customNames()}</datalist>
      </fieldset>
      ${input('labels','Labels',false,'Separate labels with commas. Labels can contain spaces, such as open source or needs review.',60398)}
      ${kind.roles ? `<fieldset class="catalog-roles"><legend>Role in this project</legend>${Object.entries(roles).map(([value,label]) => `<label><input type="checkbox" data-role="${value}" ${draft.roles.includes(value) ? 'checked' : ''}> ${label}</label>`).join('')}</fieldset>` : ''}
      ${input('facet_values',`Other ${one} names in findings`,true,`One per line. The ${one} name and these spellings match existing ${kind.facets.join(' or ')} facet values.`,60200)}
      <details class="catalog-link-picker"><summary>Link papers and claims</summary>
        <label for="${one}-link-search">Search your library</label><input type="search" id="${one}-link-search" class="catalog-link-search" placeholder="Find a paper or claim…">
        <div id="${one}-link-options" class="catalog-link-options"></div>
      </details>
      <section aria-label="Linked research"><h3>Linked research</h3><p class="hint">Includes your links and claims whose ${one} facets match this record.</p><div id="${one}-research"></div></section>
      <div id="${one}-saved-version" class="catalog-saved-version" hidden></div>
    </form>`;
    renderActions(); renderPicker(); renderResearch(); renderSource();
    if (busy) setBusy(true);
  }
  // A new record's first save swaps Cancel for the saved record's actions
  // without rebuilding the fields under the cursor.
  function renderActions() {
    const actions = el(one + '-form')?.querySelector('.catalog-actions');
    if (!actions) return;
    actions.innerHTML = saved ? button('reload','Reload saved version') + button('delete','Delete record') : button('cancel',`Discard new ${one}`);
    if (busy) actions.querySelectorAll('button').forEach(field => field.disabled = true);
  }
  function renderSource() {
    const container = el(one + '-source-link');
    container.replaceChildren();
    if (kind.loupe && /^(\/|~\/|file:)/.test(draft.source.trim())) {
      container.innerHTML = button('loupe', 'Open in Loupe');
      return;
    }
    if (kind.loupe && huggingFace(draft.source)) container.innerHTML = button('loupe', 'Open in Loupe');
    try {
      const url = new URL(draft.source);
      if (!['http:','https:'].includes(url.protocol)) return;
      const link = document.createElement('a');
      link.href = url.href; link.textContent = 'Open source'; link.target = '_blank'; link.rel = 'noopener noreferrer';
      container.append(link);
    } catch (_) { /* Local paths remain available as editable text. */ }
  }
  // Loupe downloads these itself; other remote sources need a local copy.
  function huggingFace(source) {
    return /^(https?:\/\/(www\.)?huggingface\.co\/|hf:\/\/)datasets\/[^/?#]+\/[^/?#]+/i.test(source.trim());
  }
  function openInLoupe() {
    if (busy) return;
    // Reserve the viewer during the click so browsers allow the new window.
    const viewer = window.open('about:blank', '_blank');
    if (viewer) viewer.opener = null;
    run(async () => {
      status(huggingFace(draft.source) ? 'Downloading the dataset for Loupe. Large datasets can take a few minutes…' : 'Starting Loupe…');
      try {
        const result = await request('/loupe', 'POST', {source:draft.source});
        const url = new URL(result.url);
        if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || !url.port) throw new Error('Loupe returned an invalid local address.');
        if (viewer && !viewer.closed) viewer.location.replace(url.href);
        status('Loupe is ready. ');
        const link = document.createElement('a');
        link.href = url.href; link.target = '_blank'; link.rel = 'noopener noreferrer'; link.textContent = 'Open viewer';
        el(many + '-status').append(link);
      } catch (error) {
        if (viewer && !viewer.closed) viewer.close();
        throw error;
      }
    });
  }
  function renderSavedVersion(item) {
    if (!item) return `<p>This ${one} record has been deleted.</p>`;
    const labels = {name:`${One} name`,description:'Description',source:'Source',version:'Version',license:'License',access_notes:'Access notes',
      roles:'Roles',labels:'Labels',facet_values:`Other ${one} names`,papers:'Linked papers',claims:'Linked claims',custom_fields:'Custom fields'};
    return '<dl>' + fields.map(field => {
      let value = item[field];
      if (field === 'roles') value = value.map(role => roles[role]).join(', ');
      else if (field === 'papers') value = value.map(id => client.state.papers.find(p => p.key === id)?.title || `Missing paper: ${id}`).join('\n');
      else if (field === 'custom_fields') value = value.map(custom => `${custom.name}: ${custom.value}`).join('\n');
      else if (field === 'claims') value = value.map(ref => client.state.claims.find(c => c.paper === ref.paper && c.id === ref.claim)?.text || `Missing claim: ${ref.claim}`).join('\n');
      else if (Array.isArray(value)) value = value.join('\n');
      return `<dt>${labels[field]}</dt><dd>${esc(value || 'None recorded')}</dd>`;
    }).join('') + '</dl>';
  }
  function showSavedComparison(version) {
    inspectedRevision = version?.revision ?? null;
    const comparison = el(one + '-saved-version');
    comparison.hidden = false;
    comparison.innerHTML = '<h3>Saved version</h3>' + renderSavedVersion(version) + button('replace','Replace draft with saved version');
  }
  // A record's research as the editor lists it: its own links, the papers of
  // linked claims, claims whose facets name it, and removed sources.
  function facetMatch(item) {
    const names = new Set([item.name, ...item.facet_values].map(key).filter(Boolean));
    return claim => Object.entries(claim.facets || {}).some(([facet, values]) => kind.facets.includes(facet)
      && values.some(value => names.has(key(value))));
  }
  function research(item) {
    const matches = facetMatch(item);
    const manual = new Set(item.claims.map(ref => ref.paper + ':' + ref.claim));
    const claims = client.state.claims.filter(claim => manual.has(claim.paper + ':' + claim.id) || matches(claim));
    const missing = item.claims.filter(ref => !claims.some(c => c.paper === ref.paper && c.id === ref.claim));
    const papers = [...new Set([...item.papers, ...item.claims.map(ref => ref.paper), ...claims.map(c => c.paper)])];
    return {papers, claims, missing};
  }
  function renderResearch() {
    if (!draft) return;
    const {papers, claims, missing} = research(draft);
    const rows = papers.map(paper => {
      const source = client.state.papers.find(p => p.key === paper);
      const linked = claims.filter(c => c.paper === paper);
      const removed = missing.filter(ref => ref.paper === paper);
      return `<div class="catalog-source">${source ? button('follow',esc(source.title || paper),`data-paper="${esc(paper)}"`)
        : `<p class="hint">Missing paper: ${esc(paper)}</p>`}
        ${linked.map(claim => button('follow',esc(claim.text),`class="catalog-claim-link" data-paper="${esc(paper)}" data-claim="${esc(claim.id)}"`)).join('')}
        ${removed.map(ref => `<p class="hint">Missing claim: ${esc(ref.claim)}</p>`).join('')}</div>`;
    });
    el(one + '-research').innerHTML = rows.join('') || `<p class="hint">No linked research yet. Link sources above or use ${kind.a_facet} on your claims.</p>`;
  }
  function renderPicker() {
    const query = el(one + '-link-search').value.toLowerCase().trim();
    const choices = [
      ...client.state.papers.map(p => ({paper:p.key, title:p.title || p.key, kind:'Paper', selected:draft.papers.includes(p.key)})),
      ...client.state.claims.map(c => ({paper:c.paper, claim:c.id, title:c.text, kind:'Claim', selected:draft.claims.some(ref => ref.paper === c.paper && ref.claim === c.id)})),
      ...draft.papers.filter(paper => !client.state.papers.some(p => p.key === paper)).map(paper => ({paper, title:`Missing paper: ${paper}`,kind:'Paper',selected:true})),
      ...draft.claims.filter(ref => !client.state.claims.some(c => c.paper === ref.paper && c.id === ref.claim)).map(ref => ({...ref,title:`Missing claim: ${ref.claim}`,kind:'Claim',selected:true})),
    ];
    const selected = choices.filter(choice => choice.selected);
    const available = choices.filter(choice => !choice.selected && (choice.title + ' ' + choice.paper).toLowerCase().includes(query));
    el(one + '-link-options').innerHTML = [...selected,...available.slice(0,100)].map(choice => `<label><input type="checkbox" data-link-paper="${esc(choice.paper)}" ${choice.claim ? `data-link-claim="${esc(choice.claim)}"` : ''} ${choice.selected ? 'checked' : ''}><span><small>${choice.kind}</small> ${esc(choice.title)}</span></label>`).join('')
      + (available.length > 100 ? '<p class="hint">Search to narrow the remaining results.</p>' : '')
      || '<p class="hint">No matching papers or claims.</p>';
  }
  async function discard() {
    if (await autosave.flush()) return true;
    return confirmDialog(`This ${one} couldn’t be saved. ${autosave.failure} Discard your changes?`, {ok:'Discard changes'});
  }
  // The record a new draft's create stored, and whether it still holds only
  // what this draft wrote. A record another window changed since is theirs.
  async function created(target) {
    if (saved) return {item:saved, ours:conflicted !== target};
    if (posted?.target !== target) return {};
    if (!await readItems()) return {};
    const item = items.find(row => row.id === target.id);
    return {item, ours:!!item && posted.bodies.some(sent => stored(sent) === stored(item))};
  }
  function choose(item) {
    inspectedRevision = undefined;
    saved = structuredClone(item); draft = structuredClone(item);
    select(item.id); renderList(); renderDocument(); progress();
  }
  function reset() {
    void autosave.settle();
    workspace = client.workspaceId; items = []; draft = saved = null; loaded = false; inspectedRevision = undefined;
    reading = afterChange = null;
    cacheVisit = {}; notifiedItems = items; appliedRead = ++cacheOrder;
    el(many + '-search').value = '';
  }
  // The record a URL names, or else the one last open, or else the first.
  async function reveal(id, current) {
    if ((dirty() || autosave.unconfirmed) && (!id || id === draft.id)) { renderList(); progress(); return; }
    if (id && id !== draft?.id && !await discard()) {
      if (current()) select(saved?.id || null);
      return;
    }
    if (!current()) return;
    // A record made since the list was read, in another window.
    if (id && !items.some(item => item.id === id)) {
      if (!await readItems(current)) return;
    }
    const item = items.find(item => item.id === (id || draft?.id)) || (!id && [...items].sort((a,b) => a.name.localeCompare(b.name))[0]);
    if (item) choose(item);
    else {
      draft = saved = null; select(null); renderList(); renderDocument();
      status(id ? `This ${one} record no longer exists.` : `Add ${kind.a} to this workspace.`);
    }
  }
  async function revealRequested() {
    if (!shown) return;
    const target = requested;
    try { await reveal(target.id, () => shown && requested === target); }
    finally { revealed = target; }
  }
  async function show(id) {
    // A link to a record is a request to read it, whatever layout was last used.
    if (id && table.view === 'table') { showView('list'); remember(); renderList(); }
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
    el(many + '-sheet').hidden = false;
    if (workspace !== client.workspaceId) reset();
    renderList(); renderDocument();
    if (dirty()) progress(); else status(`Loading ${many}…`);
    await run(async () => {
      if (await readItems()) await revealRequested();
    });
  }
  function hide() {
    const wasShown = shown;
    shown = false;
    el(many + '-sheet').hidden = true;
    if (wasShown) void autosave.flush();
  }
  async function action(name, target) {
    const visit = cacheVisit;
    // Opening or adding a record shows it in the editor.
    if (name === 'new' || name === 'open') setView('list');
    if (name === 'new') {
      if (!loaded || !await discard()) return;
      saved = null;
      inspectedRevision = undefined;
      const id = Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2,'0')).join('');
      draft = {id,name:'',description:'',source:'',version:'',license:'',access_notes:'',...(kind.roles ? {roles:[]} : {}),labels:[],facet_values:[],papers:[],claims:[],custom_fields:[]};
      select(null); renderList(); renderDocument(); status(`New ${one}. It saves once it has a name.`);
      focusName = true;
    } else if (name === 'open') {
      if (draft?.id === target.dataset.id || !await discard()) return;
      choose(items.find(item => item.id === target.dataset.id));
    } else if (name === 'cancel') {
      if (!await confirmDialog(`Discard this new ${one}?`, {ok:`Discard ${one}`})) return;
      // Its create can land while the prompt waits, or land with its answer
      // lost. Discarding the draft also deletes the record it made.
      await autosave.settle();
      const {item, ours} = await created(draft);
      if (visit !== cacheVisit) return;
      if (item && !ours) {
        // Another window changed the record, so it is theirs to keep.
        choose(item);
        status(`Another window changed this ${one}, so it was kept. This is its saved version.`);
        return;
      }
      if (item) {
        await request('/' + item.id + '?' + new URLSearchParams({revision:item.revision}),'DELETE');
        if (!mutateItems(rows => rows.filter(row => row.id !== item.id), visit)) return;
      }
      draft = saved = null; select(null); renderDocument(); renderList(); status('');
    } else if (name === 'reload') {
      await autosave.flush();
      if (!await readItems()) return;
      const version = items.find(item => item.id === saved.id);
      // Always leave the draft intact while the reader inspects the disk version.
      showSavedComparison(version);
      renderList(); status('Your draft is still in the editor. Inspect the saved version below before replacing it.');
    } else if (name === 'replace') {
      if (!await readItems()) return;
      const version = items.find(item => item.id === saved.id);
      renderList();
      if (inspectedRevision === undefined || (version?.revision ?? null) !== inspectedRevision) {
        showSavedComparison(version);
        status('The saved version changed again. Your draft is still here. Inspect the updated comparison before replacing it.');
        return;
      }
      // Replacing is the point here, so the draft is not saved first.
      if (dirty() && !await confirmDialog(`Discard unsaved changes to this ${one}?`, {ok:'Discard changes'})) return;
      if (version) choose(version);
      else { draft = saved = null; select(null); renderList(); renderDocument(); status(`This ${one} record has been deleted.`); }
    } else if (name === 'delete') {
      if (!await confirmDialog(`Delete the ${one} record “${saved.name}”? ${kind.text.deleteKeeps}`,{ok:'Delete record'})) return;
      const id = saved.id;
      await request('/' + id + '?' + new URLSearchParams({revision:saved.revision}),'DELETE');
      if (!mutateItems(rows => rows.filter(item => item.id !== id), visit)) return;
      draft = saved = null;
      select(null); renderList(); renderDocument(); status(`${One} record deleted.`);
    } else if (name === 'from-facets') {
      const names = unnamed();
      if (!names.length) return;
      if (!await confirmDialog(`Add a record for each ${one} your claims name?`,
                               {note:names.join(', '), ok:`Add ${names.length === 1 ? kind.a : `${names.length} ${many}`}`})) return;
      const added = (await request('/from-facets', 'POST', {names}))[many];
      if (!mutateItems(rows => [...rows, ...added], visit)) return;
      renderList();
      if (!draft && added.length) choose([...added].sort((a, b) => a.name.localeCompare(b.name))[0]);
      status(`Added ${added.length} ${added.length === 1 ? one : many}.`);
    } else if (name === 'follow') {
      const {paper,claim} = target.dataset;
      navigate(paper,claim);
    }
  }
  el(many + '-sheet').addEventListener('click', event => {
    const custom = event.target.closest('[data-custom-action]');
    if (custom && draft && !busy) {
      if (custom.dataset.customAction === 'add') {
        el(one + '-custom-list').insertAdjacentHTML('beforeend', customRow());
        el(one + '-custom-list').lastElementChild.querySelector('input').focus();
      } else {
        custom.closest('.catalog-custom-field').remove();
        el(one + '-form').querySelector('[data-custom-action="add"]').focus();
      }
      readCustomFields();
      autosave.schedule();
      return;
    }
    const target = event.target.closest('[data-catalog-action]');
    if (!target) return;
    // Selecting text in a table row is not a request to open it.
    if (target.tagName === 'TR' && !getSelection().isCollapsed) return;
    event.preventDefault();
    const name = target.dataset.catalogAction;
    if (name === 'loupe') { openInLoupe(); return; }
    if (name === 'view') { setView(target.dataset.view); return; }
    if (name === 'sort') { sortBy(target.dataset.column); return; }
    run(() => action(name,target));
  });
  el(many + '-sheet').addEventListener('submit', event => { event.preventDefault(); if (!busy) void autosave.flush(); });
  el(many + '-search').addEventListener('input', renderList);
  el(many + '-sheet').addEventListener('input', event => {
    if (event.target.id === one + '-link-search') return renderPicker();
    if (draft && event.target.dataset.custom) {
      readCustomFields();
      if (event.target.dataset.custom === 'name') {
        const row = event.target.closest('.catalog-custom-field'), name = strip(event.target.value);
        row.querySelector('[data-custom="value"]').setAttribute('aria-label', name || 'Field value');
        row.querySelector('[data-custom-action]').setAttribute('aria-label', `Remove ${name || 'field'}`);
      }
      return autosave.schedule();
    }
    const field = event.target.dataset.field;
    if (!draft || !field) return;
    draft[field] = field === 'labels' ? event.target.value.split(',').map(normalizeLabel).filter(Boolean)
      : field === 'facet_values' ? event.target.value.split('\n').map(v => v.trim()).filter(Boolean) : event.target.value;
    autosave.schedule();
    if (['name','facet_values'].includes(field)) renderResearch();
    if (field === 'source') renderSource();
  });
  el(many + '-sheet').addEventListener('change', event => {
    const target = event.target;
    if (target.dataset.tableColumn) {
      const column = target.dataset.tableColumn;
      table.columns = Object.keys(columns).filter(other => other === column ? target.checked : table.columns.includes(other));
      // A hidden column cannot explain the order, so the table sorts by name again.
      if (!table.columns.includes(table.sort) && table.sort !== 'name') Object.assign(table, {sort:'name', descending:false});
      remember(); renderList();
      return;
    }
    if (!draft) return;
    if (target.dataset.role) draft.roles = [...el(many + '-sheet').querySelectorAll('[data-role]:checked')].map(field => field.dataset.role);
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
  el(many + '-sheet').addEventListener('keydown', event => {
    // Saving is automatic. The shortcut saves now, or retries a failed save.
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's') { event.preventDefault(); if (!busy) void autosave.flush(); }
  });
  showView(table.view);
  window.addEventListener('beforeunload', event => {
    if (dirty() || busy || autosave.saving || autosave.unconfirmed) { void autosave.flush(); event.preventDefault(); event.returnValue = ''; }
  });
  return {connect(value, follow, selected) {
      client = value; navigate = follow;
      // The table names no record, so its address does not either. An address
      // that names one opens it in the editor.
      select = id => selected(table.view === 'table' ? null : id);
    }, show, hide, flush: () => autosave.flush(),
    // The records a paper's panel lists: those whose Linked research names the
    // paper, through their own links, linked claims, or matching facets.
    forPaper(key) {
      if (!loaded || workspace !== client?.workspaceId) return [];
      const claims = client.state.claims.filter(claim => claim.paper === key);
      return items.filter(item => item.papers.includes(key) || item.claims.some(ref => ref.paper === key) || claims.some(facetMatch(item)))
        .sort((a, b) => a.name.localeCompare(b.name));
    },
    loaded: () => loaded && workspace === client?.workspaceId,
    // Observe confirmed cache changes for the current paper panel, reading the
    // list if needed. Only this visit's request may publish or clear its task;
    // leaving and returning to the same workspace is a new visit too.
    ensureLoaded(onChange) {
      if (!client) return;
      if (workspace !== client.workspaceId) reset();
      afterChange = onChange;
      if (loaded || reading?.workspace === workspace) return;
      const visit = cacheVisit, task = reading = {workspace};
      readItems(() => reading === task).then(current => {
        if (current) notifyCache(visit);
      }, () => { /* the panel lists none until the next look */ })
        .finally(() => { if (reading === task) reading = null; });
    },
    // Reset first: a draft the writer chose to discard must not save on hide.
    resetWorkspace() { reset(); hide(); },
    hasUnsavedChanges:() => dirty() || busy || autosave.saving || autosave.unconfirmed,
    async beforeWorkspaceSwitch() {
      // A later guard may still refuse. Only committed selection clears drafts.
      if (busy || !await discard()) return false;
      discarding = writing();
      return true;
    },
    // Held deletes leave the editor open while they finish. Edits made in
    // that time were never agreed to; ask about them before the switch commits.
    async beforeWorkspaceSelect() {
      if (busy || autosave.saving) return false;
      if (writing() !== discarding && !await discard()) return false;
      discarding = writing();
      return true;
    },
    // No asynchronous gap remains between this check and committed selection.
    writingSnapshot: writing,
    canWorkspaceSelect: () => !busy && !autosave.saving && (writing() === null || writing() === discarding)};
}

const Datasets = createCatalog({one:'dataset', many:'datasets', One:'Dataset', Many:'Datasets', a:'a dataset',
  a_facet:'a dataset facet', facets:['dataset', 'datasets'], loupe:true, columns:['version', 'license', 'roles', 'labels'],
  roles:{training:'Training', evaluation:'Evaluation', reference:'Reference', 'project-output':'Project output'},
  text:{listEmpty:'Catalog the datasets your project uses or produces.',
    welcome:'<h3>Your project’s data, in one place</h3><p>Record where each dataset lives, its version and access requirements, and the research it supports.</p>',
    source:'Use a local conversation JSON/JSONL file, survey export, Gloom directory, or Hugging Face dataset URL to inspect it in Loupe.',
    custom:'Record other details, such as the model that generated the data.',
    deleteKeeps:'Its source files and research stay in place.'}});

const Benchmarks = createCatalog({one:'benchmark', many:'benchmarks', One:'Benchmark', Many:'Benchmarks', a:'a benchmark',
  a_facet:'a benchmark facet', facets:['benchmark', 'benchmarks'], loupe:false, roles:null,
  columns:['version', 'labels', 'papers', 'claims'],
  text:{listEmpty:'Catalog the benchmarks your papers measure results on.',
    welcome:'<h3>The benchmarks your research measures on</h3><p>Record what each benchmark tests, where it lives, its version, and the results reported on it.</p>',
    source:'A web page, repository, or local path for the benchmark.',
    custom:'Record other details, such as the metric it reports or the number of tasks.',
    deleteKeeps:'The research that uses it stays in place.'}});
