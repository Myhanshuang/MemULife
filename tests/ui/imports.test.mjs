/** Browser UI fixtures only. Real import admission is covered by Python integration tests.
 * Run: node --test --test-concurrency=1 tests/ui/imports.test.mjs
 * Optional overrides: MEMULIFE_PLAYWRIGHT_MODULE, MEMULIFE_CHROMIUM, MEMULIFE_UI_REPORT_DIR.
 */
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';
import { createServer } from 'node:http';
import { createRequire } from 'node:module';
import { readFile, mkdir } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { dirname, extname, join, relative, resolve } from 'node:path';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const ui = join(root, 'memu/acquisition/ui');
const fixtureKey = 'UI-FIXTURE-ONLY-NOT-A-REAL-MANAGEMENT-KEY';
const providerNames = { chatgpt: 'ChatGPT', claude: 'Claude', gemini: 'Gemini', perplexity: 'Perplexity', deepseek: 'DeepSeek', grok: 'Grok' };
const identity = number => `00000000-0000-4000-8000-${String(number).padStart(12, '0')}`;
const profiles = [
  ['filesystem.typora', '文件', 'files', 'files', false],
  ['qq.napcat', 'QQ', 'qq', 'chats', true],
  ['wechat.localdb', '微信', 'wechat', 'chats', true],
  ['feishu.documents', '飞书', 'feishu', 'documents', false],
  ['notion.workspace', 'Notion', 'notion', 'documents', false],
  ['immich.assets', 'Immich', 'immich', 'media', true],
  ['browser.ai-history', '网页 AI', 'ai', 'conversations', false],
  ['ai.local-logs', '本地 AI', 'local-ai', 'logs', true],
  ['screen.native', '屏幕记录', 'screen', 'segments', true],
].map(([adapter_id, name, slug, page_kind, time_filter]) => ({ adapter_id, name, slug, page_kind, time_filter,
  mode: adapter_id === 'browser.ai-history' ? 'push' : 'collect', unit: '项', continuous_updates: adapter_id !== 'screen.native' }));

const sources = profiles.filter(profile => profile.slug !== 'ai').map((profile, index) => ({
  source_id: identity(index + 1), adapter_id: profile.adapter_id, namespace: `${profile.name}测试来源`,
  profile: profile.slug === 'local-ai' ? 'codex' : 'fixture', enabled: true,
})).concat(Object.entries(providerNames).map(([provider, name], index) => ({
  source_id: identity(index + 20), adapter_id: 'browser.ai-history', namespace: `${name}测试账号`,
  profile: provider, provider, enabled: true,
})));

const sourceFor = slug => sources.find(source => profiles.find(p => p.adapter_id === source.adapter_id)?.slug === slug);
const qq = sourceFor('qq');
const wechat = sourceFor('wechat');
const files = sourceFor('files');
const fixedDate = '2026-10-20T10:00:00Z';
const candidate = (id, name, kind = 'conversation', extra = {}) => ({ id, name, title: name, kind,
  created_at: '2026-10-01T01:00:00Z', updated_at: fixedDate, message_count: null, ...extra });

function candidates(source, params) {
  const adapter = source.adapter_id;
  let items;
  if (adapter === 'qq.napcat') items = [
    candidate('group:101', '项目讨论', 'group', { message_count: 42, first_at: '2026-10-01T01:00:00Z', last_at: fixedDate }),
    candidate('private:202', '林青', 'private', { message_count: 8, first_at: '2026-10-03T01:00:00Z', last_at: fixedDate }),
  ];
  else if (adapter === 'wechat.localdb') items = [
    candidate('conversations:room-one', '产品小组', 'group', { message_count: 30 }),
    candidate('conversations:friend-two', '陈晓', 'private', { message_count: 9 }),
  ];
  else if (adapter === 'filesystem.typora') items = params.get('parent') === 'research' ? [
    candidate('research/selected.md', '设计笔记.md', 'file', { size_bytes: 2048, version: '1:1:2048:1' }),
  ] : [
    candidate('research', '研究笔记', 'directory', { size_bytes: null, version: null }),
    candidate('weekly.md', '周记.md', 'file', { size_bytes: 1024, version: '1:2:1024:1' }),
  ];
  else if (adapter === 'browser.ai-history') items = [
    candidate(`${source.provider}-one`, `${providerNames[source.provider]} 会话一`, 'conversation', { provider: source.provider, locator: `${source.provider}-one`, revision: '1' }),
    candidate(`${source.provider}-two`, `${providerNames[source.provider]} 会话二`, 'conversation', { provider: source.provider, locator: `${source.provider}-two`, revision: '2' }),
  ];
  else if (adapter === 'notion.workspace') {
    const root = candidate('page_ids:page-one', '产品规划', 'page_ids', { can_expand: true, scope_description: '此页面' });
    const database = candidate('database_ids:database-one', '资料库', 'database_ids', { can_expand: true, scope_description: '此数据库中的条目' });
    const source = candidate('data_source_ids:source-one', '设计资料', 'data_source_ids', { can_expand: true, scope_description: '此数据源中的条目' });
    items = params.get('parent') === root.id ? [candidate('page_ids:child-one', '迭代计划', 'page_ids', { can_expand: true, scope_description: '此页面' }), database]
      : params.get('parent') === database.id ? [source]
      : params.get('parent') === source.id ? [candidate('page_ids:entry-one', '调研条目', 'page_ids', { can_expand: true, scope_description: '此页面' }), candidate('page_ids:entry-two', '未选条目', 'page_ids', { can_expand: true, scope_description: '此页面' })]
      : params.get('parent') && params.get('parent') !== '.' ? [] : [root, database];
  }
  else if (adapter === 'feishu.documents') items = [candidate('document_ids:document-one', '会议记录', 'document_ids'), candidate('folder_tokens:folder-one', '共享文档', 'folder_tokens')];
  else if (adapter === 'immich.assets') items = [candidate('album_ids:album-one', '旅行相册', 'album_ids')];
  else if (adapter === 'ai.local-logs') items = [candidate('sessions/selected.jsonl', 'selected.jsonl', 'file')];
  else items = [candidate('session/stream/segment-one', '2026-10-20 10:00', 'segment')];
  const query = (params.get('query') || '').toLocaleLowerCase();
  const kind = params.get('kind');
  return items.filter(item => (!query || item.name.toLocaleLowerCase().includes(query)) && (!kind || item.kind === kind));
}

const contentTypes = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.svg': 'image/svg+xml', '.woff2': 'font/woff2' };

