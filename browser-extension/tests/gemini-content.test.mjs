import test from 'node:test';
import assert from 'node:assert/strict';
import { projectMessages } from '../dist/src/messages.js';
import { conversationRecord } from '../dist/src/providers.js';
const item = { id: 'c_synthetic', locator: 'c_synthetic', revision: 'one' };
function turn(id, second, answerSecond = second + 40) {
  return [[item.id, 'r_' + id], null, [[`quoted question ${id}`]],
    [[['rc_' + id, [`answer ${id}`]]], null, [[1, null, '', 'rc_' + id, [answerSecond, 123456789]]]], [second, 987654321]];
}
const page = turns => ({ data: { frames: [[turns, null]] }, raw: 'ORIGINAL_TRANSPORT_WITH_DUPLICATE_RENDER_TREE', cursor: null });
test('Gemini maps native turn IDs, candidate-specific times, and chronological pagination without copying render trees', async () => {
  const pages = [page([turn('new', 1791874000)]), page([turn('old', 1791873000)])];
  const messages = await projectMessages('gemini', item, pages);
  assert.deepEqual(messages.map(m => m.id), ['turn:r_old:user', 'candidate:rc_old', 'turn:r_new:user', 'candidate:rc_new']);
  assert.equal(messages[0].occurred_at, new Date(1791873000987).toISOString());
  assert.equal(messages[1].occurred_at, new Date(1791873040123).toISOString());
  const record = await conversationRecord('gemini', item, pages);
  const raw = JSON.parse(record.raw);
  assert.equal(raw.messages.length, 4);
  assert.equal(raw.responses.length, 2);
  assert.equal(raw.time_basis.user, 'native_turn_time');
  assert.ok(!raw.messages.some(m => m.text.includes('RENDER_TREE')));
});
test('unknown and invalid timestamps remain unknown, and candidate time cannot leak to another candidate', async () => {
  const value = turn('one', 1791873000); value[4] = [1791873000, 1e9]; value[3][2][0][3] = 'another-candidate';
  const messages = await projectMessages('gemini', item, [page([value])]);
  assert.deepEqual(messages.map(m => m.occurred_at), [null, null]);
});
