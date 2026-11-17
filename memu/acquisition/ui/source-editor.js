import { node, button, field } from './dom.js';
import { sourceIcon } from './icons.js';

export function sourceEditor({ client, page, source, onSaved, onDeleted, onCancel }) {
  const abort = new AbortController();
  let active = true, busy = false;
  const name = node('input', { name: 'connection_name', required: true, maxlength: 200, value: source.namespace });
  const error = node('p', { class: 'error', role: 'alert', hidden: true });
  const fields = node('fieldset', { class: 'setup-fieldset' }, field('连接名称', name),
    node('div', { class: 'setup-footer' }, button('删除连接', remove, { class: 'danger-button', icon: 'trash' }),
      node('div', { class: 'setup-actions' },
      button('取消', onCancel), button('保存更改', () => {}, { type: 'submit', class: 'primary', icon: 'check' }))));
  const form = node('form', {}, fields, error);
  const element = node('section', { class: 'setup-card', 'aria-label': '编辑连接账号' },
    node('div', { class: 'setup-heading' }, node('span', { class: 'source-glyph' }, sourceIcon(page.slug)),
      node('div', {}, node('h2', {}, '编辑连接账号'), node('p', {}, page.title))), form);
  async function remove() {
    if (busy || !confirm(`删除“${source.namespace}”的连接？\n\n将停止后续采集和持续更新。已接收的内容继续处理，已有记忆和导入记录保留。`)) return;
    busy = true; fields.disabled = true; error.hidden = true;
    try {
      await client(`imports/sources/${source.source_id}/delete`, 'POST', {}, abort.signal);
      if (active) await onDeleted();
    } catch (failure) {
      if (active && failure.name !== 'AbortError') { error.textContent = failure.message; error.hidden = false; }
    } finally { busy = false; fields.disabled = false; }
  }
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy || !form.reportValidity()) return;
    busy = true; fields.disabled = true; error.hidden = true;
    try {
      await client(`imports/sources/${source.source_id}`, 'POST', { name: name.value.trim() }, abort.signal);
      if (active) await onSaved();
    } catch (failure) {
      if (active && failure.name !== 'AbortError') { error.textContent = failure.message; error.hidden = false; }
    } finally { busy = false; fields.disabled = false; }
  });
  queueMicrotask(() => { if (active) name.focus(); });
  return { element, dispose() { active = false; abort.abort(); } };
}