export async function startFixtureServer() {
  const server = createServer(async (request, response) => {
    try {
      const url = new URL(request.url, 'http://127.0.0.1');
      let path;
      if (url.pathname.startsWith('/manage/assets/imports/')) path = resolve(ui, decodeURIComponent(url.pathname.slice('/manage/assets/imports/'.length)));
      else if (url.pathname === '/manage/imports/feishu/oauth/callback') path = join(ui, 'feishu-callback.html');
      else if (url.pathname === '/manage/imports' || url.pathname.startsWith('/manage/imports/') || url.pathname === '/manage/memory' || url.pathname.startsWith('/manage/memory/')) path = join(ui, 'index.html');
      else if (url.pathname === '/' || url.pathname === '/manage') path = join(ui, 'index.html');
      else if (url.pathname.startsWith('/manage/')) path = resolve(ui, decodeURIComponent(url.pathname.slice('/manage/'.length)));
      else throw new Error('unknown fixture path');
      if (relative(ui, path).startsWith('..')) throw new Error('outside fixture root');
      const body = await readFile(path);
      response.writeHead(200, { 'Content-Type': contentTypes[extname(path)] || 'application/octet-stream', 'Cache-Control': 'no-store' });
      response.end(body);
    } catch {
      response.writeHead(404, { 'Content-Type': 'text/plain' });
      response.end('UI fixture asset not found');
    }
  });
  await new Promise((accept, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', accept); });
  return { origin: `http://127.0.0.1:${server.address().port}`, close: () => new Promise(resolve => server.close(resolve)) };
}

export async function installFixtureAPI(page, origin) {
  const state = { requests: [], jobs: [], subscriptions: [], watches: new Map(), topics: [], delayed: new Map(), gates: new Map(), failures: [], previewCounts: new Map() };
  page.on('pageerror', error => state.failures.push(error.message));
  await page.route('**/*', async route => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.origin !== origin) return route.abort();
    if (!url.pathname.startsWith('/v1/')) return route.continue();
    let body = null;
    if (request.postData()) body = JSON.parse(request.postData());
    const path = url.pathname.replace('/v1/manage/', '');
    state.requests.push({ path, search: url.search, method: request.method(), body,
      authorized: request.headers().authorization === `Bearer ${fixtureKey}` });
    const reply = (value, status = 200) => route.fulfill({ status, contentType: 'application/json', headers: { 'Cache-Control': 'no-store' }, body: JSON.stringify(value) });
    if (request.headers().authorization !== `Bearer ${fixtureKey}`) return reply({ code: 'unauthorized' }, 401);
    const gate = state.gates.get(path + ':' + (body?.selection?.scopes?.[0] || ''));
    if (gate) { gate.started(); await gate.wait; }
    if (path === 'sources') return reply({ sources });
    if (path === 'groups') return reply({ groups: [] });
    if (path === 'items') return reply({ items: [], next_before: null });
    if (path === 'agents') return reply({ agents: [] });
    if (path === 'imports/sources') return reply({ profiles, sources });
    if (path === 'memory') {
      const origin = { source_instance_id: qq.source_id, source_namespace: qq.namespace, adapter_id: qq.adapter_id, source_profile: 'fixture', object_id: '项目讨论' };
      const all = [
        { ref: 'u1:1', kind: 'unit', body: 'Alpha.* 项目记忆 <script>bad()</script>', facts: ['c1:1-2'] },
        { ref: 'c1:1-2', kind: 'source', body: 'Alpha.* 原始聊天正文', facts: [] },
        { ref: 'r1', kind: 'day', scope: '2026-11-17', body: 'Alpha.* 日汇总', facts: ['u1:1'] },
        { ref: 'r2', kind: 'month', scope: '2026-11', body: 'Alpha.* 月汇总', facts: ['u1:1'] },
        { ref: 'r3', kind: 'runtime', scope: '项目', body: 'Alpha.* 主题记忆', facts: ['u1:1'] },
      ].map(row => ({ ...row, date_start: '2026-11-17', date_end: '2026-11-17', origins: ['unit', 'source'].includes(row.kind) ? [origin] : [] }));
      const items = all.filter(row => (body.layers.includes(row.kind) || (body.layers.includes('summary') && ['day', 'month'].includes(row.kind)))
        && (!body.query || row.body.includes(body.query)) && (!body.adapter || row.origins.some(o => o.adapter_id === body.adapter)));
      return reply({ items, next_cursor: null, formats: ['fixture'], timezone: 'Asia/Shanghai' });
    }
    if (path === 'memory/detail') return reply({ ref: body.ref, kind: body.ref.startsWith('c') ? 'source' : 'unit',
      body: body.original ? '{"original":"原始导入 JSON"}' : 'Alpha.* 完整正文 <img src=x onerror=bad()>', original_available: body.original,
      facts: body.ref.startsWith('u') ? ['c1:1-2'] : [], date_start: '2026-11-17', date_end: '2026-11-17', origins: [], next_offset: null, revision: 'fixture' });
    if (path === 'topics' && request.method() === 'GET') return reply({ topics: state.topics, discovery: { state: 'done' } });
    if (path === 'topics' && request.method() === 'POST') {
      const existing = state.topics.find(topic => topic.key === body.key);
      if (existing) Object.assign(existing, body);
      else state.topics.push({ ...body, origin: 'user', unit_count: 0, body: '', state: 'queued' });
      return reply({ key: body.key, status: 'queued' });
    }
    const sourceMatch = path.match(/^imports\/sources\/([^/]+)(?:\/(candidates|preview|watch))?$/);
    if (sourceMatch) {
      const source = sources.find(source => source.source_id === sourceMatch[1]);
      if (!source) return reply({ code: 'not_found' }, 404);
      const profile = profiles.find(profile => profile.adapter_id === source.adapter_id);
      if (sourceMatch[2] === 'watch') {
        if (request.method() === 'POST') state.watches.set(source.source_id, body.enabled);
        return reply({ enabled: state.watches.get(source.source_id) || false, include_attachments: true });
      }
      if (sourceMatch[2] === 'candidates') {
        const query = url.searchParams.get('query') || '';
        if (state.delayed.has(query)) {
          const delay = state.delayed.get(query);
          delay.started?.();
          await delay.wait;
          try { await reply({ items: [candidate('group:late', '旧搜索结果', 'group')], next_cursor: null, content_persisted: false }); }
          catch { /* AbortController may have discarded this request. */ }
          delay.finished?.();
          return;
        }
        const items = query === '新搜索' ? [candidate('group:fresh', '新搜索会话', 'group')] : candidates(source, url.searchParams);
        const hierarchy = [{ id: '.', title: '已连接范围' }, { id: 'page_ids:page-one', title: '产品规划' },
          { id: 'database_ids:database-one', title: '资料库' }, { id: 'data_source_ids:source-one', title: '设计资料' }];
        const position = hierarchy.findIndex(row => row.id === (url.searchParams.get('parent') || '.'));
        return reply({ items, next_cursor: null, content_persisted: false,
          ...(source.adapter_id === 'notion.workspace' ? { breadcrumbs: hierarchy.slice(0, position + 1) } : {}) });
      }
      if (sourceMatch[2] === 'preview') {
        const scope = body.selection.scopes[0];
        if (source.adapter_id === 'browser.ai-history') {
          const count = state.previewCounts.get(scope) || 0; state.previewCounts.set(scope, count + 1);
          if (!body.cursor) return reply({ status: 'pending', request_id: `preview-${scope}`, items: [], next_cursor: `preview-${scope}`, content_persisted: false });
          const second = body.cursor === `page-two-${scope}`;
          return reply({ status: 'ready', request_id: `preview-${scope}`, items: second
            ? [{ id: `${scope}:three`, role: 'assistant', text: '第三条回复', occurred_at: fixedDate, digest: 'c'.repeat(64) }]
            : [{ id: `${scope}:one`, role: 'user', text: '需要保留的问题', occurred_at: fixedDate, digest: 'a'.repeat(64) },
              { id: `${scope}:two`, role: 'assistant', text: '暂不导入的回复', occurred_at: fixedDate, digest: 'b'.repeat(64) }],
            next_cursor: second ? null : `page-two-${scope}`, content_persisted: false });
        }
        const occurredAt = body.selection.start ? new Date(Date.parse(body.selection.start) + 120000).toISOString() : fixedDate;
        return reply({ items: [{ id: `${scope}:message-1`, occurred_at: occurredAt,
          actor: { display_name: '测试发言者' }, text: `预览内容 ${scope}` }], next_cursor: null, content_persisted: false });
      }
      return reply({ ...profile, ...source, binding_available: true, import_only: true,
        preview: profile.slug === 'notion' || ['chats', 'conversations'].includes(profile.page_kind), preview_supported: profile.slug === 'notion' || ['chats', 'conversations'].includes(profile.page_kind),
        scopes: candidates(source, new URLSearchParams()), continuous_updates: { supported: profile.slug !== 'screen', minimum_interval_seconds: 150, default_interval_seconds: 900 } });
    }
    if (path === 'imports/plan-batch') return reply({ plans: body.selections.map(selection => ({
      source_id: body.source_id, selection, config_hash: 'ui-fixture-config-hash', scope_count: selection.scopes.length,
      publication: 'pending_private', persistence: 'after_selection',
    })) });
    if (path === 'imports/jobs/batch') {
      const jobs = body.plans.map((plan, index) => ({ job_id: identity(100 + state.jobs.length + index), source_id: body.source_id,
        selection: plan.selection, state: 'queued', progress: {}, created_at: fixedDate, updated_at: fixedDate, subscription_id: null, error_code: null }));
      state.jobs.push(...jobs);
      return reply({ jobs }, 202);
    }
    if (path === 'imports/jobs' && request.method() === 'GET') return reply({ jobs: state.jobs });
    if (path === 'imports/subscriptions' && request.method() === 'GET') return reply({ subscriptions: state.subscriptions });
    if (path === 'imports/subscriptions' && request.method() === 'POST') {
      const job = state.jobs.find(job => job.job_id === body.job_id);
      const subscription = { subscription_id: identity(500 + state.subscriptions.length), source_id: job.source_id,
        seed_job_id: job.job_id, interval_seconds: body.interval_seconds, state: 'active', last_success_at: null };
      state.subscriptions.push(subscription);
      return reply(subscription, 201);
    }
    return reply({ code: 'fixture_route_not_implemented' }, 404);
  });
  return state;
}

let fixture;
let browser;
before(async () => {
  const require = createRequire(import.meta.url);
  const { chromium } = require(process.env.MEMULIFE_PLAYWRIGHT_MODULE || 'playwright');
  fixture = await startFixtureServer();
  browser = await chromium.launch({ ...(process.env.MEMULIFE_CHROMIUM ? { executablePath: process.env.MEMULIFE_CHROMIUM } : {}),
    headless: true, args: ['--disable-dev-shm-usage', '--no-sandbox'] });
});
after(async () => {
  await browser?.close();
  await fixture?.close();
});

async function visit(t, path, viewport = { width: 1280, height: 800 }, setup = () => {}) {
  const context = await browser.newContext({ viewport, timezoneId: 'UTC' });
  t.after(() => context.close());
  const page = await context.newPage();
  const state = await installFixtureAPI(page, fixture.origin);
  setup(state);
  await page.goto(fixture.origin + path);
  await page.getByLabel('管理密钥', { exact: true }).fill(fixtureKey);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await page.locator('#login').waitFor({ state: 'hidden' });
  return { page, state };
}

function holdRequest(state, path) {
  let release, started;
  const ready = new Promise(resolve => { started = resolve; });
  state.gates.set(path, { started, wait: new Promise(resolve => { release = resolve; }) });
  return { ready, release };
}

