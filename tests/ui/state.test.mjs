import assert from 'node:assert/strict';
import test from 'node:test';
import { Basket, interval, rangeFor, scopeBatches } from '../../memu/acquisition/ui/state.js';

test('calendar validation rejects normalized impossible dates and accepts leap day', () => {
  for (const value of ['2026-02-30', '2025-02-29', '2026-13-01', '2026-00-10', '2026-01-00', '0000-01-01']) {
    assert.throws(() => interval({ start: value }), /日期/);
  }
  assert.ok(interval({ start: '2024-02-29', end: '2024-02-29' }).end);
  assert.throws(() => interval({ end: '9999-12-31' }), /日期/);
  assert.throws(() => interval({ start: '2026-10-05', end: '2026-10-01' }), /结束日期/);
});

test('inclusive end dates advance by a local calendar day across DST', () => {
  const previous = process.env.TZ;
  process.env.TZ = 'America/New_York';
  try {
    const value = interval({ start: '2026-03-08', end: '2026-03-08' });
    assert.equal(Date.parse(value.end) - Date.parse(value.start), 23 * 60 * 60 * 1000);
    assert.equal(value.start, '2026-03-08T05:00:00.000Z');
    assert.equal(value.end, '2026-03-09T04:00:00.000Z');
  } finally { previous === undefined ? delete process.env.TZ : process.env.TZ = previous; }
});

test('a URL range belongs only to its focused conversation and stored ranges remain isolated', () => {
  const basket = new Basket(), drafts = new Map();
  const params = new URLSearchParams({ focus: 'group:a', start: '2026-10-01', end: '2026-10-05' });
  assert.deepEqual(rangeFor('group:a', drafts, basket, params), { start: '2026-10-01', end: '2026-10-05' });
  assert.deepEqual(rangeFor('private:b', drafts, basket, params), {});
  drafts.set('group:a', rangeFor('group:a', drafts, basket, params));
  const next = rangeFor('private:b', drafts, basket, params);
  params.set('focus', 'private:b'); params.delete('start'); params.delete('end');
  drafts.set('private:b', next);
  assert.deepEqual(rangeFor('private:b', drafts, basket, params), {});
  assert.deepEqual(rangeFor('group:a', drafts, basket, params), { start: '2026-10-01', end: '2026-10-05' });
  const detached = rangeFor('group:a', drafts, basket, params);
  detached.start = '2020-01-01';
  assert.equal(drafts.get('group:a').start, '2026-10-01');
});

test('the basket groups equal ranges while retaining distinct per-chat intervals', () => {
  const basket = new Basket();
  basket.add({ id: 'group:a' }, { start: '2026-10-01', end: '2026-10-05' });
  basket.add({ id: 'private:b' }, { start: '2026-10-10', end: '2026-10-12' });
  basket.add({ id: 'group:c' }, { start: '2026-10-01', end: '2026-10-05' });
  const selections = basket.selections(true);
  assert.equal(selections.length, 2);
  assert.deepEqual(selections[0].scopes, ['group:a', 'group:c']);
  assert.deepEqual(selections[1].scopes, ['private:b']);
  assert.notEqual(selections[0].start, selections[1].start);
  assert.equal(selections[0].include_attachments, true);
});

test('completion removes only the submitted snapshot and preserves any newer selections', () => {
  const basket = new Basket();
  basket.add({ id: 'unchanged' });
  basket.add({ id: 'edited' }, { start: '2026-10-01' });
  const snapshot = basket.snapshot();
  basket.add({ id: 'edited' }, { start: '2026-10-02' });
  basket.add({ id: 'new' });
  basket.complete(snapshot);
  assert.equal(basket.has('unchanged'), false);
  assert.equal(basket.get('edited').range.start, '2026-10-02');
  assert.equal(basket.has('new'), true);
});

