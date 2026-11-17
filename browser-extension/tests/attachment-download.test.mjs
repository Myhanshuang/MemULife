import test from 'node:test';
import assert from 'node:assert/strict';
import { browserBroker } from '../dist/src/broker.js';

test('ChatGPT resolves a conversation attachment at the native route and reports bounded failures', async t => {
  t.mock.method(globalThis, 'fetch');
  const oldChrome = globalThis.chrome, oldLocation = globalThis.location;
  t.after(() => { globalThis.chrome = oldChrome; globalThis.location = oldLocation; });
  globalThis.location = { origin: 'https://chatgpt.com' };
  globalThis.chrome = { tabs: { query: async () => [{ id: 7 }] }, scripting: {
    executeScript: async ({ func, args }) => [{ result: await func(...args) }],
  } };
  const requests = [];
  globalThis.fetch.mock.mockImplementation(async (url, init) => {
    requests.push({ url, init });
    return requests.length === 1
      ? Response.json({ download_url: 'https://files.oaiusercontent.com/original' })
      : new Response(new Uint8Array([0, 128, 255]), { headers: { 'content-type': 'image/jpeg' } });
  });
  const result = await browserBroker.chatgptAttachment('local-test-token', 'workspace', 'file_001', 100, 'conversation');
  assert.equal(requests[0].url, '/backend-api/files/download/file_001?conversation_id=conversation');
  assert.equal(requests[0].init.headers.Authorization, 'Bearer local-test-token');
  assert.equal(requests[1].init.credentials, 'omit');
  assert.equal(requests[1].init.headers, undefined);
  assert.deepEqual([...Buffer.from(result.data, 'base64')], [0, 128, 255]);
  globalThis.fetch.mock.mockImplementation(async () => new Response('', { status: 403 }));
  await assert.rejects(browserBroker.chatgptAttachment('local-test-token', 'workspace', 'file_001', 100, 'conversation'),
    error => error.code === 'attachment_access_denied' && error.diagnostics.stage === 'resolve_url'
      && error.diagnostics.http_status === 403);
});
