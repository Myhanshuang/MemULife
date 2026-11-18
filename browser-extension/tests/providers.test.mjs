import test from 'node:test';
import assert from 'node:assert/strict';
import { Provider, parseRpc, project } from '../dist/src/providers.js';
import { ReadClient } from '../dist/src/http.js';
import { parseJson, retryAt, retryAfter } from '../dist/src/wire.js';
import { AdapterError } from '../dist/src/types.js';
import { browserBroker, chatgptFetch, deepseekFetch } from '../dist/src/broker.js';

const binding = (provider, account = 'account', workspace = 'workspace') => ({
  id: provider, provider, account_id: account, workspace_id: workspace, enabled: true,
  next_sync_at: 0, label: 'test', identity_strength: 'native', status: 'queued', last_error: null,
});
const item = { id: 'conversation', locator: 'conversation', revision: null };
const broker = { cookie: async () => 'session-only-secret', deepseekToken: async () => 'deepseek-only-secret' };
const response = data => new Response(typeof data === 'string' ? data : JSON.stringify(data), { headers: { 'Content-Type': 'application/json' } });
const page = data => ({ data, raw: JSON.stringify(data), cursor: null });

test('unsafe long IDs and fractional opaque cursors preserve exact tokens', () => {
  const parsed = parseJson('{"id":9223372036854775807,"cursor":1792601234.123456789,"small":2}');
  assert.equal(parsed.id, '9223372036854775807');
  assert.equal(parsed.cursor, '1792601234.123456789');
  assert.equal(parsed.small, 2);
});

test('list metadata exposes native titles and dates without requesting conversation details', async () => {
  const fixtures = [
    ['chatgpt', { items: [{ id: 'one', title: '<Project> 设计', create_time: '2026-10-01T01:00:00Z', update_time: '2026-10-02T01:00:00Z', message_count: 4 }] }, '<Project> 设计', '2026-10-02T01:00:00.000Z', 4],
    ['claude', [{ uuid: 'one', name: '方案评审', created_at: '2026-10-01T01:00:00+08:00', updated_at: '2026-10-02T01:00:00Z' }], '方案评审', '2026-10-02T01:00:00.000Z', null],
    ['perplexity', [{ context_uuid: 'one', slug: 'one-slug', title: '原始标题', last_query_datetime: '2026-10-02T01:00:00Z' }], '原始标题', '2026-10-02T01:00:00.000Z', null],
    ['deepseek', { data: { biz_data: { chat_sessions: [{ id: 'one', title: '代码会话', created_at: 1790816400, updated_at: 1790902800 }], has_more: false } } }, '代码会话', '2026-10-02T01:00:00.000Z', null],
    ['grok', { conversations: [{ conversationId: 'one', title: '会话标题', createTime: '2026-10-01T01:00:00Z', modifyTime: '2026-10-02T01:00:00Z' }] }, '会话标题', '2026-10-02T01:00:00.000Z', null],
  ];
  for (const [id, data, title, updated, count] of fixtures) {
    const requests = [];
    const provider = new Provider(id, new ReadClient(id, async url => {
      requests.push(String(url));
      return response(data);
    }, 0), broker);
    const value = await provider.list(binding(id), null);
    assert.equal(requests.length, 1);
    assert.equal(value.items[0].title, title);
    assert.equal(value.items[0].updated_at, updated);
    assert.equal(value.items[0].message_count, count);
    assert.equal(value.items[0].provider, id);
    assert.equal(value.items[0].raw, undefined);
  }
});

test('Gemini list extracts only documented title and last-update positions', async () => {
  const provider = new Provider('gemini', new ReadClient('gemini', async url => String(url).endsWith('/app')
    ? response('<script>"SNlM0e":"nonce","FdrFJe":"sid","cfb2h":"build"</script>')
    : response(JSON.stringify([['wrb.fr', 'MaZiqc', JSON.stringify([null, null,
      [['c_one', 'Gemini 标题', null, null, null, [1790902800, 0]]]])]])), 0), broker);
  await provider.accounts();
  const value = (await provider.list(binding('gemini'), null)).items[0];
  assert.equal(value.title, 'Gemini 标题');
  assert.equal(value.updated_at, '2026-10-02T01:00:00.000Z');
  assert.equal(value.created_at, null);
  assert.equal(value.message_count, null);
});

