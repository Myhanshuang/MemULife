import { AdapterError } from './types.ts';
import type { CaptureRequest, JsonObject } from './types.ts';
import { bytes, sha256 } from './wire.ts';

export type NativeSend = (message: JsonObject) => Promise<JsonObject>;
const MAX_MESSAGE = 1024 * 1024;
const MAX_TRANSFER = 16 * MAX_MESSAGE;
const CHUNK_BYTES = 192 * 1024;

function base64(data: Uint8Array): string {
  let value = '';
  for (let offset = 0; offset < data.length; offset += 8192) {
    value += String.fromCharCode(...data.subarray(offset, offset + 8192));
  }
  return btoa(value);
}

export function assertReceipt(request: CaptureRequest, reply: JsonObject): JsonObject {
  if (reply.jsonrpc !== '2.0' || reply.id !== request.id || reply.error ||
      reply.result?.batch_id !== request.params.batch_id || reply.result?.durable_level !== 'edge_durable') {
    throw new AdapterError('receipt_mismatch');
  }
  return reply.result;
}

/** Retries resend the persisted exact message. Chunk ACKs never release the outbox. */
export async function deliver(message: CaptureRequest, send: NativeSend): Promise<JsonObject> {
  const serialized = JSON.stringify(message);
  const data = bytes(serialized);
  if (data.length <= MAX_MESSAGE) return assertReceipt(message, await send(message));
  if (data.length > MAX_TRANSFER) throw new AdapterError('capture_too_large');
  const digest = await sha256(serialized);
  const total = Math.ceil(data.length / CHUNK_BYTES);
  const transfer = message.params.batch_id;
  const route = { provider: message.params.provider, account_id: message.params.account_id,
    workspace_id: message.params.workspace_id, import_job_id: message.params.import_job_id,
    object_ids: message.params.records.map(row => row.object_id) };
  for (let index = 0; index < total; index++) {
    const id = `${message.id}:chunk:${index}`;
    const result = await send({ jsonrpc: '2.0', id, method: 'capture.chunk', params: {
      ...route, transfer_id: transfer, index, total, sha256: digest,
      data_base64: base64(data.subarray(index * CHUNK_BYTES, (index + 1) * CHUNK_BYTES)),
    } });
    if (result.jsonrpc !== '2.0' || result.id !== id || result.error ||
        result.result?.transfer_id !== transfer || result.result?.index !== index || result.result?.stored !== true) {
      throw new AdapterError('chunk_receipt_mismatch');
    }
  }
  // The host returns the original capture id, not an intermediate transfer receipt.
  return assertReceipt(message, await send({ jsonrpc: '2.0', id: message.id,
    method: 'capture.commit', params: { ...route, transfer_id: transfer, sha256: digest } }));
}

export function nativeSend(message: JsonObject): Promise<JsonObject> {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => reject(new AdapterError('native_host_timeout')), 30_000);
    chrome.runtime.sendNativeMessage('org.memu.acquisition', message, reply => {
      clearTimeout(timeout);
      if (chrome.runtime.lastError) reject(new AdapterError('native_host_unavailable'));
      else resolve(reply);
    });
  });
}
