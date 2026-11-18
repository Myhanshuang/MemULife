import { perplexityAnswer, perplexityTime } from './perplexity.ts';
import { projectMessages } from './messages.ts';
import { AdapterError } from './types.ts';
import type { Account, Binding, DetailPage, JsonObject, ListPage, ProviderId, SourceRecord, Summary } from './types.ts';
import { ORIGINS, ReadClient } from './http.ts';
import { array, nativeId, object, parseJson, sha256, textParts } from './wire.ts';
import type { AttachmentRef, DownloadedAttachment } from './attachments.ts';

export const PROVIDERS: ProviderId[] = ['chatgpt', 'claude', 'gemini', 'perplexity', 'deepseek', 'grok'];
export const PROFILE_VERSION = 'consumer-web-2026-10-v1';

export interface AuthBroker {
  // Returns only the requested platform's credential in short-lived memory.
  cookie(provider: 'perplexity' | 'grok'): Promise<string>;
  deepseekToken(): Promise<string>;
  geminiPaths?(): Promise<string[]>;
  geminiRequest?(path: string, method: 'GET' | 'POST', body?: string): Promise<string>;
  chatgptActiveWorkspace?(accessToken: string, workspaceIds: string[]): Promise<string | null>;
  chatgptAttachment?(accessToken: string, workspaceId: string, fileId: string, maxBytes: number, conversationId: string): Promise<DownloadedAttachment>;
}

function geminiPath(scope: string): string {
  if (scope === 'default') return '/app';
  if (/^u\/\d+$/.test(scope)) return `/${scope}/app`;
  throw new AdapterError('scope_denied');
}

function catalogTitle(value: unknown): string | null {
  return typeof value === 'string' && value.trim() && value.length <= 512 && !/[\u0000-\u001f\u007f\ud800-\udfff]/u.test(value)
    ? value : null;
}

function catalogTime(value: unknown, seconds = false): string | null {
  let stamp: number;
  if (seconds) {
    if ((typeof value !== 'number' && typeof value !== 'string') ||
        !/^\d+(?:\.\d+)?$/.test(String(value))) return null;
    stamp = Number(value) * 1000;
  } else {
    if (typeof value !== 'string') return null;
    const parts = value.match(/^(\d{4})-(\d{2})-(\d{2})T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d+)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$/);
    if (!parts || Number(parts[2]) < 1 || Number(parts[2]) > 12 || Number(parts[3]) < 1 ||
        Number(parts[3]) > new Date(Date.UTC(Number(parts[1]), Number(parts[2]), 0)).getUTCDate()) return null;
    stamp = Date.parse(value);
  }
  return Number.isFinite(stamp) && stamp >= 0 && stamp <= 253402300799999
    ? new Date(stamp).toISOString() : null;
}

function summary(item: any, id: any, revision: any, locator = id,
  metadata: Partial<Summary> = {}): Summary {
  if (!item || typeof item !== 'object') throw new AdapterError('incompatible_schema');
  const count = item.message_count;
  return { id: nativeId(id), locator: nativeId(locator), revision: revision == null ? null : String(revision),
    title: null, created_at: null, updated_at: null,
    message_count: Number.isSafeInteger(count) && count >= 0 ? count : null, ...metadata };
}

function offsetPage(items: Summary[], offset: number, size: number, warnings: string[] = []): ListPage {
  return { items, cursor: items.length < size ? null : { offset: offset + size }, warnings };
}

function biz(data: any): JsonObject {
  const outer = object(data);
  if (outer.code !== undefined && outer.code !== 0) throw new AdapterError('request_rejected');
  const envelope = object(outer.data);
  if (envelope.biz_code !== undefined && envelope.biz_code !== 0) throw new AdapterError('request_rejected');
  return object(envelope.biz_data);
}

