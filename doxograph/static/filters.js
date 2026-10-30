'use strict';

// Filtering the library: the query, the paper-text search, topics, labels,
// and the filter controls. Shares the library state declared in app.js.

function focusClaimSearch() {
  revealSidebar($('papers'));
  if (!['library', 'claims'].includes(V.view)) { showView('library'); syncHash(true); renderAll(); }
  if (readingPaper()) setReadingFilters(true);
  $('q').focus();
  $('q').select();
}

// --- filtering ------------------------------------------------------------

const FILTER_DEFAULTS = { paper: null, tag: null, label: null, facet: null, q: '', kind: '',
  unreviewed: false, unverified: false, tensionStatus: '', agreementStatus: '',
  tensionFocus: null, agreementFocus: null };
let activeFiltersHTML = null;

function renderActiveFilters() {
  const filters = [];
  const add = (key, label) => { if (V[key]) filters.push([key, label]); };
  add('paper', `Paper: ${S.papers.find((p) => p.key === V.paper)?.title || V.paper}`);
  add('tag', `Topic: ${V.tag}`);
  add('label', `Label: ${V.label}`);
  if (facetFilter()) add('facet', `${facetFilter().name}: ${facetFilter().value}`);
  if (V.q.trim()) add('q', `Search: ${V.q.trim()}`);
  add('kind', `Kind: ${V.kind}`);
  add('unreviewed', 'Unreviewed');
  add('unverified', 'Quote not found');
  if (V.view === 'tensions' || V.view === 'agreements') {
    const prefix = V.view === 'tensions' ? 'tension' : 'agreement';
    add(`${prefix}Status`, `Status: ${V[`${prefix}Status`]}`);
    const focus = V[`${prefix}Focus`];
    add(`${prefix}Focus`, `Claim: ${S.claims.find((c) => c.id === focus)?.text || 'no longer available'}`);
  }
  const container = $('active-filters');
  const html = filters.map(([key, label]) => `<button type="button" class="filter-chip"
    data-clear-filter="${key}" aria-label="${esc(`Remove filter: ${label}`)}" title="${esc(label)}">
    <span class="filter-label">${esc(label)}</span><span class="filter-remove" aria-hidden="true">×</span>
  </button>`).join('') + (filters.length
    ? '<button type="button" class="clear-filters" data-clear-filter="all">Clear all</button>' : '');
  // Polls and selection changes should leave a focused chip in place.
  if (activeFiltersHTML !== html) {
    container.innerHTML = html;
    activeFiltersHTML = html;
  }
  container.hidden = !filters.length;
}

function clearFilters(key) {
  if (key !== 'all' && !Object.hasOwn(FILTER_DEFAULTS, key)) return;
  captureOpenEditor();
  if (key === 'all' || key === 'paper') parkNewClaimForNavigation(null, { retainUnsaved: true });
  if (key === 'all') Object.assign(V, FILTER_DEFAULTS);
  else V[key] = FILTER_DEFAULTS[key];
  $('q').value = V.q;
  $('kind').value = V.kind;
  $('only-unreviewed').checked = V.unreviewed;
  $('only-unverified').checked = V.unverified;
  if (key === 'all' || key === 'q') dropTextSearch();
  if (V.view === 'research' && V.researchSaving) {
    // The save owns the disabled form and inserts any failure beside it.
    // Update the filters and sidebar while keeping that form connected.
    render();
    syncHash();
  } else {
    renderAll();
  }
  // The activated button has gone; leave keyboard users on the next action.
  ($('active-filters').querySelector('button') || $('q')).focus();
}

$('active-filters').addEventListener('click', (event) => {
  const button = event.target.closest('[data-clear-filter]');
  if (button) clearFilters(button.dataset.clearFilter);
});

