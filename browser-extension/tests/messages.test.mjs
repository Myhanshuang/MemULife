import 'fake-indexeddb/auto';
import test from 'node:test';
import assert from 'node:assert/strict';
import { projectMessages, selectedRecord, messageDigest } from '../dist/src/messages.js';
import { Store } from '../dist/src/store.js';
import { Synchronizer } from '../dist/src/sync.js';

const item = { id: 'conversation', locator: 'conversation', revision: 'version' };
const binding = { id: 'binding', provider: 'chatgpt', account_id: 'account', workspace_id: 'workspace', enabled: true,
  next_sync_at: Date.now() + 900_000, label: 'fixture', identity_strength: 'native', status: 'queued', last_error: null };
const page = (data, cursor = null) => ({ data, raw: JSON.stringify(data), cursor });
const chat = (messages, cursor = null) => page({ mapping: Object.fromEntries(messages.map(([id, text, actor = 'user']) => [id,
  { id, message: { id, author: { role: actor }, content: { parts: [text, { asset_pointer: 'UNSELECTED_ASSET' }] }, create_time: 1790902800 } }])) }, cursor);
const previewRequest = { request_id: 'preview-request', conversation: item };
const empty = { job_id: null, scopes: [] };
const approval = (messages) => ({ job_id: 'import-selected', scopes: [item.id], items: [item], messages: { [item.id]: messages } });
const ack = message => ({ jsonrpc: '2.0', id: message.id, result: message.method === 'capture'
  ? { batch_id: message.params.batch_id, durable_level: 'edge_durable' } : { accepted: true } });
async function setup() { const store = await Store.open('message-fixture-' + crypto.randomUUID()); await store.bind(binding); return store; }
async function stored(store) {
  return JSON.stringify(await Promise.all(['bindings', 'lists', 'jobs', 'conversations', 'outbox', 'receipts', 'attachments'].map(name => store.all(name))));
}

test('six platforms expose stable text messages without opaque response blocks', async () => {
  const fixtures = [
    ['chatgpt', chat([['question', '问题📎'], ['answer', '回答', 'assistant']])],
    ['claude', page({ chat_messages: [{ uuid: 'q', sender: 'human', text: 'question', created_at: '2026-10-01T01:00:00Z' },
      { uuid: 'a', sender: 'assistant', content: [{ type: 'text', text: 'answer' }, { type: 'tool_use', input: { secret: 'OPAQUE' } }] }] })],
    ['gemini', page({ frames: [[[['turn', null, [['question']], [[['candidate', ['answer']]]]]], null]] })],
    ['perplexity', page({ entries: [{ uuid: 'entry', query_str: 'question', blocks: [{ markdown_block: { answer: 'answer', citations: ['OPAQUE'] } }] }] })],
    ['deepseek', page({ data: { biz_data: { chat_messages: [{ id: 10, role: 'USER', content: 'question' },
      { id: 11, role: 'ASSISTANT', fragments: [{ type: 'RESPONSE', content: 'answer' }, { type: 'FILE', file: { name: 'OPAQUE' } }] }] } } })],
    ['grok', page({ responses: [{ responseId: 'q', isHuman: true, message: 'question' }, { responseId: 'a', isHuman: false, message: 'answer' }] })],
  ];
  for (const [provider, raw] of fixtures) {
    const messages = await projectMessages(provider, item, [raw]);
    assert.equal(messages.length, 2, provider);
    assert.deepEqual(messages.map(m => m.role), ['user', 'assistant'], provider);
    assert.deepEqual(await projectMessages(provider, item, [raw]), messages, provider);
    assert.ok(!JSON.stringify(messages).includes('OPAQUE'), provider);
    for (const message of messages) {
      assert.equal(message.digest, await messageDigest(message));
      assert.deepEqual(Object.keys(message), ['id', 'role', 'text', 'occurred_at', 'digest']);
    }
  }
});

