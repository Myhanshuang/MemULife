import { AdapterError } from './types.ts';
import type { Binding, CaptureRequest, DetailPage, JsonObject, Summary } from './types.ts';

const STORES = ['bindings', 'lists', 'jobs', 'conversations', 'outbox', 'receipts', 'attachments'] as const;
type StoreName = typeof STORES[number];

function request<T>(value: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    value.onsuccess = () => resolve(value.result);
    value.onerror = () => reject(new AdapterError('storage_blocked'));
  });
}

/** Visit one queued record at a time; a manual retry must not load every raw outbox body. */
function retryFailures(store: IDBObjectStore, binding: string, now: number): Promise<number> {
  return new Promise((resolve, reject) => {
    let throttledUntil = 0;
    const scan = store.index('binding_id').openCursor(binding);
    scan.onerror = () => reject(new AdapterError('storage_blocked'));
    scan.onsuccess = () => {
      const cursor = scan.result;
      if (!cursor) { resolve(throttledUntil); return; }
      const value = cursor.value;
      if (value.error === 'throttled') throttledUntil = Math.max(throttledUntil, value.not_before ?? 0);
      else if (value.error && value.not_before > now) cursor.update({ ...value, not_before: 0 });
      cursor.continue();
    };
  });
}

export interface DetailJob {
  id: string;
  binding_id: string;
  summary: Summary;
  cursor: JsonObject | null;
  pages: DetailPage[];
  attempts: number;
  not_before: number;
  error: string | null;
  rerun_summary?: Summary;
  import_job_id: string;
}

export interface Pending {
  id: string;
  binding_id: string;
  conversation_id: string;
  message: CaptureRequest;
  attempts: number;
  not_before: number;
  error: string | null;
  order: number;
}

/** Transactions contain IndexedDB requests only; network and hashing stay outside. */
export class Store {
  readonly db: IDBDatabase;
  constructor(db: IDBDatabase) { this.db = db; }

  static async open(name = 'memulife-browser-v1'): Promise<Store> {
    const opening = indexedDB.open(name, 1);
    opening.onupgradeneeded = () => {
      for (const name of STORES) {
        const store = opening.result.createObjectStore(name, { keyPath: 'id' });
        if (name !== 'bindings') store.createIndex('binding_id', 'binding_id', { unique: false });
      }
    };
    return new Store(await request(opening));
  }

  private transaction<T>(names: StoreName[], mode: IDBTransactionMode,
    work: (stores: Record<string, IDBObjectStore>) => Promise<T>): Promise<T> {
    return new Promise((resolve, reject) => {
      const tx = this.db.transaction(names, mode, mode === 'readwrite' ? { durability: 'strict' } : undefined);
      let result: T;
      let failure: unknown;
      tx.oncomplete = () => resolve(result);
      tx.onabort = tx.onerror = () => reject(failure ?? new AdapterError('storage_blocked'));
      const stores = Object.fromEntries(names.map(name => [name, tx.objectStore(name)]));
      work(stores).then(value => { result = value; }).catch(error => {
        failure = error;
        try { tx.abort(); } catch { reject(error); }
      });
    });
  }

  get<T = any>(name: StoreName, id: string): Promise<T | undefined> {
    return this.transaction([name], 'readonly', stores => request(stores[name].get(id)));
  }

  all<T = any>(name: StoreName, binding?: string): Promise<T[]> {
    return this.transaction([name], 'readonly', stores => request(binding == null
      ? stores[name].getAll() : stores[name].index('binding_id').getAll(binding)));
  }

  put(name: StoreName, value: any): Promise<void> {
    return this.transaction([name], 'readwrite', async stores => { await request(stores[name].put(value)); });
  }

  async bind(value: Binding): Promise<void> {
    await this.transaction(['bindings', 'lists'], 'readwrite', async stores => {
      const existing = await request(stores.bindings.get(value.id));
      await request(stores.bindings.put(existing ? { ...existing, enabled: true, next_sync_at: 0, blocked_until: 0 } : value));
      if (!existing) await request(stores.lists.put({ id: value.id, binding_id: value.id, cursor: null,
        phase: 'scan', cycle: 1, pages: 0, warnings: [], completed_at: null }));
    });
  }

