export function interval(range = {}) {
  const parse = (value, exclusive = false) => {
    if (!value) return null;
    if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) throw new Error('日期格式无效。');
    const [year, month, day] = value.split('-').map(Number);
    const stamp = new Date(0);
    stamp.setHours(0, 0, 0, 0);
    stamp.setFullYear(year, month - 1, day);
    if (year < 1 || Number.isNaN(stamp.getTime()) || stamp.getFullYear() !== year || stamp.getMonth() !== month - 1 || stamp.getDate() !== day) {
      throw new Error('日期格式无效。');
    }
    if (exclusive) stamp.setDate(stamp.getDate() + 1);
    if (stamp.getFullYear() > 9999) throw new Error('日期超出支持范围。');
    return stamp.toISOString();
  };
  if (range.start && range.end && range.start > range.end) throw new Error('结束日期不能早于开始日期。');
  return { start: parse(range.start), end: parse(range.end, true) };
}

export function rangeFor(id, drafts, basket, params) {
  return { ...(drafts.get(id) || basket.get(id)?.range || (params.get('focus') === id
    ? { start: params.get('start') || '', end: params.get('end') || '' } : {})) };
}

export class Basket {
  rows = new Map();
  revision = 0;
  changed = () => {};
  has(id) { return this.rows.has(id); }
  get(id) { return this.rows.get(id); }
  add(item, range = {}, messages = null) {
    interval(range);
    if (messages !== null) {
      const entries = Object.entries(messages);
      if (!entries.length) throw new Error('请先选择消息。');
      if (entries.length > 500) throw new Error('每个会话最多选择 500 条消息。');
      if (entries.some(([id, digest]) => !id || typeof digest !== 'string' || !/^[a-f0-9]{64}$/.test(digest))) {
        throw new Error('消息预览已变化，请重新读取。');
      }
      messages = Object.fromEntries(entries);
    }
    this.rows.set(item.id, { item, range: { ...range }, messages }); this.touch();
  }
  addMany(entries) {
    const additions = new Map(entries.filter(({ item }) => !this.has(item.id)).map(({ item, range = {} }) =>
      [item.id, { item, range: { ...range }, messages: null }]));
    for (const { range } of additions.values()) interval(range);
    for (const [id, row] of additions) this.rows.set(id, row);
    if (additions.size) this.touch();
  }
  removeMany(ids) {
    let removed = false;
    for (const id of ids) if (this.rows.delete(id)) removed = true;
    if (removed) this.touch();
  }
  remove(id) { this.rows.delete(id); this.touch(); }
  clear() { this.rows.clear(); this.touch(); }
  snapshot() { return new Map([...this.rows].map(([id, row]) => [id, selectionSignature(row)])); }
  complete(snapshot) {
    let removed = false;
    for (const [id, previous] of snapshot) {
      const current = this.rows.get(id);
      if (current && selectionSignature(current) === previous) {
        this.rows.delete(id); removed = true;
      }
    }
    if (removed) this.touch();
  }
  touch() { this.revision++; this.changed(); }
  selections(attachments = false) {
    if (!this.rows.size) throw new Error('请先选择导入范围。');
    const groups = new Map();
    for (const { item, range, messages } of this.rows.values()) {
      const dates = interval(range), key = JSON.stringify(dates);
      if (!groups.has(key)) groups.set(key, []);
      const group = groups.get(key);
      const ownMessages = messages ? { [item.id]: { ...messages } } : {};
      if (messageBytes(ownMessages) > 256 * 1024) throw new Error('此会话的消息选择清单过大，请减少所选消息。');
      let selection = group.at(-1);
      const combined = { ...selection?.messages, ...ownMessages };
      if (!selection || selection.scopes.length >= 200 || messageCount(combined) > 500 || messageBytes(combined) > 256 * 1024) {
        selection = { scopes: [], ...dates, include_attachments: attachments };
        group.push(selection);
      }
      selection.scopes.push(item.id);
      if (messages) selection.messages = { ...selection.messages, ...ownMessages };
    }
    return [...groups.values()].flat();
  }
}

const messageCount = messages => Object.values(messages || {}).reduce((n, entries) => n + Object.keys(entries).length, 0);
const messageBytes = messages => new TextEncoder().encode(JSON.stringify(messages)).length;

// The API has a per-request budget; it is not a limit on the user's selection.
export function scopeBatches(values, selectionOf = value => value) {
  const batches = [];
  let batch = [], scopes = 0, messages = 0;
  for (const value of values) {
    const selection = selectionOf(value), count = selection.scopes.length, selected = messageCount(selection.messages);
    if (count > 200 || selected > 500) throw new Error('单个导入范围超过请求容量，请缩小该范围。');
    if (batch.length && (scopes + count > 200 || messages + selected > 500)) {
      batches.push(batch); batch = []; scopes = 0; messages = 0;
    }
    batch.push(value); scopes += count; messages += selected;
  }
  if (batch.length) batches.push(batch);
  return batches;
}

function selectionSignature(row) {
  return JSON.stringify([row.range.start || '', row.range.end || '', row.messages && Object.entries(row.messages).sort(([a], [b]) => a.localeCompare(b))]);
}
