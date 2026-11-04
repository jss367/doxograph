const {test} = require('node:test');
const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const FindingsModel = new Function(readFileSync(resolve(__dirname, '../../doxograph/static/findings-model.js'), 'utf8') + '\nreturn FindingsModel;')();

const claim = (id, paper, author, year, reviewed = true) => ({id, paper, paper_authors: [author], paper_year: year, reviewed});

test('the lead of a synthesis is its first sentence with the citations taken out', () => {
  const synthesis = {text: 'Steering holds within a window [doe2026-c1, li2025-c2]. Beyond it the effect collapses [doe2026-c3].'};
  assert.equal(FindingsModel.lead(synthesis), 'Steering holds within a window.');
  assert.equal(FindingsModel.lead(null), '');
});

test('a sentence does not end at a decimal point or an abbreviation that is not followed by a capital', () => {
  assert.equal(FindingsModel.firstSentence('Recovery is 3.8% at 70B, vs. 1% below it. Smaller models stay under 1%.'),
    'Recovery is 3.8% at 70B, vs. 1% below it.');
  assert.equal(FindingsModel.firstSentence('Maiya et al. (2025) find tuning is manual, whereas Becker et al. (2026) claim a scheme. The difference may be the task.'),
    'Maiya et al. (2025) find tuning is manual, whereas Becker et al. (2026) claim a scheme.');
  assert.equal(FindingsModel.firstSentence('Han et al. (2026, welfare axis) report a monotonic dose-response (Fig. 2). Bigelow finds collapse.'),
    'Han et al. (2026, welfare axis) report a monotonic dose-response (Fig. 2).');
  assert.equal(FindingsModel.firstSentence('Is it real? Maybe.'), 'Is it real?');
  assert.equal(FindingsModel.firstSentence('No full stop here'), 'No full stop here');
  assert.equal(FindingsModel.firstSentence('  spaced\n out.  Then more.'), 'spaced out.');
});

test('topics follow the vocabulary order, with unlisted ones after it, and carry their findings', () => {
  const a = claim('a-c1', 'a', 'Jane Doe', 2026), b = claim('b-c1', 'b', 'Bo Li', 2025, false);
  // A third paper under recovery that no finding names still counts there.
  const c = {...claim('c-c1', 'c', 'Cy Chen', 2024), tags: ['recovery']};
  const state = {
    tags: [{name: 'recovery', description: 'How often a model returns.'}, {name: 'scaling', description: ''}],
    claims: [{...a, tags: ['recovery']}, {...b, tags: ['recovery']}, c],
    syntheses: [{topic: 'scaling', text: 'Flat [a-c1].', stale: true, n_papers: 2}],
    tensions: [{id: 't1', topics: ['recovery', 'zeta'], claims: [a, b], status: 'open', stale: false}],
    agreements: [{id: 'a1', topics: ['recovery'], claims: [a, b], status: 'open', stale: false}],
  };
  const sections = FindingsModel.topics(state);
  assert.deepEqual(sections.map((s) => s.name), ['recovery', 'scaling', 'zeta']);
  assert.equal(sections[0].description, 'How often a model returns.');
  assert.equal(sections[0].count, 2);
  assert.equal(sections[0].papers, 3);
  assert.equal(sections[1].papers, 2);
  assert.equal(sections[0].stale, false);
  assert.equal(sections[1].stale, true);
  assert.equal(sections[1].count, 0);
  assert.equal(sections[2].tensions.length, 1);
});

test('a topic is stale when any finding under it is', () => {
  const a = claim('a-c1', 'a', 'Jane Doe', 2026), b = claim('b-c1', 'b', 'Bo Li', 2025);
  const state = {tags: [], syntheses: [], agreements: [], tensions: [{id: 't1', topics: ['x'], claims: [a, b], status: 'open', stale: true}]};
  assert.equal(FindingsModel.topics(state)[0].stale, true);
});

test('papers are named by first author and year, once each, and unreviewed claims are counted', () => {
  const a = claim('a-c1', 'a', 'Jane Doe', 2026), b = claim('b-c1', 'b', 'Bo Li', 2025, false), c = claim('a-c2', 'a', 'Jane Doe', 2026, false);
  assert.deepEqual(FindingsModel.papersOf({claims: [a, b, c]}), ['Doe 2026', 'Li 2025']);
  assert.equal(FindingsModel.unreviewedCount([a, b, c]), 2);
  assert.deepEqual(FindingsModel.papersOf({claims: [claim('k-c1', 'key2024', null, null)]}), ['key2024 n.d.']);
});

test('two papers by the same first author in the same year are told apart by their keys', () => {
  const a = claim('a-c1', 'doe2026steering', 'Jane Doe', 2026), b = claim('b-c1', 'doe2026recovery', 'Jane Doe', 2026);
  assert.deepEqual(FindingsModel.papersOf({claims: [a, b]}), ['Doe 2026 (doe2026recovery)', 'Doe 2026 (doe2026steering)']);
});

for (const kind of ['agreement', 'tension']) {
  test(`${kind} paper links exclude topicless, dismissed and unrelated findings`, () => {
    const row = {id:'live', topics:['remaining'], status:'open', claims:[{paper:'a'}, {paper:'b'}]};
    const rows = [row, {...row,id:'orphan',topics:[]}, {...row,id:'missing',topics:undefined},
      {...row,id:'dismissed',status:'dismissed'}, {...row,id:'other',claims:[{paper:'b'}]}];
    const state = {[kind === 'agreement' ? 'agreements' : 'tensions']: rows};
    assert.deepEqual(FindingsModel.forPaper(state, 'a').map(({row}) => row.id), ['live']);
  });
  test(`${kind} stays linked with remaining topics and returns after restoration`, () => {
    const row = {id:'live', topics:['first','remaining'], status:'confirmed', claims:[{paper:'a'}, {paper:'b'}]};
    const state = {[kind === 'agreement' ? 'agreements' : 'tensions']: [row]};
    assert.equal(FindingsModel.forPaper(state, 'a').length, 1);
    row.topics.shift();
    assert.equal(FindingsModel.forPaper(state, 'a').length, 1);
    row.topics.length = 0;
    assert.equal(FindingsModel.forPaper(state, 'a').length, 0);
    row.topics.push('restored');
    assert.equal(FindingsModel.forPaper(state, 'a').length, 1);
  });
}
