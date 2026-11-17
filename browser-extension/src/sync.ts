import { AdapterError } from './types.ts';
import type { Binding, CaptureRequest, ImportSelection } from './types.ts';
import { Provider, conversationRecord } from './providers.ts';
import { bytes, retryAt, sha256 } from './wire.ts';
import { deliver } from './native.ts';
import type { NativeSend } from './native.ts';
import { Store } from './store.ts';
import type { DetailJob, Pending } from './store.ts';
import { projectMessages, selectedRecord, MAX_PREVIEW_BYTES } from './messages.ts';
import type { DetailPage, Summary } from './types.ts';
import { attachOriginals } from './attachments.ts';

const FATAL_AUTH = new Set(['account_changed', 'auth_required', 'dormant_browser', 'permission_required']);
const errorCode = (error: unknown): string => error instanceof AdapterError ? error.code : 'adapter_failed';

/** One bounded wake; every continuation and failed detail is persisted before returning. */
export class Synchronizer {
  readonly store: Store;
  readonly provider: Provider;
  readonly send: NativeSend;

  constructor(store: Store, provider: Provider, send: NativeSend) {
    this.store = store;
    this.provider = provider;
    this.send = send;
  }

  private async reportFailure(binding: Binding, jobId: string, error: unknown, phase: string, retryAtMs: number): Promise<void> {
    try {
      await this.send({ jsonrpc: '2.0', id: crypto.randomUUID(), method: 'progress.offer', params: {
        provider: binding.provider, account_id: binding.account_id, workspace_id: binding.workspace_id,
        import_job_id: jobId, error_code: errorCode(error), phase, retry_at: retryAtMs,
        diagnostics: error instanceof AdapterError ? error.diagnostics ?? {} : {},
        extension_version: typeof chrome !== 'undefined' ? chrome.runtime.getManifest().version : 'unknown',
      } });
    } catch { /* Keep the durable browser error; a later retry can report after service recovery. */ }
  }

  async flush(binding: Binding, deadline: number): Promise<boolean> {
    const pending = (await this.store.all<Pending>('outbox', binding.id)).sort((a, b) => a.order - b.order);
    for (const entry of pending) {
      // Legacy automatically captured outbox entries have no import decision.
      if (!entry.message.params.import_job_id) continue;
      // Preserve observation order, including A→B→A, across failures and restarts.
      if (Date.now() >= deadline || entry.not_before > Date.now()) return false;
      try {
        await this.store.acknowledge(entry, await deliver(entry.message, this.send));
      } catch (error) {
        const retry = retryAt(error, entry.attempts);
        await this.store.put('outbox', { ...entry, attempts: entry.attempts + 1,
          not_before: retry, error: errorCode(error) });
        await this.reportFailure(binding, entry.message.params.import_job_id, error, 'delivery', retry);
        await this.store.updateBinding(binding.id, { status: errorCode(error), last_error: errorCode(error) });
        return false;
      }
    }
    return true;
  }

  private async selection(binding: Binding, claimPreviews = false): Promise<ImportSelection> {
    const id = crypto.randomUUID();
    const response = await this.send({ jsonrpc: '2.0', id, method: 'selection.get', params: {
      provider: binding.provider, account_id: binding.account_id, workspace_id: binding.workspace_id,
      ...(claimPreviews ? { claim_previews: true } : {}),
    } });
    if (response.id !== id || response.error || !Array.isArray(response.result?.scopes)) throw new AdapterError('import_selection_unavailable');
    const selection = response.result as ImportSelection;
    // A preview is a transient request, not a durable import decision.
    const { previews: _previews, ...approved } = selection;
    await this.store.updateBinding(binding.id, { import_selection: approved });
    return selection;
  }

  private async permitted(binding: Binding, job: DetailJob): Promise<boolean> {
    const selection = await this.selection(binding);
    return !!job.import_job_id && job.import_job_id === selection.job_id && selection.scopes.includes(job.summary.id);
  }

  async run(binding: Binding, deadline = Date.now() + 180_000): Promise<void> {
    if (!await this.flush(binding, deadline)) return;
    if (!binding.enabled || Date.now() >= deadline ||
        Math.max(binding.blocked_until ?? 0, binding.throttled_until ?? 0) > Date.now()) return;
    try {
      // Local approvals can arrive while the provider list is cooling down.
      // Check them on each wake without forcing another upstream list request.
      const selection = await this.selection(binding, true);
      for (const preview of (selection.previews ?? []).slice(0, 2)) {
        if (Date.now() >= deadline) break;
        await this.readPreview(binding, preview, deadline);
      }
      const queued = await this.store.all<DetailJob>('jobs', binding.id);
      const due = queued.some(job => job.import_job_id === selection.job_id &&
        selection.scopes.includes(job.summary.id) && job.not_before <= Date.now());
      if (binding.next_sync_at > Date.now() && !due && !selection.items?.length) return;
      await this.provider.assertBinding(binding);
      if (selection.job_id && selection.items?.length) {
        const state = await this.store.get('lists', binding.id);
        await this.store.discover(binding, selection.items, state);
      }
      if (binding.next_sync_at <= Date.now()) await this.scan(binding);
      const jobs = (await this.store.all<DetailJob>('jobs', binding.id))
        .filter(job => job.not_before <= Date.now()).sort((a, b) => a.not_before - b.not_before);
      for (const job of jobs.slice(0, 8)) {
        if (Date.now() >= deadline) break;
        await this.readDetail(binding, job, deadline);
      }
      if (!await this.flush(binding, deadline)) return;
      await this.store.updateBinding(binding.id, { status: 'partial_coverage', last_error: null });
    } catch (error) {
      const code = errorCode(error);
      const until = FATAL_AUTH.has(code) ? Date.now() + 900_000 : retryAt(error, 4);
      await this.store.updateBinding(binding.id, { status: code, last_error: code,
        blocked_until: until, ...(code === 'throttled' ? { throttled_until: until } : {}) });
    }
  }