test('canonical digest retains Unicode, newlines and null without locale transformation', async () => {
  const message = { id: 'id-📎', role: 'user', text: '中文\n"quote"\t\u2028', occurred_at: null };
  const { createHash } = await import('node:crypto');
  assert.equal(await messageDigest(message), createHash('sha256').update(JSON.stringify(['id-📎', 'user', '中文\n"quote"\t\u2028', null])).digest('hex'));
});

test('missing IDs are content/path addressed and distinct repeated messages stay distinct', async () => {
  const raw = page({ chat_messages: [{ text: 'repeat', sender: 'human' }, { text: 'repeat', sender: 'human' }] });
  const messages = await projectMessages('claude', item, [raw]);
  assert.equal(messages.length, 2); assert.notEqual(messages[0].id, messages[1].id);
  assert.ok(messages.every(m => m.id.startsWith('derived:')));
  assert.deepEqual(await projectMessages('claude', item, [raw]), messages);
});

test('overlapping pages deduplicate identical native messages but conflicting IDs fail closed', async () => {
  const one = chat([['same', 'message']]);
  assert.equal((await projectMessages('chatgpt', item, [one, one])).length, 1);
  await assert.rejects(projectMessages('chatgpt', item, [one, chat([['same', 'changed']])]), /message_identity_conflict/);
  await assert.rejects(projectMessages('chatgpt', item, [chat([['bad', 'broken\ud800text']])]), /incompatible_schema/);
});

test('selected records contain exactly the chosen canonical messages and refuse changed or missing ones', async () => {
  const messages = await projectMessages('chatgpt', item, [chat([['q', 'SELECTED'], ['a', 'UNSELECTED']])]);
  const choice = { q: messages[0].digest };
  const record = await selectedRecord('chatgpt', item, messages, choice);
  assert.equal(record.text, 'SELECTED'); assert.ok(!JSON.stringify(record).includes('UNSELECTED'));
  assert.deepEqual(Object.keys(JSON.parse(record.raw)), ['format', 'provider', 'native_conversation_id', 'messages']);
  assert.deepEqual(JSON.parse(record.raw).messages.map(m => m.id), ['q']);
  assert.equal(record.revision, null);
  await assert.rejects(selectedRecord('chatgpt', item, messages, { missing: messages[0].digest }), /import_message_changed/);
  await assert.rejects(selectedRecord('chatgpt', item, messages, { q: 'f'.repeat(64) }), /import_message_changed/);
  await assert.rejects(selectedRecord('chatgpt', item, messages, {}), /import_message_selection_invalid/);
});

test('message projection never silently truncates the count or byte budget', async () => {
  await assert.rejects(projectMessages('chatgpt', item, [chat(Array.from({ length: 501 }, (_, index) => [String(index), 'body']))]), /preview_budget_exceeded/);
  await assert.rejects(projectMessages('chatgpt', item, [chat([['oversize', '中'.repeat(400_000)]])]), /preview_budget_exceeded/);
});

test('preview pages and unselected message bodies never enter any IndexedDB store', async () => {
  const store = await setup(), sent = [];
  let claimed = false;
  const provider = { assertBinding: async () => {}, detail: async () => chat([['q', 'PREVIEW_PRIVATE_BODY']]) };
  const send = async message => {
    if (message.method === 'selection.get') {
      if (message.params.claim_previews && !claimed) { claimed = true; return { id: message.id, result: { ...empty, previews: [previewRequest] } }; }
      return { id: message.id, result: empty };
    }
    sent.push(message); return ack(message);
  };
  await new Synchronizer(store, provider, send).run(binding);
  assert.equal(sent.length, 1); assert.equal(sent[0].method, 'preview.offer');
  assert.equal(sent[0].params.messages[0].text, 'PREVIEW_PRIVATE_BODY');
  const durable = await stored(store);
  assert.ok(!durable.includes('PREVIEW_PRIVATE_BODY')); assert.ok(!durable.includes('preview-request'));
  assert.equal((await store.all('jobs')).length, 0);
  store.db.close();
});

