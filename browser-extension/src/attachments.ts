/** Explicit native attachment references, never URLs scraped from message prose. */
import { AdapterError } from './types.ts';
import type { DetailPage, ProviderId, SourceAttachment, SourceRecord } from './types.ts';
import { sha256 } from './wire.ts';

export interface AttachmentRef {
  id: string;
  name: string;
  mime: string;
  messageIds: string[];
  originalUrl?: string;
  outputPath?: string;
  inlineText?: string;
}
export interface DownloadedAttachment { data: string; mime_type: string; }
export const MAX_ATTACHMENT_BYTES = 100 * 1024 * 1024;

function decodeBase64(data: string): Uint8Array<ArrayBuffer> {
  const text = atob(data);
  const bytes = new Uint8Array(text.length);
  for (let i = 0; i < text.length; i++) bytes[i] = text.charCodeAt(i);
  return bytes;
}

function mime(name: string, value?: string): string {
  if (value && value !== 'application/octet-stream') return value.split(';')[0].toLowerCase();
  const ext = name.split('.').pop()?.toLowerCase() ?? '';
  if (['txt', 'md', 'py', 'js', 'ts', 'tsx', 'jsx', 'sh', 'csv', 'yaml', 'yml', 'toml', 'ini', 'log', 'sql', 'rs', 'go', 'java', 'c', 'cpp', 'h', 'css', 'html'].includes(ext)) return 'text/plain';
  return ({ png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', webp: 'image/webp', gif: 'image/gif',
    json: 'application/json', pdf: 'application/pdf', mp3: 'audio/mpeg', wav: 'audio/wav', mp4: 'video/mp4' })[ext] ?? 'application/octet-stream';
}

export function claudeOutputPath(value: unknown): value is string {
  const root = '/mnt/user-data/outputs/';
  return typeof value === 'string' && value.startsWith(root) && value.length <= 512 &&
    value.slice(root.length).split('/').every(part => part && part !== '.' && part !== '..' &&
      !/[\\\x00-\x1f\x7f]/.test(part));
}

export function claudeWorkspacePath(value: unknown): value is string {
  return claudeOutputPath(value) || (typeof value === 'string' && value.startsWith('/mnt/user-data/uploads/') &&
    claudeOutputPath(value.replace('/mnt/user-data/uploads/', '/mnt/user-data/outputs/')));
}

function confirmedClaudeWrite(blocks: any[], file: any): string | undefined {
  if (file.success !== true || !/\.(?:md|txt)$/i.test(file.file_name ?? '')) return;
  const writes = blocks.filter(block => block?.type === 'tool_use' &&
    block.input?.file_path === file.file_name);
  if (writes.length !== 1) return;
  const write = writes[0];
  if (write.name !== 'Write' || typeof write.id !== 'string' || typeof write.input.content !== 'string') return;
  if (!blocks.some(block => block?.type === 'tool_result' && block.tool_use_id === write.id && block.is_error === false)) return;
  if (new TextEncoder().encode(write.input.content).length === file.size_bytes) return write.input.content;
}

