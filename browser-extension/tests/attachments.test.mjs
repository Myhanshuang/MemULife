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

test('excluded attachments never download and a failed download cannot become a successful text-only import', async () => {
  const record = await conversationRecord('chatgpt', item, pages);
  await attachOriginals('chatgpt', record, pages, false, async () => assert.fail('excluded download'), async () => true);
  assert.equal(record.attachments[0].status, 'excluded');
  await assert.rejects(attachOriginals('chatgpt', record, pages, true,
    async () => { throw new AdapterError('attachment_download_failed'); }, async () => true), /attachment_download_failed/);
  await assert.rejects(attachOriginals('chatgpt', record, pages, true,
    async () => assert.fail('revoked download'), async () => false), /import_selection_unavailable/);
});
