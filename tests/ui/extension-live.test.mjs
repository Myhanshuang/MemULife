import assert from 'node:assert/strict';
import { test } from 'node:test';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { mkdtemp, cp, readFile, writeFile, rm } from 'node:fs/promises';
import { resolve } from 'node:path';
import { tmpdir } from 'node:os';
import { chromium } from 'playwright';

test('installed extension pairs and delivers selected raw data over real HTTP to the full memory core', { timeout: 45000 }, async t => {
  const root = resolve(import.meta.dirname, '../..');
  const child = spawn(resolve(root, '.venv/bin/python'), ['tests/acquisition_ui_fixture.py'],
    { cwd: root, env: { ...process.env, PYTHONPATH: root }, stdio: ['ignore', 'pipe', 'pipe'] });
  t.after(() => child.kill('SIGTERM'));
  let readyResolve, readyReject, doneResolve, doneReject, stderr = '';
  const ready = new Promise((resolve, reject) => { readyResolve = resolve; readyReject = reject; });
  const done = new Promise((resolve, reject) => { doneResolve = resolve; doneReject = reject; });
  done.catch(() => {});
  child.stderr.on('data', data => { stderr += data; });
  child.on('exit', code => { readyReject(Error(`fixture exited ${code}: ${stderr}`)); });
  createInterface({ input: child.stdout }).on('line', line => {
    const event = JSON.parse(line);
    if (event.event === 'ready') readyResolve(event);
    if (event.event === 'completed') doneResolve(event);
    if (event.event === 'failed') doneReject(Error(JSON.stringify(event)));
  });
  const service = await ready;
  const scratch = await mkdtemp(resolve(tmpdir(), 'memu-extension-live-'));
  t.after(() => rm(scratch, { recursive: true, force: true }));
  const extension = resolve(scratch, 'extension');
  await cp(resolve(root, 'browser-extension/dist'), extension, { recursive: true });
  // Test-only grant in an isolated browser profile; no user browser permissions are changed.
  const manifest = JSON.parse(await readFile(resolve(extension, 'manifest.json')));
  manifest.host_permissions = ['http://127.0.0.1/*', 'https://gemini.google.com/*'];
  await writeFile(resolve(extension, 'manifest.json'), JSON.stringify(manifest));
  // Replace only the upstream reader in this temporary extension. The bridge, permission checks,
  // pairing, browser HTTP transport, IndexedDB and core pipeline run their real implementations.
  const backgroundPath = resolve(extension, 'src/background.js');
  let background = await readFile(backgroundPath, 'utf8');
  background = background.replace(/const createProvider = [\s\S]*?;\n/, `const createProvider = id => ({
    accounts: async () => [{ provider: id, account_id: 'fixture-account', workspace_id: 'default', label: '合成账号', identity_strength: 'native' }],
    assertBinding: async () => {},
    list: async () => ({ items: [{ id: 'c_selected', locator: 'c_selected', revision: 'r1', title: '合成 Gemini 会话', provider: 'gemini' }], cursor: null, warnings: [] }),
    detail: async () => {
      const turn = [['c_selected', 'r_turn'], null, [['USER_ORIGINAL']], [[['rc_reply', ['ASSISTANT_ORIGINAL']]], null, [[1, null, '', 'rc_reply', [1791873040, 123000000]]]], [1791873000, 456000000]];
      const frames = [[[turn], null]];
      return { data: { frames }, raw: JSON.stringify(frames), cursor: null };
    }
  });\n`);
  await writeFile(backgroundPath, background);
  const browser = await chromium.launchPersistentContext(resolve(scratch, 'profile'), {
    headless: true, ignoreDefaultArgs: ['--disable-extensions'],
    args: ['--no-sandbox', '--disable-dev-shm-usage', `--disable-extensions-except=${extension}`, `--load-extension=${extension}`],
    ...(process.env.MEMULIFE_CHROMIUM ? { executablePath: process.env.MEMULIFE_CHROMIUM } : {}),
  });
  t.after(() => browser.close());
  const worker = browser.serviceWorkers()[0] || await browser.waitForEvent('serviceworker');
  const page = await browser.newPage();
  const errors = []; page.on('pageerror', error => errors.push(error.message));
  await page.goto(service.origin + '/manage/imports/ai/gemini');
  await page.locator('#token').fill(service.token);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  assert.equal(await page.locator('.app-header nav').count(), 0);
  await page.getByRole('button', { name: '添加来源', exact: true }).click();
  await page.getByRole('button', { name: '检测已登录账号', exact: true }).click();
  await page.getByText('已检测到 1 个账号', { exact: true }).waitFor();
  assert.equal(await page.getByLabel('扩展 ID', { exact: true }).count(), 0);
  assert.equal(await page.getByLabel('一次性配对码', { exact: true }).count(), 0);
  await page.getByRole('button', { name: '连接并选择数据', exact: true }).click();
  await page.getByRole('button', { name: '合成 Gemini 会话', exact: true }).click();
  await page.getByText('USER_ORIGINAL', { exact: true }).waitFor();
  await page.getByText('ASSISTANT_ORIGINAL', { exact: true }).waitFor();
  assert.equal(await page.getByLabel('来源账号', { exact: true }).count(), 0);
  const output = '/tmp/memu-acquisition-ui';
  await page.screenshot({ path: output + '/connector-conversation.png', fullPage: true });
  await page.setViewportSize({ width: 360, height: 900 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await page.screenshot({ path: output + '/connector-conversation-mobile.png', fullPage: true });
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.getByRole('button', { name: '完整会话', exact: true }).click();
  await page.getByRole('button', { name: '选择完整会话', exact: true }).click();
  await page.getByRole('button', { name: '核对范围', exact: true }).click();
  await page.getByRole('button', { name: '开始导入', exact: true }).click();
  const completion = await done;
  assert.ok(completion.materials[0].includes('USER_ORIGINAL'));
  assert.ok(completion.materials[0].includes('AI 助手'));
  assert.ok(!completion.materials[0].includes('responses'));
  assert.ok(completion.model_calls.unit > 0);
  assert.ok(completion.model_calls.relate > 0);
  await page.getByRole('button', { name: '查看导入记录', exact: true }).click();
  await page.getByText('已完成', { exact: true }).waitFor();
  await page.getByRole('button', { name: '查看详情', exact: true }).click();
  await page.getByRole('heading', { name: '记忆内容', exact: true }).waitFor();
  await page.getByText(/原始材料 ·/).click();
  await page.screenshot({ path: output + '/connector-import-detail.png', fullPage: true });
  // A same-origin page without a server-issued management ticket cannot ask for account data.
  const denied = await page.evaluate(async () => {
    const { connector } = await import('/manage/assets/imports/connector.js');
    try { await connector('probe', { provider: 'gemini', code: 'not-authorized' }); return false; }
    catch { return true; }
  });
  assert.equal(denied, true);
  assert.deepEqual(errors, []);
});