export function attachmentRefs(provider: ProviderId, pages: DetailPage[], onOverflow?: () => void): AttachmentRef[] {
  const refs = new Map<string, AttachmentRef>();
  function add(id: unknown, name: unknown, type: unknown, messageId: unknown, originalUrl?: unknown) {
    if (typeof id !== 'string' || !id || id.length > 512) return;
    const label = typeof name === 'string' && name ? name.slice(0, 512) : id;
    const actor = typeof messageId === 'string' ? messageId : String(messageId ?? '');
    const existing = refs.get(id);
    if (existing) {
      if (!existing.originalUrl && typeof originalUrl === 'string') existing.originalUrl = originalUrl;
      if (!existing.messageIds.includes(actor)) existing.messageIds.push(actor); return; }
    if (refs.size >= 64) { onOverflow?.(); return; }
    refs.set(id, { id, name: label, mime: mime(label, typeof type === 'string' ? type : undefined), messageIds: [actor],
      ...(typeof originalUrl === 'string' ? { originalUrl } : {}) });
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
    } else if (provider === 'gemini') {
      for (const frame of page.data.frames ?? []) for (const turn of frame[0] ?? []) {
        const turnId = turn?.[0]?.[1];
        const files = turn?.[2]?.[0]?.[3];
        if (typeof turnId !== 'string' || !Array.isArray(files)) continue;
        for (const [index, file] of files.entries()) {
          const url = file?.[0]?.[0], name = file?.[1];
          if (typeof url === 'string' && typeof name === 'string' && name)
            add(`turn:${turnId}:file:${index}`, name, null, `turn:${turnId}:user`, url);
        }
      }
    } else if (provider === 'claude') {
      for (const message of page.data.chat_messages ?? []) {
        const actor = message.uuid ?? message.id;
        const outputPaths = new Set<string>();
        if (message.sender === 'assistant' || message.role === 'assistant') {
          for (const block of Array.isArray(message.content) ? message.content : []) {
            if (block?.type !== 'tool_use') continue;
            const paths = block.name === 'create_file' ? [block.input?.path] :
              block.name === 'present_files' && Array.isArray(block.input?.filepaths) ? block.input.filepaths : [];
            for (const path of paths) if (claudeOutputPath(path)) outputPaths.add(path);
          }
        }
        for (const file of [...(message.attachments ?? []), ...(message.files ?? []), ...(message.files_v2 ?? [])]) {
          const asset = [file.original_asset, file.document_asset, file.preview_asset]
            .find(asset => asset?.file_variant === 'original');
          const id = file.uuid ?? file.id ?? file.file_uuid;
          const name = file.file_name ?? file.name;
          add(id, name, file.file_type ?? file.mime_type, message.uuid ?? message.id, asset?.url ?? file.download_url);
          const ref = refs.get(id);
          if (ref && (message.sender === 'assistant' || message.role === 'assistant')) {
            const path = file.file_path ?? file.path;
            if (claudeWorkspacePath(path)) ref.outputPath = path;
            const written = confirmedClaudeWrite(Array.isArray(message.content) ? message.content : [], file);
            if (written !== undefined) ref.inlineText = written;
            if (!ref.outputPath && typeof name === 'string') {
              const matches = [...outputPaths].filter(path => path.split('/').at(-1) === name);
              if (matches.length === 1) ref.outputPath = matches[0];
              else if (!matches.length && !/[\/\\]/.test(name) && claudeOutputPath('/mnt/user-data/outputs/' + name))
                ref.outputPath = '/mnt/user-data/outputs/' + name;
            }
          }
          if (ref && typeof name === 'string' && /\.(?:md|txt)$/i.test(name) &&
              typeof file.extracted_content === 'string' && file.extracted_content.length &&
              new TextEncoder().encode(file.extracted_content).length === file.file_size) {
            ref.inlineText = file.extracted_content;
          }
        }
        for (const path of outputPaths) {
          if ([...refs.values()].some(ref => ref.outputPath === path)) continue;
          const id = 'output:' + path;
          add(id, path.split('/').at(-1), null, actor);
          const ref = refs.get(id);
          if (ref) ref.outputPath = path;
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
  let remaining = Math.min(MAX_ATTACHMENT_BYTES, Math.max(0, Math.floor((159 * 1024 * 1024 -
    new TextEncoder().encode(JSON.stringify(record)).length) * 0.7)));
  let throttled = false;
  const refs = attachmentRefs(provider, pages, () => { record.metadata.attachment_limit_reached = true; });
  for (const ref of refs) {
    const item: SourceAttachment = { attachment_id: ref.id, data: null, media_type: ref.mime,
      status: include ? 'pending' : 'excluded', metadata: { name: ref.name, message_ids: ref.messageIds } };
    if (include) {
      if (!await permitted()) throw new AdapterError('import_selection_unavailable');
      try {
        if (throttled) throw new AdapterError('throttled');
        if (!remaining) throw new AdapterError('attachment_budget_exceeded');
        let value: DownloadedAttachment;
        if (provider === 'claude' && !ref.originalUrl && ref.inlineText !== undefined) {
          const bytes = new TextEncoder().encode(ref.inlineText);
          if (bytes.length > remaining) throw new AdapterError('attachment_too_large');
          let binary = '';
          for (let offset = 0; offset < bytes.length; offset += 8192)
            binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
          value = { data: btoa(binary), mime_type: ref.mime };
          item.metadata.representation = 'claude_inline_utf8';
        } else {
          value = await download(ref, remaining);
        }
        const size = atob(value.data).length;
        if (!size || size > remaining) throw new AdapterError('attachment_too_large');
        remaining -= size;
        item.data = value.data;
        item.media_type = mime(ref.name, value.mime_type);
        item.status = 'available';
      } catch (error) {
        if (error instanceof AdapterError && !error.code.startsWith('attachment_') && error.code !== 'throttled') throw error;
        throttled = error instanceof AdapterError && error.code === 'throttled';
        item.metadata.error_code = error instanceof AdapterError ? error.code : 'attachment_unavailable';
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
    const raw = item.data ? decodeBase64(item.data) : null;
    const hash = raw ? [...new Uint8Array(await crypto.subtle.digest('SHA-256', raw))].map(v => v.toString(16).padStart(2, '0')).join('') : null;
    manifest.push({ id: item.attachment_id, status: item.status, sha256: hash });
  }
  record.metadata.original_raw_sha256 = await sha256(record.raw);
  record.raw = JSON.stringify({ ...JSON.parse(record.raw), attachment_manifest: manifest });
}