// The phrase if the corpus holds it, otherwise the words.
//
// One substring over the whole row could not find "recovery under steering"
// from "steering recovery", which is how anyone types a search. Every word in
// any order finds it — but it also makes "Paper A" match everything holding
// "paper" and a word starting with "a", and a title typed into the box should
// narrow to that title. Which was meant is decided by the corpus: if the
// phrase is in it, that is what was wanted, and otherwise the words are all
// there is to go on. Nothing that used to be findable stops being findable.
// The page has to fold exactly as the server does, or a query finds a paper
// by its text and hides the claim that says the same thing. JavaScript's
// `toLowerCase` is not `casefold`: it lowercases one character to one, so ß
// stays ß, and it knows where a Greek word ends where `casefold` does not.
// Both tables below are the differences, written out rather than guessed at —
// a narrowing that suited one script has been wrong for the next one twice.
//
// CASEFOLD: every code point whose `casefold` differs from its `lower`.
//   python -c "print([c for c in map(chr, range(0x110000)) if c.casefold() != c.lower()])"
// COMBINING: every code point with a combining class that is an accent — not
//   a nukta, a kana voicing mark or a virama, each of which changes the word
//   — which is what the server drops.
//   python -c "import unicodedata; print([c for c in map(chr, range(0x110000))
//              if unicodedata.combining(c) not in (0, 7, 8, 9)])"
const CASEFOLD = {'\u00b5':'\u03bc','\u00df':'\u0073\u0073','\u0149':'\u02bc\u006e','\u017f':'\u0073','\u01f0':'\u006a\u030c','\u0345':'\u03b9','\u0390':'\u03b9\u0308\u0301','\u03b0':'\u03c5\u0308\u0301','\u03c2':'\u03c3','\u03d0':'\u03b2','\u03d1':'\u03b8','\u03d5':'\u03c6','\u03d6':'\u03c0','\u03f0':'\u03ba','\u03f1':'\u03c1','\u03f5':'\u03b5','\u0587':'\u0565\u0582','\u13a0':'\u13a0','\u13a1':'\u13a1','\u13a2':'\u13a2','\u13a3':'\u13a3','\u13a4':'\u13a4','\u13a5':'\u13a5','\u13a6':'\u13a6','\u13a7':'\u13a7','\u13a8':'\u13a8','\u13a9':'\u13a9','\u13aa':'\u13aa','\u13ab':'\u13ab','\u13ac':'\u13ac','\u13ad':'\u13ad','\u13ae':'\u13ae','\u13af':'\u13af','\u13b0':'\u13b0','\u13b1':'\u13b1','\u13b2':'\u13b2','\u13b3':'\u13b3','\u13b4':'\u13b4','\u13b5':'\u13b5','\u13b6':'\u13b6','\u13b7':'\u13b7','\u13b8':'\u13b8','\u13b9':'\u13b9','\u13ba':'\u13ba','\u13bb':'\u13bb','\u13bc':'\u13bc','\u13bd':'\u13bd','\u13be':'\u13be','\u13bf':'\u13bf','\u13c0':'\u13c0','\u13c1':'\u13c1','\u13c2':'\u13c2','\u13c3':'\u13c3','\u13c4':'\u13c4','\u13c5':'\u13c5','\u13c6':'\u13c6','\u13c7':'\u13c7','\u13c8':'\u13c8','\u13c9':'\u13c9','\u13ca':'\u13ca','\u13cb':'\u13cb','\u13cc':'\u13cc','\u13cd':'\u13cd','\u13ce':'\u13ce','\u13cf':'\u13cf','\u13d0':'\u13d0','\u13d1':'\u13d1','\u13d2':'\u13d2','\u13d3':'\u13d3','\u13d4':'\u13d4','\u13d5':'\u13d5','\u13d6':'\u13d6','\u13d7':'\u13d7','\u13d8':'\u13d8','\u13d9':'\u13d9','\u13da':'\u13da','\u13db':'\u13db','\u13dc':'\u13dc','\u13dd':'\u13dd','\u13de':'\u13de','\u13df':'\u13df','\u13e0':'\u13e0','\u13e1':'\u13e1','\u13e2':'\u13e2','\u13e3':'\u13e3','\u13e4':'\u13e4','\u13e5':'\u13e5','\u13e6':'\u13e6','\u13e7':'\u13e7','\u13e8':'\u13e8','\u13e9':'\u13e9','\u13ea':'\u13ea','\u13eb':'\u13eb','\u13ec':'\u13ec','\u13ed':'\u13ed','\u13ee':'\u13ee','\u13ef':'\u13ef','\u13f0':'\u13f0','\u13f1':'\u13f1','\u13f2':'\u13f2','\u13f3':'\u13f3','\u13f4':'\u13f4','\u13f5':'\u13f5','\u13f8':'\u13f0','\u13f9':'\u13f1','\u13fa':'\u13f2','\u13fb':'\u13f3','\u13fc':'\u13f4','\u13fd':'\u13f5','\u1c80':'\u0432','\u1c81':'\u0434','\u1c82':'\u043e','\u1c83':'\u0441','\u1c84':'\u0442','\u1c85':'\u0442','\u1c86':'\u044a','\u1c87':'\u0463','\u1c88':'\ua64b','\u1e96':'\u0068\u0331','\u1e97':'\u0074\u0308','\u1e98':'\u0077\u030a','\u1e99':'\u0079\u030a','\u1e9a':'\u0061\u02be','\u1e9b':'\u1e61','\u1e9e':'\u0073\u0073','\u1f50':'\u03c5\u0313','\u1f52':'\u03c5\u0313\u0300','\u1f54':'\u03c5\u0313\u0301','\u1f56':'\u03c5\u0313\u0342','\u1f80':'\u1f00\u03b9','\u1f81':'\u1f01\u03b9','\u1f82':'\u1f02\u03b9','\u1f83':'\u1f03\u03b9','\u1f84':'\u1f04\u03b9','\u1f85':'\u1f05\u03b9','\u1f86':'\u1f06\u03b9','\u1f87':'\u1f07\u03b9','\u1f88':'\u1f00\u03b9','\u1f89':'\u1f01\u03b9','\u1f8a':'\u1f02\u03b9','\u1f8b':'\u1f03\u03b9','\u1f8c':'\u1f04\u03b9','\u1f8d':'\u1f05\u03b9','\u1f8e':'\u1f06\u03b9','\u1f8f':'\u1f07\u03b9','\u1f90':'\u1f20\u03b9','\u1f91':'\u1f21\u03b9','\u1f92':'\u1f22\u03b9','\u1f93':'\u1f23\u03b9','\u1f94':'\u1f24\u03b9','\u1f95':'\u1f25\u03b9','\u1f96':'\u1f26\u03b9','\u1f97':'\u1f27\u03b9','\u1f98':'\u1f20\u03b9','\u1f99':'\u1f21\u03b9','\u1f9a':'\u1f22\u03b9','\u1f9b':'\u1f23\u03b9','\u1f9c':'\u1f24\u03b9','\u1f9d':'\u1f25\u03b9','\u1f9e':'\u1f26\u03b9','\u1f9f':'\u1f27\u03b9','\u1fa0':'\u1f60\u03b9','\u1fa1':'\u1f61\u03b9','\u1fa2':'\u1f62\u03b9','\u1fa3':'\u1f63\u03b9','\u1fa4':'\u1f64\u03b9','\u1fa5':'\u1f65\u03b9','\u1fa6':'\u1f66\u03b9','\u1fa7':'\u1f67\u03b9','\u1fa8':'\u1f60\u03b9','\u1fa9':'\u1f61\u03b9','\u1faa':'\u1f62\u03b9','\u1fab':'\u1f63\u03b9','\u1fac':'\u1f64\u03b9','\u1fad':'\u1f65\u03b9','\u1fae':'\u1f66\u03b9','\u1faf':'\u1f67\u03b9','\u1fb2':'\u1f70\u03b9','\u1fb3':'\u03b1\u03b9','\u1fb4':'\u03ac\u03b9','\u1fb6':'\u03b1\u0342','\u1fb7':'\u03b1\u0342\u03b9','\u1fbc':'\u03b1\u03b9','\u1fbe':'\u03b9','\u1fc2':'\u1f74\u03b9','\u1fc3':'\u03b7\u03b9','\u1fc4':'\u03ae\u03b9','\u1fc6':'\u03b7\u0342','\u1fc7':'\u03b7\u0342\u03b9','\u1fcc':'\u03b7\u03b9','\u1fd2':'\u03b9\u0308\u0300','\u1fd3':'\u03b9\u0308\u0301','\u1fd6':'\u03b9\u0342','\u1fd7':'\u03b9\u0308\u0342','\u1fe2':'\u03c5\u0308\u0300','\u1fe3':'\u03c5\u0308\u0301','\u1fe4':'\u03c1\u0313','\u1fe6':'\u03c5\u0342','\u1fe7':'\u03c5\u0308\u0342','\u1ff2':'\u1f7c\u03b9','\u1ff3':'\u03c9\u03b9','\u1ff4':'\u03ce\u03b9','\u1ff6':'\u03c9\u0342','\u1ff7':'\u03c9\u0342\u03b9','\u1ffc':'\u03c9\u03b9','\uab70':'\u13a0','\uab71':'\u13a1','\uab72':'\u13a2','\uab73':'\u13a3','\uab74':'\u13a4','\uab75':'\u13a5','\uab76':'\u13a6','\uab77':'\u13a7','\uab78':'\u13a8','\uab79':'\u13a9','\uab7a':'\u13aa','\uab7b':'\u13ab','\uab7c':'\u13ac','\uab7d':'\u13ad','\uab7e':'\u13ae','\uab7f':'\u13af','\uab80':'\u13b0','\uab81':'\u13b1','\uab82':'\u13b2','\uab83':'\u13b3','\uab84':'\u13b4','\uab85':'\u13b5','\uab86':'\u13b6','\uab87':'\u13b7','\uab88':'\u13b8','\uab89':'\u13b9','\uab8a':'\u13ba','\uab8b':'\u13bb','\uab8c':'\u13bc','\uab8d':'\u13bd','\uab8e':'\u13be','\uab8f':'\u13bf','\uab90':'\u13c0','\uab91':'\u13c1','\uab92':'\u13c2','\uab93':'\u13c3','\uab94':'\u13c4','\uab95':'\u13c5','\uab96':'\u13c6','\uab97':'\u13c7','\uab98':'\u13c8','\uab99':'\u13c9','\uab9a':'\u13ca','\uab9b':'\u13cb','\uab9c':'\u13cc','\uab9d':'\u13cd','\uab9e':'\u13ce','\uab9f':'\u13cf','\uaba0':'\u13d0','\uaba1':'\u13d1','\uaba2':'\u13d2','\uaba3':'\u13d3','\uaba4':'\u13d4','\uaba5':'\u13d5','\uaba6':'\u13d6','\uaba7':'\u13d7','\uaba8':'\u13d8','\uaba9':'\u13d9','\uabaa':'\u13da','\uabab':'\u13db','\uabac':'\u13dc','\uabad':'\u13dd','\uabae':'\u13de','\uabaf':'\u13df','\uabb0':'\u13e0','\uabb1':'\u13e1','\uabb2':'\u13e2','\uabb3':'\u13e3','\uabb4':'\u13e4','\uabb5':'\u13e5','\uabb6':'\u13e6','\uabb7':'\u13e7','\uabb8':'\u13e8','\uabb9':'\u13e9','\uabba':'\u13ea','\uabbb':'\u13eb','\uabbc':'\u13ec','\uabbd':'\u13ed','\uabbe':'\u13ee','\uabbf':'\u13ef','\ufb00':'\u0066\u0066','\ufb01':'\u0066\u0069','\ufb02':'\u0066\u006c','\ufb03':'\u0066\u0066\u0069','\ufb04':'\u0066\u0066\u006c','\ufb05':'\u0073\u0074','\ufb06':'\u0073\u0074','\ufb13':'\u0574\u0576','\ufb14':'\u0574\u0565','\ufb15':'\u0574\u056b','\ufb16':'\u057e\u0576','\ufb17':'\u0574\u056d'};
const COMBINING = /[\u0300-\u034e\u0350-\u036f\u0483-\u0487\u0591-\u05bd\u05bf\u05c1-\u05c2\u05c4-\u05c5\u05c7\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06dc\u06df-\u06e4\u06e7-\u06e8\u06ea-\u06ed\u0711\u0730-\u074a\u07eb-\u07f3\u07fd\u0816-\u0819\u081b-\u0823\u0825-\u0827\u0829-\u082d\u0859-\u085b\u0897-\u089f\u08ca-\u08e1\u08e3-\u08ff\u0951-\u0954\u09fe\u0c55-\u0c56\u0e38-\u0e39\u0e48-\u0e4b\u0eb8-\u0eb9\u0ec8-\u0ecb\u0f18-\u0f19\u0f35\u0f37\u0f39\u0f71-\u0f72\u0f74\u0f7a-\u0f7d\u0f80\u0f82-\u0f83\u0f86-\u0f87\u0fc6\u108d\u135d-\u135f\u17dd\u18a9\u1939-\u193b\u1a17-\u1a18\u1a75-\u1a7c\u1a7f\u1ab0-\u1abd\u1abf-\u1ace\u1b6b-\u1b73\u1cd0-\u1cd2\u1cd4-\u1ce0\u1ce2-\u1ce8\u1ced\u1cf4\u1cf8-\u1cf9\u1dc0-\u1dff\u20d0-\u20dc\u20e1\u20e5-\u20f0\u2cef-\u2cf1\u2de0-\u2dff\u302a-\u302f\ua66f\ua674-\ua67d\ua69e-\ua69f\ua6f0-\ua6f1\ua8e0-\ua8f1\ua92b-\ua92d\uaab0\uaab2-\uaab4\uaab7-\uaab8\uaabe-\uaabf\uaac1\ufb1e\ufe20-\ufe2f\u{101fd}\u{102e0}\u{10376}-\u{1037a}\u{10a0d}\u{10a0f}\u{10a38}-\u{10a3a}\u{10ae5}-\u{10ae6}\u{10d24}-\u{10d27}\u{10d69}-\u{10d6d}\u{10eab}-\u{10eac}\u{10efd}-\u{10eff}\u{10f46}-\u{10f50}\u{10f82}-\u{10f85}\u{11100}-\u{11102}\u{11366}-\u{1136c}\u{11370}-\u{11374}\u{1145e}\u{16af0}-\u{16af4}\u{16b30}-\u{16b36}\u{16ff0}-\u{16ff1}\u{1bc9e}\u{1d165}-\u{1d169}\u{1d16d}-\u{1d172}\u{1d17b}-\u{1d182}\u{1d185}-\u{1d18b}\u{1d1aa}-\u{1d1ad}\u{1d242}-\u{1d244}\u{1e000}-\u{1e006}\u{1e008}-\u{1e018}\u{1e01b}-\u{1e021}\u{1e023}-\u{1e024}\u{1e026}-\u{1e02a}\u{1e08f}\u{1e130}-\u{1e136}\u{1e2ae}\u{1e2ec}-\u{1e2ef}\u{1e4ec}-\u{1e4ef}\u{1e5ee}-\u{1e5ef}\u{1e8d0}-\u{1e8d6}\u{1e944}-\u{1e949}]/gu;
const CASEFOLD_RE = /[\u00b5\u00df\u0149\u017f\u01f0\u0345\u0390\u03b0\u03c2\u03d0\u03d1\u03d5\u03d6\u03f0\u03f1\u03f5\u0587\u13a0\u13a1\u13a2\u13a3\u13a4\u13a5\u13a6\u13a7\u13a8\u13a9\u13aa\u13ab\u13ac\u13ad\u13ae\u13af\u13b0\u13b1\u13b2\u13b3\u13b4\u13b5\u13b6\u13b7\u13b8\u13b9\u13ba\u13bb\u13bc\u13bd\u13be\u13bf\u13c0\u13c1\u13c2\u13c3\u13c4\u13c5\u13c6\u13c7\u13c8\u13c9\u13ca\u13cb\u13cc\u13cd\u13ce\u13cf\u13d0\u13d1\u13d2\u13d3\u13d4\u13d5\u13d6\u13d7\u13d8\u13d9\u13da\u13db\u13dc\u13dd\u13de\u13df\u13e0\u13e1\u13e2\u13e3\u13e4\u13e5\u13e6\u13e7\u13e8\u13e9\u13ea\u13eb\u13ec\u13ed\u13ee\u13ef\u13f0\u13f1\u13f2\u13f3\u13f4\u13f5\u13f8\u13f9\u13fa\u13fb\u13fc\u13fd\u1c80\u1c81\u1c82\u1c83\u1c84\u1c85\u1c86\u1c87\u1c88\u1e96\u1e97\u1e98\u1e99\u1e9a\u1e9b\u1e9e\u1f50\u1f52\u1f54\u1f56\u1f80\u1f81\u1f82\u1f83\u1f84\u1f85\u1f86\u1f87\u1f88\u1f89\u1f8a\u1f8b\u1f8c\u1f8d\u1f8e\u1f8f\u1f90\u1f91\u1f92\u1f93\u1f94\u1f95\u1f96\u1f97\u1f98\u1f99\u1f9a\u1f9b\u1f9c\u1f9d\u1f9e\u1f9f\u1fa0\u1fa1\u1fa2\u1fa3\u1fa4\u1fa5\u1fa6\u1fa7\u1fa8\u1fa9\u1faa\u1fab\u1fac\u1fad\u1fae\u1faf\u1fb2\u1fb3\u1fb4\u1fb6\u1fb7\u1fbc\u1fbe\u1fc2\u1fc3\u1fc4\u1fc6\u1fc7\u1fcc\u1fd2\u1fd3\u1fd6\u1fd7\u1fe2\u1fe3\u1fe4\u1fe6\u1fe7\u1ff2\u1ff3\u1ff4\u1ff6\u1ff7\u1ffc\uab70\uab71\uab72\uab73\uab74\uab75\uab76\uab77\uab78\uab79\uab7a\uab7b\uab7c\uab7d\uab7e\uab7f\uab80\uab81\uab82\uab83\uab84\uab85\uab86\uab87\uab88\uab89\uab8a\uab8b\uab8c\uab8d\uab8e\uab8f\uab90\uab91\uab92\uab93\uab94\uab95\uab96\uab97\uab98\uab99\uab9a\uab9b\uab9c\uab9d\uab9e\uab9f\uaba0\uaba1\uaba2\uaba3\uaba4\uaba5\uaba6\uaba7\uaba8\uaba9\uabaa\uabab\uabac\uabad\uabae\uabaf\uabb0\uabb1\uabb2\uabb3\uabb4\uabb5\uabb6\uabb7\uabb8\uabb9\uabba\uabbb\uabbc\uabbd\uabbe\uabbf\ufb00\ufb01\ufb02\ufb03\ufb04\ufb05\ufb06\ufb13\ufb14\ufb15\ufb16\ufb17]/gu;

