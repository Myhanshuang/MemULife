import assert from 'node:assert/strict';
import { test } from 'node:test';
import { localServiceURL, pairConnection, connectionStatus, sourceSend } from '../dist/src/connection.js';
import { bindingId } from '../dist/src/wire.js';

test('web transport accepts only explicit loopback origins without embedded credentials or paths', () => {
  assert.equal(localServiceURL('http://127.0.0.1:5073/'), 'http://127.0.0.1:5073');
  for (const value of ['https://example.com:5073', 'http://127.0.0.1:5073/path', 'http://a:b@localhost:5073',
    'http://localhost', 'http://127.0.0.1:5073?token=secret']) {
    assert.throws(() => localServiceURL(value));
  }
});

test('paired transport hides keys from UI state and never falls back or retries an uncertain write', async t => {
  const previous = { chrome: globalThis.chrome, fetch: globalThis.fetch };
  t.after(() => Object.assign(globalThis, previous));
  const stored = {}, calls = [];
  let nativeCalls = 0;
  globalThis.chrome = { storage: { local: { get: async () => stored, set: async value => Object.assign(stored, value) } },
    runtime: { sendNativeMessage() { nativeCalls++; } } };
  const account = { provider: 'chatgpt', account_id: 'account', workspace_id: 'workspace' };
  const binding = { ...account, id: bindingId(account.provider, account.account_id, account.workspace_id) };
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options });
    return Response.json({ ...account, token: 'SYNTHETIC_SOURCE_ONLY_TOKEN', source_id: 'source' });
  };
  await pairConnection(binding, 'http://127.0.0.1:5073', 'SYNTHETIC_PAIR_CODE');
  assert.equal(calls[0].options.credentials, 'omit');
  assert.equal(calls[0].options.redirect, 'error');
  assert.equal(calls[0].options.headers.Authorization, undefined);
  assert.deepEqual(await connectionStatus(), { [binding.id]: { url: 'http://127.0.0.1:5073', source_id: 'source' } });
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options });
    throw new Error('lost response after possible commit');
  };
  const request = { jsonrpc: '2.0', id: 'one', method: 'capture', params: account };
  await assert.rejects(sourceSend(request), /web_service_unavailable/);
  assert.equal(nativeCalls, 0);
  assert.equal(calls.length, 2);
  assert.equal(calls[1].options.headers.Authorization, 'Bearer SYNTHETIC_SOURCE_ONLY_TOKEN');
  assert.deepEqual(JSON.parse(calls[1].options.body), request);
});

test('pairing refuses a response for a different account before storing a key', async t => {
  const previous = { chrome: globalThis.chrome, fetch: globalThis.fetch };
  t.after(() => Object.assign(globalThis, previous));
  let saved = false;
  globalThis.chrome = { storage: { local: { get: async () => ({}), set: async () => { saved = true; } } } };
  globalThis.fetch = async () => Response.json({ provider: 'chatgpt', account_id: 'other', workspace_id: 'workspace', token: 'fixture', source_id: 'source' });
  await assert.rejects(pairConnection({ id: 'binding', provider: 'chatgpt', account_id: 'account', workspace_id: 'workspace' },
    'http://localhost:5073', 'fixture'), /source_binding_mismatch/);
  assert.equal(saved, false);
});
