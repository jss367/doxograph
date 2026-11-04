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

  // The papers a finding rests on, named the way a citation names them. Two
  // papers by the same first author in the same year share that name, so the
  // names are kept per paper and a shared one carries the paper's key, which
  // is what the rest of the page falls back on when a paper has no author.
  function papersOf(finding) {
    const names = new Map();
    for (const claim of finding.claims || []) {
      const author = (claim.paper_authors || [])[0];
      const who = author ? author.split(' ').pop() : claim.paper;
      names.set(claim.paper, `${who} ${claim.paper_year || 'n.d.'}`);
    }
    const shared = new Map();
    for (const name of names.values()) shared.set(name, (shared.get(name) || 0) + 1);
    return [...names].map(([paper, name]) => (shared.get(name) > 1 ? `${name} (${paper})` : name)).sort();
  }

  // One section per topic that has a finding or a synthesis, in the order of
  // the vocabulary, which is the reader's own order of importance; topics the
  // vocabulary does not list follow, alphabetically. A topic is stale when
  // any finding or synthesis under it is: a finding, when a claim it rests on
  // changed since it was judged; a synthesis, when any claim under the topic
  // did, added ones included, since it rests on all of them. So a claim no
  // finding cites stales the topic once a synthesis is on file, which the
  // combined pass writes for every topic it runs over.
  // Its paper count is of the papers with claims under it, not only of those
  // a finding names: a paper nobody has set against another is still there.
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
    for (const claim of state.claims || []) {
      for (const name of claim.tags || []) if (byName.has(name)) byName.get(name).papers.add(claim.paper);
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

  // The agreements and tensions a paper takes part in, through its claims.
  function forPaper(state, key) {
    const involves = (row) => (row.claims || []).some((claim) => claim.paper === key);
    return [...(state.agreements || []).filter(involves).map((row) => ({...row, kind: 'agreement'})),
            ...(state.tensions || []).filter(involves).map((row) => ({...row, kind: row.kind || 'tension', finding: 'tension'}))];
  }

  return {stripCitations, firstSentence, lead, unreviewedCount, papersOf, topics, forPaper};
})();
