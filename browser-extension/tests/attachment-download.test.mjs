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

test('Claude and Gemini attachment references deliver exact bytes through the capture path', async t => {
  const { attachOriginals } = await import('../dist/src/attachments.js');
  const { Provider } = await import('../dist/src/providers.js');
  const previous = {chrome: globalThis.chrome, location: globalThis.location};
  t.after(() => Object.assign(globalThis, previous));
  const bytes = new Uint8Array([0, 17, 128, 255]);
  const fetch = t.mock.method(globalThis, 'fetch', async () => new Response(bytes));
  globalThis.chrome = { tabs: {query: async () => [{id: 1, url: globalThis.location.href}]},
    scripting: {executeScript: async ({func,args}) => [{result: await func(...args)}]} };
  const google = 'https://lh3.googleusercontent.com/opaque';
  const claude = '/api/org/files/abc/document_pdf';
  for (const [id,workspace,location,data,url] of [
    ['claude','org','https://claude.ai/chat/sample',{chat_messages:[{uuid:'msg',files_v2:[{file_uuid:'abc',
      file_name:'report.pdf',document_asset:{file_variant:'original',url:claude}}]}]},'https://claude.ai'+claude],
    ['claude','org','https://claude.ai/chat/sample',{chat_messages:[{uuid:'reply',sender:'assistant',
      files:[{file_uuid:'result',file_name:'report.md'}],
      content:[{type:'tool_use',name:'create_file',input:{path:'/mnt/user-data/outputs/drafts/report.md'}}]}]},
      'https://claude.ai/api/organizations/org/conversations/sample/wiggle/download-file?path=%2Fmnt%2Fuser-data%2Foutputs%2Fdrafts%2Freport.md'],
    ['gemini','u/1','https://gemini.google.com/u/1/app/sample',{frames:[[[[['cid','rid'],null,
      [['question',0,null,[[[google],'sample.bin']]]]]]]]},google],
  ]) {
    globalThis.location = new URL(location);
    const provider = new Provider(id, null, browserBroker);
    const record = {raw:'{}',metadata:{}};
    await attachOriginals(id, record, [{data}], true,
      (ref,max) => provider.attachment({workspace_id:workspace},ref,max,'sample'), async () => true);
    assert.equal(fetch.mock.calls.at(-1).arguments[0],url);
    assert.equal(record.attachments.length,1);
    assert.equal(record.attachments[0].status,'available');
    assert.deepEqual(Buffer.from(record.attachments[0].data,'base64'),Buffer.from(bytes));
    assert.match(JSON.parse(record.raw).attachment_manifest[0].sha256,/^[0-9a-f]{64}$/);
  }
});