function fold(text) {
  // Lowercased, then folded, then decomposed and stripped of the marks that
  // are accents, then lowercased again: a compatibility capital has no
  // lowercase of its own, and NFKD turns it into a plain uppercase letter the
  // first pass never saw.
  // Folded on both sides of the decomposition: a compatibility character can
  // decompose into one that folds — mathematical bold final sigma comes out
  // as ς, which is σ to `casefold` and itself to `toLowerCase`.
  // A soft hyphen goes with them: it says where a word may be broken, and a
  // word carrying one is the word, as the server reads it too.
  return String(text ?? '').toLowerCase()
    .replace(/\u00ad/g, '')
    .replace(CASEFOLD_RE, (ch) => CASEFOLD[ch])
    .normalize('NFKD')
    .replace(CASEFOLD_RE, (ch) => CASEFOLD[ch])
    .replace(COMBINING, '')
    .toLowerCase();
}

function queryMatcher() {
  const query = fold(V.q.trim());
  if (!query) return () => true;
  const phrase = (text) => fold(text).includes(query);
  if (S.claims.some((row) => phrase(haystack(row)))
      || S.papers.some((paper) => phrase(paperHaystack(paper)))) {
    return phrase;
  }
  const patterns = queryPatterns(query);
  // A query of punctuation alone has no words to look for, and `every` over
  // nothing is true: it would show the whole corpus for a query the phrase
  // pass has already failed to find. Nothing matches it.
  if (!patterns.length) return () => false;
  // Both sides folded, so the patterns need no case flag of their own.
  return (text) => { const folded = fold(text); return patterns.every((p) => p.test(folded)); };
}