async function chooseChat(page, name, start, end) {
  await page.getByRole('button', { name, exact: true }).click();
  const from = page.getByLabel('开始日期', { exact: true });
  const to = page.getByLabel('结束日期', { exact: true });
  await from.fill(await from.getAttribute('type') === 'datetime-local' ? start + 'T00:00' : start);
  await to.fill(await to.getAttribute('type') === 'datetime-local' ? end + 'T00:00' : end);
  await page.getByRole('button', { name: '预览消息', exact: true }).click();
  await page.getByText('预览内容', { exact: false }).waitFor();
  await page.getByRole('button', { name: '加入选择', exact: true }).click();
  assert.equal(await page.getByRole('button', { name: '已选择', exact: true }).isDisabled(), true);
}

test('QQ and Notion setup keeps credentials separate from content selection', { timeout: 30000 }, async t => {
  const { execFileSync } = await import('node:child_process');
  const setup = JSON.parse(execFileSync(join(root, '.venv/bin/python'), ['-c',
    'import json; from memu.acquisition.setup import setup_catalog; print(json.dumps(setup_catalog()))'], { cwd: root, encoding: 'utf8' }));
  for (const [slug, secretField] of [['qq', 'webui_token_ref'], ['notion', 'token_ref']]) {
    const { page } = await visit(t, '/manage/imports/' + slug);
    const source = sourceFor(slug);
    let submitted;
    await page.route('**/v1/manage/imports/setup', route => route.fulfill({ json: setup }));
    await page.route('**/v1/manage/imports/sources', async route => {
      if (route.request().method() !== 'POST') return route.fallback();
      submitted = route.request().postDataJSON();
      await route.fulfill({ json: { source_id: source.source_id, status: 'configured' } });
    });
    await page.getByRole('button', { name: '添加来源', exact: true }).click();
    const form = page.getByRole('region', { name: '添加来源', exact: true });
    await form.locator(`[data-config-field="${secretField}"]`).fill('FIXTURE_CONNECTION_SECRET');
    if (slug === 'qq') {
      await form.getByLabel('NapCat WebUI 地址 *', { exact: true }).fill('http://localhost:16099/webui/');
      assert.equal(await form.locator('[data-config-field="token_env"]').count(), 0);
    }
    for (const field of ['account_id', 'workspace_id', 'principal_id', 'conversations', 'page_ids', 'document_ids', 'folder_tokens', 'wiki_spaces']) {
      assert.equal(await form.locator(`[data-config-field="${field}"]`).count(), 0);
    }
    await form.getByRole('button', { name: '连接并选择数据', exact: true }).click();
    await form.waitFor({ state: 'detached' });
    assert.equal(submitted.credentials[secretField], 'FIXTURE_CONNECTION_SECRET');
    assert.ok(!JSON.stringify(submitted.source.config).includes('FIXTURE_CONNECTION_SECRET'));
    assert.equal(submitted.source.adapter_id, source.adapter_id);
    if (slug === 'qq') assert.equal(submitted.source.source_profile, 'napcat-webui');
  }
});

test('Feishu connects through an authorization popup without entering a short-lived token', { timeout: 30000 }, async t => {
  const { page, state } = await visit(t, '/manage/imports/feishu');
  const { execFileSync } = await import('node:child_process');
  const setup = JSON.parse(execFileSync(join(root, '.venv/bin/python'), ['-c',
    'import json; from memu.acquisition.setup import setup_catalog; print(json.dumps(setup_catalog()))'], { cwd: root, encoding: 'utf8' }));
  await page.route('**/v1/manage/imports/setup', route => route.fulfill({ json: setup }));
  const origin = new URL(page.url()).origin, ticket = 's'.repeat(43);
  let start, completed;
  await page.route('**/v1/manage/imports/feishu/authorize', async route => {
    start = route.request().postDataJSON();
    await route.fulfill({ json: { state: ticket, authorization_url: origin + '/fixture-feishu-authorize' } });
  });
  await page.context().route('**/fixture-feishu-authorize', route => route.fulfill({ status: 302,
    headers: { location: origin + '/manage/imports/feishu/oauth/callback?state=' + ticket + '&code=fixture-code' } }));
  await page.route('**/v1/manage/imports/feishu/complete', async route => {
    completed = route.request().postDataJSON();
    await route.fulfill({ json: { source_id: sourceFor('feishu').source_id, status: 'configured' } });
  });
  await page.getByRole('button', { name: '添加来源', exact: true }).click();
  const form = page.getByRole('region', { name: '添加来源', exact: true });
  await form.getByRole('textbox', { name: 'App ID', exact: true }).fill('cli_fixture');
  await form.getByLabel('App Secret', { exact: true }).fill('PRIVATE_FIXTURE_SECRET');
  assert.equal(await form.locator('[data-config-field="access_token_ref"]').count(), 0);
  await form.getByRole('button', { name: '授权连接飞书', exact: true }).click();
  await form.waitFor({ state: 'detached' });
  assert.equal(start.client_id, 'cli_fixture');
  assert.equal(start.client_secret, 'PRIVATE_FIXTURE_SECRET');
  assert.equal(start.redirect_uri, origin + '/manage/imports/feishu/oauth/callback');
  assert.equal(completed.code, 'fixture-code');
  assert.equal(completed.state, ticket);
  assert.equal(state.jobs.length, 0);
  assert.deepEqual(state.failures, []);
});

test('owner can define and edit a memory topic through a compatible old link', async t => {
  const { page, state } = await visit(t, '/manage/imports/topics');
  await page.getByRole('heading', { name: '记忆主题', exact: true }).waitFor();
  assert.ok(page.url().includes('/manage/memory/topics'));
  await page.getByRole('button', { name: '添加主题', exact: true }).click();
  await page.getByLabel('主题名称', { exact: true }).fill('本地模型部署');
  await page.getByLabel('关注范围', { exact: true }).fill('部署进展、延迟和并发优化');
  await page.getByRole('button', { name: '保存', exact: true }).click();
  await page.getByRole('heading', { name: '本地模型部署', exact: true }).waitFor();
  assert.equal(state.topics.length, 1);
  assert.equal(state.topics[0].description, '部署进展、延迟和并发优化');
  await page.getByText('用户指定', { exact: true }).waitFor();
  await page.getByRole('button', { name: '编辑主题', exact: true }).click();
  assert.equal(await page.getByLabel('主题名称', { exact: true }).getAttribute('readonly'), '');
  await page.getByLabel('关注范围', { exact: true }).fill('只跟踪实际部署的性能变化');
  await page.getByRole('button', { name: '保存', exact: true }).click();
  await page.getByText('只跟踪实际部署的性能变化', { exact: true }).waitFor();
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  const output = process.env.MEMULIFE_UI_REPORT_DIR || join(root, 'reports/import-ui');
  await mkdir(output, { recursive: true });
  await page.screenshot({ path: join(output, 'topics-mobile.png'), fullPage: true });
  await page.setViewportSize({ width: 1280, height: 800 });
  await page.screenshot({ path: join(output, 'topics-desktop.png'), fullPage: true });
  assert.deepEqual(state.failures, []);
});

test('every source has a directly reachable subpage with an authorized fixture API', { timeout: 60000 }, async t => {
  for (const slug of ['qq', 'wechat', 'files', 'notion', 'feishu', 'immich', ...Object.keys(providerNames).map(id => 'ai/' + id), 'local-ai', 'screen', 'activity']) {
    const { page, state } = await visit(t, '/manage/imports/' + slug);
    await page.waitForLoadState('networkidle');
    assert.equal(page.url(), fixture.origin + '/manage/imports/' + slug);
    assert.equal(await page.getByLabel('管理密钥', { exact: true }).isVisible(), false,
      JSON.stringify({ slug, errors: state.failures, loginError: await page.locator('#login-error').textContent() }));
    assert.deepEqual(state.failures, [], slug);
    assert.ok(state.requests.length > 0, slug);
    assert.ok(state.requests.every(request => request.authorized), slug);
    await page.context().close();
  }
});

test('chat previews create no jobs; each conversation retains its own date range before batch submission', { timeout: 30000 }, async t => {
  const { page, state } = await visit(t, '/manage/imports/qq');
  await chooseChat(page, '项目讨论', '2026-10-01', '2026-10-05');
  await page.getByRole('button', { name: '林青', exact: true }).click();
  assert.equal(await page.getByLabel('开始日期', { exact: true }).inputValue(), '');
  assert.equal(await page.getByLabel('结束日期', { exact: true }).inputValue(), '');
  await chooseChat(page, '林青', '2026-10-10', '2026-10-12');
  await page.getByRole('button', { name: '项目讨论', exact: true }).click();
  assert.equal(await page.getByLabel('开始日期', { exact: true }).inputValue(), '2026-10-01');
  assert.equal(await page.getByLabel('结束日期', { exact: true }).inputValue(), '2026-10-05');
  await page.getByRole('button', { name: '移除 项目讨论', exact: true }).click();
  assert.equal(await page.getByRole('button', { name: '加入选择', exact: true }).isEnabled(), true);
  await page.getByRole('button', { name: '加入选择', exact: true }).click();
  assert.ok(state.requests.some(request => request.path.endsWith('/preview')));
  assert.equal(state.jobs.length, 0);
  await page.getByRole('region', { name: '已选范围' }).getByText('项目讨论', { exact: true }).waitFor();
  await page.getByRole('region', { name: '已选范围' }).getByText('林青', { exact: true }).waitFor();
  await page.getByRole('button', { name: '核对范围', exact: true }).click();
  await page.getByRole('button', { name: '开始导入', exact: true }).waitFor({ state: 'visible' });
  const plan = state.requests.find(request => request.path === 'imports/plan-batch');
  assert.equal(plan.body.source_id, qq.source_id);
  assert.equal(plan.body.selections.length, 2);
  const group = plan.body.selections.find(value => value.scopes[0] === 'group:101');
  const person = plan.body.selections.find(value => value.scopes[0] === 'private:202');
  assert.equal(group.start, '2026-10-01T00:00:00.000Z');
  assert.equal(group.end, '2026-10-06T00:00:00.000Z');
  assert.equal(person.start, '2026-10-10T00:00:00.000Z');
  assert.equal(person.end, '2026-10-13T00:00:00.000Z');
  assert.equal(state.jobs.length, 0);
  await Promise.all([
    page.waitForResponse(response => new URL(response.url()).pathname.endsWith('/jobs/batch')),
    page.getByRole('button', { name: '开始导入', exact: true }).click(),
  ]);
  assert.equal(state.jobs.length, 2);
  const submitted = state.requests.find(request => request.path === 'imports/jobs/batch');
  assert.deepEqual(submitted.body.plans.map(plan => plan.selection), plan.body.selections);
  assert.deepEqual(state.failures, []);
});

