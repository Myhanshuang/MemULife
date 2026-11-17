import assert from 'node:assert/strict';
import test from 'node:test';
import { createServer } from 'node:http';
import { readFile, mkdir } from 'node:fs/promises';
import { resolve, extname } from 'node:path';
import { chromium } from 'playwright';

test('account refresh, scoped completion and connection editing share the source UI', { timeout: 30000 }, async t => {
  const root = resolve(import.meta.dirname, '../../memu/acquisition/ui');
  const server = createServer(async (req, res) => {
    if (req.url === '/') {
      res.setHeader('Content-Type', 'text/html');
      return res.end('<link rel="stylesheet" href="/style.css"><link rel="stylesheet" href="/setup.css"><div id="root"></div>');
    }
    const path = resolve(root, '.' + new URL(req.url, 'http://localhost').pathname);
    if (!path.startsWith(root + '/')) return res.writeHead(404).end();
    try {
      res.setHeader('Content-Type', extname(path) === '.js' ? 'text/javascript' : 'text/css');
      res.end(await readFile(path));
    } catch { res.writeHead(404).end(); }
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r));
  t.after(() => new Promise(r => server.close(r)));
  const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'],
    ...(process.env.MEMULIFE_CHROMIUM ? { executablePath: process.env.MEMULIFE_CHROMIUM } : {}) });
  t.after(() => browser.close());
  const page = await browser.newPage({ viewport: { width: 1280, height: 850 } });
  const errors = []; page.on('pageerror', e => errors.push(e.message));
  await page.goto(`http://127.0.0.1:${server.address().port}/`);
  await page.evaluate(async () => {
    const { mountImportPage } = await import('/page.js');
    const sources = ['gemini', 'chatgpt'].flatMap(profile => ['A', 'B'].map(account => ({
      source_id: `${profile}-${account}`, adapter_id: 'browser.ai-history', profile,
      namespace: `${profile} 账号 ${account}`, enabled: true,
    })));
    window.calls = []; window.available = {}; window.connectionErrors = {};
    window.progress = source => window.postMessage({ channel: 'memu-connector-progress', source_ids: [source] }, location.origin);
    addEventListener('message', event => {
      const request = event.data;
      if (request?.channel !== 'memu-connector-request') return;
      const source = request.payload.source_id;
      window.calls.push({ action: request.action, source });
      const result = request.action === 'status'
        ? { bindings: [{ source_id: source, status: 'partial_coverage', last_error: window.connectionErrors[source] || null }] }
        : { scheduled: true };
      window.postMessage({ channel: 'memu-connector-response', id: request.id, ok: true, result }, location.origin);
    });
    const client = async (path, method = 'GET', body) => {
      window.calls.push({ path, method, body });
      if (path === 'imports/sources') return { profiles: [], sources };
      const source = sources.find(s => path.startsWith(`imports/sources/${s.source_id}`));
      if (path.endsWith('/delete')) { source.enabled = false; return { source_id: source.source_id, deleted: true }; }
      if (path.includes('/candidates')) return { items: window.available[source.source_id] ? [{
        id: source.source_id + '-conversation', title: source.namespace + ' 会话', kind: 'conversation',
      }] : [], next_cursor: null };
      if (method === 'POST') { source.namespace = body.name; return { source_id: source.source_id, namespace: source.namespace }; }
      return { ...source, binding_available: true, page_kind: 'conversations', time_filter: false, scopes: [] };
    };
    window.app = await mountImportPage(document.querySelector('#root'), { client, route: 'ai/gemini' });
  });
  const picker = page.getByRole('combobox', { name: '来源账号' });
  for (const provider of ['gemini', 'chatgpt']) {
    if (provider !== 'gemini') await page.evaluate(() => window.app.navigate('ai/chatgpt'));
    await picker.selectOption(`${provider}-B`);
    await page.getByRole('button', { name: '刷新会话目录', exact: true }).click();
    await page.waitForFunction(provider => window.calls.some(call => call.action === 'sync' && call.source === `${provider}-B`), provider);
    await page.evaluate(provider => { window.available[`${provider}-B`] = true; window.progress(`${provider}-A`); }, provider);
    await page.getByText('正在读取会话目录…', { exact: true }).waitFor();
    assert.equal(await page.getByRole('button', { name: `${provider} 账号 B 会话`, exact: true }).count(), 0);
    await page.evaluate(provider => window.progress(`${provider}-B`), provider);
    await page.getByRole('button', { name: `${provider} 账号 B 会话`, exact: true }).waitFor();
    await page.getByText('会话目录已刷新', { exact: true }).waitFor();
    await page.evaluate(provider => { window.connectionErrors[`${provider}-B`] = 'account_changed'; window.progress(`${provider}-B`); }, provider);
    await page.getByText('平台账号已变化，请重新检测账号。', { exact: true }).waitFor();
    await page.evaluate(provider => { window.connectionErrors[`${provider}-B`] = null; window.progress(`${provider}-B`); }, provider);
  }
  await page.getByRole('button', { name: '编辑连接', exact: true }).click();
  await page.getByLabel('连接名称', { exact: true }).fill('工作账号');
  await page.getByRole('button', { name: '保存更改', exact: true }).click();
  await page.getByRole('option', { name: '工作账号', exact: true }).waitFor({ state: 'attached' });
  assert.equal(await picker.inputValue(), 'chatgpt-B');
  const edit = page.getByRole('button', { name: '编辑连接', exact: true });
  const left = await picker.boundingBox(), right = await edit.boundingBox();
  assert.ok(left.x + left.width < right.x);
  assert.ok(Math.abs(left.y + left.height - right.y - right.height) < 2);
  const output = resolve(import.meta.dirname, '../../.local/ui-account-switch'); await mkdir(output, { recursive: true });
  await page.screenshot({ path: output + '/desktop.png', fullPage: true });
  await page.setViewportSize({ width: 360, height: 800 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await page.screenshot({ path: output + '/mobile.png', fullPage: true });
  await page.getByRole('button', { name: '编辑连接', exact: true }).click();
  await page.screenshot({ path: output + '/delete-mobile.png', fullPage: true });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  page.once('dialog', dialog => dialog.dismiss());
  await page.getByRole('button', { name: '删除连接', exact: true }).click();
  assert.equal((await page.evaluate(() => window.calls)).filter(call => call.path?.endsWith('/delete')).length, 0);
  page.once('dialog', dialog => dialog.accept());
  await page.getByRole('button', { name: '删除连接', exact: true }).click();
  await picker.waitFor();
  assert.equal(await picker.inputValue(), 'chatgpt-A');
  assert.equal(await page.getByRole('option', { name: '工作账号', exact: true }).count(), 0);
  await page.getByRole('button', { name: '编辑连接', exact: true }).click();
  page.once('dialog', dialog => dialog.accept());
  await page.getByRole('button', { name: '删除连接', exact: true }).click();
  await page.getByRole('heading', { name: '尚未连接来源', exact: true }).waitFor();
  assert.deepEqual(errors, []);
});
