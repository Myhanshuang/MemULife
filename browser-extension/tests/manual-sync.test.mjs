import 'fake-indexeddb/auto';
import test from 'node:test';
import assert from 'node:assert/strict';
import { Store } from '../dist/src/store.js';
import { Synchronizer } from '../dist/src/sync.js';
import { AdapterError } from '../dist/src/types.js';

const binding = { id: 'binding', provider: 'chatgpt', account_id: 'account', workspace_id: 'space', enabled: true,
  next_sync_at: 0, label: 'test', identity_strength: 'native', status: 'queued', last_error: null };
const summary = { id: 'conversation', locator: 'conversation', revision: null };
const selection = { job_id: 'selected-import', scopes: [summary.id] };
const pending = { id: 'batch', binding_id: binding.id, conversation_id: 'conversation-row', attempts: 5, order: 1,
  error: 'native_host_unavailable', not_before: Date.now() + 900_000,
  message: { jsonrpc: '2.0', id: 'batch', method: 'capture', params: {
    batch_id: 'batch', provider: 'chatgpt', account_id: 'account', workspace_id: 'space', import_job_id: selection.job_id,
    records: [{ object_id: summary.id, raw: 'AUTHORIZED CONTENT', text: 'AUTHORIZED CONTENT', metadata: {} }], cursor: {},
  } } };
const reply = (message, result) => ({ jsonrpc: '2.0', id: message.id, result });

async function setup() {
  const store = await Store.open('manual-sync-' + crypto.randomUUID());
  await store.bind(binding);
  return store;
}

test('manual sync retries enabled failures without changing scopes, payloads or paused bindings', async () => {
  const store = await setup();
  const later = Date.now() + 900_000;
  await store.updateBinding(binding.id, { import_selection: selection, next_sync_at: later,
    blocked_until: later, status: 'auth_required', last_error: 'auth_required' });
  const paused = { ...binding, id: 'paused', enabled: false, next_sync_at: later,
    blocked_until: later, status: 'paused', last_error: 'auth_required' };
  await store.bind(paused);
  const job = { id: 'detail', binding_id: binding.id, summary, import_job_id: selection.job_id,
    cursor: { opaque: 'keep-checkpoint' }, pages: [], attempts: 4, not_before: later, error: 'auth_required' };
  await store.put('jobs', job);
  await store.put('jobs', { ...job, id: 'paused-detail', binding_id: paused.id });
  await store.put('outbox', pending);
  const list = await store.get('lists', binding.id);
  await store.put('lists', { ...list, cursor: { tail: 'old-page' }, pages: 5 });
  await store.requestSync();
  assert.equal((await store.get('lists', binding.id)).cursor, null);
  assert.equal((await store.get('lists', binding.id)).pages, 0);
  const current = await store.get('bindings', binding.id);
  assert.equal(current.next_sync_at, 0);
  assert.equal(current.blocked_until, 0);
  assert.equal(current.status, 'queued');
  assert.deepEqual(current.import_selection, selection);
  assert.deepEqual([current.provider, current.account_id, current.workspace_id], ['chatgpt', 'account', 'space']);
  assert.deepEqual(await store.get('bindings', paused.id), paused);
  assert.deepEqual(await store.get('jobs', job.id), { ...job, not_before: 0 });
  assert.equal((await store.get('jobs', 'paused-detail')).not_before, later);
  assert.deepEqual(await store.get('outbox', pending.id), { ...pending, not_before: 0 });
  const delivered = [];
  await new Synchronizer(store, {}, async message => {
    delivered.push(message);
    return reply(message, { batch_id: message.params.batch_id, durable_level: 'edge_durable' });
  }).flush(current, Date.now() + 10_000);
  assert.deepEqual(delivered, [pending.message]);
  assert.equal((await store.all('outbox')).length, 0);
  store.db.close();
});

test('manual sync preserves Retry-After from binding and queued failures even after another status replaces it', async () => {
  const store = await setup();
  const later = Date.now() + 7_200_000;
  await store.updateBinding(binding.id, { next_sync_at: later, blocked_until: later,
    status: 'throttled', last_error: 'throttled' });
  await store.bind({ ...binding, id: 'permission-changed', next_sync_at: later, blocked_until: 0,
    throttled_until: later, status: 'permission_required', last_error: 'permission_required' });
  await store.bind({ ...binding, id: 'detail-throttled' });
  await store.put('jobs', { id: 'rate-limited-detail', binding_id: 'detail-throttled',
    summary, import_job_id: selection.job_id, pages: [], cursor: null, attempts: 1, not_before: later, error: 'throttled' });
  await store.bind({ ...binding, id: 'delivery-throttled' });
  const throttledDelivery = { ...pending, binding_id: 'delivery-throttled', error: 'throttled', not_before: later };
  await store.put('outbox', throttledDelivery);
  await store.requestSync();
  for (const current of await store.all('bindings')) {
    assert.equal(current.next_sync_at, 0);
    assert.equal(current.blocked_until, later);
    assert.equal(current.throttled_until, later);
    assert.equal(current.last_error, 'throttled');
    await new Synchronizer(store, { assertBinding: async () => assert.fail('provider contacted during Retry-After') },
      async () => assert.fail('native retry before its deadline')).run(current);
  }
  assert.equal((await store.get('jobs', 'rate-limited-detail')).not_before, later);
  assert.deepEqual(await store.get('outbox', pending.id), throttledDelivery);
  store.db.close();
});

