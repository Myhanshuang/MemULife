/** Explicit native attachment references, never URLs scraped from message prose. */
import { AdapterError } from './types.ts';
import type { DetailPage, ProviderId, SourceAttachment, SourceRecord } from './types.ts';
import { sha256 } from './wire.ts';

export interface AttachmentRef {
  id: string;
  name: string;
  mime: string;
  messageIds: string[];
}
export interface DownloadedAttachment { data: string; mime_type: string; }
export const MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024;

function mime(name: string, value?: string): string {
  if (value && value !== 'application/octet-stream') return value.split(';')[0].toLowerCase();
  const ext = name.split('.').pop()?.toLowerCase() ?? '';
  if (['txt', 'md', 'py', 'js', 'ts', 'tsx', 'jsx', 'sh', 'csv', 'yaml', 'yml', 'toml', 'ini', 'log', 'sql', 'rs', 'go', 'java', 'c', 'cpp', 'h', 'css', 'html'].includes(ext)) return 'text/plain';
  return ({ png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', webp: 'image/webp', gif: 'image/gif',
    json: 'application/json', pdf: 'application/pdf', mp3: 'audio/mpeg', wav: 'audio/wav', mp4: 'video/mp4' })[ext] ?? 'application/octet-stream';
}

export function attachmentRefs(provider: ProviderId, pages: DetailPage[]): AttachmentRef[] {
  const refs = new Map<string, AttachmentRef>();
  function add(id: unknown, name: unknown, type: unknown, messageId: unknown) {
    if (typeof id !== 'string' || !id || id.length > 512) return;
    const label = typeof name === 'string' && name ? name.slice(0, 512) : id;
    const actor = typeof messageId === 'string' ? messageId : String(messageId ?? '');
    const existing = refs.get(id);
    if (existing) { if (!existing.messageIds.includes(actor)) existing.messageIds.push(actor); return; }
    if (refs.size >= 64) throw new AdapterError('attachment_budget_exceeded');
    refs.set(id, { id, name: label, mime: mime(label, typeof type === 'string' ? type : undefined), messageIds: [actor] });
  }
  for (const page of pages) {
    if (provider === 'chatgpt') {
      for (const [id, node] of Object.entries<any>(page.data.mapping ?? {})) {
        const message = node.message;
        if (!message) continue;
        for (const file of message.metadata?.attachments ?? []) add(file.id, file.name, file.mime_type, message.id ?? id);
        for (const part of message.content?.parts ?? []) {
          // Both legacy uploads and current sediment uploads use native file IDs.
          const file = typeof part?.asset_pointer === 'string' && part.asset_pointer.match(/^(?:file-service|sediment):\/\/(file[-_][A-Za-z0-9_-]+)$/);
          if (file) add(file[1], file[1], part.mime_type, message.id ?? id);
        }
      }
    } else if (provider === 'deepseek') {
      for (const message of page.data.data?.biz_data?.chat_messages ?? []) {
        for (const fragment of message.fragments ?? []) for (const file of fragment.files ?? []) {
          add(file.id, file.file_name, file.mime_type, message.message_id ?? message.id);
        }
      }
    } else if (provider === 'claude') {
      for (const message of page.data.chat_messages ?? []) {
        for (const file of [...(message.attachments ?? []), ...(message.files ?? [])]) {
          add(file.uuid ?? file.id ?? file.file_uuid, file.file_name ?? file.name, file.file_type ?? file.mime_type, message.uuid ?? message.id);
        }
      }
    }
  }
  return [...refs.values()];
}

export async function attachOriginals(provider: ProviderId, record: SourceRecord, pages: DetailPage[], include: boolean,
  download: (ref: AttachmentRef, maxBytes: number) => Promise<DownloadedAttachment>, permitted: () => Promise<boolean>): Promise<void> {
  const attachments: SourceAttachment[] = [];
  // Leave room for the original conversation and the base64 expansion in the native capture.
  let remaining = Math.min(MAX_ATTACHMENT_BYTES, Math.max(0, Math.floor((15 * 1024 * 1024 -
    new TextEncoder().encode(JSON.stringify(record)).length) * 0.7)));
  for (const ref of attachmentRefs(provider, pages)) {
    const item: SourceAttachment = { attachment_id: ref.id, data: null, media_type: ref.mime,
      status: include ? 'pending' : 'excluded', metadata: { name: ref.name, message_ids: ref.messageIds } };
    if (include) {
      if (!await permitted()) throw new AdapterError('import_selection_unavailable');
      try {
        if (!remaining) throw new AdapterError('attachment_budget_exceeded');
        const value = await download(ref, remaining);
        const size = atob(value.data).length;
        if (!size || size > remaining) throw new AdapterError('attachment_too_large');
        remaining -= size;
        item.data = value.data;
        item.media_type = mime(ref.name, value.mime_type);
        item.status = 'available';
      } catch (error) {
        if (error instanceof AdapterError && ['account_changed', 'auth_required', 'import_selection_unavailable'].includes(error.code)) throw error;
        item.metadata.error_code = error instanceof AdapterError ? error.code : 'attachment_unavailable';
        if (provider === 'chatgpt') throw error instanceof AdapterError ? error : new AdapterError(item.metadata.error_code);
      }
    }
    attachments.push(item);
  }
  record.attachments = attachments;
  record.metadata.attachments = include ? 'originals' : 'excluded';
  record.metadata.attachment_coverage = provider === 'chatgpt' ? 'native_references' : 'unverified';
  // Freeze attachment identity/bytes into the fingerprint as well as the native outbox.
  const manifest = [];
  for (const item of attachments) {
    const raw = item.data ? Uint8Array.from(atob(item.data), c => c.charCodeAt(0)) : null;
    const hash = raw ? [...new Uint8Array(await crypto.subtle.digest('SHA-256', raw))].map(v => v.toString(16).padStart(2, '0')).join('') : null;
    manifest.push({ id: item.attachment_id, status: item.status, sha256: hash });
  }
  record.metadata.original_raw_sha256 = await sha256(record.raw);
  record.raw = JSON.stringify({ ...JSON.parse(record.raw), attachment_manifest: manifest });
}