export function parseRpc(raw: string, rpcId: string): any[] {
  const frames: any[] = [];
  for (const line of raw.split('\n')) {
    if (!line.trimStart().startsWith('[')) continue;
    const rows = array(parseJson(line));
    for (const entry of rows) {
      if (Array.isArray(entry) && entry[0] === 'wrb.fr' && entry[1] === rpcId) {
        if (typeof entry[2] !== 'string') throw new AdapterError('incompatible_schema');
        frames.push(array(parseJson(entry[2])));
      }
    }
  }
  if (!frames.length) throw new AdapterError('incompatible_schema');
  return frames;
}

function geminiSession(html: string): { at: string; sid: string; bl: string } {
  function read(key: string): string {
    const match = html.match(new RegExp(`"${key}"\\s*:\\s*("(?:\\\\.|[^"\\\\])*")`));
    return match ? parseJson(match[1]) : '';
  }
  const session = { at: read('SNlM0e'), sid: read('FdrFJe'), bl: read('cfb2h') };
  if (!session.at || !session.sid) throw new AdapterError('auth_required');
  return session;
}

/** API shapes are pinned profiles, never fallback guesses or empty success responses. */
export class Provider {
  readonly id: ProviderId;
  readonly client: ReadClient;
  readonly broker: AuthBroker;
  private token = '';
  private session: (ReturnType<typeof geminiSession> & { path: string }) | null = null;

  constructor(id: ProviderId, client: ReadClient, broker: AuthBroker) {
    this.id = id;
    this.client = client;
    this.broker = broker;
  }

  async accounts(scope?: string): Promise<Account[]> {
    const make = (account: string, workspace: string, label: string, native = true, openTab = false): Account => ({
      provider: this.id, account_id: account, workspace_id: workspace, label,
      identity_strength: native ? 'native' : 'session_bound', requires_open_tab_for_auth: openTab,
    });
    if (this.id === 'chatgpt') {
      const session = object((await this.client.request('/api/auth/session')).data);
      const accountId = nativeId(object(session.user).id);
      if (typeof session.accessToken !== 'string' || !session.accessToken) throw new AdapterError('auth_required');
      this.token = session.accessToken;
      const value = object((await this.client.request('/backend-api/accounts/check/v4-2023-04-27', { headers: this.headers() })).data);
      const listed = Object.values(object(value.accounts)).filter((entry: any) => !entry?.account?.is_deactivated)
        .map((entry: any) => {
          const account = object(entry.account ?? entry);
          return make(accountId, nativeId(account.account_id ?? account.id), String(account.organization_name ?? account.name ?? account.plan_type ?? 'workspace'));
        });
      const accounts = [...new Map(listed.map(account => [account.account_id + '\0' + account.workspace_id, account])).values()];
      const active = await this.broker.chatgptActiveWorkspace?.(this.token, accounts.map(account => account.workspace_id));
      return accounts.map(account => ({ ...account, active_in_recent_tab: account.workspace_id === active }));
    }
    if (this.id === 'claude') {
      const value = object((await this.client.request('/api/account')).data);
      const account = object(value.account ?? value);
      const id = nativeId(account.uuid ?? account.id);
      const result = (await this.client.request('/api/organizations')).data;
      const orgs = Array.isArray(result) ? result : object(result).organizations;
      return array(orgs).map(org => make(id, nativeId(org.uuid ?? org.id), String(org.name ?? 'organization')));
    }
    if (this.id === 'gemini') {
      const paths = scope ? [geminiPath(scope)] : await (this.broker.geminiPaths?.() ?? Promise.resolve(['/app']));
      if (!paths.length) throw new AdapterError('dormant_browser');
      const accounts: Account[] = [];
      let lastError: unknown;
      for (const path of paths) {
        if (!/^(?:\/u\/\d+)?\/app$/.test(path)) throw new AdapterError('scope_denied');
        try {
          const raw = this.broker.geminiRequest
            ? await this.broker.geminiRequest(path, 'GET')
            : (await this.client.request(path, {}, false)).raw;
          this.session = { ...geminiSession(raw), path };
          const accountScope = path === '/app' ? 'default' : path.slice(1, -4);
          // A URL /u/N is an account index, not a principal. Keep the session fingerprint too.
          accounts.push(make(`session:${await sha256(this.session.sid + '\0' + this.session.at)}`,
            accountScope, `Gemini · ${accountScope === 'default' ? '默认账号' : `浏览器账号 ${accountScope.slice(2)}`}`, false, true));
        } catch (error) { lastError = error; }
      }
      if (!accounts.length) throw lastError ?? new AdapterError('auth_required');
      return accounts;
    }
    if (this.id === 'deepseek') {
      this.token = await this.broker.deepseekToken();
      const value = biz((await this.client.request('/api/v0/users/current', { headers: this.headers() })).data);
      const native = value.user?.id ?? value.user_id;
      return [make(native == null ? `session:${await sha256(this.token)}` : nativeId(native), 'default', 'DeepSeek · 当前浏览器会话', native != null, true)];
    }
    const guard = await this.broker.cookie(this.id);
    if (!guard) throw new AdapterError('auth_required');
    return [make(`session:${await sha256(guard)}`, 'default', `${this.id} · 当前浏览器会话`, false)];
  }