test('a restored account can serve a pending preview and refresh the catalog immediately', async () => {
  const store = await setup();
  await store.updateBinding(binding.id, { next_sync_at: Date.now() + 900_000,
    blocked_until: Date.now() + 900_000, status: 'auth_required', last_error: 'auth_required' });
  let lists = 0, details = 0;
  const sent = [];
  const data = { mapping: { node: { message: { id: 'm1', author: { role: 'user' }, content: { parts: ['PREVIEW RAM ONLY'] } } } } };
  const sync = new Synchronizer(store, {
    assertBinding: async current => assert.equal(current.account_id, 'account'),
    list: async () => { lists++; return { items: [summary], cursor: null, warnings: [] }; },
    detail: async () => { details++; return { raw: JSON.stringify(data), data, cursor: null }; },
  }, async message => {
    sent.push(message);
    return reply(message, message.method === 'selection.get'
      ? { job_id: null, scopes: [], previews: message.params.claim_previews ? [{ request_id: 'preview', conversation: summary }] : [] }
      : { accepted: 1 });
  });
  await sync.run(await store.get('bindings', binding.id));
  assert.equal(sent.length, 0);
  await store.requestSync();
  await sync.run(await store.get('bindings', binding.id));
  assert.equal(lists, 1);
  assert.equal(details, 1);
  assert.equal(sent.find(message => message.method === 'preview.offer').params.messages[0].text, 'PREVIEW RAM ONLY');
  assert.ok(sent.some(message => message.method === 'catalog.offer'));
  for (const name of ['bindings', 'lists', 'jobs', 'conversations', 'outbox']) {
    assert.ok(!JSON.stringify(await store.all(name)).includes('PREVIEW RAM ONLY'));
  }
  store.db.close();
});

test('preview throttling stops the wake and remains effective across explicit manual retries', async () => {
  const store = await setup();
  const later = Date.now() + 7_200_000;
  let reads = 0, claims = 0;
  const offered = [];
  const sync = new Synchronizer(store, {
    assertBinding: async () => {},
    list: async () => assert.fail('list read after preview was throttled'),
    detail: async () => { reads++; throw new AdapterError('throttled', later); },
  }, async message => {
    if (message.method === 'selection.get') {
      if (message.params.claim_previews) claims++;
      return reply(message, { job_id: null, scopes: [], previews: [
        { request_id: 'first', conversation: summary }, { request_id: 'second', conversation: summary },
      ] });
    }
    assert.equal(message.method, 'preview.offer');
    offered.push(message.params);
    return reply(message, { accepted: 0 });
  });
  await sync.run(await store.get('bindings', binding.id));
  assert.equal(reads, 1);
  assert.equal(offered.length, 1);
  assert.equal(offered[0].error_code, 'throttled');
  assert.equal((await store.get('bindings', binding.id)).throttled_until, later);
  await store.updateBinding(binding.id, { status: 'permission_required', last_error: 'permission_required' });
  await store.requestSync();
  await sync.run(await store.get('bindings', binding.id));
  assert.equal(reads, 1);
  assert.equal(claims, 1);
  assert.equal((await store.get('bindings', binding.id)).blocked_until, later);
  store.db.close();
});