test('failed preview pagination sends only a stable error and leaves no body checkpoint', async () => {
  const store = await setup(), sent = [];
  let count = 0;
  const provider = { assertBinding: async () => {}, detail: async () => {
    if (count++) throw new Error('SENSITIVE_EXCEPTION_BODY');
    return chat([['q', 'TRANSIENT_PRIVATE_BODY']], { next: 'two' });
  } };
  const send = async message => {
    if (message.method === 'selection.get') return { id: message.id, result: { ...empty, previews: message.params.claim_previews ? [previewRequest] : [] } };
    sent.push(message); return ack(message);
  };
  await new Synchronizer(store, provider, send).run(binding);
  assert.equal(sent[0].params.error_code, 'adapter_failed');
  assert.equal(sent[0].params.messages, undefined);
  assert.ok(!JSON.stringify(sent).includes('SENSITIVE_EXCEPTION'));
  assert.ok(!(await stored(store)).includes('PRIVATE_BODY'));
  store.db.close();
});

test('message-scoped jobs read all pages in RAM and stage only selected text', async () => {
  const store = await setup(), sent = [];
  const pages = [chat([['q', 'SELECTED_BODY']], { next: 'two' }), chat([['a', 'UNSELECTED_BODY']])];
  const projected = await projectMessages('chatgpt', item, pages);
  const selection = approval({ q: projected[0].digest });
  const provider = { assertBinding: async () => {}, detail: async (_binding, _item, cursor) => pages[cursor ? 1 : 0] };
  const sync = new Synchronizer(store, provider, async message => {
    if (message.method === 'selection.get') return { id: message.id, result: selection };
    sent.push(message); return ack(message);
  });
  await sync.run(binding);
  const captures = sent.filter(m => m.method === 'capture');
  assert.equal(captures.length, 1); assert.equal(captures[0].params.records[0].text, 'SELECTED_BODY');
  assert.ok(!JSON.stringify(captures).includes('UNSELECTED'));
  assert.ok(!(await stored(store)).includes('UNSELECTED'));
  assert.equal((await store.all('jobs')).length, 0);
  store.db.close();
});

test('changed selected messages and late revoked selections persist no fetched body', async () => {
  for (const revoke of [false, true]) {
    const store = await setup();
    const original = await projectMessages('chatgpt', item, [chat([['q', 'ORIGINAL']])]);
    const selection = approval({ q: original[0].digest });
    let read = false;
    const provider = { assertBinding: async () => {}, detail: async () => { read = true; return chat([['q', 'DO_NOT_SAVE_CHANGED']]); } };
    await new Synchronizer(store, provider, async message => {
      assert.equal(message.method, 'selection.get');
      return { id: message.id, result: revoke && read ? empty : selection };
    }).run(binding);
    assert.equal((await store.all('outbox')).length, 0);
    assert.ok(!(await stored(store)).includes('DO_NOT_SAVE'));
    assert.deepEqual((await store.all('jobs'))[0].pages, []);
    store.db.close();
  }
});

test('Store rejects full page checkpoints or staging after a message selection change', async () => {
  const store = await setup();
  const selection = approval({ q: 'a'.repeat(64) });
  await store.updateBinding(binding.id, { import_selection: selection });
  await store.discover(binding, [item], await store.get('lists', binding.id));
  const [job] = await store.all('jobs');
  await store.progressJob(job, { pages: [chat([['a', 'UNSELECTED']])] });
  assert.deepEqual((await store.all('jobs'))[0].pages, []);
  const id = crypto.randomUUID();
  const message = { id, jsonrpc: '2.0', method: 'capture', params: { import_job_id: selection.job_id, batch_id: id, records: [{ object_id: item.id }] } };
  assert.equal(await store.stage(job, message, 'fingerprint'), false);
  assert.equal(await store.stage(job, message, 'fingerprint', { q: 'b'.repeat(64) }), false);
  assert.equal((await store.all('outbox')).length, 0);
  store.db.close();
});
