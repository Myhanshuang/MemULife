import assert from 'node:assert/strict';
import { test } from 'node:test';
import { configureService, configuredService, managementOrigin, registerManagement } from '../dist/src/service.js';

function browser(t) {
  const previous = globalThis.chrome;
  t.after(() => { globalThis.chrome = previous; });
  const stored = { 'memu-web-connections': { account: { url: 'http://localhost:5073', token: 'SYNTHETIC_OLD', source_id: 'old' } } };
  const scripts = new Map();
  const allowed = new Set();
  globalThis.chrome = {
    storage: { local: { get: async () => stored, set: async value => Object.assign(stored, value) } },
    permissions: { contains: async ({ origins }) => origins.every(origin => allowed.has(origin)) },
    scripting: {
      getRegisteredContentScripts: async () => [...scripts.values()],
      registerContentScripts: async values => values.forEach(value => scripts.set(value.id, value)),
      updateContentScripts: async values => values.forEach(value => scripts.set(value.id, value)),
      unregisterContentScripts: async ({ ids }) => ids.forEach(id => scripts.delete(id)),
    },
  };
  return { stored, scripts, allowed };
}

test('HTTPS management needs an explicit grant and exact configured origin, including port', async t => {
  const { stored, scripts, allowed } = browser(t);
  await assert.rejects(configureService('https://memory.example:9443'), /permission_required/);
  assert.equal(await configuredService(), undefined);
  allowed.add('https://memory.example/*');
  await configureService('https://memory.example:9443');
  assert.equal(await managementOrigin('https://memory.example:9443'), 'https://memory.example:9443');
  await assert.rejects(managementOrigin('https://memory.example'), /scope_denied/);
  await assert.rejects(managementOrigin('https://other.example'), /scope_denied/);
  const script = [...scripts.values()][0];
  assert.deepEqual(script.matches, ['https://memory.example/manage/imports*']);
  assert.equal(script.allFrames, false);
  assert.equal(stored['memu-web-connections'].account.url, 'http://localhost:5073');
  assert.equal(stored['memu-web-connections'].account.token, 'SYNTHETIC_OLD');
  allowed.clear();
  await assert.rejects(managementOrigin('https://memory.example:9443'), /scope_denied/);
});

test('changing server replaces its management script without moving existing source credentials', async t => {
  const { scripts, allowed, stored } = browser(t);
  allowed.add('https://first.example/*'); allowed.add('https://second.example/*'); allowed.add('http://localhost/*');
  await configureService('https://first.example');
  await configureService('https://second.example');
  assert.equal(scripts.size, 1);
  assert.deepEqual([...scripts.values()][0].matches, ['https://second.example/manage/imports*']);
  await assert.rejects(managementOrigin('https://first.example'), /scope_denied/);
  await configureService('http://localhost:5073');
  assert.equal(scripts.size, 0);
  assert.equal(await managementOrigin('http://localhost:5073'), 'http://localhost:5073');
  assert.equal(stored['memu-web-connections'].account.source_id, 'old');
  await registerManagement();
  assert.equal(scripts.size, 0);
});

test('HTTPS web connector rejects other ports, paths and frames before account access', async t => {
  const { allowed } = browser(t);
  allowed.add('https://memory.example/*');
  await configureService('https://memory.example:9443');
  let listener;
  const event = { addListener() {} };
  Object.assign(globalThis.chrome, {
    runtime: { id: 'fixture', getURL: path => `chrome-extension://fixture/${path}`,
      getManifest: () => ({ version: '0.6.2' }), onInstalled: event, onStartup: event,
      onMessage: { addListener(value) { listener = value; } } },
    action: { onClicked: event }, alarms: { onAlarm: event },
  });
  await import('../dist/src/background.js?https-management-boundary');
  const sender = { id: 'fixture', tab: { id: 1 }, frameId: 0, url: 'https://memory.example:9443/manage/imports' };
  const send = value => new Promise(resolve => listener({ type: 'web-connector', action: 'hello' }, value, resolve));
  assert.equal((await send(sender)).ok, true);
  for (const value of [
    { ...sender, frameId: 1 },
    { ...sender, id: 'other-extension' },
    { ...sender, url: 'https://memory.example/manage/imports' },
    { ...sender, url: 'https://other.example:9443/manage/imports' },
    { ...sender, url: 'https://memory.example:9443/unrelated' },
  ]) assert.deepEqual(await send(value), { ok: false, error: 'scope_denied' });
});
