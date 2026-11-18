import test from 'node:test';
import assert from 'node:assert/strict';
import { attachmentRefs, attachOriginals } from '../dist/src/attachments.js';
import { conversationRecord } from '../dist/src/providers.js';
import { AdapterError } from '../dist/src/types.js';

const item = { id: 'conversation', locator: 'conversation', revision: null };
const data = { mapping: { q: { id: 'q', message: { id: 'q', author: { role: 'user' }, create_time: 1790902800,
  content: { parts: [{ content_type: 'image_asset_pointer', asset_pointer: 'sediment://file_001', mime_type: 'image/jpeg' }] },
  metadata: { attachments: [{ id: 'file_001', name: 'photo.jpg', mime_type: 'image/jpeg' }] } } } } };
const pages = [{ data, raw: JSON.stringify(data), cursor: null }];

test('image-only messages and original bytes survive projection, deduplicated by native file ID', async () => {
  assert.equal(attachmentRefs('chatgpt', pages).length, 1);
  const record = await conversationRecord('chatgpt', item, pages);
  assert.match(JSON.parse(record.raw).messages[0].text, /附件/);
  let calls = 0;
  await attachOriginals('chatgpt', record, pages, true, async ref => {
    calls++; assert.equal(ref.id, 'file_001'); return { data: btoa('original bytes'), mime_type: 'image/jpeg' };
  }, async () => true);
  assert.equal(calls, 1);
  assert.equal(atob(record.attachments[0].data), 'original bytes');
  assert.match(record.metadata.original_raw_sha256, /^[0-9a-f]{64}$/);
  assert.equal(JSON.parse(record.raw).attachment_manifest[0].status, 'available');
});

test('excluded and unavailable attachments preserve the conversation; revoked selection stops import', async () => {
  const record = await conversationRecord('chatgpt', item, pages);
  await attachOriginals('chatgpt', record, pages, false, async () => assert.fail('excluded download'), async () => true);
  assert.equal(record.attachments[0].status, 'excluded');
  const mixed = structuredClone(data);
  mixed.mapping.q.message.metadata.attachments.push({id: 'file_002', name: 'notes.txt'});
  const mixedPages = [{data: mixed, raw: JSON.stringify(mixed), cursor: null}];
  const partial = await conversationRecord('chatgpt', item, mixedPages);
  const messages = JSON.parse(partial.raw).messages;
  await attachOriginals('chatgpt', partial, mixedPages, true, async ref => {
    if (ref.id === 'file_001') throw new AdapterError('attachment_source_unavailable');
    return {data: btoa('notes'), mime_type: 'text/plain'};
  }, async () => true);
  assert.deepEqual(JSON.parse(partial.raw).messages, messages);
  assert.deepEqual(partial.attachments.map(a => a.status), ['pending', 'available']);
  assert.equal(partial.attachments[0].metadata.error_code, 'attachment_source_unavailable');
  await assert.rejects(attachOriginals('chatgpt', record, pages, true,
    async () => assert.fail('revoked download'), async () => false), /import_selection_unavailable/);
});