test('out-of-order search responses cannot replace the latest conversation list', { timeout: 30000 }, async t => {
  const { page, state } = await visit(t, '/manage/imports/qq');
  await page.getByRole('button', { name: '项目讨论', exact: true }).waitFor();
  let release, started, finished;
  const oldStarted = new Promise(resolve => { started = resolve; });
  const oldFinished = new Promise(resolve => { finished = resolve; });
  state.delayed.set('旧搜索', { wait: new Promise(resolve => { release = resolve; }), started, finished });
  t.after(() => release());
  const search = page.getByRole('searchbox', { name: '搜索会话', exact: true });
  await search.fill('旧搜索');
  await oldStarted;
  await search.fill('新搜索');
  await page.getByRole('button', { name: '新搜索会话', exact: true }).waitFor();
  release();
  await oldFinished;
  await page.waitForLoadState('networkidle');
  assert.equal(await page.getByRole('button', { name: '旧搜索结果', exact: true }).count(), 0);
  assert.equal(await page.getByRole('button', { name: '新搜索会话', exact: true }).count(), 1);
  assert.deepEqual(state.failures, []);
});

test('a preview from an earlier conversation cannot overwrite the current conversation', { timeout: 30000 }, async t => {
  const { page, state } = await visit(t, '/manage/imports/qq');
  await page.getByRole('button', { name: '项目讨论', exact: true }).click();
  const held = holdRequest(state, `imports/sources/${qq.source_id}/preview:group:101`);
  t.after(held.release);
  await page.getByRole('button', { name: '预览消息', exact: true }).click();
  await held.ready;
  await page.getByRole('button', { name: '林青', exact: true }).click();
  await page.getByRole('button', { name: '预览消息', exact: true }).click();
  await page.getByText('预览内容 private:202', { exact: true }).waitFor();
  held.release();
  await page.waitForLoadState('networkidle');
  assert.equal(await page.getByText('预览内容 group:101', { exact: true }).count(), 0);
  assert.equal(await page.getByText('预览内容 private:202', { exact: true }).count(), 1);
  assert.equal(await page.getByText('Expired preview', { exact: true }).count(), 0);
  assert.equal(state.jobs.length, 0);
  assert.deepEqual(state.failures, []);
});

test('batch submission locks selection controls until the request completes', { timeout: 30000 }, async t => {
  const { page, state } = await visit(t, '/manage/imports/qq');
  await chooseChat(page, '项目讨论', '2026-10-01', '2026-10-05');
  await page.getByRole('button', { name: '核对范围', exact: true }).click();
  await page.getByRole('button', { name: '开始导入', exact: true }).waitFor();
  const held = holdRequest(state, 'imports/jobs/batch:');
  t.after(held.release);
  await page.getByRole('button', { name: '开始导入', exact: true }).click();
  await held.ready;
  for (const region of ['.catalog-panel', '.candidate-detail', '.selection-panel']) {
    assert.equal(await page.locator(region).evaluate(element => element.inert), true, region);
  }
  assert.equal(await page.locator('.workbench').getAttribute('aria-busy'), 'true');
  assert.equal(await page.getByRole('checkbox', { name: '选择 林青', exact: true, includeHidden: true }).isChecked(), false);
  assert.equal(state.jobs.length, 0);
  held.release();
  await page.getByText('已创建 1 个导入任务', { exact: true }).waitFor();
  assert.equal(state.jobs.length, 1);
  assert.deepEqual(state.jobs[0].selection.scopes, ['group:101']);
  assert.equal(await page.locator('.catalog-panel').evaluate(element => element.inert), false);
  await page.getByRole('checkbox', { name: '选择 林青', exact: true }).check();
  await page.getByText('已选 1 项', { exact: true }).waitFor();
  assert.deepEqual(state.failures, []);
});

test('continuous updates are available only for sources declaring that capability', { timeout: 30000 }, async t => {
  const { page, state } = await visit(t, '/manage/imports/activity', undefined, state => {
    for (const [index, source] of [qq, sourceFor('screen')].entries()) state.jobs.push({
      job_id: identity(300 + index), source_id: source.source_id, state: 'completed',
      selection: { scopes: ['fixture-scope'] }, updated_at: fixedDate, subscription_id: null,
    });
  });
  const qqRow = page.locator('.activity-row').filter({ hasText: qq.namespace });
  const screenRow = page.locator('.activity-row').filter({ hasText: sourceFor('screen').namespace });
  await qqRow.getByRole('button', { name: '持续更新', exact: true }).waitFor();
  assert.equal(await screenRow.getByRole('button', { name: '持续更新', exact: true }).count(), 0);
  await qqRow.getByLabel('更新间隔', { exact: true }).selectOption('3600');
  await qqRow.getByRole('button', { name: '持续更新', exact: true }).click();
  await page.getByText('更新中', { exact: true }).waitFor();
  assert.equal(state.subscriptions.length, 1);
  assert.equal(state.subscriptions[0].source_id, qq.source_id);
  assert.equal(state.subscriptions[0].interval_seconds, 3600);
  assert.equal(await page.getByRole('button', { name: '持续更新', exact: true }).count(), 0);
  assert.deepEqual(state.failures, []);
});


test('file navigation uses source candidates and never submits a job while browsing', { timeout: 30000 }, async t => {
  const { page, state } = await visit(t, '/manage/imports/files');
  await page.getByRole('button', { name: '研究笔记', exact: true }).click();
  await page.getByRole('checkbox', { name: '选择 设计笔记.md', exact: true }).waitFor();
  assert.ok(state.requests.some(request => request.path === `imports/sources/${files.source_id}/candidates` && new URLSearchParams(request.search).get('parent') === 'research'));
  assert.equal(state.jobs.length, 0);
  assert.deepEqual(state.failures, []);
});

test('an AI platform subpage filters bindings and candidates to that provider', { timeout: 30000 }, async t => {
  const { page, state } = await visit(t, '/manage/imports/ai/claude');
  await page.getByRole('checkbox', { name: '选择完整会话 Claude 会话一', exact: true }).waitFor();
  assert.equal(await page.getByRole('checkbox', { name: '选择完整会话 ChatGPT 会话一', exact: true }).count(), 0);
  const candidateRequests = state.requests.filter(request => request.path.endsWith('/candidates'));
  assert.ok(candidateRequests.length > 0);
  assert.ok(candidateRequests.every(request => request.path.includes(sources.find(source => source.provider === 'claude').source_id)));
  assert.deepEqual(state.failures, []);
});

test('source watch aligns inline and locks selection without losing it', { timeout: 30000 }, async t => {
  const { page, state } = await visit(t, '/manage/imports/ai/chatgpt', { width: 1920, height: 900 });
  const toggle = page.getByRole('checkbox', { name: '持续关注当前来源' });
  const region = page.locator('.source-content');
  await page.waitForFunction(() => document.querySelector('.source-content')?.inert === false);
  const selected = page.getByRole('checkbox', { name: '选择完整会话 ChatGPT 会话一', exact: true });
  await selected.check();
  const labelBox = await page.locator('.source-watch-control').boundingBox();
  const hintBox = await page.getByText('开启后自动导入此来源所有内容', { exact: true }).boundingBox();
  assert.ok(Math.abs(labelBox.y + labelBox.height / 2 - hintBox.y - hintBox.height / 2) < 2);
  await toggle.check();
  await page.waitForFunction(() => document.querySelector('.source-content')?.inert === true);
  assert.equal(await region.getAttribute('aria-disabled'), 'true');
  assert.equal(await selected.isChecked(), true);
  await toggle.uncheck();
  await page.waitForFunction(() => document.querySelector('.source-content')?.inert === false);
  assert.equal(await selected.isChecked(), true);
  assert.deepEqual(state.failures, []);
});

