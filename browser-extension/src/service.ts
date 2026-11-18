import { AdapterError } from './types.ts';

const KEY = 'memulife-service-url';
const SCRIPT = 'memulife-management';
export const DEFAULT_SERVICE = 'http://127.0.0.1:5073';

export function serviceURL(value: string): string {
  let url: URL;
  try { url = new URL(value); } catch { throw new AdapterError('web_address_invalid'); }
  const loopback = url.protocol === 'http:' && ['127.0.0.1', 'localhost'].includes(url.hostname) &&
    Boolean(url.port) && Number(url.port) >= 1024;
  if ((!loopback && url.protocol !== 'https:') || !url.hostname || url.username || url.password ||
      url.pathname !== '/' || url.search || url.hash) throw new AdapterError('web_address_invalid');
  return url.origin;
}

export function servicePermission(address: string): string {
  const url = new URL(serviceURL(address));
  // Chrome host permissions cover every port; exact-origin checks below still apply.
  return `${url.protocol}//${url.hostname}/*`;
}

export async function configuredService(): Promise<string | undefined> {
  const value = (await chrome.storage.local.get(KEY))[KEY];
  return typeof value === 'string' ? serviceURL(value) : undefined;
}

export async function managementOrigin(value: string): Promise<string> {
  const origin = serviceURL(value);
  if (origin.startsWith('http:')) return origin; // Existing loopback installations.
  if (origin !== await configuredService() ||
      !await chrome.permissions.contains({ origins: [servicePermission(origin)] })) throw new AdapterError('scope_denied');
  return origin;
}

export async function registerManagement(address?: string): Promise<void> {
  const origin = address ?? await configuredService();
  const existing = await chrome.scripting.getRegisteredContentScripts({ ids: [SCRIPT] });
  if (!origin?.startsWith('https:')) {
    if (existing.length) await chrome.scripting.unregisterContentScripts({ ids: [SCRIPT] });
    return;
  }
  if (!await chrome.permissions.contains({ origins: [servicePermission(origin)] })) throw new AdapterError('permission_required');
  const script: chrome.scripting.RegisteredContentScript = { id: SCRIPT,
    matches: [servicePermission(origin).replace('/*', '/manage/imports*')],
    js: ['src/connector.js'], runAt: 'document_start', allFrames: false, persistAcrossSessions: true };
  if (existing.length) await chrome.scripting.updateContentScripts([script]);
  else await chrome.scripting.registerContentScripts([script]);
}

export async function configureService(value: string): Promise<string> {
  const origin = serviceURL(value);
  if (!await chrome.permissions.contains({ origins: [servicePermission(origin)] })) throw new AdapterError('permission_required');
  await registerManagement(origin);
  await chrome.storage.local.set({ [KEY]: origin });
  // Paired credentials remain tied to their original server. Never retarget them here.
  return origin;
}
