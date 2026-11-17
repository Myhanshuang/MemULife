import { mkdir, readFile, readdir, writeFile, copyFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { stripTypeScriptTypes } from 'node:module';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const destination = join(root, 'dist');
await mkdir(destination, { recursive: true });
for (const entry of await readdir(join(root, 'src'), { recursive: true, withFileTypes: true })) {
  if (!entry.isFile() || !entry.name.endsWith('.ts') || entry.name.endsWith('.d.ts')) continue;
  const source = join(entry.parentPath, entry.name);
  const relative = source.slice(root.length + 1).replace(/\.ts$/, '.js');
  const target = join(destination, relative);
  await mkdir(dirname(target), { recursive: true });
  const code = stripTypeScriptTypes(await readFile(source, 'utf8'), { mode: 'strip' })
    .replace(/(from\s+['"][^'"]+)\.ts(['"])/g, '$1.js$2');
  await writeFile(target, code);
}
for (const name of ['manifest.json', 'options.html', 'options.css']) {
  await copyFile(join(root, name), join(destination, name));
}
await mkdir(join(destination, 'shared'), { recursive: true });
for (const name of ['tokens.css', 'base.css', 'chrome.css', 'style.css', 'setup.css', 'icons.js', 'chrome.js']) {
  await copyFile(join(root, '../memu/acquisition/ui', name), join(destination, 'shared', name));
}
console.log('Built browser-extension/dist (no downloaded runtime or bundler).');