test('missing or malformed optional list metadata stays unknown', async () => {
  const provider = new Provider('chatgpt', new ReadClient('chatgpt', async () => response({ items: [
    { id: 'missing' },
    { id: 'invalid', title: { unexpected: 'body' }, update_time: '2026-02-30T01:00:00Z', create_time: '2026-10-01T01:00:00', message_count: true },
    { id: 'oversize', title: 'x'.repeat(513), update_time: 'not a date', message_count: -1 },
    { id: 'malformed', title: 'broken\ud800unicode', update_time: '2026-10-01T24:00:00Z', create_time: '2026-10-01T01:00:00+00:60' },
  ] }), 0), broker);
  const values = (await provider.list(binding('chatgpt'), null)).items;
  for (const value of values) {
    assert.equal(value.title, null);
    assert.equal(value.created_at, null);
    assert.equal(value.updated_at, null);
    assert.equal(value.message_count, null);
  }
});

test('ChatGPT preserves all DAG branches and distinct repeated text', () => {
  const data = { current_node: 'b', mapping: {
    root: { id: 'root', parent: null, children: ['a', 'b'], message: null },
    a: { id: 'a', parent: 'root', children: [], message: { id: 'message-a', content: { parts: ['same', { asset_pointer: 'file-service://asset' }] } } },
    b: { id: 'b', parent: 'root', children: [], message: { id: 'message-b', content: { parts: ['same'] } } },
  } };
  const record = project('chatgpt', item, [page(data)]);
  assert.equal(record.text, 'same\n\nsame');
  assert.equal(record.metadata.branches, 'raw_dag');
  assert.equal(record.metadata.attachments, 'metadata_only');
  assert.deepEqual(JSON.parse(JSON.parse(record.raw).responses[0]), data);
  assert.equal(record.metadata.actor.role, 'unknown');
  assert.equal(record.occurred_at, undefined);
});

test('Claude retains tool-only messages, opaque blocks, parent UUIDs and missing time', () => {
  const data = { chat_messages: [{ uuid: 'tool-only', parent_uuid: 'parent', sender: 'assistant',
    content: [{ type: 'tool_use', id: 'call', input: { path: 'a' } }, { type: 'redacted_thinking', data: 'opaque' }] }] };
  const record = project('claude', item, [page(data)]);
  assert.equal(record.text, '');
  assert.match(record.raw, /redacted_thinking/);
  assert.equal(record.metadata.branches, 'unknown');
  assert.equal(record.occurred_at, undefined);
});

test('Gemini reads all matching RPC frames and preserves missing message times', () => {
  const turn = ['turn-1', null, [['question']], [[['candidate-a', ['answer-a']], ['candidate-b', ['answer-b']]]]];
  const frames = [[[turn], null], [[], null]];
  const raw = ")]}'\n\n123\n" + JSON.stringify(frames.map(frame => ['wrb.fr', 'hNvQHb', JSON.stringify(frame)])) + '\n';
  assert.deepEqual(parseRpc(raw, 'hNvQHb'), frames);
  const record = project('gemini', item, [{ data: { frames }, raw, cursor: null }]);
  assert.equal(record.text, 'question\n\nanswer-a\n\nanswer-b');
  assert.equal(record.metadata.time_basis, 'unknown');
  assert.throws(() => parseRpc('[["wrb.fr","other","[]"]]', 'hNvQHb'), /incompatible_schema/);
});

test('Gemini malformed list arrays fail instead of returning an empty account', async () => {
  const client = new ReadClient('gemini', async url => String(url).endsWith('/app')
    ? response('<script>"SNlM0e":"nonce","FdrFJe":"sid","cfb2h":"build"</script>')
    : response(JSON.stringify([['wrb.fr', 'MaZiqc', JSON.stringify([null, null, null])]])), 0);
  const provider = new Provider('gemini', client, broker);
  await provider.accounts();
  await assert.rejects(provider.list(binding('gemini'), null), /incompatible_schema/);
});

