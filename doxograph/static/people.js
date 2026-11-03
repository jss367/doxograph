/* The people behind the papers. Every author in the library has a page with
   their papers, claims, and coauthors; a record adds what the reader knows
   about them and the other spellings author lists use for them. A view in the
   main pane. Edits save themselves shortly after typing stops, and leaving
   the view saves at once; a draft that cannot save stays put. */
const People = (() => {
  let client, navigate, select, workspace, items = [], draft = null, saved = null, busy = false, loaded = false, focusField = null, shown = false;
  // The author on screen when no record holds their name.
  let author = null;
  let inspectedRevision;
  let requested = null, revealed = null;
  // The new draft whose create has gone out, and each body it sent.
  let posted = null;
  // A draft whose record another window changed before this one heard back.
  let conflicted = null;
  const conflict = 'This person changed in another window. Your edits are still here. Reload the saved version before saving.';
  // The draft the reader agreed to discard when a workspace switch began.
  let discarding = null;
  const writing = () => draft && (dirty() || autosave.unconfirmed || autosave.saving)
    ? JSON.stringify([draft?.id, body(draft), autosave.generation, autosave.unconfirmed]) : null;
  const el = id => document.getElementById(id);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
  const fields = ['name','other_names','affiliation','homepage','notes','labels','papers'];
  const body = item => Object.fromEntries(fields.map(key => [key, item[key]]));
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
  // `people.name_key`: case, accents, punctuation, and spacing do not make
  // two names. An author spelling already in the library takes the server's key.
  const nameKey = name => Object.hasOwn(client.state.author_keys || {}, name) ? client.state.author_keys[name]
    : foldLabel((name.normalize('NFKD').replace(/\p{Mn}/gu, '').match(/[\p{L}\p{N}]+/gu) || []).join(' '));
  const names = item => new Set([item.name, ...item.other_names].map(nameKey).filter(Boolean));
  // A body as the server stores it: names stripped and one spelling per key,
  // and labels as `store.normalize_labels` leaves them.
  const stored = item => {
    const name = nameKey(item.name);
    return JSON.stringify({...body(item), name:strip(item.name),
      other_names:unique(item.other_names.map(strip), nameKey).filter(value => nameKey(value) !== name),
      labels:unique(item.labels.map(normalizeLabel).filter(Boolean), labelKey)
        .sort((a, b) => byCodePoint(labelKey(a), labelKey(b)) || byCodePoint(a, b)),
      papers:unique(item.papers)});
  };
  const button = (action, text, attrs = '') => `<button type="button" data-person-action="${action}" ${attrs}>${text}</button>`;
  const plural = (n, one, many = one + 's') => `${n} ${n === 1 ? one : many}`;
  function status(message, error = false) {
    el('people-status').textContent = message;
    el('people-status').className = error ? 'warn' : 'hint';
  }
  // The record that already goes by one of this draft's names, if any.
  function clash(target) {
    const keys = [target.name, ...target.other_names].map(nameKey).filter(Boolean);
    for (const other of items) {
      if (other.id === target.id) continue;
      const taken = names(other);
      const index = keys.findIndex(key => taken.has(key));
      if (index >= 0) return `“${strip([target.name, ...target.other_names][index])}” already names ${other.name}. Add it to one record only.`;
    }
    return '';
  }
  const autosave = createAutosave({current: () => draft, dirty,
    blocked: () => conflicted === draft ? conflict : !nameKey(draft.name) ? 'Give this person a name.' : clash(draft),
    async write(target) {
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
            items = [...items.filter(row => row.id !== item.id), item];
            select(item.id, null); renderActions(); renderList();
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
        const labels = el('person-labels');
        if (labels && document.activeElement !== labels) labels.value = target.labels.join(', ');
      }
      items = [...items.filter(row => row.id !== item.id), item];
      if (first) { select(item.id, null); renderActions(); }
      renderList();
    },
    changed: () => progress()});
  function progress() {
    if (draft) status(autosave.describe(), Boolean(autosave.failure));
  }
  async function request(path = '', method = 'GET', data) {
    return client.request('/api/people' + path, {method,
      headers:{'Content-Type':'application/json', 'X-Doxograph-Workspace':workspace,
        ...(method === 'POST' ? {'Idempotency-Key':draft.id} : {})},
      ...(data === undefined ? {} : {body:JSON.stringify(data)})});
  }
  function setBusy(value) {
    busy = value;
    el('people-sheet').setAttribute('aria-busy', String(value));
    el('people-sheet').querySelectorAll('button, input, textarea').forEach(field => field.disabled = value);
    el('person-new').disabled = value || !loaded;
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
      if (focusField) { el(focusField)?.focus(); focusField = null; }
      // Hash navigation can arrive during a load, save, or discard prompt.
      // Drain the latest target once that work releases the editor.
      if (shown && loaded && requested !== revealed) void run(revealRequested);
    }
  }

  // --- the authors in the library -----------------------------------------

  // Every author on a paper, one entry per name key, under the spelling the
  // library uses most.
  function authors() {
    const found = new Map();
    for (const paper of client.state.papers) {
      for (const spelling of paper.authors || []) {
        const key = nameKey(spelling);
        if (!key) continue;
        if (!found.has(key)) found.set(key, {key, spellings:new Map(), papers:new Set()});
        const entry = found.get(key);
        entry.spellings.set(spelling, (entry.spellings.get(spelling) || 0) + 1);
        entry.papers.add(paper.key);
      }
    }
    for (const entry of found.values())
      entry.name = [...entry.spellings].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))[0][0];
    return found;
  }
  function recordFor(name) {
    const key = nameKey(name);
    return key ? items.find(item => names(item).has(key)) : undefined;
  }
  // The papers whose author lists use any of these names.
  function authored(keys) {
    return client.state.papers.filter(paper => (paper.authors || []).some(name => keys.has(nameKey(name))));
  }

  function renderList() {
    const query = el('people-search').value.toLowerCase().trim();
    const all = authors();
    const claimed = new Set(items.flatMap(item => [...names(item)]));
    const records = items.map(item => ({item, papers:authored(names(item)).length}))
      .filter(({item}) => [item.name, ...item.other_names, item.affiliation, ...item.labels].join('\n').toLowerCase().includes(query))
      .sort((a, b) => a.item.name.localeCompare(b.item.name));
    const others = [...all.values()].filter(entry => !claimed.has(entry.key)
      && [...entry.spellings.keys()].join('\n').toLowerCase().includes(query))
      .sort((a, b) => b.papers.size - a.papers.size || a.name.localeCompare(b.name));
    const current = draft ? null : author && nameKey(author);
    const recordRows = records.map(({item, papers}) => button('open', `<strong>${esc(item.name)}</strong><small>${esc([item.affiliation, plural(papers, 'paper')].filter(Boolean).join(' · '))}</small>${item.labels.length ? `<span class="person-labels">${item.labels.map(label => `<span class="person-label">${esc(label)}</span>`).join('')}</span>` : ''}`,
      `data-id="${item.id}" ${draft?.id === item.id ? 'aria-current="page"' : ''}`)).join('');
    const authorRows = others.slice(0, 200).map(entry => button('author', `<strong>${esc(entry.name)}</strong><small>${plural(entry.papers.size, 'paper')}</small>`,
      `data-name="${esc(entry.name)}" ${current === entry.key ? 'aria-current="page"' : ''}`)).join('');
    el('people-list').innerHTML = (recordRows ? `<h3>Your notes</h3>${recordRows}` : '')
      + (authorRows ? `<h3>Authors in your library</h3>${authorRows}` : '')
      + (others.length > 200 ? '<p class="hint">Search to find the remaining authors.</p>' : '')
      || `<p class="hint">${query ? 'No one matches your search.' : 'Authors appear here as papers arrive. Add a person to keep notes on anyone else.'}</p>`;
    el('people-count').textContent = `${plural(items.length, 'record')} · ${plural(others.length + records.length, 'person', 'people')}`;
  }

  // Papers, their claims, and coauthors for a set of names: the part of a
  // person's page that comes from the library rather than from the reader.
  function workHtml(keys, linked = []) {
    const papers = authored(keys);
    const claims = new Map();
    for (const claim of client.state.claims) {
      if (!claims.has(claim.paper)) claims.set(claim.paper, []);
      claims.get(claim.paper).push(claim);
    }
    const paperRow = paper => {
      const own = claims.get(paper.key) || [];
      return `<div class="person-paper">${button('follow', esc(paper.title || paper.key), `data-paper="${esc(paper.key)}"`)}
        <p class="hint">${esc([(paper.authors || []).join(', '), paper.year, paper.venue].filter(Boolean).join(' · '))}</p>
        ${own.length ? `<details><summary>${plural(own.length, 'claim')}</summary>${own.map(claim =>
          button('follow', esc(claim.text), `class="person-claim-link" data-paper="${esc(paper.key)}" data-claim="${esc(claim.id)}"`)).join('')}</details>` : ''}</div>`;
    };
    const coauthors = new Map();
    for (const paper of papers) {
      for (const name of paper.authors || []) {
        const key = nameKey(name);
        if (!key || keys.has(key)) continue;
        const entry = coauthors.get(key) || {name, count:0};
        entry.count++;
        coauthors.set(key, entry);
      }
    }
    const top = [...coauthors.values()].sort((a, b) => b.count - a.count || a.name.localeCompare(b.name)).slice(0, 40);
    const others = linked.map(key => client.state.papers.find(paper => paper.key === key)
      ? paperRow(client.state.papers.find(paper => paper.key === key)) : `<p class="hint">Missing paper: ${esc(key)}</p>`);
    const count = papers.reduce((sum, paper) => sum + (claims.get(paper.key) || []).length, 0);
    return `<section aria-labelledby="person-papers-heading"><h3 id="person-papers-heading">Papers</h3>
        <p class="hint">${papers.length ? `${plural(papers.length, 'paper')} and ${plural(count, 'claim')} in this workspace.` : 'No papers in this workspace list this name as an author.'}</p>
        ${papers.map(paperRow).join('')}</section>
      ${others.length ? `<section aria-labelledby="person-other-papers-heading"><h3 id="person-other-papers-heading">Other papers</h3>${others.join('')}</section>` : ''}
      ${top.length ? `<section aria-labelledby="person-coauthors-heading"><h3 id="person-coauthors-heading">Coauthors</h3>
        <div class="person-coauthors">${top.map(entry => button('author', `${esc(entry.name)}${entry.count > 1 ? ` <small>${entry.count}</small>` : ''}`, `data-name="${esc(entry.name)}"`)).join('')}</div></section>` : ''}`;
  }

  function input(field, label, multiline = false, hint = '', max = 300, rows = 2) {
    const value = field === 'other_names' ? draft[field].join('\n') : field === 'labels' ? draft[field].join(', ') : draft[field];
    const help = hint ? `aria-describedby="person-${field}-help"` : '';
    return `<div class="person-field"><label for="person-${field}">${label}</label>${multiline
      ? `<textarea id="person-${field}" data-field="${field}" rows="${rows}" maxlength="${max}" ${help}>${esc(value)}</textarea>`
      : `<input id="person-${field}" data-field="${field}" maxlength="${max}" value="${esc(value)}" ${field === 'name' ? 'required' : ''} ${help}>`}
      ${hint ? `<small id="person-${field}-help" class="hint">${hint}</small>` : ''}${field === 'homepage' ? '<span id="person-homepage-link"></span>' : ''}</div>`;
  }
  function renderDocument() {
    if (!draft && author) {
      el('person-document').innerHTML = `<article class="person-page">
        <h2>${esc(author)}</h2>
        <div class="person-actions">${button('create', 'Add notes on this person', 'class="primary"')}</div>
        <p class="hint">Notes, an affiliation, and other spellings of this name are kept once you add them.</p>
        <div id="person-work">${workHtml(new Set([nameKey(author)]))}</div></article>`;
      if (busy) setBusy(true);
      return;
    }
    if (!draft) {
      el('person-document').innerHTML = '<div class="people-empty"><h3>The people behind your papers</h3><p>Pick an author to see their papers, claims, and coauthors.</p><p>Add notes to keep what you know about them, and list other spellings of their name so their papers stay together.</p></div>';
      return;
    }
    el('person-document').innerHTML = `<form id="person-form">
      <div class="person-actions"></div>
      <div class="person-fields">${input('name','Name')}${input('affiliation','Affiliation',false,'',1000)}
      ${input('homepage','Web page',false,'',4000)}
      ${input('other_names','Other names in author lists',true,'One per line. Papers listing any of these names appear on this page.',15100)}</div>
      ${input('labels','Labels',false,'Separate labels with commas.',60398)}
      ${input('notes','Notes',true,'',100000,8)}
      <details class="person-link-picker"><summary>Link other papers</summary>
        <p class="hint">Papers this person did not write but that belong here, such as ones they recommended or that discuss their work.</p>
        <label for="person-link-search">Search your library</label><input type="search" id="person-link-search" placeholder="Find a paper…">
        <div id="person-link-options"></div>
      </details>
      <div id="person-work"></div>
      <div id="person-saved-version" hidden></div>
    </form>`;
    renderActions(); renderPicker(); renderWork(); renderHomepage();
    if (busy) setBusy(true);
  }
  function renderWork() {
    if (draft && el('person-work')) el('person-work').innerHTML = workHtml(names(draft), draft.papers);
  }
  // A new record's first save swaps Discard for the saved record's actions
  // without rebuilding the fields under the cursor.
  function renderActions() {
    const actions = el('person-form')?.querySelector('.person-actions');
    if (!actions) return;
    actions.innerHTML = saved ? button('reload','Reload saved version') + button('delete','Delete record') : button('cancel','Discard new person');
    if (busy) actions.querySelectorAll('button').forEach(field => field.disabled = true);
  }
  function renderHomepage() {
    const container = el('person-homepage-link');
    container.replaceChildren();
    try {
      const url = new URL(draft.homepage);
      if (!['http:','https:'].includes(url.protocol)) return;
      const link = document.createElement('a');
      link.href = url.href; link.textContent = 'Open web page'; link.target = '_blank'; link.rel = 'noopener noreferrer';
      container.append(link);
    } catch (_) { /* Anything else stays editable text. */ }
  }
  function renderPicker() {
    const query = el('person-link-search').value.toLowerCase().trim();
    const choices = [
      ...client.state.papers.map(p => ({paper:p.key, title:p.title || p.key, selected:draft.papers.includes(p.key)})),
      ...draft.papers.filter(paper => !client.state.papers.some(p => p.key === paper)).map(paper => ({paper, title:`Missing paper: ${paper}`, selected:true})),
    ];
    const selected = choices.filter(choice => choice.selected);
    const available = choices.filter(choice => !choice.selected && (choice.title + ' ' + choice.paper).toLowerCase().includes(query));
    el('person-link-options').innerHTML = [...selected,...available.slice(0,100)].map(choice => `<label><input type="checkbox" data-link-paper="${esc(choice.paper)}" ${choice.selected ? 'checked' : ''}><span>${esc(choice.title)}</span></label>`).join('')
      + (available.length > 100 ? '<p class="hint">Search to narrow the remaining results.</p>' : '')
      || '<p class="hint">No matching papers.</p>';
  }
  function renderSavedVersion(item) {
    if (!item) return '<p>This person’s record has been deleted.</p>';
    const labels = {name:'Name',other_names:'Other names',affiliation:'Affiliation',homepage:'Web page',notes:'Notes',labels:'Labels',papers:'Other papers'};
    return '<dl>' + fields.map(field => {
      let value = item[field];
      if (field === 'papers') value = value.map(id => client.state.papers.find(p => p.key === id)?.title || `Missing paper: ${id}`).join('\n');
      else if (Array.isArray(value)) value = value.join('\n');
      return `<dt>${labels[field]}</dt><dd>${esc(value || 'None recorded')}</dd>`;
    }).join('') + '</dl>';
  }
  function showSavedComparison(version) {
    inspectedRevision = version?.revision ?? null;
    const comparison = el('person-saved-version');
    comparison.hidden = false;
    comparison.innerHTML = '<h3>Saved version</h3>' + renderSavedVersion(version) + button('replace','Replace draft with saved version');
  }
  async function discard() {
    if (await autosave.flush()) return true;
    return confirmDialog(`This person couldn’t be saved. ${autosave.failure} Discard your changes?`, {ok:'Discard changes'});
  }
  // The record a new draft's create stored, and whether it still holds only
  // what this draft wrote. A record another window changed since is theirs.
  async function created(target) {
    if (saved) return {item:saved, ours:conflicted !== target};
    if (posted?.target !== target) return {};
    const item = (await request()).people.find(row => row.id === target.id);
    return {item, ours:!!item && posted.bodies.some(sent => stored(sent) === stored(item))};
  }
  function choose(item) {
    inspectedRevision = undefined; author = null;
    saved = structuredClone(item); draft = structuredClone(item);
    select(item.id, null); renderList(); renderDocument(); progress();
  }
  function chooseAuthor(name) {
    inspectedRevision = undefined;
    draft = saved = null; author = name;
    select(null, name); renderList(); renderDocument(); status('');
  }
  function clear(message = '') {
    draft = saved = null; author = null;
    select(null, null); renderList(); renderDocument(); status(message);
  }
  function reset() {
    void autosave.settle();
    workspace = client.workspaceId; items = []; draft = saved = null; author = null; loaded = false; inspectedRevision = undefined;
    el('people-search').value = '';
  }
  // The record or author a URL names, or else whoever was last open.
  async function reveal(target, current) {
    const elsewhere = target.id ? target.id !== draft?.id
      : target.author ? !(draft && names(draft).has(nameKey(target.author))) : false;
    if ((dirty() || autosave.unconfirmed) && !elsewhere) { renderList(); progress(); return; }
    if (elsewhere && draft && !await discard()) {
      if (current()) select(saved?.id || null, null);
      return;
    }
    if (!current()) return;
    // A record made since the list was read, in another window.
    if (target.id && !items.some(item => item.id === target.id)) {
      const ws = workspace, latest = (await request()).people;
      if (!current() || ws !== client.workspaceId) return;
      items = latest;
    }
    const item = target.id ? items.find(item => item.id === target.id)
      : target.author ? recordFor(target.author) : draft && items.find(item => item.id === draft.id);
    if (item) choose(item);
    else if (target.id) clear('This person’s record no longer exists.');
    else if (target.author) chooseAuthor(target.author);
    else if (!draft) { renderList(); renderDocument(); }
  }
  async function revealRequested() {
    if (!shown) return;
    const target = requested;
    try { await reveal(target, () => shown && requested === target); }
    finally { revealed = target; }
  }
  async function show(id, name) {
    const target = {id: id || null, author: id ? null : name || null};
    // The URL catching up with a record or author picked here is not a new
    // destination, and must not rebuild the editor under the cursor.
    const here = target.id ? target.id === draft?.id : target.author && !draft && target.author === author;
    if (shown && here) { requested = revealed = target; return; }
    const changed = !shown || requested?.id !== target.id || requested?.author !== target.author;
    if (changed) requested = target;
    if (shown) {
      if (changed && loaded) await run(revealRequested);
      return;
    }
    shown = true;
    el('people-sheet').hidden = false;
    if (workspace !== client.workspaceId) reset();
    renderList(); renderDocument();
    if (dirty()) progress(); else status('Loading people…');
    await run(async () => {
      items = (await request()).people; loaded = true;
      if (!dirty()) status('');
      await revealRequested();
    });
  }
  function hide() {
    const wasShown = shown;
    shown = false;
    el('people-sheet').hidden = true;
    if (wasShown) void autosave.flush();
  }
  // Papers arrive and change while the view is open. Redraw what comes from
  // the library without touching the fields being typed in.
  function sync() {
    if (!shown || !loaded) return;
    renderList();
    if (draft) { renderWork(); if (!el('person-link-search')?.value) renderPicker(); }
    else if (author && el('person-work')) el('person-work').innerHTML = workHtml(new Set([nameKey(author)]));
  }
  const newId = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2,'0')).join('');
  const blank = name => ({id:newId(), name, other_names:[], affiliation:'', homepage:'', notes:'', labels:[], papers:[]});
  async function action(name, target) {
    if (name === 'new') {
      if (!loaded || !await discard()) return;
      saved = null; author = null;
      inspectedRevision = undefined;
      draft = blank('');
      select(null, null); renderList(); renderDocument(); status('New person. Their record saves once it has a name.');
      focusField = 'person-name';
    } else if (name === 'create') {
      // Adding notes on an author is asking to keep them, so the record saves now.
      if (!loaded || !author) return;
      draft = blank(author); saved = null; author = null; inspectedRevision = undefined;
      renderDocument();
      await autosave.flush();
      focusField = 'person-notes';
    } else if (name === 'open') {
      if (draft?.id === target.dataset.id || !await discard()) return;
      choose(items.find(item => item.id === target.dataset.id));
    } else if (name === 'author') {
      const record = recordFor(target.dataset.name);
      if (record ? draft?.id === record.id : !draft && author === target.dataset.name) return;
      if (!await discard()) return;
      if (record) choose(record); else chooseAuthor(target.dataset.name);
    } else if (name === 'cancel') {
      if (!await confirmDialog('Discard this new person?', {ok:'Discard person'})) return;
      // Its create can land while the prompt waits, or land with its answer
      // lost. Discarding the draft also deletes the record it made.
      await autosave.settle();
      const {item, ours} = await created(draft);
      if (item && !ours) {
        // Another window changed the record, so it is theirs to keep.
        items = [...items.filter(row => row.id !== item.id), item];
        choose(item);
        status('Another window changed this person, so their record was kept. This is its saved version.');
        return;
      }
      if (item) {
        await request('/' + item.id + '?' + new URLSearchParams({revision:item.revision}),'DELETE');
        items = items.filter(row => row.id !== item.id);
      }
      clear();
    } else if (name === 'reload') {
      await autosave.flush();
      const latest = (await request()).people;
      const version = latest.find(item => item.id === saved.id);
      // Always leave the draft intact while the reader inspects the disk version.
      showSavedComparison(version);
      items = latest; renderList(); status('Your draft is still in the editor. Inspect the saved version below before replacing it.');
    } else if (name === 'replace') {
      const latest = (await request()).people;
      const version = latest.find(item => item.id === saved.id);
      items = latest; renderList();
      if (inspectedRevision === undefined || (version?.revision ?? null) !== inspectedRevision) {
        showSavedComparison(version);
        status('The saved version changed again. Your draft is still here. Inspect the updated comparison before replacing it.');
        return;
      }
      // Replacing is the point here, so the draft is not saved first.
      if (dirty() && !await confirmDialog('Discard unsaved changes to this person?', {ok:'Discard changes'})) return;
      if (version) choose(version);
      else clear('This person’s record has been deleted.');
    } else if (name === 'delete') {
      if (!await confirmDialog(`Delete your record of ${saved.name}? Their papers stay in the library.`,{ok:'Delete record'})) return;
      await request('/' + saved.id + '?' + new URLSearchParams({revision:saved.revision}),'DELETE');
      items = items.filter(item => item.id !== saved.id);
      const left = authors().has(nameKey(saved.name)) ? saved.name : null;
      if (left) chooseAuthor(left); else clear();
      status('Record deleted.');
    } else if (name === 'follow') {
      const {paper,claim} = target.dataset;
      navigate(paper,claim);
    }
  }
  el('people-sheet').addEventListener('click', event => {
    const target = event.target.closest('[data-person-action]');
    if (!target) return;
    event.preventDefault();
    run(() => action(target.dataset.personAction,target));
  });
  el('people-sheet').addEventListener('submit', event => { event.preventDefault(); if (!busy) void autosave.flush(); });
  el('people-search').addEventListener('input', renderList);
  el('people-sheet').addEventListener('input', event => {
    if (event.target.id === 'person-link-search') return renderPicker();
    const field = event.target.dataset.field;
    if (!draft || !field) return;
    draft[field] = field === 'labels' ? event.target.value.split(',').map(normalizeLabel).filter(Boolean)
      : field === 'other_names' ? event.target.value.split('\n').map(v => v.trim()).filter(Boolean) : event.target.value;
    autosave.schedule();
    if (['name','other_names'].includes(field)) renderWork();
    if (field === 'homepage') renderHomepage();
  });
  el('people-sheet').addEventListener('change', event => {
    if (!draft || !event.target.dataset.linkPaper) return;
    const paper = event.target.dataset.linkPaper;
    draft.papers = draft.papers.filter(value => value !== paper);
    if (event.target.checked) draft.papers.push(paper);
    renderWork();
    autosave.schedule();
  });
  el('people-sheet').addEventListener('keydown', event => {
    // Saving is automatic. The shortcut saves now, or retries a failed save.
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's') { event.preventDefault(); if (!busy) void autosave.flush(); }
  });
  window.addEventListener('beforeunload', event => {
    if (dirty() || busy || autosave.saving || autosave.unconfirmed) { void autosave.flush(); event.preventDefault(); event.returnValue = ''; }
  });
  return {connect(value, follow, selected) { client = value; navigate = follow; select = selected; }, show, hide, sync, flush: () => autosave.flush(),
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
})();