test('AI message browsing is transient and only explicitly selected messages reach a plan', { timeout: 30000 }, async t => {
  const { page, state } = await visit(t, '/manage/imports/ai/chatgpt');
  await page.getByRole('button', { name: 'ChatGPT 会话一', exact: true }).click();
  await page.getByText('正在读取会话…', { exact: true }).waitFor();
  await page.waitForLoadState('networkidle');
  assert.equal(state.requests.filter(request => request.path.endsWith('/preview')).length, 1);
  assert.equal(state.jobs.length, 0);
  await page.getByRole('button', { name: '刷新消息', exact: true }).click();
  await page.getByText('需要保留的问题', { exact: true }).waitFor();
  await page.getByRole('checkbox', { name: '选择消息 1', exact: true }).check();
  await page.getByRole('button', { name: '更多消息', exact: true }).click();
  await page.getByRole('checkbox', { name: '选择消息 3', exact: true }).check();
  await page.getByRole('button', { name: '加入选择', exact: true }).click();
  await page.getByRole('region', { name: '已选范围' }).getByText('2 条消息', { exact: true }).waitFor();
  assert.equal(await page.getByRole('checkbox', { name: '选择完整会话 ChatGPT 会话一', exact: true }).isDisabled(), true);
  assert.equal(state.jobs.length, 0);
  const storage = await page.evaluate(async () => ({ local: { ...localStorage }, session: { ...sessionStorage }, databases: await indexedDB.databases() }));
  assert.deepEqual(storage, { local: {}, session: {}, databases: [] });
  await page.getByRole('button', { name: '核对范围', exact: true }).click();
  await page.getByRole('button', { name: '开始导入', exact: true }).waitFor();
  const plan = state.requests.find(request => request.path === 'imports/plan-batch');
  assert.deepEqual(plan.body.selections[0].messages, { 'chatgpt-one': { 'chatgpt-one:one': 'a'.repeat(64), 'chatgpt-one:three': 'c'.repeat(64) } });
  assert.ok(!JSON.stringify(plan.body).includes('需要保留的问题'));
  assert.ok(!JSON.stringify(plan.body).includes('暂不导入的回复'));
  await page.getByRole('button', { name: '开始导入', exact: true }).click();
  await page.getByText('已创建 1 个导入任务', { exact: true }).waitFor();
  assert.deepEqual(state.jobs[0].selection.messages, plan.body.selections[0].messages);
  assert.deepEqual(state.failures, []);
});

test('switching AI selection modes cannot silently keep the broader whole-conversation scope', { timeout: 30000 }, async t => {
  const { page, state } = await visit(t, '/manage/imports/ai/claude');
  await page.getByRole('checkbox', { name: '选择完整会话 Claude 会话一', exact: true }).check();
  await page.getByRole('button', { name: 'Claude 会话一', exact: true }).click();
  await page.getByRole('button', { name: '已选择完整会话', exact: true }).waitFor();
  await page.getByRole('button', { name: '选择消息', exact: true }).click();
  await page.getByText('已选 0 项', { exact: true }).waitFor();
  assert.equal(await page.getByRole('button', { name: '核对范围', exact: true }).isDisabled(), true);
  await page.getByRole('checkbox', { name: '选择消息 1', exact: true }).check();
  await page.getByRole('button', { name: '加入选择', exact: true }).click();
  await page.getByText('已选 1 项', { exact: true }).waitFor();
  await page.getByRole('checkbox', { name: '选择消息 2', exact: true }).check();
  await page.getByText('已选 0 项', { exact: true }).waitFor();
  await page.getByRole('button', { name: '加入选择', exact: true }).click();
  await page.getByRole('button', { name: '完整会话', exact: true }).click();
  await page.getByText('已选 0 项', { exact: true }).waitFor();
  assert.equal(state.jobs.length, 0);
  assert.deepEqual(state.failures, []);
});

test('Notion center navigation previews content and selects descendants without replacing the catalog', { timeout: 30000 }, async t => {
  const { page, state } = await visit(t, '/manage/imports/notion');
  const catalog = page.locator('.catalog-panel'), detail = page.locator('.candidate-detail');
  await catalog.getByRole('button', { name: '产品规划', exact: true }).click();
  await detail.getByText('预览内容 page_ids:page-one', { exact: true }).waitFor();
  assert.equal(await catalog.getByRole('button', { name: /^展开/ }).count(), 0);
  await detail.getByRole('button', { name: '下级内容', exact: true }).click();
  await detail.getByRole('button', { name: '迭代计划', exact: true }).waitFor();
  await detail.getByRole('button', { name: '资料库', exact: true }).click();
  await detail.getByRole('button', { name: '设计资料', exact: true }).click();
  await detail.getByRole('checkbox', { name: '选择 调研条目', exact: true }).check();
  assert.equal(await detail.getByRole('checkbox', { name: '选择 未选条目', exact: true }).isChecked(), false);
  await detail.getByRole('button', { name: '调研条目', exact: true }).click();
  await detail.getByText('预览内容 page_ids:entry-one', { exact: true }).waitFor();
  await page.getByText('此页面', { exact: true }).first().waitFor();
  assert.equal(await catalog.getByRole('button', { name: '产品规划', exact: true }).count(), 1);
  assert.equal(await catalog.getByRole('button', { name: '调研条目', exact: true }).count(), 0);
  assert.equal(state.jobs.length, 0);
  assert.ok(state.requests.some(request => request.path.endsWith('/candidates') && new URLSearchParams(request.search).get('parent') === 'data_source_ids:source-one'));
  await detail.getByRole('navigation', { name: 'Notion 路径' }).getByRole('button', { name: '产品规划', exact: true }).click();
  await detail.getByText('预览内容 page_ids:page-one', { exact: true }).waitFor();
  await page.getByRole('button', { name: '核对范围', exact: true }).click();
  await page.getByRole('button', { name: '开始导入', exact: true }).waitFor();
  const plan = state.requests.find(request => request.path === 'imports/plan-batch');
  assert.deepEqual(plan.body.selections[0].scopes, ['page_ids:entry-one']);
  assert.equal(plan.body.selections[0].notion_paths, undefined);
  assert.equal(state.jobs.length, 0);
  assert.deepEqual(state.failures, []);
});

test('the manager key stays out of URLs, cookies, localStorage and sessionStorage', { timeout: 30000 }, async t => {
  const { page, state } = await visit(t, '/manage/imports/wechat');
  await page.getByRole('button', { name: '产品小组', exact: true }).waitFor();
  assert.ok(!page.url().includes(fixtureKey));
  assert.ok(state.requests.every(request => !(request.path + request.search).includes(fixtureKey)));
  const browserStorage = await page.evaluate(() => JSON.stringify({ local: { ...localStorage }, session: { ...sessionStorage }, cookie: document.cookie }));
  assert.ok(!browserStorage.includes(fixtureKey));
  assert.deepEqual(await page.evaluate(() => indexedDB.databases()), []);
  assert.equal(await page.locator('input[type="password"]').inputValue(), '');
  assert.deepEqual(state.failures, []);
});

test('chat and file subpages fit a 320px viewport without page-level horizontal overflow', { timeout: 30000 }, async t => {
  for (const slug of ['qq', 'files', 'ai/chatgpt']) {
    const { page, state } = await visit(t, '/manage/imports/' + slug, { width: 320, height: 800 });
    await page.waitForLoadState('networkidle');
    const dimensions = await page.evaluate(() => ({ width: innerWidth, html: document.documentElement.scrollWidth, body: document.body.scrollWidth,
      overflow: [...document.querySelectorAll('*')].map(element => ({ tag: element.tagName, class: element.className,
        right: element.getBoundingClientRect().right, scrollWidth: element.scrollWidth })).filter(value => value.right > innerWidth + 1) }));
    assert.ok(dimensions.html <= dimensions.width + 1, JSON.stringify({ slug, ...dimensions }));
    assert.ok(dimensions.body <= dimensions.width + 1, JSON.stringify({ slug, ...dimensions }));
    if (process.env.MEMULIFE_UI_REPORT_DIR) {
      await mkdir(process.env.MEMULIFE_UI_REPORT_DIR, { recursive: true });
      await page.screenshot({ path: join(process.env.MEMULIFE_UI_REPORT_DIR, slug.replace('/', '-') + '-320.png'), fullPage: true });
    }
    assert.deepEqual(state.failures, []);
    await page.context().close();
  }
});

test('source cards and custom checkboxes remain keyboard accessible with reduced motion', { timeout: 30000 }, async t => {
  const { page, state } = await visit(t, '/manage/imports');
  const cards = page.locator('.source-directory a');
  await cards.first().waitFor();
  assert.equal(await cards.count(), sources.length);
  assert.equal(await cards.locator('svg:not([aria-hidden="true"])').count(), 0);
  if (process.env.MEMULIFE_UI_REPORT_DIR) {
    await mkdir(process.env.MEMULIFE_UI_REPORT_DIR, { recursive: true });
    await page.screenshot({ path: join(process.env.MEMULIFE_UI_REPORT_DIR, 'sources-1280.png'), fullPage: true });
  }
  await page.emulateMedia({ reducedMotion: 'reduce' });
  const qqCard = page.locator('.source-directory').getByRole('link', { name: /^QQ/ });
  await qqCard.focus();
  await qqCard.press('Enter');
  const check = page.getByRole('checkbox', { name: '选择 项目讨论', exact: true });
  await check.focus();
  await check.press('Space');
  await page.getByText('已选 1 项', { exact: true }).waitFor();
  assert.equal(await check.isChecked(), true);
  await page.getByRole('button', { name: '项目讨论', exact: true }).click();
  const animation = await page.locator('.detail-content').evaluate(element => getComputedStyle(element).animationDuration);
  assert.ok(parseFloat(animation) <= .00001, animation);
  await page.emulateMedia({ forcedColors: 'active' });
  assert.equal(await check.evaluate(element => getComputedStyle(element).appearance), 'auto');
  assert.equal(state.jobs.length, 0);
  assert.deepEqual(state.failures, []);
});