test('Gemini account-index paths stay scoped through probe, binding and RPC', async () => {
  const requests = [];
  const client = new ReadClient('gemini', async url => {
    requests.push(new URL(url));
    if (String(url).endsWith('/app')) return response('<script>"SNlM0e":"nonce","FdrFJe":"sid","cfb2h":"build"</script>');
    return response(")]}'\n" + JSON.stringify([['wrb.fr', 'MaZiqc', JSON.stringify([null, null, []])]]));
  }, 0);
  const provider = new Provider('gemini', client, { ...broker, geminiPaths: async () => ['/u/1/app'] });
  const [account] = await provider.accounts();
  assert.equal(account.workspace_id, 'u/1');
  assert.equal(account.identity_strength, 'session_bound');
  assert.equal(account.requires_open_tab_for_auth, true);
  assert.equal(requests[0].pathname, '/u/1/app');
  await provider.assertBinding(account);
  await provider.list(binding('gemini', account.account_id, account.workspace_id), null);
  assert.equal(requests.at(-1).pathname, '/u/1/_/BardChatUi/data/batchexecute');
  assert.equal(requests.at(-1).searchParams.get('source-path'), '/u/1/app');
});

test('Gemini page broker targets home and conversation tabs without crossing accounts', async () => {
  const previous = { chrome: globalThis.chrome, location: globalThis.location, document: globalThis.document };
  let injection;
  let pathname = '/u/1/app/conversation-id';
  globalThis.document = { documentElement: { outerHTML: 'gemini-html' } };
  globalThis.chrome = {
    tabs: { query: async () => [{ id: 16, url: 'https://gemini.google.com/u/2/app/other' },
      { id: 17, url: `https://gemini.google.com${pathname}?pageId=none` }] },
    scripting: { executeScript: async options => {
      injection = options;
      return [{ result: await options.func(...options.args) }];
    } },
  };
  try {
    assert.deepEqual(await browserBroker.geminiPaths(), ['/u/1/app', '/u/2/app']);
    for (pathname of ['/u/1/app/conversation-id', '/u/1/app']) {
      globalThis.location = { origin: 'https://gemini.google.com', pathname };
      assert.equal(await browserBroker.geminiRequest('/u/1/app', 'GET'), 'gemini-html');
    }
    assert.equal(injection.target.tabId, 17);
    assert.equal(injection.world, 'MAIN');
    globalThis.location.pathname = '/u/2/app/other';
    await assert.rejects(browserBroker.geminiRequest('/u/1/app', 'GET'), /account_changed/);
    await assert.rejects(browserBroker.geminiRequest('/u/0/app', 'GET'), /dormant_browser/);
    await assert.rejects(browserBroker.geminiRequest('https://other.example/app', 'GET'), /scope_denied/);
  } finally { Object.assign(globalThis, previous); }
});

test('DeepSeek full fetch rejects empty MERGE; FILE fragments remain in raw', async () => {
  let mode = 'MERGE';
  const client = new ReadClient('deepseek', async () => response({ code: 0, data: { biz_code: 0, biz_data: {
    cache_control: mode, chat_messages: [{ id: '1', fragments: [{ type: 'FILE', file: { name: 'a.txt' } }, { type: 'REQUEST', content: 'query' }] }],
  } } }), 0);
  const provider = new Provider('deepseek', client, broker);
  await assert.rejects(provider.detail(binding('deepseek'), item, null), /full_snapshot_required/);
  mode = 'REPLACE';
  const result = await provider.detail(binding('deepseek'), item, null);
  const record = project('deepseek', item, [result]);
  assert.equal(record.text, 'query');
  assert.match(record.raw, /FILE/);
});

test('DeepSeek opaque fractional cursor and equal-time ID tie-break are sent exactly', async () => {
  let requested;
  const provider = new Provider('deepseek', new ReadClient('deepseek', async url => {
    requested = new URL(url);
    return response('{"data":{"biz_data":{"chat_sessions":[{"id":9223372036854775807,"updated_at":1792601234.123456789}],"has_more":true}}}');
  }, 0), broker);
  const first = await provider.list(binding('deepseek'), null);
  await provider.list(binding('deepseek'), first.cursor);
  assert.equal(requested.searchParams.get('lte_cursor.updated_at'), '1792601234.123456789');
  assert.equal(requested.searchParams.get('lte_cursor.id'), '9223372036854775807');
  assert.deepEqual(first.warnings, ['pinned_scope_unverified']);
});

