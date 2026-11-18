import { attachmentRefs } from './attachments.ts';
import { perplexityAnswer, perplexityTime } from './perplexity.ts';
/** Bounded, canonical message views. Original response frames never enter this format. */
import { AdapterError } from './types.ts';
import type { DetailPage, ProviderId, SourceRecord, Summary } from './types.ts';
import { array, bytes, nativeId, object, sha256, claudeMessageText, textParts } from './wire.ts';

export const MAX_PREVIEW_MESSAGES = 500;
export const MAX_PREVIEW_BYTES = 900 * 1024;
export type MessageRole = 'user' | 'assistant' | 'system' | 'tool' | 'unknown';
export interface Message {
  id: string;
  role: MessageRole;
  text: string;
  occurred_at: string | null;
}
export interface PreviewMessage extends Message { digest: string; }

function role(value: unknown): MessageRole {
  if (typeof value !== 'string') return 'unknown';
  const normalized = value.toLowerCase();
  if (normalized === 'human') return 'user';
  return ['user', 'assistant', 'system', 'tool'].includes(normalized) ? normalized as MessageRole : 'unknown';
}

function time(value: unknown): string | null {
  if (Array.isArray(value)) {
    if (value.length !== 2 || !Number.isSafeInteger(value[0]) || value[0] < 0 || !Number.isSafeInteger(value[1]) || value[1] < 0 || value[1] >= 1e9) return null;
    value = value[0] + value[1] / 1e9;
  }
  let stamp: number;
  if (typeof value === 'number' || typeof value === 'string' && /^\d+(?:\.\d+)?$/.test(value)) {
    stamp = Number(value) * 1000;
  } else if (typeof value === 'string' && /^\d{4}-\d{2}-\d{2}T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d+)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$/.test(value)) {
    stamp = Date.parse(value);
    const [year, month, day] = value.slice(0, 10).split('-').map(Number);
    if (month < 1 || month > 12 || day < 1 || day > new Date(Date.UTC(year, month, 0)).getUTCDate()) return null;
  } else return null;
  return Number.isFinite(stamp) && stamp >= 0 && stamp <= 253402300799999 ? new Date(stamp).toISOString() : null;
}

export const messageDigest = (message: Message): Promise<string> =>
  sha256(JSON.stringify([message.id, message.role, message.text, message.occurred_at]));

