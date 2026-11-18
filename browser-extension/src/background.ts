import { AdapterError } from './types.ts';
import type { Account, Binding, ProviderId } from './types.ts';
import { ORIGINS, ReadClient } from './http.ts';
import { PROVIDERS, Provider } from './providers.ts';
import { browserBroker, chatgptFetch, claudeFetch, deepseekFetch, perplexityFetch } from './broker.ts';
import { Store } from './store.ts';
import { Synchronizer } from './sync.ts';
import { sourceSend, pairConnection, connectionStatus, localServiceURL, authorizeConnection } from './connection.ts';
import { bindingId } from './wire.ts';
import { configuredService, configureService, managementOrigin, registerManagement, servicePermission } from './service.ts';

const ALARM = 'memulife-sync';
let manualWakePending = false;
const deferredSources = new Set<string>();
const validProvider = (id: string): id is ProviderId => PROVIDERS.includes(id as ProviderId);
const createProvider = (id: ProviderId) => new Provider(id,
  new ReadClient(id, id === 'chatgpt' ? chatgptFetch : id === 'claude' ? claudeFetch : id === 'deepseek' ? deepseekFetch : id === 'perplexity' ? perplexityFetch : fetch), browserBroker);

async function ensureAlarm(): Promise<void> {
  await chrome.storage.local.setAccessLevel({ accessLevel: 'TRUSTED_CONTEXTS' });
  const alarm = await chrome.alarms.get(ALARM);
  if (alarm?.periodInMinutes !== 2.5) await chrome.alarms.create(ALARM, { periodInMinutes: 2.5 });
}

async function wake(manual = false, only?: Set<string>): Promise<void> {
  if (manual && manualWakePending) {
    if (only) for (const id of only) deferredSources.add(id);
    return;
  }
  if (manual) manualWakePending = true;
  const processed = new Set<string>();
  try {
    await ensureAlarm();
    // An explicit retry waits for the active run, then resets its local cooldowns.
    // Resetting beforehand could be overwritten by that run's final checkpoint.
    await navigator.locks.request('memulife-browser-sync', manual ? {} : { ifAvailable: true }, async lock => {
      if (!lock) return;
      const store = await Store.open();
      try {
        if (manual) await store.requestSync(Date.now(), only);
        const deadline = Date.now() + 210_000;
        const bindings = await store.all<Binding>('bindings');
        for (const binding of bindings.filter(value => (!manual || value.enabled) && (!only || only.has(value.id))).sort((a, b) => a.next_sync_at - b.next_sync_at)) {
          if (Date.now() >= deadline) break;
          processed.add(binding.id);
          if (!await chrome.permissions.contains({ origins: [`${ORIGINS[binding.provider]}/*`] })) {
            await store.updateBinding(binding.id, { status: 'permission_required', last_error: 'permission_required' });
            continue;
          }
          await new Synchronizer(store, createProvider(binding.provider), sourceSend).run(binding, deadline);
        }
      } finally { store.db.close(); }
    });
  } finally {
    if (manual) {
      manualWakePending = false;
      if (deferredSources.size) { const next = new Set(deferredSources); deferredSources.clear(); void wake(true, next); }
    }
    if (processed.size) void progress(processed).catch(() => {});
  }
}

const grants = new Map<string, { resolve: (value: boolean) => void; tab?: number }>();
async function permission(provider: ProviderId): Promise<void> {
  const wanted: chrome.permissions.Permissions = { origins: [`${ORIGINS[provider]}/*`], permissions: provider === 'grok' || provider === 'perplexity' ? ['cookies'] : [] };
  if (await chrome.permissions.contains(wanted)) return;
  const nonce = crypto.randomUUID();
  const allowed = await new Promise<boolean>((resolve, reject) => {
    const timer = setTimeout(() => { grants.delete(nonce); resolve(false); }, 110000);
    grants.set(nonce, { resolve: value => { clearTimeout(timer); grants.delete(nonce); resolve(value); } });
    chrome.windows.create({ url: chrome.runtime.getURL(`options.html?authorize=${nonce}&provider=${provider}`), type: 'popup', width: 510, height: 570 })
      .then(window => { const entry = grants.get(nonce); if (entry) entry.tab = window?.tabs?.[0]?.id; })
      .catch(error => { clearTimeout(timer); grants.delete(nonce); reject(error); });
  });
  if (!allowed || !await chrome.permissions.contains(wanted)) throw new AdapterError('permission_required');
}
async function webOrigin(sender: chrome.runtime.MessageSender): Promise<string> {
  if (sender.id !== chrome.runtime.id || sender.frameId !== 0 || sender.tab?.id === undefined) throw new AdapterError('scope_denied');
  const url = new URL(sender.url ?? '');
  if (!/^\/manage\/imports(?:\/|$)/.test(url.pathname)) throw new AdapterError('scope_denied');
  return managementOrigin(url.origin);
}
async function progress(bindings: Set<string>): Promise<void> {
  const connections = await connectionStatus();
  const service = await configuredService();
  const tabs = await chrome.tabs.query({ url: [...new Set(['http://127.0.0.1/*', 'http://localhost/*',
    ...(service ? [servicePermission(service)] : [])])] });
  await Promise.all(tabs.filter(tab => tab.id !== undefined && tab.url).map(tab => {
    const origin = new URL(tab.url!).origin;
    const source_ids = [...bindings].filter(id => connections[id]?.url === origin).map(id => connections[id].source_id);
    return source_ids.length ? chrome.tabs.sendMessage(tab.id!, { type: 'connector-progress', source_ids }).catch(() => {}) : undefined;
  }));
}

