// Contract tool only. No extension/provider capture is claimed yet.
// Preserve integer tokens too: JSON.parse's normal Number path would truncate
// contract-valid sequence counters larger than Number.MAX_SAFE_INTEGER.
import { readFileSync } from 'node:fs';

const value: unknown = JSON.parse(readFileSync(0, 'utf8'),
  (_key: string, parsed: unknown, context: { source?: string }) =>
    typeof parsed === 'number' ? JSON.rawJSON(context.source!) : parsed);
process.stdout.write(JSON.stringify(value));