test('embedded workbench follows its narrow host independently of the desktop viewport', { timeout: 30000 }, async t => {
  const { page, state } = await visit(t, '/manage/imports/qq', { width: 1440, height: 900 });
  await page.getByRole('button', { name: '项目讨论', exact: true }).waitFor();
  await page.locator('#import-root').evaluate(element => { element.style.width = '320px'; });
  assert.equal(await page.getByRole('navigation', { name: '导入来源', exact: true }).isVisible(), false);
  assert.equal(await page.getByText('切换来源', { exact: true }).isVisible(), true);
  await chooseChat(page, '项目讨论', '2026-10-01', '2026-10-05');
  const overflow = await page.locator('#import-root').evaluate(host => {
    const outer = host.getBoundingClientRect();
    return [...host.querySelectorAll('.workbench, .catalog-panel, .candidate-detail, .selection-panel, .date-fields input')]
      .filter(element => { const rect = element.getBoundingClientRect(); return rect.width && rect.right > outer.right + 1; })
      .map(element => element.className || element.name);
  });
  assert.deepEqual(overflow, []);
  assert.equal(state.jobs.length, 0);
  assert.deepEqual(state.failures, []);
  const embedded = await visit(t, '/manage/imports/notion?embed=1', { width: 320, height: 800 });
  assert.equal(await embedded.page.locator('.app-header').isVisible(), false);
  await embedded.page.getByRole('button', { name: '产品规划', exact: true }).click();
  assert.equal(await embedded.page.getByRole('heading', { name: '产品规划', exact: true }).isVisible(), true);
  assert.deepEqual(embedded.state.failures, []);
});

test('fixture screenshots and script-free snapshots show selected work rather than empty pages', { timeout: 60000 }, async t => {
  const output = process.env.MEMULIFE_UI_REPORT_DIR || join(root, 'reports/local/import-ui');
  await mkdir(join(output, 'render'), { recursive: true });
  const { writeFile } = await import('node:fs/promises');
  for (const [name, route, first, second] of [
    ['qq', 'qq', '项目讨论', '林青'],
    ['ai', 'ai/claude', 'Claude 会话一', 'Claude 会话二'],
    ['notion', 'notion', '产品规划', '资料库'],
  ]) {
    const { page, state } = await visit(t, '/manage/imports/' + route);
    if (name === 'qq') {
      await chooseChat(page, first, '2026-10-01', '2026-10-05');
      await chooseChat(page, second, '2026-10-10', '2026-10-12');
    } else if (name === 'ai') {
      await page.getByRole('checkbox', { name: '选择完整会话 ' + second, exact: true }).check();
      await page.getByRole('button', { name: first, exact: true }).click();
      await page.getByRole('button', { name: '刷新消息', exact: true }).click();
      await page.getByRole('checkbox', { name: '选择消息 1', exact: true }).check();
      await page.getByRole('button', { name: '加入选择', exact: true }).click();
    } else {
      await page.getByRole('checkbox', { name: '选择 ' + first, exact: true }).check();
      await page.getByRole('checkbox', { name: '选择 ' + second, exact: true }).check();
      await page.getByRole('button', { name: first, exact: true }).click();
    }
    await page.getByText('已选 2 项', { exact: true }).waitFor();
    for (const width of [1280, 1440, 320]) {
      await page.setViewportSize({ width, height: 900 });
      await page.screenshot({ path: join(output, `${name}-${width}.png`), fullPage: true });
    }
    const snapshot = await page.evaluate(() => {
      const copy = document.documentElement.cloneNode(true);
      copy.querySelectorAll('script').forEach(script => script.remove());
      copy.querySelectorAll('input').forEach(input => {
        if (input.type === 'password') input.removeAttribute('value');
        else if (input.type === 'checkbox') input.toggleAttribute('checked', input.checked);
        else input.setAttribute('value', input.value);
      });
      copy.querySelectorAll('option').forEach(option => option.toggleAttribute('selected', option.selected));
      return '<!doctype html>\n' + copy.outerHTML;
    });
    assert.ok(!snapshot.includes(fixtureKey));
    assert.equal(state.jobs.length, 0);
    assert.deepEqual(state.failures, []);
    await writeFile(join(output, 'render', name + '.html'), snapshot);
    await page.context().close();
  }
});


test('memory workspaces keep import navigation and browse source evidence and originals', async t => {
  const { page, state } = await visit(t, '/manage/imports');
  const workspace = page.getByRole('navigation', { name: '工作区', exact: true });
  assert.equal(await page.locator('.source-nav').getByRole('link', { name: '记忆主题', exact: true }).count(), 0);
  await workspace.getByRole('link', { name: '记忆管理', exact: true }).click();
  await page.getByRole('heading', { name: '全部记忆', exact: true }).waitFor();
  await page.locator('.memory-card').first().waitFor();
  assert.equal(await page.locator('.source-nav').getByRole('link', { name: '导入记录', exact: true }).count(), 0);
  await page.locator('.source-nav').getByRole('link', { name: /QQ/ }).click();
  await page.getByRole('heading', { name: 'QQ', exact: true }).waitFor();
  await page.locator('.memory-card').first().waitFor();
  await page.getByLabel('开始日期', { exact: true }).fill('2026-11-17');
  await page.getByLabel('结束日期', { exact: true }).fill('2026-11-18');
  await page.getByLabel('原导入格式', { exact: true }).selectOption('fixture');
  await page.getByRole('button', { name: '筛选', exact: true }).click();
  await page.locator('.memory-card').first().waitFor();
  const request = state.requests.filter(r => r.path === 'memory').at(-1).body;
  assert.deepEqual([request.adapter, request.profile, request.start, request.end], ['qq.napcat', 'fixture', '2026-11-17', '2026-11-18']);
  await page.getByRole('button', { name: '查看详情', exact: true }).first().click();
  const dialog = page.getByRole('dialog', { name: '记忆详情', exact: true });
  await dialog.getByText('Alpha.* 完整正文', { exact: false }).waitFor();
  assert.equal(await dialog.locator('img').count(), 0);
  await dialog.getByRole('button', { name: 'c1:1-2', exact: true }).click();
  await dialog.getByRole('button', { name: '原始导入内容', exact: true }).click();
  await dialog.getByText('原始导入 JSON', { exact: false }).waitFor();
  await dialog.getByRole('button', { name: '关闭', exact: true }).click();
  await page.getByRole('button', { name: 'Source', exact: true }).click();
  await page.getByText('Alpha.* 原始聊天正文', { exact: true }).waitFor();
  await workspace.getByRole('link', { name: '记忆导入', exact: true }).click();
  await page.getByRole('heading', { name: '来源', exact: true }).waitFor();
  assert.equal(await page.locator('.source-nav').getByRole('link', { name: '导入记录', exact: true }).count(), 1);
  await page.goBack();
  await page.getByRole('heading', { name: 'QQ', exact: true }).waitFor();
  assert.deepEqual(state.failures, []);
});