async function handle(message: any, sender: chrome.runtime.MessageSender): Promise<any> {
  const ownPage = sender.id === chrome.runtime.id && sender.url?.startsWith(chrome.runtime.getURL('options.html'));
  if (!message || typeof message !== 'object') throw new AdapterError('invalid_request');
  if (ownPage && message.type === 'configure-service' && typeof message.address === 'string') {
    return { url: await configureService(message.address) };
  }
  if (ownPage && message.type === 'permission-result') {
    const entry = grants.get(message.nonce);
    if (!entry || entry.tab !== sender.tab?.id) throw new AdapterError('scope_denied');
    entry.resolve(message.allowed === true); return { acknowledged: true };
  }
  if (message.type === 'web-connector') {
    const url = await webOrigin(sender), input = message.payload ?? {};
    if (message.action === 'hello') return { extension_id: chrome.runtime.id, version: chrome.runtime.getManifest().version };
    if (message.action === 'probe' || message.action === 'connect') {
      if (!validProvider(input.provider) || typeof input.code !== 'string') throw new AdapterError('invalid_request');
      await authorizeConnection(url, input.code, input.provider);
      await permission(input.provider);
      const accounts = await createProvider(input.provider).accounts(input.account?.workspace_id);
      if (message.action === 'probe') return { accounts };
      const actual = accounts.find(account => account.account_id === input.account?.account_id && account.workspace_id === input.account?.workspace_id);
      if (!actual) throw new AdapterError('account_changed');
      const store = await Store.open();
      try {
        const id = bindingId(actual.provider, actual.account_id, actual.workspace_id);
        const old = (await store.all<Binding>('bindings')).find(binding => binding.id === id);
        const binding = { ...old, ...actual, id, enabled: true, next_sync_at: 0, status: 'queued', last_error: null, blocked_until: 0 };
        if (old) await store.updateBinding(id, binding); else await store.bind(binding);
        await pairConnection(binding, url, input.code);
        const connection = (await connectionStatus())[id];
        void wake(true, new Set([id]));
        return { source_id: connection.source_id };
      } finally { store.db.close(); }
    }
    if (message.action === 'sync' || message.action === 'status') {
      const connections = await connectionStatus();
      const ids = Object.keys(connections).filter(id => connections[id].url === url && (!input.source_id || connections[id].source_id === input.source_id));
      if (!ids.length) throw new AdapterError('scope_denied');
      if (message.action === 'sync') { void wake(true, new Set(ids)); return { scheduled: true }; }
      const store = await Store.open();
      try { return { bindings: (await store.status()).filter(row => ids.includes(row.id)).map(row => ({
        source_id: connections[row.id].source_id, status: row.status, enabled: row.enabled,
        last_error: row.last_error, pending_details: row.pending_details, pending_delivery: row.pending_delivery,
        detail_errors: row.detail_gaps?.map((gap: any) => gap.error), delivery_errors: row.delivery_gaps?.map((gap: any) => gap.error),
      })) }; } finally { store.db.close(); }
    }
    throw new AdapterError('scope_denied');
  }
  const store = await Store.open();
  try {
    if (ownPage && message.type === 'status') return { bindings: await store.status(), extension_id: chrome.runtime.id,
      connections: await connectionStatus() };
    if (ownPage && message.type === 'pair' && typeof message.id === 'string' && typeof message.code === 'string') {
      const address = localServiceURL(message.url);
      if (!await chrome.permissions.contains({ origins: [new URL(address).protocol + '//' + new URL(address).hostname + '/*'] })) throw new AdapterError('permission_required');
      const binding = (await store.all<Binding>('bindings')).find(value => value.id === message.id);
      if (!binding) throw new AdapterError('binding_not_found');
      await pairConnection(binding, address, message.code.trim());
      await store.updateBinding(binding.id, { enabled: true, status: 'queued', next_sync_at: 0, blocked_until: 0 });
      void wake(true);
      return { paired: true };
    }
    if (ownPage && message.type === 'probe' && validProvider(message.provider)) {
      const provider: ProviderId = message.provider;
      if (!await chrome.permissions.contains({ origins: [`${ORIGINS[provider]}/*`] })) throw new AdapterError('permission_required');
      return { accounts: await createProvider(provider).accounts() };
    }
    if (ownPage && message.type === 'connect' && validProvider(message.account?.provider)) {
      const account: Account = message.account;
      const provider = createProvider(account.provider);
      const actual = (await provider.accounts(account.provider === 'gemini' ? account.workspace_id : undefined))
        .find(value => value.account_id === account.account_id && value.workspace_id === account.workspace_id);
      if (!actual) throw new AdapterError('account_changed');
      const id = bindingId(actual.provider, actual.account_id, actual.workspace_id);
      await store.bind({ ...actual, id, enabled: true, next_sync_at: 0, status: 'queued', last_error: null });
      const scriptId = `memulife-${actual.provider}`;
      if (!(await chrome.scripting.getRegisteredContentScripts({ ids: [scriptId] })).length) {
        await chrome.scripting.registerContentScripts([{ id: scriptId, matches: [`${ORIGINS[actual.provider]}/*`],
          js: ['src/observer.js'], runAt: 'document_idle', persistAcrossSessions: true }]);
      }
      void wake();
      return { binding_id: id, provider: actual.provider, account_id: actual.account_id, workspace_id: actual.workspace_id };
    }
    if (ownPage && message.type === 'pause' && typeof message.id === 'string') {
      await store.updateBinding(message.id, { enabled: !message.paused, status: message.paused ? 'paused' : 'queued', next_sync_at: 0 });
      return { status: 'saved' };
    }
    if (ownPage && message.type === 'sync') { void wake(true); return { status: 'queued' }; }
    if (message.type === 'dirty' && validProvider(message.provider) && sender.tab?.id !== undefined) {
      const provider: ProviderId = message.provider;
      const url = new URL(sender.url ?? '');
      if (url.origin !== ORIGINS[provider] || typeof message.conversation_id !== 'string' ||
          !/^[\w-]{1,256}$/.test(message.conversation_id)) throw new AdapterError('scope_denied');
      for (const binding of await store.all<Binding>('bindings')) {
        if (!binding.enabled || binding.provider !== message.provider) continue;
        const known = await store.all('conversations', binding.id);
        const match = known.find(value => value.summary.id === message.conversation_id ||
          value.summary.locator === message.conversation_id ||
          (binding.provider === 'gemini' && value.summary.locator === `c_${message.conversation_id}`));
        if (match) await store.dirty(binding, match.summary);
        else await store.updateBinding(binding.id, { next_sync_at: 0 });
      }
      await chrome.alarms.create('memulife-dirty', { when: Date.now() + 2000 });
      return { status: 'queued' };
    }
    throw new AdapterError('scope_denied');
  } finally { store.db.close(); }
}

chrome.runtime.onInstalled.addListener(() => { void ensureAlarm(); void registerManagement().catch(() => {}); });
chrome.runtime.onStartup.addListener(() => { void registerManagement().catch(() => {}); void wake(); });
chrome.action.onClicked.addListener(() => { void (async () => {
  const url = await configuredService();
  if (url) await chrome.tabs.create({ url: `${url}/manage/imports` });
  else await chrome.runtime.openOptionsPage();
})(); });
chrome.alarms.onAlarm.addListener(alarm => { if (alarm.name === ALARM || alarm.name === 'memulife-dirty') void wake(); });
chrome.runtime.onMessage.addListener((message, sender, reply) => {
  handle(message, sender).then(result => reply({ ok: true, result }), error => reply({ ok: false,
    error: error instanceof AdapterError ? error.code : 'adapter_failed' }));
  return true;
});
