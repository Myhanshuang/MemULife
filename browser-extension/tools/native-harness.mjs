// Integration-only transport: exercise the production extension with a real host process.
import { spawn } from 'node:child_process';
import { deliver } from '../dist/src/native.js';

const [python, config, origin, importJob] = process.argv.slice(2);
const child = spawn(python, ['-m', 'memu', 'native-host', '--config', config, origin], {
  stdio: ['pipe', 'pipe', 'pipe'],
});
let buffer = Buffer.alloc(0);
let waiting;
child.stdout.on('data', chunk => {
  buffer = Buffer.concat([buffer, chunk]);
  if (buffer.length < 4) return;
  const size = buffer.readUInt32LE();
  if (buffer.length < size + 4) return;
  const reply = JSON.parse(buffer.subarray(4, size + 4).toString('utf8'));
  buffer = buffer.subarray(size + 4);
  if (waiting) { waiting.resolve(reply); waiting = null; }
});
child.on('error', error => { waiting?.reject(error); });
child.on('exit', code => { if (waiting) waiting.reject(new Error(`host_exit_${code}`)); });
const send = message => new Promise((resolve, reject) => {
  if (waiting) return reject(new Error('parallel_request'));
  waiting = { resolve, reject };
  const wire = Buffer.from(JSON.stringify(message));
  const header = Buffer.alloc(4);
  header.writeUInt32LE(wire.length);
  child.stdin.write(Buffer.concat([header, wire]));
});
try {
  const id = crypto.randomUUID();
  const text = '中文资料📎'.repeat(80_000);
  const message = { jsonrpc: '2.0', id, method: 'capture', params: { provider: 'chatgpt', account_id: 'account', workspace_id: 'workspace',
    batch_id: id, import_job_id: importJob, records: [{ object_id: 'conversation', raw: text, text, metadata: {} }], cursor: { page: 'fixture' } } };
  const first = await deliver(message, send);
  const replay = await deliver(message, send);
  if (JSON.stringify(first.event_ids) !== JSON.stringify(replay.event_ids)) throw new Error('duplicate_event');
  console.log(JSON.stringify({ status: 'passed', batch_id: id, event_ids: first.event_ids, durable_level: first.durable_level }));
} finally {
  child.stdin.end();
}