test('Perplexity details page past 100 entries without losing UUID or citations', async () => {
  const requested = [];
  const provider = new Provider('perplexity', new ReadClient('perplexity', async url => {
    const offset = Number(new URL(url).searchParams.get('offset'));
    requested.push(offset);
    return response({ entries: Array.from({ length: offset === 0 ? 100 : 5 }, (_, index) => ({
      query_str: `q${offset + index}`, blocks: [{ markdown_block: { answer: `a${offset + index}`, citations: [{ url: 'https://example.test' }] } }],
    })) });
  }, 0), broker);
  const summary = { id: 'stable-uuid', locator: 'changed-slug', revision: null };
  const first = await provider.detail(binding('perplexity'), summary, null);
  const second = await provider.detail(binding('perplexity'), summary, first.cursor);
  assert.deepEqual(requested, [0, 100]);
  assert.equal(second.cursor, null);
  const record = project('perplexity', summary, [first, second]);
  assert.equal(record.object_id, 'stable-uuid');
  assert.match(record.text, /q104/);
  assert.match(record.raw, /citations/);
  assert.match(record.text, /a104\n\n网站来源\n\[1\] https:\/\/example\.test/);
});

test('Grok discovers a bounded list and requires every requested response ID', async () => {
  let missing = true;
  const provider = new Provider('grok', new ReadClient('grok', async (url, options) => {
    const path = new URL(url).pathname;
    if (path.endsWith('/conversations')) return response({ conversations: [{ conversationId: 'old', modifyTime: null }] });
    if (path.endsWith('/response-node')) return response({ responseNodes: [
      { responseId: 'a', parentResponseId: null }, { responseId: 'b', parentResponseId: 'a' }, { responseId: 'c', parentResponseId: 'a' },
    ] });
    assert.deepEqual(JSON.parse(options.body).responseIds, ['a', 'b', 'c']);
    return response({ responses: (missing ? ['a', 'b'] : ['a', 'b', 'c']).map(id => ({ responseId: id, message: id })) });
  }, 0), broker);
  assert.ok((await provider.list(binding('grok'), null)).warnings.includes('discovery_bounded_60'));
  const nodes = await provider.detail(binding('grok'), item, null);
  await assert.rejects(provider.detail(binding('grok'), item, nodes.cursor), /missing_response_nodes/);
  missing = false;
  const responses = await provider.detail(binding('grok'), item, nodes.cursor);
  assert.equal(project('grok', item, [nodes, responses]).text, 'a\n\nb\n\nc');
});

test('account changes refuse to capture into a previous binding', async () => {
  let secret = 'session-a';
  const provider = new Provider('grok', new ReadClient('grok', async () => response({}), 0), { ...broker, cookie: async () => secret });
  const [account] = await provider.accounts();
  secret = 'session-b';
  await assert.rejects(provider.assertBinding(account), /account_changed/);
  assert.equal(account.identity_strength, 'session_bound');
  assert.ok(!JSON.stringify(account).includes('session-a'));
});

test('ChatGPT workspaces use the same native account and distinct workspace IDs', async () => {
  const provider = new Provider('chatgpt', new ReadClient('chatgpt', async url => {
    if (String(url).includes('/auth/session')) return response({ user: { id: 'native-user' }, accessToken: 'private-token' });
    return response({ accounts: { a: { account: { account_id: 'workspace-a', name: 'same' } }, b: { account: { account_id: 'workspace-b', name: 'same' } } } });
  }, 0), broker);
  const accounts = await provider.accounts();
  assert.deepEqual(accounts.map(value => value.workspace_id), ['workspace-a', 'workspace-b']);
  assert.equal(accounts[0].account_id, 'native-user');
  assert.ok(!JSON.stringify(accounts).includes('private-token'));
});

test('ChatGPT marks only a uniquely verified recent-conversation workspace', async () => {
  const provider = new Provider('chatgpt', new ReadClient('chatgpt', async url =>
    String(url).includes('/auth/session')
      ? response({ user: { id: 'native-user' }, accessToken: 'private-token' })
      : response({ accounts: { a: { account: { account_id: 'workspace-a', name: 'prolite' } },
        b: { account: { account_id: 'workspace-b', name: 'prolite' } } } }), 0),
  { ...broker, chatgptActiveWorkspace: async (_token, ids) => {
    assert.deepEqual(ids, ['workspace-a', 'workspace-b']);
    return 'workspace-b';
  } });
  const accounts = await provider.accounts();
  assert.deepEqual(accounts.map(account => account.active_in_recent_tab), [false, true]);
});

