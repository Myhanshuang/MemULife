export type ProviderId = 'chatgpt' | 'claude' | 'gemini' | 'perplexity' | 'deepseek' | 'grok';
export type JsonObject = Record<string, any>;

export interface Account {
  provider: ProviderId;
  account_id: string;
  workspace_id: string;
  label: string;
  identity_strength: 'native' | 'session_bound';
  requires_open_tab_for_auth: boolean;
  active_in_recent_tab?: boolean;
}

export interface Binding extends Account {
  id: string;
  enabled: boolean;
  next_sync_at: number;
  status: string;
  last_error: string | null;
  blocked_until?: number;
  throttled_until?: number;
  import_selection?: ImportSelection;
}

export interface ImportSelection {
  job_id: string | null;
  scopes: string[];
  items?: Summary[];
  messages?: Record<string, Record<string, string>>;
  previews?: { request_id: string; conversation: Summary }[];
  include_attachments?: boolean;
}

export interface Summary {
  id: string;
  locator: string;
  revision: string | null;
  title?: string | null;
  provider?: ProviderId;
  created_at?: string | null;
  updated_at?: string | null;
  message_count?: number | null;
}

export interface ListPage {
  items: Summary[];
  cursor: JsonObject | null;
  warnings: string[];
}

export interface SourceRecord {
  object_id: string;
  raw: string;
  text: string;
  revision?: string | null;
  metadata: JsonObject;
  attachments?: SourceAttachment[];
}

export interface SourceAttachment {
  attachment_id: string;
  data: string | null;
  media_type: string;
  status: 'available' | 'pending' | 'excluded';
  metadata: { name: string; message_ids: string[]; representation?: string; error_code?: string };
}

export interface DetailPage {
  raw: string;
  data: JsonObject;
  cursor: JsonObject | null;
}

export interface CaptureRequest {
  jsonrpc: '2.0';
  id: string;
  method: 'capture';
  params: {
    provider: ProviderId;
    account_id: string;
    workspace_id: string;
    records: SourceRecord[];
    cursor: JsonObject;
    batch_id: string;
    import_job_id: string;
  };
}

export class AdapterError extends Error {
  readonly code: string;
  readonly retryAfter: number | null;
  readonly diagnostics?: { stage: string; http_status?: number };

  constructor(code: string, retryAfter: number | null = null, diagnostics?: { stage: string; http_status?: number }) {
    super(code);
    this.name = 'AdapterError';
    this.code = code;
    this.retryAfter = retryAfter;
    this.diagnostics = diagnostics;
  }
}