/** A missing native ID is content/path addressed; shifted or edited messages then fail closed. */
export async function projectMessages(provider: ProviderId, item: Summary, pages: DetailPage[], preview = true): Promise<PreviewMessage[]> {
  const values: PreviewMessage[] = [], seen = new Map<string, string>();
  const filesByMessage = new Map<string, string[]>();
  if (!preview && (provider === 'claude' || provider === 'gemini')) {
    for (const ref of attachmentRefs(provider, pages)) for (const id of ref.messageIds) {
      const names = filesByMessage.get(id) ?? [];
      names.push(ref.name);
      filesByMessage.set(id, names);
    }
  }
  let total = 0;
  async function add(identity: unknown, path: string, actor: MessageRole, text: string, occurred: unknown = null) {
    const names = typeof identity === 'string' && filesByMessage.get(identity);
    if (!text && names) text = `[附件：${names.join('、')}]`;
    if (!text) return; // Non-text/opaque blocks are not selectable text messages.
    if (/[\ud800-\udfff]/u.test(text)) throw new AdapterError('incompatible_schema');
    let id: string;
    if ((typeof identity === 'string' && identity) || typeof identity === 'number' && Number.isSafeInteger(identity)) {
      id = nativeId(identity);
      if (id.length > 512 || /[\u0000-\u001f\u007f\ud800-\udfff]/u.test(id)) throw new AdapterError('incompatible_schema');
    } else id = 'derived:' + await sha256(JSON.stringify([provider, item.id, path, actor, text]));
    const message = { id, role: actor, text, occurred_at: time(occurred) };
    const digest = await messageDigest(message);
    if (seen.has(id)) {
      if (seen.get(id) !== digest) throw new AdapterError('message_identity_conflict');
      return; // An identical overlapping page is not a second selectable message.
    }
    seen.set(id, digest);
    const value = { ...message, digest };
    total += bytes(JSON.stringify(value)).length;
    if (values.length >= (preview ? MAX_PREVIEW_MESSAGES : 20000) || total > (preview ? MAX_PREVIEW_BYTES : 8 * 1024 * 1024)) throw new AdapterError('preview_budget_exceeded');
    values.push(value);
  }
  // Gemini history is newest-turn first, including paginated older history.
  if (provider === 'gemini') pages = [{ raw: '', cursor: null, data: { frames: [[pages.flatMap(page => array(page.data.frames).flatMap(frame => array(frame[0]))).reverse()]] } }];
  for (const [pageIndex, page] of pages.entries()) {
    const data = object(page.data);
    if (provider === 'chatgpt') {
      for (const [nodeId, node] of Object.entries(object(data.mapping))) {
        object(node);
        if (node.id != null && nativeId(node.id) !== nodeId) throw new AdapterError('incompatible_schema');
        if (!node.message) continue;
        const message = object(node.message);
        const parts = textParts(message.content?.parts).join('\n\n');
        const names = (message.metadata?.attachments ?? []).map((file: any) => file.name ?? file.id).filter(Boolean);
        await add(message.id ?? nodeId, `mapping/${nodeId}`, role(message.author?.role),
          parts || (!preview && names.length ? `[附件：${names.join('、')}]` : ''), message.create_time);
      }
    } else if (provider === 'claude') {
      for (const [index, message] of array(data.chat_messages).entries()) {
        object(message);
        await add(message.uuid ?? message.id, `${pageIndex}/chat_messages/${index}`, role(message.sender ?? message.role),
          claudeMessageText(message), message.created_at);
      }
    } else if (provider === 'gemini') {
      for (const [frameIndex, frame] of array(data.frames).entries()) for (const [turnIndex, turn] of array(frame[0]).entries()) {
        if (!Array.isArray(turn)) throw new AdapterError('incompatible_schema');
        const turnId = typeof turn[0]?.[1] === 'string' ? turn[0][1] : typeof turn[0] === 'string' && turn[0] ? turn[0] : null;
        const path = `${pageIndex}/frames/${frameIndex}/turns/${turnIndex}`;
        const question = turn[2]?.[0]?.[0];
        await add(turnId && `turn:${turnId}:user`, `${path}/user`, 'user', typeof question === 'string' ? question : '', turn[4]);
        for (const [index, candidate] of (Array.isArray(turn[3]?.[0]) ? turn[3][0] : []).entries()) {
          const text = candidate?.[1]?.[0];
          const candidateId = typeof candidate?.[0] === 'string' && candidate[0] ? candidate[0] : null;
          const stamps = (Array.isArray(turn[3]?.[2]) ? turn[3][2] : []).filter((row: any) => row?.[3] === candidateId);
          const stamp = stamps.length === 1 ? stamps[0][4] : null;
          if (typeof text === 'string') await add(candidateId && `candidate:${candidateId}`, `${path}/candidates/${index}`, 'assistant', text, stamp);
        }
      }
    } else if (provider === 'perplexity') {
      for (const [index, entry] of array(data.entries).entries()) {
        object(entry);
        const id = entry.uuid ?? entry.id ?? entry.entry_uuid;
        const path = `${pageIndex}/entries/${index}`;
        if (typeof entry.query_str === 'string') await add(id == null ? null : `${nativeId(id)}:user`, `${path}/user`, 'user', entry.query_str, perplexityTime(entry.query_datetime, entry.created_datetime, entry.created_at));
        const answers: string[] = [];
        for (const block of array(entry.blocks ?? [])) {
          const answer = perplexityAnswer(block.markdown_block);
          if (answer) answers.push(answer);
        }
        await add(id == null ? null : `${nativeId(id)}:assistant`, `${path}/assistant`, 'assistant', answers.join('\n\n'), perplexityTime(entry.answer_datetime, entry.answer_created_at, entry.answer_timestamp));
      }
    } else if (provider === 'deepseek') {
      const outer = object(data), envelope = object(outer.data), body = object(envelope.biz_data);
      if (outer.code !== undefined && outer.code !== 0 || envelope.biz_code !== undefined && envelope.biz_code !== 0) throw new AdapterError('request_rejected');
      for (const [index, message] of array(body.chat_messages).entries()) {
        object(message);
        const parts = typeof message.content === 'string' ? [message.content] : [];
        for (const fragment of array(message.fragments ?? [])) if (typeof fragment.content === 'string') parts.push(fragment.content);
        await add(message.message_id ?? message.id, `${pageIndex}/chat_messages/${index}`, role(message.role), parts.join('\n\n'), message.inserted_at ?? message.created_at);
      }
    } else {
      for (const [index, response] of array(data.responses ?? []).entries()) {
        object(response);
        if (typeof response.message !== 'string') continue;
        const actor = typeof response.isHuman === 'boolean' ? response.isHuman ? 'user' : 'assistant' : role(response.sender ?? response.role);
        await add(response.responseId, `${pageIndex}/responses/${index}`, actor, response.message, response.createTime);
      }
    }
  }
  return values;
}

/** Only this selected canonical envelope may be staged for a message-scoped job. */
export async function selectedRecord(provider: ProviderId, item: Summary, messages: PreviewMessage[], expected: Record<string, string>): Promise<SourceRecord> {
  const ids = Object.keys(expected);
  if (!ids.length || ids.length > MAX_PREVIEW_MESSAGES) throw new AdapterError('import_message_selection_invalid');
  const chosen = messages.filter(message => Object.hasOwn(expected, message.id));
  if (chosen.length !== ids.length || chosen.some(message => expected[message.id] !== message.digest)) throw new AdapterError('import_message_changed');
  const content = chosen.map(({ id, role, text, occurred_at }) => ({ id, role, text, occurred_at }));
  return { object_id: item.id,
    raw: JSON.stringify({ format: 'memulife.browser-messages.v1', provider, native_conversation_id: item.id, messages: content }),
    text: content.map(message => message.text).join('\n\n'), revision: null,
    metadata: { native_conversation_id: item.id, selection: 'messages', message_ids: content.map(message => message.id),
      attachments: 'excluded', actor: { role: 'unknown', source_actor_id: null, display_name: null } } };
}
