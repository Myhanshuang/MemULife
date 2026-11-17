import { AdapterError } from './types.ts';
import type { Binding, JsonObject } from './types.ts';
import { bindingId } from './wire.ts';
import { nativeSend } from './native.ts';

type Connection = { url: string; token: string; source_id: string };
const KEY = 'memu-web-connections';

export function localServiceURL(value: string): string {
  const url = new URL(value);
  if (url.protocol !== 'http:' || !['127.0.0.1', 'localhost'].includes(url.hostname) || url.username || url.password ||
      (url.pathname !== '/' && url.pathname !== '') || url.search || url.hash || !url.port || Number(url.port) < 1024) {
    throw new AdapterError('web_address_invalid');
  }
  return url.origin;
}

async function connections(): Promise<Record<string, Connection>> {
  return ((await chrome.storage.local.get(KEY))[KEY] ?? {}) as Record<string, Connection>;
}

async function request(url: string, path: string, data: JsonObject, token?: string): Promise<JsonObject> {
  let response: Response;
  try {
    response = await fetch(url + path, { method: 'POST', credentials: 'omit', redirect: 'error',
      signal: AbortSignal.timeout(30_000), headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      body: JSON.stringify(data) });
  } catch { throw new AdapterError('web_service_unavailable'); }
  let result: JsonObject;
  try { result = await response.json(); } catch { throw new AdapterError('web_service_unavailable'); }
  if (!response.ok) throw new AdapterError(result.code || 'web_service_unavailable');
  return result;
}

export async function authorizeConnection(address: string, code: string, provider: string): Promise<void> {
  const result = await request(localServiceURL(address), '/v1/browser/authorize', { code, provider });
  if (result.authorized !== true) throw new AdapterError('scope_denied');
}

export async function pairConnection(binding: Binding, address: string, code: string): Promise<void> {
  const url = localServiceURL(address);
  const result = await request(url, '/v1/browser/pair', { code, account: {
    provider: binding.provider, account_id: binding.account_id, workspace_id: binding.workspace_id,
  } });
  if (typeof result.token !== 'string' || typeof result.source_id !== 'string' ||
      result.provider !== binding.provider || result.account_id !== binding.account_id || result.workspace_id !== binding.workspace_id) {
    throw new AdapterError('source_binding_mismatch');
  }
  const values = await connections();
  values[binding.id] = { url, token: result.token, source_id: result.source_id };
  await chrome.storage.local.set({ [KEY]: values });
}

export async function connectionStatus(): Promise<Record<string, { url: string; source_id: string }>> {
  return Object.fromEntries(Object.entries(await connections()).map(([id, value]) => [id, { url: value.url, source_id: value.source_id }]));
}

export async function sourceSend(message: JsonObject): Promise<JsonObject> {
  const params = message.params;
  const id = bindingId(params.provider, params.account_id, params.workspace_id);
  const connection = (await connections())[id];
  // Existing native-host installations remain usable. A paired source never falls back after a failed HTTP write.
  if (!connection) return nativeSend(message);
  return request(localServiceURL(connection.url), '/v1/browser/rpc', message, connection.token);
}