  async assertBinding(binding: Account): Promise<void> {
    const accounts = await this.accounts(binding.provider === 'gemini' ? binding.workspace_id : undefined);
    if (!accounts.some(account => account.account_id === binding.account_id && account.workspace_id === binding.workspace_id)) {
      throw new AdapterError('account_changed');
    }
  }

  private headers(binding?: Account): Record<string, string> {
    const headers: Record<string, string> = { accept: 'application/json' };
    if (this.token) headers.Authorization = `Bearer ${this.token}`;
    if (this.id === 'chatgpt' && binding) headers['chatgpt-account-id'] = binding.workspace_id;
    return headers;
  }

  async attachment(binding: Account, ref: AttachmentRef, maxBytes: number, conversationId: string): Promise<DownloadedAttachment> {
    if (this.id !== 'chatgpt' || !this.broker.chatgptAttachment) throw new AdapterError('attachment_download_unsupported');
    return this.broker.chatgptAttachment(this.token!, binding.workspace_id, ref.id, maxBytes, conversationId);
  }

  private async rpc(id: string, args: any[]): Promise<{raw: string; frames: any[]}> {
    if (!this.session) throw new AdapterError('auth_required');
    const query = new URLSearchParams({ rpcids: id, 'source-path': this.session.path, rt: 'c',
      _reqid: String(100000 + Math.floor(Math.random() * 900000)), 'f.sid': this.session.sid, bl: this.session.bl });
    const form = new URLSearchParams({ 'f.req': JSON.stringify([[[id, JSON.stringify(args), null, 'generic']]]), at: this.session.at });
    const prefix = this.session.path.slice(0, -4);
    const path = `${prefix}/_/BardChatUi/data/batchexecute?${query}`;
    const raw = this.broker.geminiRequest
      ? await this.broker.geminiRequest(path, 'POST', form.toString())
      : (await this.client.request(path, {
        method: 'POST', body: form.toString(), headers: { 'Content-Type': 'application/x-www-form-urlencoded;charset=utf-8', 'X-Same-Domain': '1' },
      }, false)).raw;
    return { raw, frames: parseRpc(raw, id) };
  }