// Scripts that do not put spaces between their words, as `search.py` has
// them. A term in one of these is looked for wherever it falls: every
// character around it is a letter, so a word start never comes.
const UNSEGMENTED = /[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af\u0e00-\u0eff\u1780-\u17ff\u0f00-\u0fff\u1000-\u109f\ua980-\ua9df\u1b00-\u1b7f\u1b80-\u1bbf\u1a20-\u1aaf\uaa00-\uaa5f]/u;

// A term matches from the start of a word, where words have starts. `\b` in
// JavaScript knows only ASCII and would never match a query written in
// another script, and a lookbehind is not available: the app supports macOS
// 13.0, whose WKWebView has none, and the SyntaxError would take the whole
// filter down. What is left is to consume the character before the word, and
// to allow the start of the string in its place.
function queryPatterns(query) {
  // Marks count as part of the word they sit on: in a script whose vowels are
  // marks, splitting on them cuts one written word into its consonants, and
  // the server does not.
  return (fold(query).match(/[\p{L}\p{N}_\p{M}]+/gu) || [])
    .map((term) => (UNSEGMENTED.test(term)
      ? new RegExp(term, 'u')
      // The underscore is a word character to the server's `\w`, so it is one
      // here: `bar` does not start a word in `foo_bar` on either side. So is
      // a mark the folding kept — a vowel sign is part of the word it sits
      // in, and a boundary inside किताब would find ताब there.
      : new RegExp(`(?:^|[^\\p{L}\\p{N}_\\p{M}])${term}`, 'u')));
}