  async updateBinding(id: string, changes: JsonObject): Promise<void> {
    await this.transaction(['bindings'], 'readwrite', async stores => {
      const value = await request(stores.bindings.get(id));
      if (!value) throw new AdapterError('binding_not_found');
      await request(stores.bindings.put({ ...value, ...changes }));
    });
  }

  async requestSync(now = Date.now(), only?: Set<string>): Promise<void> {
    await this.transaction(['bindings', 'lists', 'jobs', 'outbox'], 'readwrite', async stores => {
      const bindings: Binding[] = await request(stores.bindings.getAll());
      for (const binding of bindings.filter(value => value.enabled && (!only || only.has(value.id)))) {
        let throttledUntil = Math.max(binding.throttled_until ?? 0,
          binding.last_error === 'throttled' ? binding.blocked_until ?? 0 : 0);
        for (const name of ['jobs', 'outbox']) {
          throttledUntil = Math.max(throttledUntil, await retryFailures(stores[name], binding.id, now));
        }
        const waiting = throttledUntil > now;
        await request(stores.bindings.put({ ...binding, next_sync_at: 0,
          blocked_until: waiting ? throttledUntil : 0, throttled_until: waiting ? throttledUntil : 0,
          status: waiting ? 'throttled' : 'queued', last_error: waiting ? 'throttled' : null }));
        // The server catalog is transient. An explicit refresh must repopulate
        // its first page even if the previous scan ended on an empty tail page.
        const list = await request(stores.lists.get(binding.id));
        if (list) await request(stores.lists.put({ ...list, cursor: null, phase: 'scan', pages: 0 }));
      }
    });
  }

  async discover(binding: Binding, summaries: Summary[], nextState: JsonObject): Promise<void> {
    await this.transaction(['bindings', 'lists', 'jobs', 'conversations'], 'readwrite', async stores => {
      const selection = (await request(stores.bindings.get(binding.id)))?.import_selection;
      for (const summary of summaries.filter(s => selection?.job_id && selection.scopes.includes(s.id))) {
        const id = JSON.stringify([binding.id, summary.id]);
        const existing = await request(stores.conversations.get(id));
        const pending = await request(stores.jobs.get(id));
        // Unknown revisions are never treated as unchanged. A daily census rechecks all.
        const stale = !existing || existing.import_job_id !== selection.job_id || summary.revision === null || existing.revision !== summary.revision ||
          !existing.observed_at || Date.now() - existing.observed_at > 86_400_000;
        if ((!pending || pending.import_job_id !== selection.job_id) && stale) await request(stores.jobs.put({ id, binding_id: binding.id, summary, import_job_id: selection.job_id,
          cursor: null, pages: [], attempts: 0, not_before: 0, error: null }));
        else if (pending && pending.summary.revision !== summary.revision) {
          await request(stores.jobs.put({ ...pending, rerun_summary: summary }));
        }
        await request(stores.conversations.put({ ...existing, id, binding_id: binding.id, summary,
          revision: existing?.revision ?? null, observed_at: existing?.observed_at ?? null }));
      }
      await request(stores.lists.put({ ...nextState, id: binding.id, binding_id: binding.id }));
    });
  }

  async dirty(binding: Binding, summary: Summary, now = Date.now()): Promise<void> {
    const id = JSON.stringify([binding.id, summary.id]);
    await this.transaction(['bindings', 'jobs', 'conversations'], 'readwrite', async stores => {
      const selection = (await request(stores.bindings.get(binding.id)))?.import_selection;
      if (!selection?.job_id || !selection.scopes.includes(summary.id)) return;
      const existing = await request(stores.jobs.get(id));
      if (!existing || existing.import_job_id !== selection.job_id) await request(stores.jobs.put({ id, binding_id: binding.id, summary, import_job_id: selection.job_id,
        cursor: null, pages: [], attempts: 0, not_before: now + 2000, error: null }));
      else await request(stores.jobs.put({ ...existing, rerun_summary: summary }));
      const conversation = await request(stores.conversations.get(id));
      await request(stores.conversations.put({ ...conversation, id, binding_id: binding.id, summary,
        dirty: true, revision: conversation?.revision ?? null }));
    });
  }

