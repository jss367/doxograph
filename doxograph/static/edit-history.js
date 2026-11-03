'use strict';

// Session history belongs to a workspace. Each inverse changes only the fields
// the action touched, so a page autosave or another window's unrelated edit survives.
function createEditHistory() {
  const histories = new Map(), notebooks = new Map();
  let pending = 0, busy = false;
  const copy = value => value === undefined ? undefined : structuredClone(value);
  const equal = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
  const conflict = () => { throw new Error('This item changed since that action. Refresh it before undoing.'); };
  const history = ws => {
    if (!histories.has(ws)) histories.set(ws, {undo: [], redo: []});
    return histories.get(ws);
  };

  // Merge an inverse into the latest notebook, retaining untouched fields and
  // collection members. Revisions are checked by the notebook API on the write.
  function merge(expected, wanted, current) {
    if (equal(expected, wanted)) return copy(current);
    if (object(expected) && object(wanted) && object(current)) {
      const result = copy(current);
      for (const key of new Set([...Object.keys(expected), ...Object.keys(wanted)])) {
        const value = merge(expected[key], wanted[key], current[key]);
        if (value === undefined) delete result[key]; else result[key] = value;
      }
      return result;
    }
    if ([expected, wanted, current].every(Array.isArray)) {
      const all = [...expected, ...wanted, ...current];
      const key = all.length && all.every(item => object(item) && typeof item.id === 'string') ? 'id'
        : all.length && all.every(item => object(item) && typeof item.paper === 'string') ? 'paper' : null;
      if (key) {
        const before = new Map(expected.map(item => [item[key], item]));
        const after = new Map(wanted.map(item => [item[key], item]));
        const now = new Map(current.map(item => [item[key], item]));
        for (const id of new Set([...before.keys(), ...after.keys()])) {
          const value = merge(before.get(id), after.get(id), now.get(id));
          if (value === undefined) now.delete(id); else now.set(id, value);
        }
        const common = new Set(expected.filter(item => after.has(item[key])).map(item => item[key]));
        const order = list => list.map(item => item[key]).filter(id => common.has(id));
        if (!equal(order(expected), order(wanted))) {
          if (!equal(order(expected), order(current))) conflict();
          return [...wanted.map(item => now.get(item[key])), ...current.filter(item => !after.has(item[key]) && now.has(item[key])).map(item => now.get(item[key]))];
        }
        // Restore removed members at their former position, keeping new ones.
        const result = current.filter(item => now.has(item[key])).map(item => now.get(item[key]));
        wanted.forEach((item, index) => {
          if (result.some(row => row[key] === item[key])) return;
          const next = wanted.slice(index + 1).find(row => result.some(r => r[key] === row[key]));
          const at = next ? result.findIndex(row => row[key] === next[key]) : result.length;
          result.splice(at, 0, now.get(item[key]));
        });
        return result;
      }
    }
    if (!equal(expected, current)) conflict();
    return copy(wanted);
  }

  function add(workspace, entry) {
    const stack = history(workspace);
    stack.undo.push(entry);
    if (stack.undo.length > 100) stack.undo.shift();
    stack.redo.length = 0;
    return entry;
  }
  function forget(workspace, entry) {
    const stack = history(workspace);
    stack.undo = stack.undo.filter(item => item !== entry);
    stack.redo = stack.redo.filter(item => item !== entry);
  }
  async function apply(workspace, redo = false, specific = null) {
    if (busy || pending) throw new Error('Wait for the current change to finish, then try again.');
    const stack = history(workspace), from = redo ? stack.redo : stack.undo;
    const entry = specific || from.at(-1);
    if (!entry || !from.includes(entry)) return false;
    if (!entry.undo) throw new Error('The latest action cannot be undone.');
    busy = true;
    try {
      await (redo ? entry.redo : entry.undo)();
      const latest = from.at(-1) === entry;
      from.splice(from.indexOf(entry), 1);
      if (latest && entry.redo) (redo ? stack.undo : stack.redo).push(entry);
      else stack.redo.length = 0;
      entry.onApply?.();
      return true;
    } finally { busy = false; }
  }

  async function request(path, options, send, workspace) {
    const method = (options.method || 'GET').toUpperCase();
    const writing = !['GET', 'HEAD', 'OPTIONS'].includes(method);
    if (writing && busy) throw new Error('Wait for undo or redo to finish, then try again.');
    const headers = new Headers(options.headers || {});
    headers.set('X-Doxograph-Workspace', workspace);
    const raw = (url, opts = {}) => send(url, {...opts, headers});
    const write = (url, verb, body) => {
      headers.set('Content-Type', 'application/json');
      return raw(url, {method: verb, body: JSON.stringify(body)});
    };
    const snapshotKey = revision => JSON.stringify([workspace, revision]);
    const remember = data => {
      if (data?.revision !== undefined) {
        notebooks.set(snapshotKey(data.revision), copy(data));
        if (notebooks.size > 200) notebooks.delete(notebooks.keys().next().value);
      }
    };
    if (writing) pending++;
    try {
      const body = typeof options.body === 'string' ? JSON.parse(options.body) : null;
      const match = path.match(/^\/api\/papers\/([^/]+)(?:\/claims\/([^/]+))?$/);
      let previous, revision;
      if (options.history !== false && method === 'PATCH' && match) {
        const snapshot = await raw(`/api/papers/${match[1]}?with_revision=true`);
        const paper = snapshot.paper;
        revision = snapshot.revision;
        previous = match[2] ? paper.claims.find(c => c.id === decodeURIComponent(match[2])) : paper;
        if (!previous) throw new Error('This item no longer exists.');
      }
      const baseline = path === '/api/notebook' && method === 'PUT' ? notebooks.get(snapshotKey(body.revision)) : null;
      const result = await raw(path, previous ? {...options,
        body: JSON.stringify({...body, expected_revision: body.expected_revision ?? revision})} : options);
      if (path === '/api/notebook' || path === '/api/notebook/notes') remember(result);
      if (!writing || options.history === false) return result;
      let entry;
      if (path === '/api/notebook' && method === 'PUT' && baseline) {
        const before = copy(baseline), after = copy(result);
        delete before.revision; delete after.revision;
        // Reading position is navigation, including when a bookmark saves it
        // alongside the bookmark. It must never block undo of the bookmark.
        const reading = new Set((before.reading || []).filter(row => after.reading?.some(r => r.paper === row.paper)).map(row => row.paper));
        for (const data of [before, after]) for (const row of data.reading || []) if (reading.has(row.paper)) delete row.page;
        if (!equal(before, after)) {
          const restore = async (expected, wanted) => {
            const current = await raw(path);
            const revision = current.revision;
            delete current.revision;
            const next = merge(expected, wanted, current);
            remember(await write(path, 'PUT', {...next, revision}));
          };
          entry = {undo: () => restore(after, before), redo: () => restore(before, after)};
        }
      } else if (path === '/api/notebook/notes' && method === 'PUT' && body.notes !== body.previous) {
        const restore = async (notes, previous) => remember(await write(path, 'PUT', {notes, previous}));
        entry = {undo: () => restore(body.previous, result.notes), redo: () => restore(result.notes, body.previous)};
      } else if (previous) {
        const keys = Object.keys(body).filter(key => key !== 'expected_revision');
        if (!match[2] && (keys.includes('year') || keys.includes('publication_date'))) keys.push('year', 'publication_date');
        const defaults = {labels: [], code: [], tags: [], facets: {}, ledger_links: [], reviewed: false,
          kind: 'finding', strength: 'supporting', evidence_relationship: 'unassessed'};
        const pick = row => Object.fromEntries([...new Set(keys)].map(key => [key, copy(row[key] ?? defaults[key] ?? (['year', 'publication_date'].includes(key) ? null : ''))]));
        const before = pick(previous), after = pick(result);
        if (!equal(before, after)) {
          const restore = async (expected, wanted) => {
            const snapshot = await raw(`/api/papers/${match[1]}?with_revision=true`);
            const paper = snapshot.paper;
            const current = match[2] ? paper.claims.find(c => c.id === decodeURIComponent(match[2])) : paper;
            if (!current) throw new Error('This item no longer exists.');
            const patch = merge(expected, wanted, pick(current));
            // The revision is compared atomically under the server's write lock.
            patch.expected_revision = snapshot.revision;
            if (match[2] && patch.facets) {
              // Claim PATCH merges named facets; omission keeps a value. Undo
              // must name removed keys explicitly, without rewriting others.
              patch.facets = Object.fromEntries([...new Set([...Object.keys(expected.facets || {}), ...Object.keys(wanted.facets || {})])]
                .filter(name => !equal(expected.facets?.[name], wanted.facets?.[name]))
                .map(name => [name, wanted.facets?.[name] || []]));
            }
            await write(path, 'PATCH', patch);
          };
          entry = {undo: () => restore(after, before), redo: () => restore(before, after)};
        }
      } else if (/^\/api\/papers\/[^/]+\/review$/.test(path) && result.changed?.length) {
        const restore = reviewed => write(path, 'POST', {reviewed, claims: result.changed});
        entry = {undo: () => restore(!result.reviewed), redo: () => restore(result.reviewed)};
      }
      if (entry) {
        add(workspace, entry);
        options.onHistory?.(entry);
      } else if (!previous && !(path === '/api/notebook' && baseline)
          && path !== '/api/notebook/notes' && !/^\/api\/papers\/[^/]+\/review$/.test(path)
          && !/^\/api\/(search|.*\/export|.*\/quote-context)$/.test(path)) {
        // Do not jump over an unsupported action and undo something older.
        // Imports, model runs, and permanent removals cannot be reversed here.
        add(workspace, {});
      }
      return result;
    } finally { if (writing) pending--; }
  }
  return {request, add, forget, apply, get busy() { return busy || pending > 0; }};
}