// A claim's searchable text. It carries its paper's key and year as well as
// the title, so that every query matching a paper also matches that paper's
// claims: listing a paper in the sidebar and then showing it as empty when
// clicked would be worse than not listing it at all.
function haystack(row) {
  return [row.text, row.evidence, row.quote, row.note, row.paper, row.paper_title,
          row.paper_year, (row.paper_authors || []).join(' '),
          paperSearchTextFor(row.paper), (row.tags || []).join(' '),
          (row.paper_labels || []).join(' '), Object.values(row.facets || {}).flat().join(' ')]
    .join(' ').toLowerCase();
}

// Paper summaries and notes, for their claims' haystacks: a query matching only
// those fields must not list the paper and then show it as empty, which is the one
// thing `haystack` carries the paper's fields to prevent.
//
// Cached against the array `S.papers` currently is. That array is replaced
// wholesale whenever the corpus is read and never edited in place, so the
// identity check rebuilds the map exactly when it has to.
let paperSearchText = { from: null, byKey: new Map() };

function paperSearchTextFor(key) {
  if (paperSearchText.from !== S.papers) {
    paperSearchText = {
      from: S.papers,
      byKey: new Map((S.papers || []).map((p) => [p.key, [p.summary, p.notes].join(' ')])),
    };
  }
  return paperSearchText.byKey.get(key) || '';
}

// A paper's own text, for the query. `haystack` covers a paper through its
// claims; this covers one that has none yet, which is exactly the paper a
// title search is most likely to be looking for.
function paperHaystack(paper) {
  return [paper.title, paper.key, (paper.authors || []).join(' '), paper.year, paper.summary,
          paper.notes, (paper.labels || []).join(' ')]
    .join(' ').toLowerCase();
}

// The papers' own text is searched on the server, because that is where it is:
// every claim is already in the page, but the PDFs never are. Debounced, since
// this runs on a keystroke, and short queries are not sent at all — every paper
// holds "a", and reading the corpus to prove it helps nobody.
let textSearchTimer = null;
let textSearchSeq = 0;
// Two characters: AI, RL and R2 are what a reader of this corpus searches
// for, and one character is every paper in it.
const TEXT_SEARCH_MIN = 2;
// Three letters of a Latin query says little; two characters of Chinese or
// Japanese is a whole word. A query carrying anything outside the Latin
// scripts is asked whatever its length.
const COMPACT_SCRIPT = /[^\p{Script=Latin}\p{N}\p{P}\p{Z}\p{C}]/u;

function worthAsking(query) {
  return query.length >= TEXT_SEARCH_MIN || (query.length > 0 && COMPACT_SCRIPT.test(query));
}

function scheduleTextSearch() {
  clearTimeout(textSearchTimer);
  const query = V.q.trim();
  if (!worthAsking(query)) { V.textSearch = null; return; }
  textSearchTimer = setTimeout(() => runTextSearch(query), 250);
}

// Strand a debounce that has not fired and any answer still in flight. Called
// when the workspace changes: the same query in the new corpus is a different
// question, and the old corpus's papers must not be drawn under it.
function dropTextSearch() {
  clearTimeout(textSearchTimer);
  textSearchTimer = null;
  textSearchSeq += 1;
  V.textSearch = null;
}

// A paper imported since the answer came back holds the query's words as much
// as any other, and one that has just been given its PDF is searchable for the
// first time. Filtering the answer against the corpus can drop a hit that has
// gone but cannot add one that has arrived, so the question is asked again.
function rerunTextSearch() {
  if (V.textSearch && V.textSearch.q === V.q.trim()) runTextSearch(V.textSearch.q, true);
}

// `quiet` keeps what is on screen until the new answer lands, for a re-run
// nobody asked for: a corpus change would otherwise blink the results back to
// "reading the papers" every time a paper is imported.
async function runTextSearch(query, quiet = false) {
  const seq = ++textSearchSeq;
  const workspace = workspaceClient.workspaceId;
  const library = V.view === 'library';
  if (!quiet) {
    captureOpenEditor();   // the redraw rebuilds any open form from state
    V.textSearch = { q: query, loading: true, papers: [], terms: [], error: null };
    drawSearchProgress();
  }
  let next;
  try {
    const papers = new Map();
    let offset = 0, found;
    do {
      found = await api(`/api/search?q=${encodeURIComponent(query)}&limit=${library ? 100 : 20}&offset=${offset}`);
      if (seq !== textSearchSeq || workspace !== workspaceClient.workspaceId) return;
      for (const hit of found.papers || []) papers.set(hit.key, hit);
      if (found.next_offset != null) {
        if (!Number.isInteger(found.next_offset) || found.next_offset <= offset) throw new Error('Search returned an invalid next page.');
        offset = found.next_offset;
      }
    } while (library && found.next_offset != null);
    next = { q: query, loading: false, papers: [...papers.values()], terms: found.terms || [], complete: found.next_offset == null, error: null };
  } catch (error) {
    next = { q: query, loading: false, papers: [], terms: [], error: error.message };
  }
  // An answer to a query that is no longer the one on screen is dropped: the
  // requests are not guaranteed to come back in the order they went out, and
  // the corpus can have changed under the one still running.
  if (seq !== textSearchSeq || workspace !== workspaceClient.workspaceId) return;
  V.textSearch = next;
  captureOpenEditor();
  drawSearchProgress();
}

// The results live under the claims, so the research form never shows them —
// and redrawing it would replace a form frozen mid-save with an enabled one,
// which is how typing into the replacement gets thrown away when the save
// lands. `render` stands off the research view for the same reason.
function drawSearchProgress() {
  if (V.view === 'library') renderPapers();
  if (V.view !== 'research') renderContent();
}

