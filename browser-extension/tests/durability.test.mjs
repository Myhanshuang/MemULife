import 'fake-indexeddb/auto';
import test from 'node:test';
import assert from 'node:assert/strict';
import { Store } from '../dist/src/store.js';
import { Synchronizer } from '../dist/src/sync.js';
import { deliver, assertReceipt } from '../dist/src/native.js';
import { sha256 } from '../dist/src/wire.js';

const binding = { id: 'binding', provider: 'chatgpt', account_id: 'account', workspace_id: 'space', enabled: true,
  next_sync_at: 0, label: 'test', identity_strength: 'native', status: 'queued', last_error: null };
const summary = { id: 'conversation', locator: 'conversation', revision: '1' };
const selection = { job_id: 'selected-import', scopes: ['conversation'] };
const request = (text, id = crypto.randomUUID()) => ({ jsonrpc: '2.0', id, method: 'capture', params: {
  batch_id: id, provider: binding.provider, account_id: binding.account_id, workspace_id: binding.workspace_id,
  records: [{ object_id: summary.id, raw: text, text, metadata: {} }], cursor: {}, import_job_id: selection.job_id,
} });
const receipt = message => ({ jsonrpc: '2.0', id: message.id, result: { batch_id: message.params.batch_id, durable_level: 'edge_durable' } });

async function setup() {
  const store = await Store.open(`test-${crypto.randomUUID()}`);
  await store.bind(binding);
  await store.updateBinding(binding.id, { import_selection: selection });
  return store;
}

async function stage(store, text, id) {
  await store.dirty(binding, { ...summary, revision: text }, 0);
  const [job] = await store.all('jobs');
  const message = request(text, id);
  await store.stage(job, message, await sha256(text));
  return message;
}

test('list checkpoint and detail jobs survive an actual IndexedDB close/reopen', async () => {
  const store = await setup();
  await store.discover(binding, [summary], { cursor: { offset: 150 }, phase: 'scan', cycle: 1, pages: 3 });
  const name = store.db.name;
  store.db.close();
  const reopened = await Store.open(name);
  assert.equal((await reopened.get('lists', binding.id)).cursor.offset, 150);
  assert.equal((await reopened.all('jobs')).length, 1);
  assert.equal((await reopened.status())[0].scope_complete, false);
  reopened.db.close();
});

test('A→B→A stages three ordered observations; unchanged retry stages none', async () => {
  const store = await setup();
  await stage(store, 'A'); await stage(store, 'B'); await stage(store, 'A'); await stage(store, 'A');
  const entries = (await store.all('outbox')).sort((a, b) => a.order - b.order);
  assert.deepEqual(entries.map(value => value.message.params.records[0].text), ['A', 'B', 'A']);
  assert.deepEqual(entries.map(value => value.order), [1, 2, 3]);
  assert.equal((await store.all('jobs')).length, 0);
  store.db.close();
});

test('staging transaction failure cannot delete detail job or advance its observed fingerprint', async () => {
  const store = await setup();
  const original = await stage(store, 'A');
  await store.dirty(binding, { ...summary, revision: 'B' }, 0);
  const [job] = await store.all('jobs');
  await assert.rejects(store.stage(job, request('B', original.params.batch_id), await sha256('B')));
  assert.equal((await store.all('jobs')).length, 1);
  assert.equal((await store.all('outbox')).length, 1);
  assert.equal((await store.all('conversations'))[0].fingerprint, await sha256('A'));
  assert.equal((await store.get('lists', binding.id)).observation, 1);
  store.db.close();
});

test('late dirty hint during detail fetch is retained for a second snapshot', async () => {
  const store = await setup();
  await store.dirty(binding, summary, 0);
  const [job] = await store.all('jobs');
  await store.dirty(binding, { ...summary, revision: '2' }, 0);
  await store.progressJob(job, { pages: [], cursor: null });
  await store.stage(job, request('one'), await sha256('one'));
  const [rerun] = await store.all('jobs');
  assert.equal(rerun.summary.revision, '2');
  assert.equal(rerun.pages.length, 0);
  store.db.close();
});

test('lost ACK resends the same batch and blocks later observations until durable receipt', async () => {
  const store = await setup();
  const first = await stage(store, 'A'); await stage(store, 'B');
  let fail = true;
  const sent = [];
  const sync = new Synchronizer(store, {}, async message => {
    if (message.method === 'progress.offer') return { jsonrpc: '2.0', id: message.id, result: { recorded: true } };
    sent.push(message);
    if (fail) throw new Error('ACK lost');
    return receipt(message);
  });
  await sync.flush(binding, Date.now() + 10_000);
  assert.equal((await store.all('outbox')).length, 2);
  assert.equal(sent.length, 1);
  await sync.flush(binding, Date.now() + 10_000);
  assert.equal(sent.length, 1);
  const retry = (await store.all('outbox')).find(entry => entry.id === first.params.batch_id);
  await store.put('outbox', { ...retry, not_before: 0 });
  fail = false;
  await sync.flush(binding, Date.now() + 10_000);
  assert.deepEqual(sent[0], sent[1]);
  assert.deepEqual(sent.map(value => value.params.records[0].text), ['A', 'A', 'B']);
  assert.equal((await store.all('outbox')).length, 0);
  assert.equal((await store.all('receipts')).length, 2);
  store.db.close();
});