  async list(binding: Binding, cursor: JsonObject | null): Promise<ListPage> {
    const offset = Number(cursor?.offset ?? 0);
    if (!Number.isSafeInteger(offset) || offset < 0) throw new AdapterError('invalid_cursor');
    const options = { headers: this.headers(binding) };
    if (this.id === 'chatgpt') {
      const data = object((await this.client.request(`/backend-api/conversations?offset=${offset}&limit=50&order=updated`, options)).data);
      return offsetPage(array(data.items).map(item => summary(item, item.id, item.update_time, item.id, {
        provider: this.id, title: catalogTitle(item.title),
        created_at: catalogTime(item.create_time), updated_at: catalogTime(item.update_time),
      })), offset, 50, ['special_scopes_unverified']);
    }
    if (this.id === 'claude') {
      const result = (await this.client.request(`/api/organizations/${encodeURIComponent(binding.workspace_id)}/chat_conversations?limit=50&offset=${offset}&consistency=eventual`, options)).data;
      const items = Array.isArray(result) ? result : object(result).data;
      return offsetPage(array(items).map(item => summary(item, item.uuid, item.updated_at, item.uuid, {
        provider: this.id, title: catalogTitle(item.name),
        created_at: catalogTime(item.created_at), updated_at: catalogTime(item.updated_at),
      })), offset, 50, ['eventual_consistency', 'special_scopes_unverified']);
    }
    if (this.id === 'gemini') {
      const response = await this.rpc('MaZiqc', cursor?.rpc_cursor == null ? [20] : [20, cursor.rpc_cursor]);
      const items = response.frames.flatMap(frame => array(frame[2]));
      const next = response.frames.at(-1)?.[1] ?? null;
      return { items: items.map(item => summary(item, item[0], item[5]?.[0], item[0], {
        provider: this.id, title: catalogTitle(item[1]), updated_at: catalogTime(item[5]?.[0], true),
      })),
        cursor: next == null || next === '' ? null : { rpc_cursor: next }, warnings: ['principal_session_bound', 'branches_and_media_unverified'] };
    }
    if (this.id === 'perplexity') {
      const result = await this.client.request('/rest/thread/list_ask_threads?version=2.18&source=default', {
        method: 'POST', headers: { ...options.headers, 'Content-Type': 'application/json' },
        body: JSON.stringify({ limit: 20, ascending: false, offset, search_term: '' }),
      });
      return offsetPage(array(result.data).map(item => summary(item, item.context_uuid ?? item.uuid, item.last_query_datetime, item.slug, {
        provider: this.id, title: catalogTitle(item.title), created_at: perplexityTime(item.created_at, item.created_datetime),
        updated_at: perplexityTime(item.last_query_datetime, item.updated_at, item.updated_datetime),
      })), offset, 20, ['principal_session_bound', 'detail_pagination_fixture_only']);
    }
    if (this.id === 'deepseek') {
      const query = new URLSearchParams({ 'lte_cursor.pinned': 'false' });
      if (cursor) { query.set('lte_cursor.updated_at', String(cursor.updated_at)); query.set('lte_cursor.id', nativeId(cursor.id)); }
      const data = biz((await this.client.request(`/api/v0/chat_session/fetch_page?${query}`, options)).data);
      const items = array(data.chat_sessions);
      if (typeof data.has_more !== 'boolean' || (data.has_more && !items.length)) throw new AdapterError('incompatible_schema');
      const last = items.at(-1);
      return { items: items.map(item => summary(item, item.id, item.updated_at, item.id, {
        provider: this.id, title: catalogTitle(item.title),
        created_at: catalogTime(item.created_at, true), updated_at: catalogTime(item.updated_at, true),
      })),
        cursor: data.has_more ? { updated_at: last.updated_at, id: nativeId(last.id) } : null,
        warnings: ['pinned_scope_unverified'] };
    }
    const data = object((await this.client.request('/rest/app-chat/conversations?pageSize=60', options)).data);
    return { items: array(data.conversations).map(item => summary(item, item.conversationId, item.modifyTime, item.conversationId, {
      provider: this.id, title: catalogTitle(item.title),
      created_at: catalogTime(item.createTime), updated_at: catalogTime(item.modifyTime),
    })),
      cursor: null, warnings: ['discovery_bounded_60', 'principal_session_bound'] };
  }

