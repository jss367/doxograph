'use strict';

// What the findings page is made of, worked out from the workspace state
// without touching the document, so node can test it. The page itself is
// drawn in findings.js.
const FindingsModel = (() => {
  // A citation as a synthesis writes it: claim ids in square brackets.
  const CITATION = /\s*\[[a-z0-9][a-z0-9-]*-c\d+(?:[,\s]+[a-z0-9][a-z0-9-]*-c\d+)*\]/g;

  function stripCitations(text) {
    return (text || '').replace(CITATION, '');
  }

  // Words a full stop does not end a sentence after: "et al. (2026)",
  // "vs. 1%", "Fig. 3". Matched on the word before the stop, lower-cased.
  const ABBREVIATIONS = new Set(['al', 'vs', 'cf', 'fig', 'figs', 'eq', 'eqs', 'sec', 'no', 'approx', 'ca', 'e.g', 'i.e', 'et', 'resp', 'ref', 'refs']);

  // The first sentence of a text: up to the first full stop, question mark or
  // exclamation mark that ends a word (so a decimal does not count) and is
  // followed by the end or by a space, unless the word is an abbreviation.
  function firstSentence(text) {
    const flat = (text || '').trim().replace(/\s+/g, ' ');
    const stops = /[.!?](?=\s|$)/g;
    let match;
    while ((match = stops.exec(flat)) !== null) {
      const before = flat.slice(0, match.index);
      const word = (/([^\s(]+)$/.exec(before) || ['', ''])[1].toLowerCase();
      if (match[0] === '.' && ABBREVIATIONS.has(word)) continue;
      return flat.slice(0, match.index + 1);
    }
    return flat;
  }

  // The one line a synthesis gets before it is opened.
  function lead(synthesis) {
    return synthesis ? firstSentence(stripCitations(synthesis.text)) : '';
  }

  function unreviewedCount(claims) {
    return (claims || []).filter((claim) => !claim.reviewed).length;
  }

  // The papers a finding rests on, named the way a citation names them.
  function papersOf(finding) {
    const names = new Set();
    for (const claim of finding.claims || []) {
      const author = (claim.paper_authors || [])[0];
      const who = author ? author.split(' ').pop() : claim.paper;
      names.add(`${who} ${claim.paper_year || 'n.d.'}`);
    }
    return [...names].sort();
  }

  // One section per topic that has a finding or a synthesis, in the order of
  // the vocabulary, which is the reader's own order of importance; topics the
  // vocabulary does not list follow, alphabetically. A topic is stale when a
  // claim under it changed since any of its findings were judged or written.
  function topics(state) {
    const byName = new Map();
    const section = (name) => {
      if (!byName.has(name)) byName.set(name, {name, description: '', synthesis: null, agreements: [], tensions: [], papers: new Set()});
      return byName.get(name);
    };
    for (const row of state.syntheses || []) section(row.topic).synthesis = row;
    for (const row of state.agreements || []) {
      for (const name of row.topics || []) {
        const entry = section(name);
        entry.agreements.push(row);
        for (const claim of row.claims || []) entry.papers.add(claim.paper);
      }
    }
    for (const row of state.tensions || []) {
      for (const name of row.topics || []) {
        const entry = section(name);
        entry.tensions.push(row);
        for (const claim of row.claims || []) entry.papers.add(claim.paper);
      }
    }
    const order = new Map((state.tags || []).map((tag, index) => [tag.name, index]));
    for (const tag of state.tags || []) if (byName.has(tag.name)) byName.get(tag.name).description = tag.description || '';
    const rank = (name) => (order.has(name) ? order.get(name) : order.size);
    return [...byName.values()]
      .map((entry) => ({
        ...entry,
        papers: entry.synthesis ? Math.max(entry.synthesis.n_papers || 0, entry.papers.size) : entry.papers.size,
        stale: Boolean(entry.synthesis?.stale) || entry.agreements.some((a) => a.stale) || entry.tensions.some((t) => t.stale),
        count: entry.agreements.length + entry.tensions.length,
      }))
      .sort((a, b) => rank(a.name) - rank(b.name) || a.name.localeCompare(b.name));
  }

  return {stripCitations, firstSentence, lead, unreviewedCount, papersOf, topics};
})();
