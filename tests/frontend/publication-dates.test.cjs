const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');

const source = readFileSync(resolve(__dirname, '../../doxograph/static/paper-map.js'), 'utf8');
const document = { getElementById: () => ({ addEventListener() {} }), addEventListener() {} };
const { graphPublication, graphDateLabel, graphDateTicks } = new Function('document', 'window',
  `${source}\nreturn { graphPublication, graphDateLabel, graphDateTicks };`)(document, {});
const utc = (date) => Date.parse(`${date}T00:00:00Z`);

test('days within one year use elapsed calendar time, including leap day', () => {
  const feb = graphPublication({ year: 2024, publication_date: '2024-02-28' });
  const leap = graphPublication({ year: 2024, publication_date: '2024-02-29' });
  const mar = graphPublication({ year: 2024, publication_date: '2024-03-01' });
  assert.equal(feb.time, utc('2024-02-28'));
  assert.equal(leap.time - feb.time, 86400000);
  assert.equal(mar.time - feb.time, 2 * 86400000);
  assert.equal(graphDateLabel(feb.date), 'Feb 28, 2024');
});

test('partial dates preserve their precision and old years still plot', () => {
  const month = graphPublication({ publication_date: '2024-02' });
  assert.equal(month.time, (utc('2024-02-01') + utc('2024-03-01')) / 2);
  assert.equal(graphDateLabel(month.date), 'Feb 2024 (month only)');
  const year = graphPublication({ year: 2024 });
  assert.equal(year.time, (utc('2024-01-01') + utc('2025-01-01')) / 2);
  assert.equal(graphDateLabel(year.date), '2024 (year only)');
  assert.deepEqual(graphPublication({}), { date: null, time: null });
  // Date.UTC interprets 1..99 as 1901..1999; the map must not.
  assert.equal(graphPublication({ publication_date: '0099-01-01' }).time, utc('0099-01-01'));
});

test('axis ticks show days for short ranges and months or years for longer ranges', () => {
  const ticks = graphDateTicks(utc('2024-02-28'), utc('2024-03-03'), 1000);
  assert.ok(ticks.some(t => t.label === 'Feb 29, 2024'));
  assert.ok(ticks.some(t => t.label === 'Mar 1, 2024'));
  const months = graphDateTicks(utc('2026-01-01'), utc('2026-12-31'), 1000);
  assert.ok(months.some(t => t.label === 'Mar 2026'));
  const years = graphDateTicks(utc('2000-01-01'), utc('2026-12-31'), 1000);
  assert.ok(years.every(t => /^\d{4}$/.test(t.label)));
  for (const group of [ticks, months, years]) {
    assert.ok(group.length > 2);
    assert.ok(group.every((t, i) => i === 0 || t.time > group[i - 1].time));
  }
});