  async progressJob(job: DetailJob, changes: JsonObject): Promise<void> {
    await this.transaction(['bindings', 'jobs'], 'readwrite', async stores => {
      const selection = (await request(stores.bindings.get(job.binding_id)))?.import_selection;
      if (!selection?.job_id || selection.job_id !== job.import_job_id || !selection.scopes.includes(job.summary.id)) return;
      // A message-scoped job may checkpoint errors/IDs, never full response pages.
      if (selection.messages?.[job.summary.id] !== undefined && changes.pages?.length) return;
      const current = await request(stores.jobs.get(job.id));
      if (!current) throw new AdapterError('job_missing');
      await request(stores.jobs.put({ ...job, ...changes, rerun_summary: current.rerun_summary }));
    });
  }

  async stage(job: DetailJob, message: CaptureRequest, fingerprint: string, messageHashes?: Record<string, string>): Promise<boolean> {
    return this.transaction(['bindings', 'jobs', 'conversations', 'outbox', 'lists'], 'readwrite', async stores => {
      const selection = (await request(stores.bindings.get(job.binding_id)))?.import_selection;
      if (!selection?.job_id || selection.job_id !== job.import_job_id || !selection.scopes.includes(job.summary.id)) return false;
      if (JSON.stringify(selection.messages?.[job.summary.id]) !== JSON.stringify(messageHashes)) return false;
      if (message.params.import_job_id !== job.import_job_id || message.params.records.some(row => row.object_id !== job.summary.id)) throw new AdapterError('import_scope_denied');
      const conversation = await request(stores.conversations.get(job.id));
      const changed = conversation?.fingerprint !== fingerprint || conversation?.import_job_id !== job.import_job_id;
      if (changed) {
        const state = await request(stores.lists.get(job.binding_id));
        const order = (state?.observation ?? 0) + 1;
        await request(stores.lists.put({ ...state, id: job.binding_id, binding_id: job.binding_id, observation: order }));
        await request(stores.outbox.add({ id: message.params.batch_id, binding_id: job.binding_id,
          conversation_id: job.id, message, attempts: 0, not_before: 0, error: null, order }));
      }
      await request(stores.conversations.put({ ...conversation, id: job.id, binding_id: job.binding_id,
        summary: job.summary, fingerprint, import_job_id: job.import_job_id, revision: job.summary.revision, observed_at: Date.now(), dirty: false }));
      const current = await request(stores.jobs.get(job.id));
      if (current?.rerun_summary) {
        await request(stores.jobs.put({ id: job.id, binding_id: job.binding_id,
          summary: current.rerun_summary, import_job_id: job.import_job_id, cursor: null, pages: [], attempts: 0,
          not_before: Date.now() + 2000, error: null }));
      } else await request(stores.jobs.delete(job.id));
      return changed;
    });
  }

  async acknowledge(pending: Pending, result: JsonObject): Promise<void> {
    await this.transaction(['outbox', 'receipts', 'conversations'], 'readwrite', async stores => {
      const current = await request(stores.outbox.get(pending.id));
      if (!current) return;
      if (current.message.id !== pending.message.id) throw new AdapterError('receipt_mismatch');
      await request(stores.receipts.put({ id: pending.id, binding_id: pending.binding_id,
        received_at: Date.now(), result }));
      await request(stores.outbox.delete(pending.id));
      const conversation = await request(stores.conversations.get(pending.conversation_id));
      if (conversation) await request(stores.conversations.put({ ...conversation, durable_at: Date.now() }));
    });
  }

  async status(): Promise<any[]> {
    const bindings = await this.all<Binding>('bindings');
    return Promise.all(bindings.map(async binding => ({ ...binding,
      // Never label the whole provider account complete: special scopes remain explicit.
      scope_complete: false,
      list: await this.get('lists', binding.id),
      pending_details: (await this.all('jobs', binding.id)).length,
      pending_delivery: (await this.all('outbox', binding.id)).length,
      detail_gaps: (await this.all<DetailJob>('jobs', binding.id)).filter(job => job.error).map(job => ({ id: job.summary.id, error: job.error })),
      delivery_gaps: (await this.all<Pending>('outbox', binding.id)).filter(job => job.error).map(job => ({ id: job.id, error: job.error })),
    })));
  }
}