  private async scan(binding: Binding): Promise<void> {
    const state = await this.store.get('lists', binding.id);
    if (!state) throw new AdapterError('binding_not_found');
    const cursor = state.phase === 'head' ? null : state.cursor;
    const page = await this.provider.list(binding, cursor);
    await this.provider.assertBinding(binding);
    // Offer only bounded selection metadata. Unselected metadata and response bodies
    // never enter IndexedDB or the durable native queue.
    for (let offset = 0; offset < page.items.length; offset += 200) {
      const id = crypto.randomUUID();
      const reply = await this.send({ jsonrpc: '2.0', id, method: 'catalog.offer', params: {
        provider: binding.provider, account_id: binding.account_id, workspace_id: binding.workspace_id,
        items: page.items.slice(offset, offset + 200).map(item => ({
          id: item.id, locator: item.locator, revision: item.revision,
          provider: binding.provider, title: item.title ?? null,
          created_at: item.created_at ?? null, updated_at: item.updated_at ?? null,
          message_count: item.message_count ?? null,
        })),
        scan_complete: state.phase !== 'head' && page.cursor === null && offset + 200 >= page.items.length,
      } });
      if (reply.id !== id || reply.error) throw new AdapterError('import_catalog_unavailable');
    }
    if (!page.items.length && state.phase !== 'head' && page.cursor === null) {
      const id = crypto.randomUUID();
      const reply = await this.send({ jsonrpc: '2.0', id, method: 'catalog.offer', params: {
        provider: binding.provider, account_id: binding.account_id, workspace_id: binding.workspace_id,
        items: [], scan_complete: true,
      } });
      if (reply.id !== id || reply.error) throw new AdapterError('import_catalog_unavailable');
    }
    if (page.cursor !== null && JSON.stringify(page.cursor) === JSON.stringify(cursor)) throw new AdapterError('pagination_stalled');
    if ((state.pages ?? 0) >= 10_000) throw new AdapterError('list_budget_paused');
    const finished = state.phase === 'head';
    const next = { ...state, cursor: finished ? null : page.cursor,
      phase: finished ? 'scan' : page.cursor === null ? 'head' : 'scan',
      pages: finished ? 0 : (state.pages ?? 0) + 1,
      cycle: (state.cycle ?? 1) + (finished ? 1 : 0),
      warnings: [...new Set([...(state.warnings ?? []), ...page.warnings])],
      completed_at: finished ? Date.now() : state.completed_at,
    };
    await this.store.discover(binding, page.items, next);
    await this.store.updateBinding(binding.id, { next_sync_at: Date.now() + (finished ? 900_000 : 150_000) });
    if (finished) {
      // Refresh known old IDs even when a bounded provider no longer lists them.
      const known = (await this.store.all('conversations', binding.id))
        .filter(value => !value.observed_at || Date.now() - value.observed_at > 86_400_000)
        .sort((a, b) => (a.observed_at ?? 0) - (b.observed_at ?? 0));
      for (const conversation of known.slice(0, 10)) await this.store.dirty(binding, conversation.summary);
    }
  }