test('memory summary and literal scoped search remain usable on mobile', async t => {
  const { page, state } = await visit(t, '/manage/memory/summaries');
  await page.getByText('Alpha.* 日汇总', { exact: true }).waitFor();
  await page.getByRole('button', { name: '月 Summary', exact: true }).click();
  await page.getByText('Alpha.* 月汇总', { exact: true }).waitFor();
  await page.getByLabel('搜索记忆文字', { exact: true }).fill('Alpha.*');
  const search = page.getByRole('search');
  await search.getByLabel('Unit', { exact: true }).uncheck();
  await search.getByLabel('Source', { exact: true }).uncheck();
  await search.getByRole('button', { name: '搜索', exact: true }).click();
  await page.getByRole('heading', { name: '搜索结果', exact: true }).waitFor();
  await page.getByText('Alpha.* 主题记忆', { exact: true }).waitFor();
  const request = state.requests.filter(r => r.path === 'memory').at(-1).body;
  assert.deepEqual(request.layers, ['summary', 'runtime']);
  assert.equal(request.query, 'Alpha.*');
  const output = process.env.MEMULIFE_UI_REPORT_DIR || join(root, 'reports/local/memory-ui');
  await mkdir(output, { recursive: true });
  await page.screenshot({ path: join(output, 'memory-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await page.screenshot({ path: join(output, 'memory-mobile.png'), fullPage: true });
  await page.locator('.mobile-menu summary').click();
  await page.getByRole('navigation', { name: '切换记忆来源', exact: true }).getByRole('link', { name: '记忆主题', exact: true }).click();
  await page.getByRole('heading', { name: '记忆主题', exact: true }).waitFor();
  assert.deepEqual(state.failures, []);
});

test('deleted sources stay out of memory connection filters and retain historical provenance', async t => {
  let request;
  const deleted = { source_id: 'deleted-immich', adapter_id: 'immich.assets', namespace: '已删除的相册',
    profile: 'fixture', enabled: false, deleted_at: '2026-11-17T00:00:00Z' };
  const connected = { ...deleted, source_id: 'active-immich', namespace: '当前相册', enabled: true, deleted_at: null };
  const context = await browser.newContext();
  t.after(() => context.close());
  const page = await context.newPage();
  const state = await installFixtureAPI(page, fixture.origin);
  await page.route('**/v1/manage/imports/sources', route => route.fulfill({ json: { sources: [deleted, connected], profiles } }));
  await page.route('**/v1/manage/memory', route => {
    request = route.request().postDataJSON();
    return route.fulfill({ json: { items: [{ ref: 'u1:1', kind: 'unit', body: '保留的历史记忆', facts: [],
      origins: [{ source_instance_id: deleted.source_id, source_namespace: deleted.namespace, source_profile: 'fixture', object_id: '历史相册' }] }],
      next_cursor: null, formats: ['fixture'], timezone: 'Asia/Shanghai' } });
  });
  await page.goto(fixture.origin + '/manage/memory/immich?source=deleted-immich');
  await page.getByLabel('管理密钥', { exact: true }).fill(fixtureKey);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await page.getByText('保留的历史记忆', { exact: true }).waitFor();
  assert.deepEqual(await page.getByLabel('来源连接', { exact: true }).locator('option').allTextContents(), ['全部连接', '当前相册']);
  assert.equal(await page.locator('.source-nav [data-route="immich"] .nav-count').textContent(), '1');
  assert.equal(new URL(page.url()).searchParams.has('source'), false);
  assert.equal(request.source_id, '');
  assert.equal(await page.locator('.memory-card').getByText('已删除的相册（来源已删除）', { exact: true }).count(), 0);
  await page.route('**/v1/manage/memory/detail', route => route.fulfill({ json: { ref: 'u1:1', kind: 'unit', body: '保留的历史记忆', facts: [],
    origins: [{ source_instance_id: deleted.source_id, source_namespace: deleted.namespace, source_profile: 'fixture', object_id: '历史相册' }], next_offset: null } }));
  await page.getByRole('button', { name: '查看详情', exact: true }).click();
  await page.getByText('已删除的相册（来源已删除）', { exact: true }).waitFor();
  assert.equal(await page.getByText('已删除的相册（已断开）', { exact: true }).count(), 0);
  assert.deepEqual(state.failures, []);
});

test('memory media previews render images and players, retain Immich links and release resources', { timeout: 30000 }, async t => {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  t.after(() => context.close());
  const page = await context.newPage();
  const state = await installFixtureAPI(page, fixture.origin);
  await page.addInitScript(() => {
    window.releasedMedia = [];
    const revoke = URL.revokeObjectURL.bind(URL);
    URL.revokeObjectURL = url => { window.releasedMedia.push(url); revoke(url); };
  });
  const media = [
    { ref: 'a1', kind: 'native', mime_type: 'image/png', name: '聊天图片.png' },
    { ref: 'a2', kind: 'native', mime_type: 'audio/wav', name: '录音.wav' },
    { ref: 'a3', kind: 'native', mime_type: 'video/mp4', name: '视频.mp4' },
    { ref: 'c2:1-2', kind: 'immich', mime_type: 'video/*', name: 'Immich 视频' },
  ];
  const record = { ref: 'c1:1-2', kind: 'source', body: '包含多媒体的记忆', facts: [], origins: [], media, next_offset: null };
  await page.route('**/v1/manage/memory', route => route.fulfill({ json: { items: [record], next_cursor: null, formats: [] } }));
  await page.route('**/v1/manage/memory/detail', route => route.fulfill({ json: record }));
  const requested = [];
  const png = 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII=';
  const wav = Buffer.alloc(844); wav.write('RIFF'); wav.writeUInt32LE(836, 4); wav.write('WAVEfmt ', 8);
  wav.writeUInt32LE(16, 16); wav.writeUInt16LE(1, 20); wav.writeUInt16LE(1, 22); wav.writeUInt32LE(8000, 24);
  wav.writeUInt32LE(8000, 28); wav.writeUInt16LE(1, 32); wav.writeUInt16LE(8, 34); wav.write('data', 36); wav.writeUInt32LE(800, 40); wav.fill(128, 44);
  await page.route('**/v1/manage/memory/media', route => {
    const ref = route.request().postDataJSON().ref; requested.push(ref);
    return route.fulfill({ json: ref.startsWith('c') ? { kind: 'immich', open_url: 'https://photos.example/photos/asset-one', preview_error: 'immich_preview_permission_required' }
      : { kind: 'native', mime_type: media.find(item => item.ref === ref).mime_type,
          data_base64: ref === 'a1' ? png : ref === 'a2' ? wav.toString('base64') : 'AAAA' } });
  });
  await page.goto(fixture.origin + '/manage/memory');
  await page.getByLabel('管理密钥', { exact: true }).fill(fixtureKey);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await page.getByAltText('聊天图片.png', { exact: true }).waitFor();
  await page.waitForFunction(() => document.querySelector('.memory-media img')?.naturalWidth > 0);
  const link = page.getByRole('link', { name: '在 Immich 中查看', exact: true });
  await link.waitFor();
  assert.equal(await link.getAttribute('href'), 'https://photos.example/photos/asset-one');
  assert.ok(!requested.includes('a2') && !requested.includes('a3'));
  await page.getByRole('button', { name: '加载音频', exact: true }).click();
  await page.locator('audio[controls]').waitFor();
  await page.waitForFunction(() => document.querySelector('audio')?.duration > 0);
  await page.getByRole('button', { name: '加载视频', exact: true }).click();
  await page.locator('video[controls]').waitFor();
  await page.getByRole('button', { name: '查看详情', exact: true }).click();
  await page.locator('dialog img').waitFor();
  const detailUrl = await page.locator('dialog img').getAttribute('src');
  await page.getByRole('button', { name: '关闭', exact: true }).click();
  await page.waitForFunction(url => window.releasedMedia.includes(url), detailUrl);
  assert.ok(await page.evaluate(url => window.releasedMedia.includes(url), detailUrl));
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await page.getByRole('navigation', { name: '工作区', exact: true }).getByRole('link', { name: '记忆导入', exact: true }).click();
  assert.ok(await page.evaluate(() => window.releasedMedia.length >= 4));
  assert.deepEqual(state.failures, []);
});

test('memory cards use responsive columns, keep provenance in details and confirm linked deletion', { timeout: 30000 }, async t => {
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  t.after(() => context.close());
  const page = await context.newPage();
  const state = await installFixtureAPI(page, fixture.origin);
  const provenance = [{ source_instance_id: qq.source_id, source_namespace: '私人账户', object_id: 'secret-object-id', source_profile: 'secret-profile' }];
  const units = [
    { ref: 'u1:1', kind: 'unit', body: '第一条记忆正文', origins: provenance, facts: ['c1:1-9'] },
    { ref: 'u1:2', kind: 'unit', body: '第二条记忆\n' + '这段内容按自己的长度展开。\n'.repeat(10), origins: provenance, facts: [] },
  ];
  const source = { ref: 'c1:1-9', kind: 'source', body: '原始对话内容', origins: provenance, facts: [],
    display: { title: '讨论记录', mode: 'conversation', messages: [
      { role: 'user', name: '', body: '用户原话 <script>bad()</script>', time: fixedDate },
      { role: 'assistant', name: '', body: '助手回复', time: fixedDate },
    ] } };
  let removed = false, committed = null, previews = 0;
  await page.route('**/v1/manage/memory', route => {
    const body = route.request().postDataJSON();
    return route.fulfill({ json: { items: body.layers.includes('unit') ? units.filter((_, i) => !removed || i > 0) : removed ? [] : [source], next_cursor: null, formats: [] } });
  });
  await page.route('**/v1/manage/memory/detail', route => route.fulfill({ json: {
    ...(route.request().postDataJSON().ref.startsWith('u') ? units[0] : source), next_offset: null, revision: 'fixture' } }));
  await page.route('**/v1/manage/memory/delete-preview', route => {
    previews++;
    return route.fulfill({ json: { ref: 'u1:1', revision: 'checked-revision', counts: { unit: 1, source: 1, media: 0 }, views_to_rebuild: 3 } });
  });
  await page.route('**/v1/manage/memory/delete', route => {
    committed = route.request().postDataJSON(); removed = true;
    return route.fulfill({ json: { deleted: ['u1:1', 'c1:1-9'], pending_views: ['r1','r2','r3'] } });
  });
  await page.goto(fixture.origin + '/manage/memory');
  await page.getByLabel('管理密钥', { exact: true }).fill(fixtureKey);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  const cards = page.locator('.memory-card'); await cards.nth(1).waitFor();
  for (const forbidden of ['secret-object-id', 'secret-profile', '私人账户', 'u1:1', 'c1:1-9', qq.namespace]) {
    assert.ok(!(await cards.allTextContents()).join('').includes(forbidden));
  }
  assert.equal(await page.locator('.memory-records').evaluate(el => getComputedStyle(el).columnCount), '2');
  const firstBox = await cards.nth(0).boundingBox(), secondBox = await cards.nth(1).boundingBox();
  assert.ok(secondBox.x > firstBox.x && secondBox.height > firstBox.height, JSON.stringify({firstBox, secondBox}));
  await cards.first().getByRole('button', { name: '查看详情', exact: true }).click();
  await page.getByRole('dialog', { name: '记忆详情', exact: true }).getByText('secret-object-id', { exact: true }).waitFor();
  await page.getByRole('button', { name: '关闭', exact: true }).click();
  await page.getByRole('button', { name: 'Source', exact: true }).click();
  await page.getByText('用户原话 <script>bad()</script>', { exact: true }).waitFor();
  assert.equal(await page.locator('.memory-message[data-role=user]').count(), 1);
  assert.equal(await page.locator('.memory-message[data-role=assistant]').count(), 1);
  assert.equal(await page.locator('.memory-conversation script').count(), 0);
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.locator('.memory-records').evaluate(el => getComputedStyle(el).columnCount), '1');
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await page.getByRole('button', { name: 'Unit', exact: true }).click();
  await cards.first().getByRole('button', { name: '删除', exact: true }).click();
  const confirmation = page.getByRole('dialog', { name: '删除记忆', exact: true });
  await confirmation.getByText('将删除 1 条 Unit、1 段 Source。', { exact: true }).waitFor();
  assert.equal(committed, null);
  await confirmation.getByRole('button', { name: '取消', exact: true }).click();
  await cards.first().getByRole('button', { name: '删除', exact: true }).click();
  await confirmation.getByRole('button', { name: '确认删除', exact: true }).click();
  await page.waitForFunction(() => document.querySelectorAll('.memory-card').length === 1);
  assert.equal(previews, 2);
  assert.deepEqual(committed, { ref: 'u1:1', revision: 'checked-revision' });
  assert.ok((await cards.first().textContent()).includes('第二条记忆'));
  assert.deepEqual(state.failures, []);
});

test('original attachments download intact and management omits explanatory captions', { timeout: 30000 }, async t => {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 }, acceptDownloads: true });
  t.after(() => context.close());
  const page = await context.newPage();
  const state = await installFixtureAPI(page, fixture.origin);
  const raw = Buffer.from('original-workbook-fixture\r\n');
  const file = { ref: 'c1:1-2', kind: 'file', file_id: 'original', name: '原始附件.xlsx',
    mime_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', size_bytes: raw.length };
  const item = { ref: file.ref, kind: 'source', body: 'xl/worksheets/sheet1.xml\n拆开的内容', origins: [], facts: [],
    display: { mode: 'attachment', title: file.name, messages: [] }, media: [file], next_offset: null };
  await page.route('**/v1/manage/memory', route => route.fulfill({ json: { items: [item], next_cursor: null, formats: [] } }));
  await page.route('**/v1/manage/memory/detail', route => route.fulfill({ json: item }));
  let loads = 0;
  await page.route('**/v1/manage/memory/file', route => {
    loads++;
    assert.deepEqual(route.request().postDataJSON(), { ref: file.ref, file_id: 'original' });
    return route.fulfill({ json: { ...file, data_base64: raw.toString('base64') } });
  });
  await page.goto(fixture.origin + '/manage/memory/files?layer=source');
  await page.getByLabel('管理密钥', { exact: true }).fill(fixtureKey);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await page.getByText(file.name, { exact: true }).waitFor();
  assert.equal(loads, 0);
  assert.equal(await page.getByText('拆开的内容', { exact: false }).count(), 0);
  for (const text of ['按连续文字匹配', '按记忆发生日期筛选', '日期未知的数据仅在未限定日期时显示', 'Immich 引用', '原件保留在来源']) {
    assert.equal(await page.getByText(text, { exact: false }).count(), 0);
  }
  const [download] = await Promise.all([page.waitForEvent('download'), page.getByRole('button', { name: '下载附件', exact: true }).click()]);
  assert.equal(download.suggestedFilename(), file.name);
  const chunks = []; for await (const chunk of await download.createReadStream()) chunks.push(chunk);
  assert.deepEqual(Buffer.concat(chunks), raw);
  assert.equal(loads, 1);
  await page.getByRole('button', { name: '查看详情', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: '记忆详情', exact: true });
  await dialog.getByRole('button', { name: '附件', exact: true }).waitFor();
  assert.equal(await dialog.locator('.memory-full-text').count(), 0);
  await dialog.getByRole('button', { name: '提取文本', exact: true }).click();
  await dialog.getByText('xl/worksheets/sheet1.xml', { exact: false }).waitFor();
  assert.deepEqual(state.failures, []);
});