test('ChatGPT removes duplicate entries for the same workspace before matching', async () => {
  const provider = new Provider('chatgpt', new ReadClient('chatgpt', async url =>
    String(url).includes('/auth/session')
      ? response({ user: { id: 'native-user' }, accessToken: 'private-token' })
      : response({ accounts: { a: { account: { account_id: 'workspace-a', name: 'prolite' } },
        b: { account: { account_id: 'workspace-a', name: 'prolite' } } } }), 0),
  { ...broker, chatgptActiveWorkspace: async (_token, ids) => {
    assert.deepEqual(ids, ['workspace-a']);
    return 'workspace-a';
  } });
  const accounts = await provider.accounts();
  assert.equal(accounts.length, 1);
  assert.equal(accounts[0].active_in_recent_tab, true);
});

test('ChatGPT page broker reads only pinned endpoints in the signed-in tab', async () => {
  const previous = globalThis.chrome;
  const requests = [];
  globalThis.chrome = {
    tabs: { query: async () => [{ id: 9, url: 'https://chatgpt.com/c/conversation' }] },
    scripting: { executeScript: async options => {
      requests.push(options);
      const path = options.args[0].path;
      const raw = path === '/api/auth/session'
        ? JSON.stringify({ user: { id: 'native-user' }, accessToken: 'private-token' })
        : JSON.stringify({ accounts: { a: { account: { account_id: 'workspace-a', name: 'workspace' } } } });
      return [{ result: { status: 200, raw } }];
    } },
  };
  try {
    const provider = new Provider('chatgpt', new ReadClient('chatgpt', chatgptFetch, 0), broker);
    assert.equal((await provider.accounts())[0].workspace_id, 'workspace-a');
    assert.equal(requests[0].target.tabId, 9);
    assert.equal(requests[0].world, 'MAIN');
    assert.equal(requests[1].args[0].headers.authorization, 'Bearer private-token');
    await assert.rejects(chatgptFetch('https://other.example/api/auth/session'), /scope_denied/);
    await assert.rejects(chatgptFetch('https://chatgpt.com/api/payments'), /scope_denied/);
  } finally { globalThis.chrome = previous; }
});

test('DeepSeek page broker reads only pinned endpoints in the signed-in tab', async () => {
  const previous = globalThis.chrome;
  const requests = [];
  globalThis.chrome = {
    tabs: { query: async () => [{ id: 12, url: 'https://chat.deepseek.com/a/chat' }] },
    scripting: { executeScript: async options => {
      requests.push(options);
      return [{ result: { status: 200, raw: '{"code":0,"data":{"biz_code":0,"biz_data":{"user":{"id":"123"}}}}' } }];
    } },
  };
  try {
    const result = await new ReadClient('deepseek', deepseekFetch, 0).request('/api/v0/users/current', {
      headers: { Authorization: 'Bearer private-token' },
    });
    assert.equal(result.data.data.biz_data.user.id, '123');
    assert.equal(requests[0].target.tabId, 12);
    assert.equal(requests[0].world, 'MAIN');
    assert.equal(requests[0].args[0].headers.authorization, 'Bearer private-token');
    await assert.rejects(deepseekFetch('https://other.example/api/v0/users/current'), /scope_denied/);
    await assert.rejects(deepseekFetch('https://chat.deepseek.com/api/payments'), /scope_denied/);
  } finally { globalThis.chrome = previous; }
});

test('read-only allowlist denies mutations, redirects, unrelated origins and HTML challenges', async () => {
  const client = new ReadClient('chatgpt', async () => response('<html>challenge</html>'), 0);
  await assert.rejects(client.request('https://example.test/backend-api/conversations'), /scope_denied/);
  await assert.rejects(client.request('/backend-api/conversation/id', { method: 'DELETE' }), /scope_denied/);
  await assert.rejects(client.request('/api/payments'), /scope_denied/);
  await assert.rejects(client.request('/backend-api/conversations'), /auth_or_challenge/);
});

test('throttle Retry-After is never shortened to local retry cap', async () => {
  const now = Date.now();
  const client = new ReadClient('grok', async () => new Response('', { status: 429, headers: { 'Retry-After': '7200' } }), 0);
  let error;
  try { await client.request('/rest/app-chat/conversations'); } catch (caught) { error = caught; }
  assert.ok(error instanceof AdapterError);
  assert.ok(retryAt(error, 1, now) >= now + 7_200_000);
  assert.equal(retryAfter('Wed, 21 Oct 2037 07:28:00 GMT', now), Date.parse('Wed, 21 Oct 2037 07:28:00 GMT'));
});