  private async readDetail(binding: Binding, job: DetailJob, deadline: number): Promise<void> {
    let messageScoped = false;
    try {
      if (!await this.permitted(binding, job)) return;
      const current = await this.store.get<Binding>('bindings', binding.id);
      const expected = current?.import_selection?.messages?.[job.summary.id];
      if (expected !== undefined) {
        messageScoped = true;
        const pages = await this.transientPages(binding, job.summary, deadline, () => this.permitted(binding, job));
        const messages = await projectMessages(binding.provider, job.summary, pages);
        const record = await selectedRecord(binding.provider, job.summary, messages, expected);
        // Hashing and projection can yield too: recheck before any selected text reaches IndexedDB.
        if (!await this.permitted(binding, job)) return;
        const latest = await this.store.get<Binding>('bindings', binding.id);
        if (JSON.stringify(latest?.import_selection?.messages?.[job.summary.id]) !== JSON.stringify(expected)) return;
        await this.stageRecord(binding, job, record, expected);
        return;
      }
      const page = await this.provider.detail(binding, job.summary, job.cursor);
      await this.provider.assertBinding(binding);
      // Recheck after IO before persisting even a partial detail page.
      if (!await this.permitted(binding, job)) return;
      if (page.cursor && JSON.stringify(page.cursor) === JSON.stringify(job.cursor)) throw new AdapterError('pagination_stalled');
      if (job.pages.some(previous => previous.raw === page.raw) && page.cursor) throw new AdapterError('pagination_stalled');
      const pages = [...job.pages, page];
      if (pages.length > 500 || pages.reduce((total, entry) => total + bytes(entry.raw).length, 0) > 8 * 1024 * 1024) {
        throw new AdapterError('detail_budget_paused');
      }
      if (page.cursor) {
        await this.store.progressJob(job, { pages, cursor: page.cursor, error: null, not_before: 0 });
        return;
      }
      const record = await conversationRecord(binding.provider, job.summary, pages);
      await attachOriginals(binding.provider, record, pages, current?.import_selection?.include_attachments === true,
        (ref, maxBytes) => this.provider.attachment(binding, ref, maxBytes, job.summary.locator), () => this.permitted(binding, job));
      await this.provider.assertBinding(binding);
      if (!await this.permitted(binding, job)) return;
      await this.stageRecord(binding, job, record);
    } catch (error) {
      const retry = retryAt(error, job.attempts);
      await this.store.progressJob(job, { ...(messageScoped ? { pages: [], cursor: null } : {}), attempts: job.attempts + 1,
        not_before: retry, error: errorCode(error) });
      await this.reportFailure(binding, job.import_job_id, error,
        errorCode(error).startsWith('attachment_') ? 'attachments' : 'detail', retry);
      if (FATAL_AUTH.has(errorCode(error)) || errorCode(error) === 'throttled' || errorCode(error).startsWith('attachment_')) throw error;
    }
  }

  private async transientPages(binding: Binding, summary: Summary, deadline: number,
    permitted: () => Promise<boolean>): Promise<DetailPage[]> {
    const pages: DetailPage[] = [], cursors = new Set<string>();
    let cursor = null, total = 0;
    do {
      if (Date.now() >= deadline || pages.length >= 50) throw new AdapterError('preview_budget_exceeded');
      if (!await permitted()) throw new AdapterError('import_selection_unavailable');
      const page = await this.provider.detail(binding, summary, cursor);
      await this.provider.assertBinding(binding);
      if (!await permitted()) throw new AdapterError('import_selection_unavailable');
      total += bytes(page.raw).length;
      if (total > 8 * 1024 * 1024) throw new AdapterError('preview_budget_exceeded');
      if (page.cursor) {
        const next = JSON.stringify(page.cursor);
        if (cursors.has(next) || next === JSON.stringify(cursor)) throw new AdapterError('pagination_stalled');
        cursors.add(next);
        if (pages.some(previous => previous.raw === page.raw)) throw new AdapterError('pagination_stalled');
      }
      pages.push(page); cursor = page.cursor;
    } while (cursor);
    return pages;
  }

  private async readPreview(binding: Binding, preview: NonNullable<ImportSelection['previews']>[number], deadline: number): Promise<void> {
    const route = { provider: binding.provider, account_id: binding.account_id, workspace_id: binding.workspace_id,
      request_id: preview.request_id, conversation_id: preview.conversation.id };
    const offer = async (payload: Record<string, unknown>) => {
      const id = crypto.randomUUID();
      const message = { jsonrpc: '2.0', id, method: 'preview.offer', params: { ...route, ...payload } };
      if (bytes(JSON.stringify(message)).length > MAX_PREVIEW_BYTES) throw new AdapterError('preview_budget_exceeded');
      const reply = await this.send(message);
      if (reply.id !== id || reply.error) throw new AdapterError('import_preview_unavailable');
    };
    try {
      await this.provider.assertBinding(binding);
      const pages = await this.transientPages(binding, preview.conversation, deadline, async () => {
        await this.selection(binding); return true;
      });
      const messages = await projectMessages(binding.provider, preview.conversation, pages);
      await this.provider.assertBinding(binding);
      await this.selection(binding);
      await offer({ messages });
    } catch (error) {
      // Only the stable code travels; raw responses and exception details are never queued.
      try { await offer({ error_code: errorCode(error) }); } catch { /* A cancelled request simply expires in RAM. */ }
      if (FATAL_AUTH.has(errorCode(error)) || errorCode(error) === 'throttled') throw error;
    }
  }

  private async stageRecord(binding: Binding, job: DetailJob, record: CaptureRequest['params']['records'][number], messageHashes?: Record<string, string>): Promise<void> {
    const id = crypto.randomUUID();
    const message: CaptureRequest = { jsonrpc: '2.0', id, method: 'capture', params: {
      provider: binding.provider, account_id: binding.account_id, workspace_id: binding.workspace_id,
      records: [record], batch_id: id, import_job_id: job.import_job_id,
      cursor: { conversation_id: job.summary.id, native_revision: record.revision,
        scope_complete: false, observed_at: new Date().toISOString() },
    } };
    if (bytes(JSON.stringify(message)).length > 16 * 1024 * 1024) throw new AdapterError('capture_too_large');
    await this.store.stage(job, message, await sha256(record.raw), messageHashes);
  }

}