test('large selections split into bounded requests without a 200-item selection cap', () => {
  const basket = new Basket();
  assert.throws(() => basket.selections(), /选择/);
  basket.addMany(Array.from({ length: 500 }, (_, i) => ({ item: { id: String(i) } })));
  basket.add({ id: '500' });
  assert.equal(basket.rows.size, 501);
  const batches = scopeBatches(basket.selections());
  assert.deepEqual(batches.map(batch => batch.reduce((n, selection) => n + selection.scopes.length, 0)), [200, 200, 101]);
  assert.equal(new Set(batches.flat().flatMap(selection => selection.scopes)).size, 501);
  basket.add({ id: '0' }, { start: '2026-10-01' });
  assert.equal(basket.rows.size, 501);
  const mixed = scopeBatches(basket.selections());
  assert.ok(mixed.every(batch => batch.reduce((n, selection) => n + selection.scopes.length, 0) <= 200));
  const plans = mixed.flat().map(selection => ({ selection }));
  assert.deepEqual(scopeBatches(plans, plan => plan.selection).map(batch => batch.map(plan => plan.selection)), mixed);
});

test('message subsets remain separate from whole conversations and carry only IDs and digests', () => {
  const basket = new Basket(), digest = 'a'.repeat(64);
  const selected = { 'message-one': digest };
  basket.add({ id: 'conversation-one', title: '会话一' }, {}, selected);
  selected['message-two'] = 'b'.repeat(64);
  basket.add({ id: 'conversation-two', title: '会话二' });
  const selection = basket.selections()[0];
  assert.deepEqual(selection.scopes, ['conversation-one', 'conversation-two']);
  assert.deepEqual(selection.messages, { 'conversation-one': { 'message-one': digest } });
  basket.add({ id: 'conversation-one' });
  assert.equal(basket.selections()[0].messages, undefined);
  assert.throws(() => basket.add({ id: 'empty' }, {}, {}), /选择消息/);
  assert.throws(() => basket.add({ id: 'invalid' }, {}, { message: 'not-a-digest' }), /重新读取/);
});

test('completion cannot clear a changed message subset', () => {
  const basket = new Basket();
  basket.add({ id: 'conversation-one' }, {}, { a: 'a'.repeat(64) });
  const snapshot = basket.snapshot();
  basket.add({ id: 'conversation-one' }, {}, { b: 'b'.repeat(64) });
  basket.complete(snapshot);
  assert.deepEqual(basket.get('conversation-one').messages, { b: 'b'.repeat(64) });
  basket.complete(basket.snapshot());
  assert.equal(basket.rows.size, 0);
});

test('the batch message limit applies across conversations, including UTF-8 metadata size', () => {
  const basket = new Basket(), digest = 'a'.repeat(64);
  basket.add({ id: 'one' }, {}, Object.fromEntries(Array.from({ length: 300 }, (_, i) => [String(i), digest])));
  basket.add({ id: 'two' }, {}, Object.fromEntries(Array.from({ length: 201 }, (_, i) => [String(i), digest])));
  const batches = scopeBatches(basket.selections());
  assert.equal(batches.length, 2);
  assert.deepEqual(batches.map(batch => batch.flatMap(selection => selection.scopes)), [['one'], ['two']]);
  // Different date ranges also share the same per-request message budget.
  basket.add({ id: 'two' }, { start: '2026-10-01' }, basket.get('two').messages);
  assert.equal(scopeBatches(basket.selections()).length, 2);
  basket.remove('two');
  basket.add({ id: 'large' }, {}, { ['字'.repeat(100000)]: digest });
  assert.throws(() => basket.selections(), /清单过大/);
});

test('opaque conversation IDs cannot mutate the selection-map prototype', () => {
  const basket = new Basket();
  basket.add({ id: '__proto__' }, {}, { constructor: 'a'.repeat(64) });
  const selected = basket.selections()[0].messages;
  assert.equal(Object.getPrototypeOf(selected), Object.prototype);
  assert.deepEqual(Object.keys(selected), ['__proto__']);
  assert.equal(JSON.parse(JSON.stringify(selected)).__proto__.constructor, 'a'.repeat(64));
  assert.equal({}.constructor, Object);
});
