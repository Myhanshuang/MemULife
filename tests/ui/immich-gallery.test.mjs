import assert from 'node:assert/strict';
import test from 'node:test';
import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { resolve, extname } from 'node:path';
import { chromium } from 'playwright';

test('Immich albums open a compact gallery with scoped selection and date refresh', { timeout: 30000 }, async t => {
  const root = resolve(import.meta.dirname, '../../memu/acquisition/ui');
  const server = createServer(async (req, res) => {
    if (req.url === '/') {
      res.setHeader('Content-Type', 'text/html');
      return res.end('<link rel="stylesheet" href="/style.css"><div id="root"></div>');
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
  const page = await browser.newPage({ viewport: { width: 1500, height: 950 } });
  const errors = []; page.on('pageerror', e => errors.push(e.message));
  await page.goto(`http://127.0.0.1:${server.address().port}/`);
  await page.evaluate(async () => {
    const { mountImportPage } = await import('/page.js');
    const source = { source_id: 'immich-test', adapter_id: 'immich.assets', profile: 'api-key', namespace: 'Immich', enabled: true };
    const albums = [{ id: 'album_ids:camera', name: 'Camera', kind: 'album', asset_count: 8 },
      { id: 'album_ids:trip', name: '旅行', kind: 'album', asset_count: 1 }];
    const colors = ['#c7dbed', '#edceb2', '#b8d8c5', '#d4d1ea'];
    const photos = Array.from({ length: 8 }, (_, i) => ({ id: `asset_ids:photo-${i}`, name: `IMG_000${i + 1}.jpg`,
      kind: 'asset', media_type: i % 3 ? 'IMAGE' : 'VIDEO', duration: '00:00:24.000', taken_at: '2026-11-16T12:00:00Z',
      thumbnail: 'data:image/svg+xml,' + encodeURIComponent(`<svg xmlns="http://www.w3.org/2000/svg" width="400" height="400"><rect width="400" height="400" fill="${colors[i % 4]}"/><circle cx="300" cy="100" r="35" fill="#fff3c4"/><path d="M0 340L120 170 230 290 300 230 400 340V400H0Z" fill="#70978b"/></svg>`),
    }));
    window.calls = []; window.slow = false;
    const client = async (path, method, body) => {
      window.calls.push({ path, method, body });
      if (path === 'imports/sources') return { profiles: [], sources: [source] };
      if (path.includes('/candidates')) return { items: albums, next_cursor: null };
      if (path.endsWith('/preview')) {
        const id = body.selection.scopes[0];
        if (window.slow && id === 'album_ids:camera') await new Promise(resolve => { window.releaseOld = resolve; });
        return { items: id === 'album_ids:trip' ? [{ ...photos[0], id: 'asset_ids:trip', name: '旅行照片.jpg' }]
          : body.selection.start ? photos.slice(0, 2) : photos, next_cursor: null, content_persisted: false };
      }
      return { ...source, binding_available: true, page_kind: 'media', time_filter: true, scopes: [] };
    };
    window.app = await mountImportPage(document.querySelector('#root'), { client, route: 'immich' });
  });
  await page.getByRole('button', { name: 'Camera', exact: true }).click();
  await page.getByRole('img', { name: 'IMG_0001.jpg', exact: true }).waitFor();
  assert.equal(await page.locator('.media-tile').count(), 8);
  assert.equal(await page.getByRole('button', { name: '浏览照片与视频' }).count(), 0);
  assert.equal(await page.locator('.media-detail .metadata').count(), 0);
  assert.equal(await page.locator('.candidate-list [data-item]').count(), 2);
  const detail = await page.locator('.media-detail').boundingBox();
  const dates = await page.locator('.media-detail .date-fields').boundingBox();
  const gallery = await page.locator('.media-gallery').boundingBox();
  assert.ok(dates.y - detail.y < 100 && gallery.y - dates.y - dates.height < 65);
  await page.getByRole('checkbox', { name: '选择 IMG_0001.jpg', exact: true }).check();
  await page.getByText('已选 1 项', { exact: true }).waitFor();
  await page.locator('.media-gallery-toolbar').getByRole('button', { name: '全选已加载' }).click();
  await page.getByText('已选 8 项', { exact: true }).waitFor();
  await page.screenshot({ path: '/tmp/memu-immich-gallery.png', fullPage: true });
  await page.getByLabel('开始日期', { exact: true }).fill('2026-11-16');
  await page.waitForFunction(() => document.querySelectorAll('.media-tile').length === 2);
  assert.equal(await page.getByText('已选 8 项', { exact: true }).count(), 1);
  assert.ok(await page.evaluate(() => window.calls.some(c => c.body?.selection?.start)));
  await page.evaluate(() => { window.slow = true; });
  await page.getByRole('button', { name: '刷新预览', exact: true }).click();
  await page.waitForFunction(() => Boolean(window.releaseOld));
  await page.getByRole('button', { name: '旅行', exact: true }).click();
  await page.getByRole('img', { name: '旅行照片.jpg', exact: true }).waitFor();
  await page.evaluate(() => window.releaseOld());
  assert.equal(await page.locator('.media-tile').count(), 1);
  assert.equal(await page.getByRole('img', { name: 'IMG_0001.jpg', exact: true }).count(), 0);
  assert.equal(await page.locator('.candidate-list [data-item]').count(), 2);
  assert.deepEqual(errors, []);
});