test('the options sync action queues behind the active lock and never enables paused or unauthorized accounts', async () => {
  const store = await setup();
  const later = Date.now() + 900_000;
  await store.updateBinding(binding.id, { blocked_until: later, next_sync_at: later, last_error: 'auth_required' });
  await store.bind({ ...binding, id: 'paused', enabled: false, blocked_until: later });
  await store.bind({ ...binding, id: 'no-permission', provider: 'claude', blocked_until: later });
  const originalOpen = Store.open, originalRun = Synchronizer.prototype.run, originalChrome = globalThis.chrome;
  const originalLocks = Object.getOwnPropertyDescriptor(navigator, 'locks');
  let listener, releaseLock, locks = 0;
  const runs = [];
  const noopEvent = { addListener() {} };
  globalThis.chrome = {
    storage: { local: { get: async () => ({}), setAccessLevel: async () => {} } },
    runtime: { id: 'fixture-extension', getURL: file => `chrome-extension://fixture-extension/${file}`,
      onInstalled: noopEvent, onStartup: noopEvent, onMessage: { addListener(value) { listener = value; } } },
    alarms: { get: async () => ({ periodInMinutes: 2.5 }), create: async () => {}, onAlarm: noopEvent },
    action: { onClicked: noopEvent },
    tabs: { query: async () => [], sendMessage: async () => {} },
    permissions: { contains: async ({ origins }) => origins[0] === 'https://chatgpt.com/*' },
  };
  Object.defineProperty(navigator, 'locks', { configurable: true, value: {
    request: (_name, options, callback) => {
      locks++;
      assert.equal(options.ifAvailable, undefined, 'explicit action must wait for the running worker');
      return new Promise((resolve, reject) => { releaseLock = async () => {
        try { await callback({ name: 'memulife-browser-sync' }); resolve(); } catch (error) { reject(error); throw error; }
      }; });
    },
  } });
  Store.open = () => originalOpen(store.db.name);
  Synchronizer.prototype.run = async function(current) { runs.push(current); };
  try {
    await import('../dist/src/background.js?manual-sync-test');
    const sender = { id: 'fixture-extension', url: 'chrome-extension://fixture-extension/options.html' };
    const send = (message, from = sender) => new Promise(resolve => listener(message, from, resolve));
    assert.equal((await send({ type: 'sync' })).ok, true);
    assert.equal((await send({ type: 'sync' })).ok, true);
    assert.equal(locks, 1, 'coalesce clicks already waiting for the same wake');
    assert.equal((await store.get('bindings', binding.id)).blocked_until, later);
    await releaseLock();
    assert.equal(runs.length, 1);
    assert.equal(runs[0].id, binding.id);
    assert.equal(runs[0].blocked_until, 0);
    assert.equal(runs[0].next_sync_at, 0);
    assert.equal((await store.get('bindings', 'paused')).enabled, false);
    assert.equal((await store.get('bindings', 'no-permission')).status, 'permission_required');
    assert.deepEqual(await send({ type: 'sync' }, { id: 'other-extension', url: sender.url }),
      { ok: false, error: 'scope_denied' });
    assert.equal(locks, 1);
  } finally {
    Store.open = originalOpen; Synchronizer.prototype.run = originalRun; globalThis.chrome = originalChrome;
    if (originalLocks) Object.defineProperty(navigator, 'locks', originalLocks); else delete navigator.locks;
    store.db.close();
  }
});

test('web source requests arriving behind a held lock are retained and leave other sources untouched', async t => {
  const store = await setup(), later = Date.now() + 900_000;
  await store.updateBinding(binding.id, { next_sync_at: later, blocked_until: later });
  for (const id of ['second', 'unrelated']) await store.bind({ ...binding, id, next_sync_at: later, blocked_until: later });
  const originalOpen = Store.open, originalRun = Synchronizer.prototype.run, originalChrome = globalThis.chrome;
  const originalLocks = Object.getOwnPropertyDescriptor(navigator, 'locks');
  const firstLock = Promise.withResolvers(), secondLock = Promise.withResolvers();
  const callbacks = [], runs = [], noopEvent = { addListener() {} };
  let listener;
  globalThis.chrome = {
    storage: { local: { get: async () => ({ 'memu-web-connections': Object.fromEntries([binding.id, 'second'].map(id => [id,
      { url: 'http://127.0.0.1:5073', source_id: id, token: 'SYNTHETIC' }])) }), setAccessLevel: async () => {} } },
    runtime: { id: 'fixture-extension', getURL: file => `chrome-extension://fixture-extension/${file}`,
      onInstalled: noopEvent, onStartup: noopEvent, onMessage: { addListener(value) { listener = value; } } },
    alarms: { get: async () => ({ periodInMinutes: 2.5 }), create: async () => {}, onAlarm: noopEvent },
    action: { onClicked: noopEvent }, tabs: { query: async () => [], sendMessage: async () => {} },
    permissions: { contains: async () => true },
  };
  Object.defineProperty(navigator, 'locks', { configurable: true, value: {
    request: (_name, _options, callback) => new Promise((resolve, reject) => {
      callbacks.push(async () => { try { await callback({}); resolve(); } catch (error) { reject(error); } });
      (callbacks.length === 1 ? firstLock : secondLock).resolve();
    }),
  } });
  Store.open = () => originalOpen(store.db.name);
  Synchronizer.prototype.run = async function(current) { runs.push(current.id); };
  t.after(() => {
    Store.open = originalOpen; Synchronizer.prototype.run = originalRun; globalThis.chrome = originalChrome;
    if (originalLocks) Object.defineProperty(navigator, 'locks', originalLocks); else delete navigator.locks;
    store.db.close();
  });
  await import('../dist/src/background.js?web-scoped-sync-test');
  const sender = { id: 'fixture-extension', frameId: 0, tab: { id: 1 }, url: 'http://127.0.0.1:5073/manage/imports/ai/chatgpt' };
  const send = id => new Promise(resolve => listener({ type: 'web-connector', action: 'sync', payload: { source_id: id } }, sender, resolve));
  assert.equal((await send(binding.id)).ok, true); await firstLock.promise;
  assert.equal((await send('second')).ok, true);
  await callbacks[0](); await secondLock.promise;
  await callbacks[1]();
  assert.deepEqual(runs, [binding.id, 'second']);
  assert.equal((await store.get('bindings', 'unrelated')).blocked_until, later);
  await new Promise(resolve => setImmediate(resolve));
});