test('accepted, wrong batch, wrong ID and central-only receipts cannot release browser data', () => {
  const message = request('original');
  for (const reply of [
    { jsonrpc: '2.0', id: message.id, result: { batch_id: message.params.batch_id, status: 'accepted' } },
    { ...receipt(message), id: 'wrong' },
    { jsonrpc: '2.0', id: message.id, result: { batch_id: 'wrong', durable_level: 'edge_durable' } },
    { jsonrpc: '2.0', id: message.id, result: { batch_id: message.params.batch_id, durable_level: 'central_durable' } },
  ]) assert.throws(() => assertReceipt(message, reply), /receipt_mismatch/);
});

test('native chunks reconstruct exact Unicode payload and require a final durable ACK', async () => {
  const message = request('中文📎'.repeat(130_000));
  const chunks = [];
  let digest;
  const result = await deliver(message, async part => {
    assert.ok(Buffer.byteLength(JSON.stringify(part)) <= 1024 * 1024);
    if (part.method === 'capture.chunk') {
      chunks[part.params.index] = Buffer.from(part.params.data_base64, 'base64');
      digest = part.params.sha256;
      assert.ok(part.params.total <= 86);
      return { jsonrpc: '2.0', id: part.id, result: { transfer_id: part.params.transfer_id, index: part.params.index, stored: true } };
    }
    assert.equal(part.method, 'capture.commit');
    assert.equal(Buffer.concat(chunks).toString('utf8'), JSON.stringify(message));
    assert.equal(digest, await sha256(JSON.stringify(message)));
    return receipt(message);
  });
  assert.equal(result.durable_level, 'edge_durable');
  await assert.rejects(deliver(message, async part => part.method === 'capture.chunk'
    ? { jsonrpc: '2.0', id: part.id, result: { transfer_id: part.params.transfer_id, index: part.params.index, stored: true } }
    : { jsonrpc: '2.0', id: part.id, result: { status: 'accepted' } }), /receipt_mismatch/);
});

test('partial pagination and retry state survive worker restart without claiming completion', async () => {
  const store = await setup();
  await store.dirty(binding, summary, 0);
  const calls = [];
  let fail = false;
  const provider = {
    assertBinding: async () => {},
    detail: async (_binding, _summary, cursor) => {
      calls.push(cursor);
      if (fail) throw new Error('network interrupted');
      return { raw: '{"mapping":{}}', data: { mapping: {} }, cursor: { next: 'opaque' } };
    },
  };
  const sync = new Synchronizer(store, provider, async message => message.method === 'selection.get'
    ? { jsonrpc: '2.0', id: message.id, result: selection } : receipt(message));
  await sync.run({ ...binding, next_sync_at: Date.now() + 99_000 });
  assert.deepEqual((await store.all('jobs'))[0].cursor, { next: 'opaque' });
  assert.equal((await store.all('outbox')).length, 0);
  fail = true;
  await sync.run({ ...binding, next_sync_at: Date.now() + 99_000 });
  assert.deepEqual(calls, [null, { next: 'opaque' }]);
  assert.equal((await store.all('jobs'))[0].pages.length, 1);
  assert.equal((await store.status())[0].detail_gaps.length, 1);
  store.db.close();
});

test('unselected discovery and dirty hints never create durable conversation or detail rows', async () => {
  const store = await setup();
  await store.updateBinding(binding.id, { import_selection: { job_id: null, scopes: [] } });
  await store.discover(binding, [summary], { cursor: null, phase: 'head' });
  await store.dirty(binding, summary, 0);
  assert.equal((await store.all('conversations')).length, 0);
  assert.equal((await store.all('jobs')).length, 0);
  assert.equal((await store.all('outbox')).length, 0);
  store.db.close();
});

test('unselected titles are offered in memory but never enter any IndexedDB store', async () => {
  const store = await setup();
  const offered = [];
  const native = async message => {
    if (message.method === 'selection.get') return { jsonrpc: '2.0', id: message.id,
      result: { job_id: null, scopes: [] } };
    assert.equal(message.method, 'catalog.offer');
    offered.push(...message.params.items);
    return { jsonrpc: '2.0', id: message.id, result: { accepted: message.params.items.length } };
  };
  const provider = {
    assertBinding: async () => {},
    list: async () => ({ items: [{ ...summary, title: 'UNSELECTED PRIVATE TITLE',
      provider: 'claude', updated_at: '2026-10-02T01:00:00.000Z', message_count: 7,
      raw: 'UNSELECTED ORIGINAL BODY' }], cursor: null, warnings: [] }),
    detail: async () => { throw new Error('unselected details must never be requested'); },
  };
  await new Synchronizer(store, provider, native).run(binding);
  assert.equal(offered.length, 1);
  assert.equal(offered[0].title, 'UNSELECTED PRIVATE TITLE');
  assert.equal(offered[0].provider, binding.provider);
  assert.equal(offered[0].raw, undefined);
  for (const name of ['bindings', 'lists', 'jobs', 'conversations', 'outbox', 'receipts', 'attachments']) {
    assert.ok(!JSON.stringify(await store.all(name)).includes('UNSELECTED'));
  }
  store.db.close();
});

