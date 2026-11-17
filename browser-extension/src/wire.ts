import { AdapterError } from './types.ts';

const encoder = new TextEncoder();

/** Raw response text remains the authority. Numbers used as IDs/cursors retain tokens. */
export function parseJson(raw: string): any {
  let contextSupported = false;
  JSON.parse('1', (_key: string, value: any, context?: any) => {
    contextSupported = context?.source === '1';
    return value;
  });
  if (!contextSupported) throw new AdapterError('browser_upgrade_required');
  try {
    return JSON.parse(raw, (_key: string, value: any, context?: any) => {
      if (typeof value !== 'number') return value;
      // Fractions often carry opaque time cursor precision. Never round them.
      return Number.isSafeInteger(value) && /^-?\d+$/.test(context.source)
        ? value : context.source;
    });
  } catch {
    throw new AdapterError(raw.trimStart().startsWith('<') ? 'auth_or_challenge' : 'incompatible_schema');
  }
}

export function object(value: any): Record<string, any> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new AdapterError('incompatible_schema');
  }
  return value;
}

export function array(value: any): any[] {
  if (!Array.isArray(value)) throw new AdapterError('incompatible_schema');
  return value;
}

export function nativeId(value: any): string {
  if (typeof value === 'string' && value.length && value.length <= 4096) return value;
  if (typeof value === 'number' && Number.isSafeInteger(value)) return String(value);
  throw new AdapterError('incompatible_schema');
}

export function textParts(parts: any): string[] {
  if (!Array.isArray(parts)) return [];
  return parts.flatMap(part => {
    if (typeof part === 'string') return [part];
    if (part && typeof part.text === 'string') return [part.text];
    // Opaque/redacted reasoning and tool JSON are preserved in raw, not invented text.
    return [];
  });
}

export function bytes(text: string): Uint8Array<ArrayBuffer> { return encoder.encode(text); }

export async function sha256(text: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', bytes(text));
  return Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
}

export function bindingId(provider: string, account: string, workspace: string): string {
  return JSON.stringify([provider, account, workspace]);
}

export function retryAt(error: unknown, attempts: number, now = Date.now(), random = Math.random): number {
  if (error instanceof AdapterError && error.retryAfter !== null) {
    return Math.max(now, error.retryAfter);
  }
  return now + Math.min(900_000, 1_000 * 2 ** Math.min(attempts, 10)) * (1 + random() * 0.25);
}

export function retryAfter(value: string | null, now = Date.now()): number | null {
  if (!value) return null;
  if (/^\d+(?:\.\d+)?$/.test(value)) return now + Number(value) * 1000;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? Math.max(now, parsed) : null;
}
