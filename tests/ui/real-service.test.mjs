import assert from 'node:assert/strict';
import { test } from 'node:test';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { chromium } from 'playwright';
import { resolve } from 'node:path';
import { mkdir } from 'node:fs/promises';

test('webpage configures a new source then hands only selected raw material to the complete core pipeline', { timeout: 30000 }, async t => {
  const root = resolve(import.meta.dirname, '../..');
  const child = spawn(process.env.MEMU_TEST_PYTHON || resolve(root, '.venv/bin/python'),
    ['tests/acquisition_ui_fixture.py'], { cwd: root, env: { ...process.env, PYTHONPATH: root },
      stdio: ['ignore', 'pipe', 'pipe'] });
  t.after(() => child.kill('SIGTERM'));
  const lines = createInterface({ input: child.stdout });
  let resolveReady, rejectReady, resolveDone, rejectDone, errors = '';
  const ready = new Promise((resolve, reject) => { resolveReady = resolve; rejectReady = reject; });
  const done = new Promise((resolve, reject) => { resolveDone = resolve; rejectDone = reject; });
  done.catch(() => {}); // Failure may arrive before the submit button finishes navigating.
  child.stderr.on('data', value => { errors += value.toString(); });
  child.on('exit', code => rejectReady(new Error(`Fixture exited ${code}: ${errors}`)));
  lines.on('line', line => {
    const value = JSON.parse(line);
    if (value.event === 'ready') resolveReady(value);
    if (value.event === 'completed') resolveDone(value);
    if (value.event === 'failed') rejectDone(new Error(JSON.stringify(value)));
  });
  const service = await ready;
  t.diagnostic('Acquisition service ready');
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox', '--disable-dev-shm-usage'],
    ...(process.env.MEMULIFE_CHROMIUM ? { executablePath: process.env.MEMULIFE_CHROMIUM } : {}) });
  t.after(() => browser.close());
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  const failures = [];
  page.on('pageerror', error => failures.push(error.message));
  await page.goto(service.origin + '/manage/imports/files');
  await page.locator('#token').fill(service.token);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  t.diagnostic('Logged in to the empty source catalog');
  await page.getByRole('button', { name: '添加来源', exact: true }).click();
  t.diagnostic('Source form opened');
  const catalog = await (await page.request.get(service.origin + '/v1/manage/imports/setup', {
    headers: { Authorization: 'Bearer ' + service.token },
  })).json();
  for (const adapter of catalog.adapters) {
    await page.getByLabel('来源类型', { exact: true }).selectOption(adapter.adapter_id);
    for (const profile of adapter.profiles) {
      await page.getByLabel('来源格式', { exact: true }).selectOption(profile.id);
      assert.equal(await page.locator('.setup-card [role=alert]').isVisible(), false);
      assert.ok(await page.locator('.setup-fields input, .setup-fields textarea, .setup-fields select').count());
    }
  }
  await page.getByLabel('来源类型', { exact: true }).selectOption('notion.workspace');
  await page.locator('[data-config-field=workspace_id]').fill('synthetic-workspace');
  await page.locator('[data-config-field=page_ids]').fill('page-one\npage-two');
  const secret = page.locator('[data-config-field=token_ref]');
  assert.equal(await secret.getAttribute('type'), 'password');
  await secret.fill('SYNTHETIC_TOKEN_ONLY');
  let submitted;
  await page.route('**/v1/manage/imports/sources', async route => {
    submitted = route.request().postDataJSON();
    await route.fulfill({ status: 400, contentType: 'application/json', body: '{"error":"fixture_validation_only"}' });
  });
  await page.getByRole('button', { name: '保存并选择数据', exact: true }).click();
  await page.locator('.setup-card [role=alert]').waitFor();
  assert.equal(submitted.credentials.token_ref, 'SYNTHETIC_TOKEN_ONLY');
  assert.equal(submitted.source.config.token_ref, undefined);
  assert.deepEqual(submitted.source.config.page_ids, ['page-one', 'page-two']);
  await page.unroute('**/v1/manage/imports/sources');
  await page.getByLabel('来源类型', { exact: true }).selectOption('filesystem.typora');
  await page.getByLabel('来源名称', { exact: true }).fill('网页新增文件来源');
  await page.getByLabel('数据目录 *', { exact: true }).fill(service.notes);
  t.diagnostic('Source configuration filled');
  const output = process.env.MEMULIFE_UI_REPORT_DIR || '/tmp/memu-acquisition-ui';
  await mkdir(output, { recursive: true });
  await page.screenshot({ path: resolve(output, 'source-setup-files.png'), fullPage: true });
  await page.setViewportSize({ width: 360, height: 900 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await page.screenshot({ path: resolve(output, 'source-setup-files-mobile.png'), fullPage: true });
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.getByRole('button', { name: '保存并选择数据', exact: true }).click();
  await page.getByRole('checkbox', { name: '选择 selected.md', exact: true }).check();
  await page.getByRole('button', { name: '核对范围', exact: true }).click();
  await page.getByRole('button', { name: '开始导入', exact: true }).click();
  t.diagnostic('Selected import submitted through the real API');
  const completion = await done; // Completion event; no status polling.
  assert.deepEqual(completion.materials, ['UI_SELECTED_ORIGINAL']);
  assert.equal(completion.model_calls.unit, 1);
  assert.equal(completion.model_calls.relate, 1);
  assert.ok(completion.model_calls.rollup > 0);
  t.diagnostic('Source, Unit, relation and view stages completed');
  await page.getByRole('button', { name: '查看导入记录', exact: true }).click();
  await page.getByText('已完成', { exact: true }).waitFor();
  await page.getByRole('button', { name: '查看详情', exact: true }).click();
  await page.getByRole('heading', { name: '记忆内容', exact: true }).waitFor();
  assert.deepEqual(failures, []);
  assert.equal(errors, '');
  await page.screenshot({ path: resolve(output, 'real-service-completed.png'), fullPage: true });
});