test('memory editor saves Unit summaries and topic entries with protection and handles conflicts', { timeout: 30000 }, async t => {
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  t.after(() => context.close());
  const page = await context.newPage();
  page.setDefaultTimeout(5000);
  const state = await installFixtureAPI(page, fixture.origin);
  let kind = 'unit', saved = false, conflict = false;
  const writes = [];
  const current = () => ({ ref: kind === 'unit' ? 'u1:1' : 'r1', kind, scope: '项目',
    body: saved ? '修改后的正文' : '原始完整正文', protected: saved, protected_count: saved ? 1 : 0, facts: ['u2:1'], origins: [] });
  await page.route('**/v1/manage/memory', route => route.fulfill({ json: { items: [current()], formats: [], next_cursor: null } }));
  await page.route('**/v1/manage/topics', route => route.fulfill({ json: { topics: [{ ...current(), key: '项目', description: '范围', unit_count: 1, state: 'done' }], discovery: { state: 'done' } } }));
  await page.route('**/v1/manage/memory/edit', route => {
    const item = current();
    return route.fulfill({ json: { ...item, entries: [{ body: item.body, edit_revision: saved ? 'version-two' : 'version-one', protected: saved,
      ...(kind === 'unit' ? {} : { entry_id: 'stable-entry' }) }, ...(kind === 'unit' ? [] : [{ body: '另一条正文', entry_id: 'other-entry', edit_revision: 'other-version', protected: false }])] } });
  });
  await page.route('**/v1/manage/memory/revise', route => {
    writes.push(route.request().postDataJSON());
    if (conflict) return route.fulfill({ status: 409, json: { code: 'memory_edit_conflict', result: { conflicts: [{ current: { body: '他人刚保存的内容' } }] } } });
    saved = true;
    return route.fulfill({ json: { status: 'done', views: 'queued', index: 'queued' } });
  });
  await page.goto(fixture.origin + '/manage/memory');
  await page.getByLabel('管理密钥', { exact: true }).fill(fixtureKey);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  for (const target of ['unit', 'day', 'month', 'runtime']) {
    kind = target; saved = false; conflict = false;
    if (kind !== 'unit') {
      await page.goto(fixture.origin + (kind === 'runtime' ? '/manage/memory/topics' : '/manage/memory/summaries?layer=' + kind));
      await page.getByLabel('管理密钥', { exact: true }).fill(fixtureKey);
      await page.getByRole('button', { name: '登录', exact: true }).click();
    }
    const edit = page.getByRole('button', { name: kind === 'runtime' ? '编辑记忆' : '编辑', exact: true });
    await edit.click();
    const dialog = page.getByRole('dialog', { name: '编辑记忆', exact: true });
    const body = dialog.getByLabel('记忆正文', { exact: true });
    assert.equal(await body.inputValue(), '原始完整正文');
    if (kind !== 'unit') {
      await dialog.getByLabel('选择条目', { exact: true }).selectOption('1');
      assert.equal(await body.inputValue(), '另一条正文');
      await dialog.getByLabel('选择条目', { exact: true }).selectOption('0');
    }
    await body.fill('修改后的正文');
    await dialog.getByRole('button', { name: '保存并保护', exact: true }).click();
    await dialog.waitFor({ state: 'hidden' });
    await page.getByText('已保护', { exact: true }).waitFor();
    const write = writes.at(-1), change = write.changes[0];
    assert.ok(write.reason && write.idempotency_key);
    assert.equal(change.old_text, '原始完整正文');
    assert.equal(change.new_text, '修改后的正文');
    assert.equal(change.expected_revision, 'version-one');
    assert.equal(change.entry_id, kind === 'unit' ? undefined : 'stable-entry');
    assert.equal(change.protect, true);
    if (kind === 'unit') {
      conflict = true;
      await edit.click();
      await body.fill('冲突时保留的草稿');
      await dialog.getByRole('button', { name: '保存并保护', exact: true }).click();
      await dialog.getByRole('button', { name: '重新读取', exact: true }).waitFor();
      assert.equal(await body.inputValue(), '冲突时保留的草稿');
      assert.equal(writes.at(-1).changes[0].allow_protected, true);
      assert.equal(writes.at(-1).changes[0].expected_revision, 'version-two');
      await dialog.getByText('查看最新内容', { exact: true }).click();
      await dialog.getByText('他人刚保存的内容', { exact: true }).waitFor();
      await dialog.getByRole('button', { name: '取消', exact: true }).click();
    }
  }
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: '编辑记忆', exact: true }).click();
  await page.getByLabel('记忆正文', { exact: true }).waitFor();
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  if (process.env.MEMULIFE_UI_REPORT_DIR) {
    await mkdir(process.env.MEMULIFE_UI_REPORT_DIR, { recursive: true });
    await page.screenshot({ path: join(process.env.MEMULIFE_UI_REPORT_DIR, 'memory-editor-mobile.png') });
  }
  assert.deepEqual(state.failures, []);
});
