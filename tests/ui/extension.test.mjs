import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createServer } from 'node:http';
import { readFile, mkdir } from 'node:fs/promises';
import { resolve, extname } from 'node:path';
import { chromium } from 'playwright';
test('connector has only workspace entry and platform permission UI in the shared style', { timeout: 30000 }, async t => {
  const root = resolve(import.meta.dirname, '../../browser-extension/dist');
  const server = createServer(async (req, res) => {
    const path = new URL(req.url, 'http://localhost').pathname;
    const file = resolve(root, '.' + path);
    if (!file.startsWith(root + '/')) { res.writeHead(403).end(); return; }
    try { const data = await readFile(file); res.setHeader('Content-Type', ({ '.js': 'text/javascript', '.css': 'text/css', '.html': 'text/html' })[extname(file)] || 'text/plain'); res.end(data); }
    catch { res.writeHead(404).end(); }
  });
  await new Promise(r => server.listen(0, '127.0.0.1', r)); t.after(() => new Promise(r => server.close(r)));
  const browser = await chromium.launch({ headless: true, executablePath: process.env.MEMULIFE_CHROMIUM, args: ['--no-sandbox'] }); t.after(() => browser.close());
  const page = await browser.newPage({ viewport: { width: 980, height: 700 } });
  const errors = []; page.on('pageerror', e => errors.push(e.message));
  await page.addInitScript(() => {
    window.calls = [];
    window.chrome = { permissions: { request: async p => { window.calls.push(p); return true; } }, runtime: { sendMessage: async m => { window.calls.push(m); return { ok: true }; } } };
    window.close = () => {};
  });
  const url = `http://127.0.0.1:${server.address().port}/options.html`;
  await page.goto(url);
  await page.getByRole('heading', { name: '浏览器连接器', exact: true }).waitFor();
  assert.equal(await page.locator('input,select').count(), 0);
  const output = '/tmp/memu-acquisition-ui'; await mkdir(output, { recursive: true });
  await page.screenshot({ path: output + '/connector-home.png', fullPage: true });
  await page.goto(url + '?authorize=synthetic-ticket&provider=gemini');
  await page.getByRole('button', { name: '授权读取', exact: true }).click();
  await page.getByText('授权成功。请回采集网页选择账号。', { exact: true }).waitFor();
  assert.equal((await page.evaluate(() => window.calls))[0].origins[0], 'https://gemini.google.com/*');
  await page.setViewportSize({ width: 360, height: 650 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
  await page.screenshot({ path: output + '/connector-permission.png', fullPage: true });
  assert.deepEqual(errors, []);
});