// The papers the query matches: directly, or through a claim of theirs. The
// claim match ignores the selected paper, so narrowing to one paper does not
// empty the list you would use to leave it.
function matchingPapers() {
  const labelled = labelledPapers();
  const within = labelled ? S.papers.filter((p) => labelled.has(p.key)) : S.papers;
  if (!V.q.trim()) return within;
  const matches = queryMatcher();
  const owners = new Set(S.claims
    .filter((row) => matches(haystack(row)))
    .map((row) => row.paper));
  if (V.view === 'library' && V.textSearch?.q === V.q.trim()) {
    for (const hit of V.textSearch.papers || []) owners.add(hit.key);
  }
  return within.filter((p) => owners.has(p.key) || matches(paperHaystack(p)));
}

// A paper appears once, even when several of its claims match. Papers without
// extracted claims still belong in the library and can match their metadata.
function visiblePapers() {
  const claimFilters = V.tag || V.facet || V.kind || V.unreviewed || V.unverified;
  const owners = claimFilters ? new Set(S.claims.filter(row =>
    (!V.tag || (row.tags || []).includes(V.tag))
    && hasFacetFilter(row)
    && (!V.kind || row.kind === V.kind)
    && (!V.unreviewed || !row.reviewed)
    && (!V.unverified || row.quote_verified === false)).map(row => row.paper)) : null;
  const matched = new Set(matchingPapers().map(p => p.key));
  return sortedPapers(S.papers.filter(p =>
    (!V.label || findLabel(p.labels, V.label) !== undefined)
    && (!owners || owners.has(p.key))
    && matched.has(p.key)), V.paperSort);
}

// A destination the label filter excludes arrives empty: a paper's header
// with nothing under it, or a citation that selects some other card instead of
// the one that was clicked. Every path that opens a paper drops the filter
// when the paper it lands on does not carry the label, and keeps it when it
// does — the filter says which papers are in view, and this one now is.
function keepLabelFor(key) {
  const paper = key && S.papers.find((p) => p.key === key);
  if (V.label && paper && findLabel(paper.labels, V.label) === undefined) V.label = null;
}

// The same for a facet filter: a paper none of whose claims carries the value
// would open with every claim filtered away, so the filter gives way to it.
function keepFacetFor(key) {
  if (V.facet && key && !S.claims.some((row) => row.paper === key && hasFacetFilter(row))) V.facet = null;
}

// The keys the label filter allows, or null when no label is chosen. A label
// belongs to the paper, so it narrows the claims by way of the papers that
// carry it rather than by anything on the claim itself.
function labelledPapers() {
  if (!V.label) return null;
  return new Set(S.papers.filter((p) => findLabel(p.labels, V.label) !== undefined).map((p) => p.key));
}

// A facet filter is one value of one facet, held as `name:value`. Facet names
// are slugs, so the first colon is the one between them.
function facetFilter() {
  const at = (V.facet || '').indexOf(':');
  return at > 0 ? { name: V.facet.slice(0, at), value: V.facet.slice(at + 1) } : null;
}

// Values are one value in any case or spacing, as `store.facet_value_key` decides.
function facetValueKey(value) {
  return foldLabel(String(value).replace(/,/g, ' ').split(/\s+/).filter(Boolean).join(' '));
}

// A facet's entry in a dictionary keyed by facet name. Own properties only: a
// facet may be named "constructor", which every object inherits.
function facetEntry(dict, name) {
  return dict && Object.hasOwn(dict, name) ? dict[name] : undefined;
}

function hasFacetFilter(row) {
  const filter = facetFilter();
  if (!filter) return true;
  const key = facetValueKey(filter.value);
  return (facetEntry(row.facets, filter.name) || []).some((value) => facetValueKey(value) === key);
}

// The defined facets' values on a claim, in the order the facets are defined.
function claimFacetValues(row) {
  return (S.facets || []).flatMap((facet) =>
    (facetEntry(row.facets, facet.name) || []).map((value) => [facet.name, value]));
}

// As `chooseLabel`: a paper open on screen with no claim carrying the value is
// left, rather than standing there with every claim filtered away.
function chooseFacet(name, value) {
  const filter = facetFilter();
  V.facet = filter && filter.name === name && facetValueKey(filter.value) === facetValueKey(value)
    ? null : `${name}:${value}`;
  if (V.facet && V.paper && !S.claims.some((row) => row.paper === V.paper && hasFacetFilter(row))) {
    captureOpenEditor();
    parkNewClaimForNavigation(null);
    parkSynthEditor();
    // The paper's note editor holds the content pane; parked, its draft is
    // kept and the claims under the new filter can be drawn.
    parkNoteEditor();
    V.paper = null;
    V.selectedId = null;
    V.editing = null;
  }
  syncHash(true);
  renderAll();
}

function visibleClaims() {
  const matches = queryMatcher();
  const labelled = labelledPapers();
  return S.claims.filter((row) =>
    (!V.paper || row.paper === V.paper)
    && (!labelled || labelled.has(row.paper))
    && (!V.tag || (row.tags || []).includes(V.tag))
    && hasFacetFilter(row)
    && (!V.kind || row.kind === V.kind)
    && (!V.unreviewed || !row.reviewed)
    && (!V.unverified || row.quote_verified === false)
    && matches(haystack(row)));
}

// Derive colors from names so existing and new labels keep the same color
// across papers, reloads, and changes to the corpus, without extra metadata.
function labelStyle(name) {
  let hash = 2166136261;
  for (const char of labelKey(name)) {
    hash = Math.imul(hash ^ char.codePointAt(0), 16777619);
  }
  return `--label-hue:${(hash >>> 0) % 360}`;
}

function labelChip(name) {
  return `<span class="label" data-label="${esc(name)}" style="${labelStyle(name)}">${esc(name)}</span>`;
}

// Hide the section until at least one paper has a label.
function renderLabels() {
  const entries = Object.entries(S.label_counts || {});
  $('labels-sec').hidden = entries.length === 0;
  $('labels').innerHTML = entries.map(([name, count]) => `
    <li class="${V.label && labelKey(V.label) === labelKey(name) ? 'active' : ''}" data-label="${esc(name)}" style="${labelStyle(name)}">
      <span class="label-dot" aria-hidden="true"></span>
      <span>${esc(name)}</span>
      <span class="n">${count}</span>
    </li>`).join('');
}

