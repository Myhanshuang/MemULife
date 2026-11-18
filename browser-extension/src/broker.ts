import { AdapterError } from './types.ts';
import { ORIGINS } from './http.ts';
import type { AuthBroker } from './providers.ts';

function geminiAppPath(value: string): string | null {
  const match = value.match(/^(\/(?:u\/\d+\/)?app)(?:\/[\w-]+)?\/?$/);
  return match?.[1] ?? null;
}

/** Fetch only ChatGPT's pinned read endpoints from an open, signed-in ChatGPT tab. */
export async function chatgptFetch(input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> {
  const url = new URL(String(input));
  const allowed = [/^\/api\/auth\/session$/, /^\/backend-api\/accounts\/check\/v4-2023-04-27$/,
    /^\/backend-api\/conversations$/, /^\/backend-api\/conversation\/[^/]+$/];
  if (url.origin !== ORIGINS.chatgpt || (init.method ?? 'GET').toUpperCase() !== 'GET' ||
      !allowed.some(pattern => pattern.test(url.pathname))) throw new AdapterError('scope_denied');
  const tabs = await chrome.tabs.query({ url: `${ORIGINS.chatgpt}/*` });
  const tab = tabs.find(value => value.id !== undefined);
  if (tab?.id === undefined) throw new AdapterError('dormant_browser');
  const supplied = new Headers(init.headers);
  const headers: Record<string, string> = {};
  for (const name of ['accept', 'authorization', 'chatgpt-account-id']) {
    const value = supplied.get(name);
    if (value) headers[name] = value;
  }
  let result: { status: number; raw?: string; retryAfter?: string | null; error?: string } | undefined;
  try {
    const injected = await chrome.scripting.executeScript({ target: { tabId: tab.id }, world: 'MAIN',
      args: [{ path: url.pathname + url.search, headers }],
      func: async request => {
        try {
          const response = await fetch(request.path, { method: 'GET', credentials: 'include',
            redirect: 'error', cache: 'no-store', signal: AbortSignal.timeout(20_000), headers: request.headers });
          if (response.url && new URL(response.url).origin !== location.origin) return { status: 0, error: 'scope_denied' };
          if (!response.ok) return { status: response.status, retryAfter: response.headers.get('retry-after') };
          const raw = await response.text();
          return raw.length <= 16 * 1024 * 1024
            ? { status: response.status, raw } : { status: 0, error: 'response_too_large' };
        } catch { return { status: 0, error: 'chatgpt_page_request_failed' }; }
      },
    });
    result = injected[0]?.result;
  } catch { throw new AdapterError('chatgpt_tab_unavailable'); }
  if (!result) throw new AdapterError('chatgpt_tab_unavailable');
  if (result.error) throw new AdapterError(result.error);
  return new Response(result.status === 204 ? null : result.raw ?? '', {
    status: result.status, headers: result.retryAfter ? { 'retry-after': result.retryAfter } : undefined,
  });
}

/** Fetch only DeepSeek's pinned read endpoints from its open, signed-in tab. */
export async function deepseekFetch(input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> {
  const url = new URL(String(input));
  const allowed = [/^\/api\/v0\/users\/current$/, /^\/api\/v0\/chat_session\/fetch_page$/,
    /^\/api\/v0\/chat\/history_messages$/];
  if (url.origin !== ORIGINS.deepseek || (init.method ?? 'GET').toUpperCase() !== 'GET' ||
      !allowed.some(pattern => pattern.test(url.pathname))) throw new AdapterError('scope_denied');
  const tabs = await chrome.tabs.query({ url: `${ORIGINS.deepseek}/*` });
  const tab = tabs.find(value => value.id !== undefined);
  if (tab?.id === undefined) throw new AdapterError('dormant_browser');
  const supplied = new Headers(init.headers);
  const headers: Record<string, string> = {};
  for (const name of ['accept', 'authorization']) {
    const value = supplied.get(name);
    if (value) headers[name] = value;
  }
  let result: { status: number; raw?: string; retryAfter?: string | null; error?: string } | undefined;
  try {
    const injected = await chrome.scripting.executeScript({ target: { tabId: tab.id }, world: 'MAIN',
      args: [{ path: url.pathname + url.search, headers }],
      func: async request => {
        try {
          const response = await fetch(request.path, { method: 'GET', credentials: 'include',
            redirect: 'error', cache: 'no-store', signal: AbortSignal.timeout(20_000), headers: request.headers });
          if (response.url && new URL(response.url).origin !== location.origin) return { status: 0, error: 'scope_denied' };
          if (!response.ok) return { status: response.status, retryAfter: response.headers.get('retry-after') };
          const raw = await response.text();
          return raw.length <= 16 * 1024 * 1024
            ? { status: response.status, raw } : { status: 0, error: 'response_too_large' };
        } catch { return { status: 0, error: 'deepseek_page_request_failed' }; }
      },
    });
    result = injected[0]?.result;
  } catch { throw new AdapterError('deepseek_tab_unavailable'); }
  if (!result) throw new AdapterError('deepseek_tab_unavailable');
  if (result.error) throw new AdapterError(result.error);
  return new Response(result.status === 204 ? null : result.raw ?? '', {
    status: result.status, headers: result.retryAfter ? { 'retry-after': result.retryAfter } : undefined,
  });
}

/** Use the signed-in page for Perplexity reads; keep background access when no tab is open. */
export async function perplexityFetch(input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> {
  const url = new URL(String(input));
  const method = (init.method ?? 'GET').toUpperCase();
  if (url.origin !== ORIGINS.perplexity || !(method === 'POST' && url.pathname === '/rest/thread/list_ask_threads' ||
      method === 'GET' && /^\/rest\/thread\/[^/]+$/.test(url.pathname))) throw new AdapterError('scope_denied');
  const [tab] = await chrome.tabs.query({url: ORIGINS.perplexity + '/*'});
  if (tab?.id === undefined) return fetch(input, init);
  const [result] = await chrome.scripting.executeScript({
    target: {tabId: tab.id}, world: 'ISOLATED',
    args: [{href: url.href, method, body: typeof init.body === 'string' ? init.body : null}],
    func: async ({href, method, body}) => {
      if (location.origin !== new URL(href).origin) return {error: 'account_changed'};
      try {
        const res = await fetch(href, {method, body, credentials: 'include', redirect: 'error',
          headers: body ? {'Content-Type': 'application/json'} : {}, cache: 'no-store', signal: AbortSignal.timeout(20000)});
        if (res.status >= 500) return {error: 'perplexity_http_' + res.status};
        const raw = res.ok ? await res.text() : '';
        if (raw.length > 16 * 1024 * 1024) return {error: 'response_too_large'};
        return {status: res.status, raw, retry: res.headers.get('retry-after')};
      } catch (error) {
        return {error: error instanceof DOMException && error.name === 'TimeoutError' ? 'perplexity_timeout' : 'perplexity_page_request_failed'};
      }
    },
  }).catch(() => { throw new AdapterError('perplexity_tab_unavailable'); });
  const value = result?.result;
  if (!value || !value.status) throw new AdapterError(value?.error || 'perplexity_tab_unavailable');
  return new Response(value.status === 204 ? null : value.raw, {status: value.status,
    headers: value.retry ? {'Retry-After': value.retry} : {}});
}

/** No cookie inventory, no general token-key scanning, no credential persistence. */
export const browserBroker: AuthBroker = {
  async chatgptAttachment(accessToken, workspaceId, fileId, maxBytes, conversationId) {
    if (!/^file[-_][A-Za-z0-9_-]{1,200}$/.test(fileId) || !Number.isSafeInteger(maxBytes) ||
        maxBytes <= 0 || maxBytes > 100 * 1024 * 1024 || !/^[A-Za-z0-9_-]{1,128}$/.test(conversationId)) throw new AdapterError('attachment_reference_invalid');
    const tabs = await chrome.tabs.query({ url: `${ORIGINS.chatgpt}/*` });
    const tab = tabs.find(value => value.id !== undefined);
    if (tab?.id === undefined) throw new AdapterError('dormant_browser');
    const injected = await chrome.scripting.executeScript({ target: { tabId: tab.id }, world: 'MAIN',
      args: [{ accessToken, workspaceId, fileId, maxBytes, conversationId }],
      func: async request => {
        let stage = 'resolve_url';
        const failure = (error: string, httpStatus?: number) => ({ error, data: '', mime_type: '',
          diagnostics: { stage, ...(httpStatus ? { http_status: httpStatus } : {}) } });
        const status = (code: number) => code === 401 ? 'auth_required' : code === 404 || code === 410
          ? 'attachment_source_unavailable' : code === 429 ? 'throttled' : code === 403
          ? 'attachment_access_denied' : 'attachment_download_failed';
        try {
          const query = new URLSearchParams({ conversation_id: request.conversationId });
          const response = await fetch(`/backend-api/files/download/${encodeURIComponent(request.fileId)}?${query}`, {
            method: 'GET', credentials: 'include', redirect: 'error', cache: 'no-store',
            signal: AbortSignal.timeout(20_000), headers: { Authorization: `Bearer ${request.accessToken}`,
              'chatgpt-account-id': request.workspaceId },
          });
          if (!response.ok) return failure(status(response.status), response.status);
          stage = 'resolve_response';
          const value = await response.json();
          if (value.status === 'retry') return failure('attachment_not_ready');
          if (typeof value.download_url !== 'string' || !value.download_url) return failure('attachment_download_url_missing');
          const url = new URL(value.download_url, location.origin);
          if (url.protocol !== 'https:' || url.username || url.password || url.port ||
              !(url.origin === 'https://chatgpt.com' || url.hostname.endsWith('.oaiusercontent.com') ||
                url.hostname.endsWith('.blob.core.windows.net'))) return failure('attachment_origin_denied');
          // Signed downloads need no bearer token or browser cookies at the media origin.
          stage = 'download_bytes';
          const sameOrigin = url.origin === location.origin;
          const file = await fetch(url.href, { method: 'GET', credentials: sameOrigin ? 'include' : 'omit', redirect: 'error',
            cache: 'no-store', signal: AbortSignal.timeout(180_000), referrerPolicy: 'no-referrer' });
          if (!file.ok) return failure(status(file.status), file.status);
          const mime = (file.headers.get('content-type') ?? 'application/octet-stream').split(';')[0];
          if (mime === 'text/html') { await file.body?.cancel(); return failure('attachment_unexpected_response'); }
          if (Number(file.headers.get('content-length')) > request.maxBytes) {
            await file.body?.cancel(); return failure('attachment_too_large');
          }
          const reader = file.body?.getReader();
          if (!reader) return failure('attachment_download_failed');
          const chunks: Uint8Array[] = []; let size = 0;
          while (true) {
            const part = await reader.read();
            if (part.done) break;
            size += part.value.byteLength;
            if (size > request.maxBytes) { await reader.cancel(); return failure('attachment_too_large'); }
            chunks.push(part.value);
          }
          if (!size) return failure('attachment_empty');
          let binary = '';
          for (const chunk of chunks) for (let offset = 0; offset < chunk.length; offset += 8192) {
            binary += String.fromCharCode(...chunk.subarray(offset, offset + 8192));
          }
          return { data: btoa(binary), mime_type: mime, error: '', diagnostics: { stage } };
        } catch { return failure('attachment_download_failed'); }
      },
    });
    const result = injected[0]?.result;
    if (!result || result.error) throw new AdapterError(result?.error || 'chatgpt_tab_unavailable', null, result?.diagnostics);
    return { data: result.data, mime_type: result.mime_type };
  },
  async chatgptActiveWorkspace(accessToken, workspaceIds) {
    if (!accessToken || workspaceIds.length > 20) return null;
    const tabs = await chrome.tabs.query({ url: `${ORIGINS.chatgpt}/*` });
    const recent = tabs.filter(tab => /\/c\/[A-Za-z0-9-]{1,128}(?:\/|$)/.test(new URL(tab.url ?? ORIGINS.chatgpt).pathname))
      .sort((a, b) => (b.lastAccessed ?? 0) - (a.lastAccessed ?? 0))[0];
    if (recent?.id === undefined) return null;
    const conversationId = new URL(recent.url!).pathname.match(/\/c\/([A-Za-z0-9-]{1,128})(?:\/|$)/)?.[1];
    if (!conversationId) return null;
    try {
      const injected = await chrome.scripting.executeScript({ target: { tabId: recent.id }, world: 'MAIN',
        args: [{ conversationId, workspaceIds, accessToken }],
        func: async request => {
          const matching: string[] = [];
          for (const workspaceId of request.workspaceIds) {
            try {
              const response = await fetch(`/backend-api/conversation/${encodeURIComponent(request.conversationId)}`, {
                method: 'GET', credentials: 'include', redirect: 'error', cache: 'no-store',
                signal: AbortSignal.timeout(20_000),
                headers: { Authorization: `Bearer ${request.accessToken}`, 'chatgpt-account-id': workspaceId },
              });
              if (response.url && new URL(response.url).origin !== location.origin) return null;
              if (response.status === 200) matching.push(workspaceId);
              await response.body?.cancel().catch(() => {});
            } catch { return null; }
          }
          return matching.length === 1 ? matching[0] : null;
        },
      });
      return injected[0]?.result ?? null;
    } catch { return null; }
  },
  async geminiPaths() {
    const tabs = await chrome.tabs.query({ url: `${ORIGINS.gemini}/*` });
    const paths = tabs.map(tab => {
      try { return geminiAppPath(new URL(tab.url ?? '').pathname); } catch { return null; }
    }).filter((path): path is string => path !== null);
    return [...new Set(paths)].sort();
  },
  async geminiRequest(path, method, body) {
    const url = new URL(path, ORIGINS.gemini);
    const scope = url.pathname.match(/^(\/(?:u\/\d+\/)?)(?:app|_\/BardChatUi\/data\/batchexecute)$/)?.[1];
    if (url.origin !== ORIGINS.gemini || !scope ||
        (method === 'GET' && (url.pathname !== `${scope}app` || url.search)) ||
        (method === 'POST' && (url.pathname !== `${scope}_/BardChatUi/data/batchexecute` ||
          !['MaZiqc', 'hNvQHb'].includes(url.searchParams.get('rpcids') ?? '') ||
          url.searchParams.get('source-path') !== `${scope}app` || !url.searchParams.has('f.sid'))) ||
        (method === 'POST' && (typeof body !== 'string' || body.length > 1024 * 1024))) {
      throw new AdapterError('scope_denied');
    }
    const appPath = `${scope}app`;
    const tabs = await chrome.tabs.query({ url: `${ORIGINS.gemini}/*` });
    const tab = tabs.find(value => {
      try { return geminiAppPath(new URL(value.url ?? '').pathname) === appPath && value.id !== undefined; }
      catch { return false; }
    });
    if (tab?.id === undefined) throw new AdapterError('dormant_browser');
    let result: { status: number; raw?: string; error?: string } | undefined;
    try {
      const injected = await chrome.scripting.executeScript({ target: { tabId: tab.id }, world: 'MAIN',
        args: [{ path: url.pathname + url.search, method, body }],
        func: async (request) => {
          try {
            // A conversation URL belongs to the same account scope as its home
            // page. Recheck in-page in case the tab navigated after selection.
            const pageScope = location.pathname.match(/^(\/(?:u\/\d+\/)?app)(?:\/[\w-]+)?\/?$/)?.[1];
            const target = new URL(request.path, location.origin);
            const requestScope = target.pathname.match(/^(\/(?:u\/\d+\/)?)(?:app|_\/BardChatUi\/data\/batchexecute)$/)?.[1];
            if (!pageScope || pageScope !== `${requestScope}app`) return { status: 0, error: 'account_changed' };
            if (request.method === 'GET') {
              // Read the already-open page. Fetching a new HTML document rotates
              // Gemini's temporary RPC tokens between probe and connect.
              const raw = document.documentElement.outerHTML;
              return raw.length <= 16 * 1024 * 1024
                ? { status: 200, raw } : { status: 0, error: 'response_too_large' };
            }
            const response = await fetch(request.path, {
              method: request.method, body: request.body, credentials: 'include', redirect: 'error',
              cache: 'no-store', signal: AbortSignal.timeout(20_000),
              headers: request.method === 'POST' ? {
                'Content-Type': 'application/x-www-form-urlencoded;charset=utf-8', 'X-Same-Domain': '1',
              } : undefined,
            });
            if (response.url && new URL(response.url).origin !== location.origin) return { status: 0, error: 'scope_denied' };
            if (!response.ok) return { status: response.status };
            const raw = await response.text();
            return raw.length <= 16 * 1024 * 1024
              ? { status: response.status, raw } : { status: 0, error: 'response_too_large' };
          } catch { return { status: 0, error: 'gemini_page_request_failed' }; }
        },
      });
      result = injected[0]?.result;
    } catch { throw new AdapterError('gemini_tab_unavailable'); }
    if (!result) throw new AdapterError('gemini_tab_unavailable');
    if (result.error) throw new AdapterError(result.error);
    if (result.status === 401) throw new AdapterError('auth_required');
    if (result.status === 403) throw new AdapterError('access_denied_or_challenge');
    if (result.status === 429) throw new AdapterError('throttled');
    if (result.status >= 500) throw new AdapterError('source_offline');
    if (result.status !== 200 || result.raw === undefined) throw new AdapterError('request_rejected');
    return result.raw;
  },
  async cookie(provider) {
    const name = provider === 'grok' ? 'sso' : '__Secure-next-auth.session-token';
    const value = await chrome.cookies.get({ url: ORIGINS[provider], name });
    if (!value?.value) throw new AdapterError('auth_required');
    return value.value;
  },
  async deepseekToken() {
    const tabs = await chrome.tabs.query({ url: `${ORIGINS.deepseek}/*` });
    const tab = tabs.find(value => value.id !== undefined);
    if (tab?.id === undefined) throw new AdapterError('dormant_browser');
    const results = await chrome.scripting.executeScript({ target: { tabId: tab.id }, world: 'MAIN',
      func: () => {
        const stored = localStorage.getItem('userToken');
        if (!stored) return null;
        try {
          const parsed = JSON.parse(stored);
          return typeof parsed === 'string' ? parsed : typeof parsed?.value === 'string' ? parsed.value : null;
        } catch { return stored; }
      },
    });
    const token = results[0]?.result;
    if (typeof token !== 'string' || token.length < 10 || token.length > 16_384) throw new AdapterError('auth_required');
    return token;
  },
};