  async detail(binding: Account, item: Summary, cursor: JsonObject | null): Promise<DetailPage> {
    const id = encodeURIComponent(item.locator);
    const options = { headers: this.headers(binding) };
    if (this.id === 'chatgpt') {
      const response = await this.client.request(`/backend-api/conversation/${id}`, options);
      object(response.data.mapping);
      if (response.data.conversation_id != null && nativeId(response.data.conversation_id) !== item.id) throw new AdapterError('source_identity_mismatch');
      return { ...response, cursor: null };
    }
    if (this.id === 'claude') {
      const response = await this.client.request(`/api/organizations/${encodeURIComponent(binding.workspace_id)}/chat_conversations/${id}?tree=True&rendering_mode=messages&render_all_tools=true&consistency=eventual`, options);
      array(response.data.chat_messages);
      if (response.data.uuid != null && nativeId(response.data.uuid) !== item.id) throw new AdapterError('source_identity_mismatch');
      return { ...response, cursor: null };
    }
    if (this.id === 'gemini') {
      const response = await this.rpc('hNvQHb', [item.locator, 20, cursor?.rpc_cursor ?? null, 1, [0], [4], null, 1]);
      response.frames.forEach(frame => array(frame[0]));
      const next = response.frames.at(-1)?.[1] ?? null;
      return { raw: response.raw, data: { frames: response.frames }, cursor: next == null || next === '' ? null : { rpc_cursor: next } };
    }
    if (this.id === 'perplexity') {
      const offset = Number(cursor?.offset ?? 0);
      const response = await this.client.request(`/rest/thread/${id}?with_parent_info=true&with_schematized_response=true&version=2.18&source=default&limit=100&offset=${offset}&from_first=true`, options);
      const entries = array(response.data.entries);
      return { ...response, cursor: entries.length < 100 ? null : { offset: offset + 100 } };
    }
    if (this.id === 'deepseek') {
      const response = await this.client.request(`/api/v0/chat/history_messages?chat_session_id=${id}`, options);
      const data = biz(response.data);
      array(data.chat_messages);
      // A cache MERGE must never replace a full snapshot. Retry a full read later.
      if (data.cache_control !== 'REPLACE') throw new AdapterError('full_snapshot_required');
      return { ...response, cursor: null };
    }
    if (!cursor) {
      const response = await this.client.request(`/rest/app-chat/conversations/${id}/response-node?includeThreads=true`, options);
      const nodes = array(response.data.responseNodes);
      const ids = nodes.map(node => nativeId(node.responseId));
      if (new Set(ids).size !== ids.length) throw new AdapterError('incompatible_schema');
      return { ...response, cursor: ids.length ? { ids, offset: 0 } : null };
    }
    const ids = array(cursor.ids).map(nativeId);
    const offset = Number(cursor.offset);
    const requested = ids.slice(offset, offset + 50);
    if (!requested.length) throw new AdapterError('invalid_cursor');
    const response = await this.client.request(`/rest/app-chat/conversations/${id}/load-responses`, {
      method: 'POST', headers: { ...options.headers, 'Content-Type': 'application/json' }, body: JSON.stringify({ responseIds: requested }),
    });
    const received = array(response.data.responses).map(value => nativeId(value.responseId));
    if (received.length !== requested.length || new Set(received).size !== received.length || received.some(value => !requested.includes(value))) {
      throw new AdapterError('missing_response_nodes');
    }
    return { ...response, cursor: offset + 50 < ids.length ? { ids, offset: offset + 50 } : null };
  }
}