// Which facets are open in the sidebar, by name, so a redraw keeps them so.
const openFacets = new Set();
let facetsHTML = null;

function renderFacets() {
  const facets = S.facets || [];
  const filter = facetFilter();
  const html = facets.length ? facets.map((facet) => {
    const counts = Object.entries(facetEntry(S.facet_counts, facet.name) || {});
    const stale = facetEntry(S.facets_stale, facet.name) || 0;
    const active = (value) => filter && filter.name === facet.name
      && facetValueKey(filter.value) === facetValueKey(value);
    return `<details class="facet" data-facet="${esc(facet.name)}" ${openFacets.has(facet.name) || (filter && filter.name === facet.name) ? 'open' : ''}>
      <summary title="${esc(facet.description || '')}"><span>${esc(facet.name)}</span>
        <span class="n">${counts.length} ${counts.length === 1 ? 'value' : 'values'}</span></summary>
      ${facet.description ? `<p class="hint">${esc(facet.description)}</p>` : ''}
      ${stale ? `<p class="hint">Not yet filled in on ${stale} ${stale === 1 ? 'paper' : 'papers'}.</p>` : ''}
      <ul class="taglist">${counts.length ? counts.map(([value, n]) => `
        <li class="${active(value) ? 'active' : ''}" data-facet-value="${esc(value)}">
          <span>${esc(value)}</span><span class="n">${n}</span></li>`).join('')
        : '<li class="hint">No values yet.</li>'}</ul>
      <div class="row facet-actions">
        <button type="button" class="mini" data-facet-act="describe">Edit description</button>
        <button type="button" class="mini" data-facet-act="values" ${counts.length ? '' : 'disabled'}>Rename or merge values</button>
        <button type="button" class="mini" data-facet-act="delete">Delete</button>
      </div>
    </details>`;
  }).join('') : '<p class="hint">A facet records something about every claim, such as the models or datasets behind it.</p>';
  // Polls should leave a focused value or an open disclosure where it is.
  if (facetsHTML !== html) {
    $('facets').innerHTML = html;
    facetsHTML = html;
  }
  const toFill = S.facet_papers_to_fill || 0;
  $('btn-fill-facets').hidden = !toFill;
  $('btn-fill-facets').textContent = `Fill in ${toFill} ${toFill === 1 ? 'paper' : 'papers'}`;
}

function renderTags() {
  const entries = Object.entries(S.tag_counts);
  const declared = new Set(S.tags.map((t) => t.name));
  $('tags').innerHTML = (entries.length ? '' : '<li class="hint">No topics yet.</li>')
    + entries.map(([name, count]) => `
      <li class="${V.tag === name ? 'active' : ''}" data-tag="${esc(name)}"
          title="${esc((S.tags.find((t) => t.name === name) || {}).description || '')}">
        <span>${esc(name)}${declared.has(name) ? '' : ' *'}</span>
        <span class="n">${count}</span>
      </li>`).join('');
}

// What the query found in the papers themselves, under the claims it found.
// A paper can hold a word no claim of it mentions, and a paper nothing has
// been extracted from yet holds all of them.
function textSearchBlock() {
  const found = V.textSearch;
  if (!found || found.q !== V.q.trim()) return '';
  const head = '<h3>In the PDFs</h3>';
  if (found.loading) return `<div class="pdfhits">${head}<p class="hint">Reading the papers…</p></div>`;
  if (found.error) {
    return `<div class="pdfhits">${head}<p class="hint">Could not search the papers: ${esc(found.error)}</p></div>`;
  }
  if (!found.papers.length) {
    return `<div class="pdfhits">${head}<p class="hint">No paper's text holds every word of that.</p></div>`;
  }
  // Drawn against the corpus as it stands, not as it stood when the answer
  // came back: a paper removed since would otherwise stay listed here and
  // clicking it would select a paper that is not there.
  const live = new Set(S.papers.map((paper) => paper.key));
  const papers = found.papers.filter((hit) => live.has(hit.key));
  if (!papers.length) {
    return `<div class="pdfhits">${head}<p class="hint">No paper's text holds every word of that.</p></div>`;
  }
  const hits = papers.map((hit) => {
    const cite = `${(hit.authors || [])[0] ? hit.authors[0].split(' ').pop() : hit.key} ${hit.year || ''}`;
    const passages = (hit.passages || []).map((passage) =>
      `<p class="pp"><span class="hint">p. ${passage.page}</span> …${mark(passage)}…</p>`).join('');
    return `<div class="pdfhit">
      <div class="ph">
        <button type="button" class="pt" data-act="open-paper" data-paper="${esc(hit.key)}"
          title="${esc(hit.title || hit.key)}">${esc(cite)}</button>
        <span class="hint">${esc(hit.title || '')}</span>
        <span class="hint" style="margin-left:auto">${hit.occurrences} ${hit.occurrences === 1 ? 'mention' : 'mentions'}</span>
      </div>${passages}</div>`;
  }).join('');
  return `<div class="pdfhits">${head}${hits}</div>`;
}

// A passage as the server cut it: it is the one that knows how the text was
// folded to find the terms, and a browser's own case-insensitive matching
// cannot expand ß to ss, so it would find nothing to mark in a passage that
// was found for exactly that reason. Pieces rather than offsets, since an
// offset into a Python string is not an offset into a JavaScript one.
function mark(passage) {
  const parts = passage.parts;
  if (!parts || !parts.length) return esc(passage.text || '');
  return parts.map((part) => (part.mark ? `<mark>${esc(part.text)}</mark>` : esc(part.text))).join('');
}

// Choosing a label from the sidebar or from a paper's own header. A label is
// a property of the paper, so it is read as "show me the papers like this
// one": a paper open on screen that does not carry the label is left, rather
// than sitting there unchanged while the list behind it narrows to papers the
// reader cannot see.
function chooseLabel(name) {
  // A chip spelled differently on its paper filters by the sidebar's spelling.
  const next = V.label && labelKey(V.label) === labelKey(name) ? null
    : findLabel(Object.keys(S.label_counts || {}), name) || name;
  const open = V.paper && S.papers.find((p) => p.key === V.paper);
  if (next && open && findLabel(open.labels, next) === undefined) {
    captureOpenEditor();
    parkNewClaimForNavigation(null);
    parkSynthEditor();
    V.paper = null;
    V.selectedId = null;
    V.editing = null;
  }
  V.label = next;
  syncHash(true);
  renderAll();
}