test('a new approved refresh runs during list cooldown without listing or reconnecting', async () => {
  const store = await setup();
  let detailReads = 0;
  let active = true;
  const approval = { job_id: 'next-refresh', scopes: [summary.id], items: [summary] };
  const sent = [];
  const provider = {
    assertBinding: async () => {},
    list: async () => { throw new Error('list cooldown must be preserved'); },
    detail: async () => {
      detailReads++;
      const data = { mapping: { q: { message: { id: 'q', author: { role: 'user' }, content: { parts: ['Approved refresh'] } } } } };
      return { raw: JSON.stringify(data), data, cursor: null };
    },
  };
  const sync = new Synchronizer(store, provider, async message => {
    if (message.method === 'selection.get') return { jsonrpc: '2.0', id: message.id,
      result: active ? approval : { job_id: null, scopes: [] } };
    sent.push(message);
    active = false;
    return receipt(message);
  });
  await sync.run({ ...binding, next_sync_at: Date.now() + 900_000 });
  assert.equal(detailReads, 1);
  assert.equal(sent.length, 1);
  assert.equal(sent[0].params.import_job_id, approval.job_id);
  assert.equal((await store.all('outbox')).length, 0);
  await sync.run({ ...binding, next_sync_at: Date.now() + 900_000 });
  assert.equal(detailReads, 1);
  store.db.close();
});

test('checking an absent approval during cooldown never contacts the provider', async () => {
  const store = await setup();
  let checks = 0;
  const sync = new Synchronizer(store, { assertBinding: async () => { throw new Error('unexpected upstream IO'); } },
    async message => {
      checks++;
      assert.equal(message.method, 'selection.get');
      return { jsonrpc: '2.0', id: message.id, result: { job_id: null, scopes: [] } };
    });
  await sync.run({ ...binding, next_sync_at: Date.now() + 900_000 });
  assert.equal(checks, 1);
  assert.equal((await store.get('bindings', binding.id)).last_error, null);
  store.db.close();
});

test('selection only schedules matching summaries and rejects late raw pages after deselection', async () => {
  const store = await setup();
  await store.discover(binding, [summary, { id: 'private', locator: 'private', revision: null }], { cursor: null });
  assert.deepEqual((await store.all('jobs')).map(job => job.summary.id), ['conversation']);
  assert.deepEqual((await store.all('conversations')).map(row => row.summary.id), ['conversation']);
  const [job] = await store.all('jobs');
  await store.updateBinding(binding.id, { import_selection: { job_id: null, scopes: [] } });
  await store.progressJob(job, { pages: [{ raw: 'SHOULD NOT PERSIST' }] });
  assert.deepEqual((await store.all('jobs'))[0].pages, []);
  assert.equal(await store.stage(job, request('SHOULD NOT PERSIST'), 'digest'), false);
  assert.equal((await store.all('outbox')).length, 0);
  store.db.close();
});

test('revocation during network read discards returned original instead of checkpointing it', async () => {
  const store = await setup();
  await store.dirty(binding, summary, 0);
  let active = true;
  let reads = 0;
  const provider = {
    assertBinding: async () => {},
    detail: async () => {
      reads++;
      active = false;
      return { raw: 'DO NOT SAVE', data: {}, cursor: { next: 'p2' } };
    },
  };
  const sync = new Synchronizer(store, provider, async message => ({ jsonrpc: '2.0', id: message.id,
    result: active ? selection : { job_id: null, scopes: [] } }));
  await sync.run({ ...binding, next_sync_at: Date.now() + 99_000 });
  assert.equal(reads, 1);
  assert.deepEqual((await store.all('jobs'))[0].pages, []);
  assert.equal((await store.all('outbox')).length, 0);
  store.db.close();
});

test('legacy outbox without an import decision is retained but never automatically delivered', async () => {
  const store = await setup();
  await stage(store, 'legacy');
  const [pending] = await store.all('outbox');
  delete pending.message.params.import_job_id;
  await store.put('outbox', pending);
  const sync = new Synchronizer(store, {}, async () => { throw new Error('legacy payload forwarded'); });
  await sync.flush(binding, Date.now() + 10_000);
  assert.equal((await store.all('outbox'))[0].attempts, 0);
  store.db.close();
});