/** One immutable conversation snapshot, with every original response frame retained. */
export function project(provider: ProviderId, item: Summary, pages: DetailPage[]): SourceRecord {
  const texts: string[] = [];
  const metadata: JsonObject = { native_conversation_id: item.id, profile: PROFILE_VERSION,
    source_uri: `${ORIGINS[provider]}/${provider === 'chatgpt' ? 'c/' : provider === 'claude' ? 'chat/' : provider === 'perplexity' ? 'search/' : provider === 'gemini' ? 'app/' : provider === 'deepseek' ? 'a/chat/s/' : 'c/'}${encodeURIComponent(item.locator)}`,
    time_basis: 'native_only', branches: 'unknown', attachments: 'metadata_only', actor: { role: 'unknown', source_actor_id: null, display_name: null },
  };
  for (const page of pages) {
    const data = object(page.data);
    if (provider === 'chatgpt') {
      metadata.branches = 'raw_dag';
      metadata.current_node = data.current_node ?? null;
      for (const [id, node] of Object.entries(object(data.mapping))) {
        object(node);
        if (node.id != null && nativeId(node.id) !== id) throw new AdapterError('incompatible_schema');
        if (node.message) texts.push(...textParts(node.message.content?.parts));
      }
    } else if (provider === 'claude') {
      for (const message of array(data.chat_messages)) {
        object(message);
        const blocks = textParts(message.content);
        texts.push(...blocks);
        if (!blocks.length && typeof message.text === 'string') texts.push(message.text);
      }
      metadata.consistency = 'eventual';
    } else if (provider === 'gemini') {
      metadata.time_basis = 'unknown';
      for (const frame of array(data.frames)) for (const turn of array(frame[0])) {
        if (!Array.isArray(turn)) throw new AdapterError('incompatible_schema');
        const user = turn[2]?.[0]?.[0];
        if (typeof user === 'string') texts.push(user);
        // Preserve all candidate arrays raw, project only the reviewed text position.
        for (const candidate of Array.isArray(turn[3]?.[0]) ? turn[3][0] : []) {
          const value = candidate?.[1]?.[0];
          if (typeof value === 'string') texts.push(value);
        }
      }
    } else if (provider === 'perplexity') {
      metadata.detail_pagination = 'bounded_until_device_probe';
      for (const entry of array(data.entries)) {
        if (typeof entry.query_str === 'string') texts.push(entry.query_str);
        for (const block of array(entry.blocks ?? [])) {
          const answer = perplexityAnswer(block.markdown_block);
          if (answer) texts.push(answer);
        }
      }
    } else if (provider === 'deepseek') {
      for (const message of array(biz(data).chat_messages)) {
        if (typeof message.content === 'string' && message.content) texts.push(message.content);
        for (const fragment of array(message.fragments ?? [])) {
          if (typeof fragment.content === 'string') texts.push(fragment.content);
        }
      }
    } else {
      metadata.branches = 'raw_dag';
      for (const response of array(data.responses ?? [])) {
        if (typeof response.message === 'string') texts.push(response.message);
      }
    }
  }
  return { object_id: item.id, raw: JSON.stringify({ format: 'memulife.browser-response-set.v1',
    provider, profile: PROFILE_VERSION, native_conversation_id: item.id, responses: pages.map(page => page.raw) }),
    text: texts.join('\n\n'), revision: item.revision, metadata };
}

/** Structured original messages for ingestion; transport bytes remain available for audit. */
export async function conversationRecord(provider: ProviderId, item: Summary, pages: DetailPage[]): Promise<SourceRecord> {
  const record = project(provider, item, pages);
  const messages = (await projectMessages(provider, item, pages, false)).map(({ digest, ...message }) => message);
  if (!messages.length) throw new AdapterError('conversation_text_unavailable');
  record.raw = JSON.stringify({ format: 'memulife.browser-conversation.v2', provider,
    native_conversation_id: item.id, messages, responses: pages.map(page => page.raw),
    time_basis: provider === 'gemini' ? { user: 'native_turn_time', assistant: 'native_candidate_time' } : { default: 'native_message_time' } });
  record.metadata = { ...record.metadata, content_format: 'browser-conversation.v2' };
  return record;
}