$('labels').addEventListener('click', (event) => {
  const li = event.target.closest('[data-label]');
  if (!li) return;
  chooseLabel(li.dataset.label);
});

// `toggle` does not bubble, so it is caught on the way down.
$('facets').addEventListener('toggle', (event) => {
  const details = event.target.closest?.('details[data-facet]');
  if (!details) return;
  if (details.open) openFacets.add(details.dataset.facet);
  else openFacets.delete(details.dataset.facet);
}, true);

async function facetRequest(path, method, body, failure) {
  try {
    await api(path, { method, headers: { 'Content-Type': 'application/json' },
                      body: body === undefined ? undefined : JSON.stringify(body) });
  } catch (error) {
    toast(`${failure}: ${error.message}`, { tone: 'warn' });
    return false;
  }
  // The change is saved; a read failing after it is a sync problem, which the
  // sync notice reports and the poll retries, not a failed change.
  await refreshShowingError();
  return true;
}

$('facets').addEventListener('click', async (event) => {
  const details = event.target.closest('details[data-facet]');
  if (!details) return;
  const name = details.dataset.facet;
  const path = `/api/facets/${encodeURIComponent(name)}`;
  const value = event.target.closest('[data-facet-value]');
  if (value) {
    chooseFacet(name, value.dataset.facetValue);
    return;
  }
  const act = event.target.closest('[data-facet-act]')?.dataset.facetAct;
  const facet = (S.facets || []).find((f) => f.name === name);
  if (!act || !facet) return;
  if (act === 'describe') {
    const description = await askText(`What should “${name}” record?`, {
      note: 'This goes into the prompt for every claim. Changing it marks the values on every paper to be filled in again.',
      value: facet.description || '',
    });
    if (description === null || description.trim() === (facet.description || '')) return;
    await facetRequest(path, 'PATCH', { description }, 'Could not save the description');
  } else if (act === 'values') {
    const values = Object.keys(facetEntry(S.facet_counts, name) || {});
    const chosen = await askDialog({
      title: `Which ${name} values?`,
      note: 'Click the values to rename, merge, or remove.',
      input: { placeholder: 'values, separated by commas', value: '' },
      labelChoices: values, ok: 'Next',
    });
    if (chosen === null) return;
    const picked = parseLabels(chosen);
    if (!picked.length) return;
    const into = await askText(picked.length === 1 ? `Rename “${picked[0]}” to` : `Merge ${picked.length} values into`, {
      note: 'Every claim carrying them gets this value instead, and later reads write it too. Leave it empty to remove them.',
      placeholder: 'leave empty to remove',
      value: picked.length === 1 ? picked[0] : '',
    });
    if (into === null) return;
    if (!into.trim() && !await confirmDialog(`Remove ${picked.length === 1 ? `“${picked[0]}”` : `${picked.length} values`} from every claim?`, {
      note: 'Later reads will leave them out too.', ok: 'Remove',
    })) return;
    // Read before the request: its refresh drops a filter on a value that is
    // gone, and the filter should follow the value to its new name instead.
    const filter = facetFilter();
    if (await facetRequest(`${path}/values`, 'POST', { values: picked, into }, 'Could not change the values')) {
      if (filter && filter.name === name && picked.some((v) => facetValueKey(v) === facetValueKey(filter.value))) {
        V.facet = into.trim() ? `${name}:${into.trim()}` : null;
        syncHash();
        renderAll();
      }
    }
  } else if (act === 'delete') {
    const counts = Object.values(facetEntry(S.facet_counts, name) || {}).reduce((a, b) => a + b, 0);
    if (!await confirmDialog(`Delete the facet “${name}”?`, {
      note: counts ? `Its values come off ${counts} ${counts === 1 ? 'claim' : 'claims'}, including any you set by hand.` : '',
      ok: 'Delete facet',
    })) return;
    await facetRequest(path, 'DELETE', undefined, 'Could not delete the facet');
  }
});

$('btn-facet').addEventListener('click', async () => {
  const name = await askText('New facet', {
    note: 'A short name for what to record about each claim, such as model, dataset, or intervention.',
    placeholder: 'model',
  });
  if (!name || !name.trim()) return;
  const description = await askText(`What should “${name.trim()}” record?`, {
    note: 'One line telling the model what to list for each claim. It goes into the extraction prompt.',
    placeholder: 'The specific language models a result was measured on',
  });
  if (description === null) return;
  await facetRequest('/api/facets', 'POST', { name, description }, 'Could not add the facet');
});

$('btn-fill-facets').addEventListener('click', async () => {
  const n = S.facet_papers_to_fill || 0;
  const go = await confirmDialog(`Fill in facets on ${n} ${n === 1 ? 'paper' : 'papers'}?`, {
    note: 'Each paper’s claims, evidence, and quotes are sent to the model, one call per paper. '
      + 'The PDFs are not sent. Values you set by hand are left alone.',
    ok: 'Fill in',
  });
  if (!go) return;
  const workspace = await settleDeletes();
  if (!workspace) return;
  try {
    await api('/api/facets/fill', {
      method: 'POST', headers: { 'Content-Type': 'application/json', ...workspace }, body: '{}',
    });
  } catch (error) {
    toast(`Could not fill in the facets: ${error.message}`, { tone: 'warn' });
    return;
  }
  await refreshShowingError();
});

$('tags').addEventListener('click', (event) => {
  const li = event.target.closest('[data-tag]');
  if (!li) return;
  V.tag = V.tag === li.dataset.tag ? null : li.dataset.tag;
  syncHash(true);
  renderAll();
});

function applyQuery(value) {
  captureOpenEditor();
  V.q = value;
  scheduleTextSearch();
  renderPapers();   // the query filters the paper list as well as the claims
  renderContent();
}

function clearQuery() {
  applyQuery('');
}

$('q').addEventListener('input', (e) => applyQuery(e.target.value));
$('kind').addEventListener('change', (e) => { captureOpenEditor(); V.kind = e.target.value; renderContent(); });

$('only-unreviewed').addEventListener('change', (e) => { captureOpenEditor(); V.unreviewed = e.target.checked; renderContent(); });
$('only-unverified').addEventListener('change', (e) => { captureOpenEditor(); V.unverified = e.target.checked; renderContent(); });
$('group-by-tag').addEventListener('change', (e) => { captureOpenEditor(); V.group = e.target.checked; renderContent(); });
