import { AdapterError } from './types.ts';
import { parseJson, retryAfter } from './wire.ts';

export const ORIGINS = Object.freeze({
  chatgpt: 'https://chatgpt.com', claude: 'https://claude.ai',
  gemini: 'https://gemini.google.com', perplexity: 'https://www.perplexity.ai',
  deepseek: 'https://chat.deepseek.com', grok: 'https://grok.com',
});

const READ_PATHS: Record<string, Record<string, RegExp[]>> = {
  chatgpt: { GET: [/^\/api\/auth\/session$/, /^\/backend-api\/accounts\/check\/v4-2023-04-27$/, /^\/backend-api\/conversations$/, /^\/backend-api\/conversation\/[^/]+$/] },
  claude: { GET: [/^\/api\/account$/, /^\/api\/organizations$/, /^\/api\/organizations\/[^/]+\/chat_conversations(?:\/[^/]+)?$/] },
  gemini: { GET: [/^(?:\/u\/\d+)?\/app$/], POST: [/^(?:\/u\/\d+)?\/_\/BardChatUi\/data\/batchexecute$/] },
  perplexity: { GET: [/^\/rest\/thread\/[^/]+$/], POST: [/^\/rest\/thread\/list_ask_threads$/] },
  deepseek: { GET: [/^\/api\/v0\/users\/current$/, /^\/api\/v0\/chat_session\/fetch_page$/, /^\/api\/v0\/chat\/history_messages$/] },
  grok: { GET: [/^\/rest\/app-chat\/conversations$/, /^\/rest\/app-chat\/conversations\/[^/]+\/response-node$/], POST: [/^\/rest\/app-chat\/conversations\/[^/]+\/load-responses$/] },
};

/** Only predefined, read-only consumer endpoints are callable. No arbitrary URL bridge. */
export class ReadClient {
  readonly provider: keyof typeof ORIGINS;
  readonly fetcher: typeof fetch;
  readonly pace: number;
  private lastRequest = 0;

  constructor(provider: keyof typeof ORIGINS, fetcher = fetch, pace = 1000) {
    this.provider = provider;
    this.fetcher = fetcher;
    this.pace = pace;
  }

  async request(path: string, options: RequestInit = {}, json = true): Promise<{raw: string; data: any}> {
    const url = new URL(path, ORIGINS[this.provider]);
    const method = (options.method ?? 'GET').toUpperCase();
    const allowed = READ_PATHS[this.provider][method] ?? [];
    if (url.origin !== ORIGINS[this.provider] || url.username || url.password ||
        !allowed.some(pattern => pattern.test(url.pathname))) {
      throw new AdapterError('scope_denied');
    }
    const delay = this.lastRequest + this.pace - Date.now();
    if (delay > 0) await new Promise(resolve => setTimeout(resolve, delay));
    this.lastRequest = Date.now();
    let response: Response;
    try {
      response = await this.fetcher(url, { ...options, method, credentials: 'include',
        redirect: 'error', cache: 'no-store', signal: AbortSignal.timeout(20_000) });
    } catch (error) {
      if (error instanceof AdapterError) throw error;
      throw new AdapterError('source_offline');
    }
    if (response.status === 401) throw new AdapterError('auth_required');
    if (response.status === 403) throw new AdapterError('access_denied_or_challenge');
    if (response.status === 429) throw new AdapterError('throttled', retryAfter(response.headers.get('retry-after')));
    if (response.status === 404 || response.status === 410) throw new AdapterError('source_unavailable');
    if (!response.ok) throw new AdapterError(response.status >= 500 ? 'source_offline' : 'request_rejected');
    const reader = response.body?.getReader();
    if (!reader) throw new AdapterError('incompatible_schema');
    const decoder = new TextDecoder('utf-8', { fatal: true });
    let raw = '', size = 0;
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > 16 * 1024 * 1024) throw new AdapterError('response_too_large');
        raw += decoder.decode(value, { stream: true });
      }
      raw += decoder.decode();
    } catch (error) {
      await reader.cancel().catch(() => {});
      throw error instanceof AdapterError ? error : new AdapterError('invalid_response_encoding');
    }
    return { raw, data: json ? parseJson(raw) : raw };
  }
}
