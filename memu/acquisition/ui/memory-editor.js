import { node, button, field, loading, state } from './dom.js';

const labels = { unit: 'Unit', day: '日 Summary', month: '月 Summary', runtime: '记忆主题' };

export function memoryEditor(client, onSaved) {
  const element = node('dialog', { class: 'memory-dialog memory-edit-dialog', 'aria-label': '编辑记忆' });
  const abort = new AbortController();
  let active = true, version = 0, saving = false;
  element.addEventListener('cancel', event => { if (saving) event.preventDefault(); });
  element.addEventListener('close', () => { version++; });

  async function open(ref) {
    const current = ++version;
    loading(element); if (!element.open) element.showModal();
    try {
      const item = await client('memory/edit', 'POST', { ref }, abort.signal);
      if (!active || current !== version) return;
      const select = node('select', { 'aria-label': '选择条目' }, ...item.entries.map((entry, index) =>
        node('option', { value: String(index) }, `${index + 1}. ${entry.body.replace(/\s+/g, ' ').slice(0, 70)}`)));
      const body = node('textarea', { rows: 14, maxlength: 100000, required: true, 'aria-label': '记忆正文' });
      const badge = node('span', { class: 'status-badge memory-protected' });
      const message = node('div', { role: 'status', class: 'memory-edit-status' });
      const cancel = button('取消', () => element.close());
      const save = node('button', { type: 'submit', class: 'primary' }, '保存并保护');
      let entry, payload = null;
      function choose() {
        entry = item.entries[Number(select.value)]; body.value = entry.body; payload = null;
        badge.textContent = entry.protected ? '已保护' : '保存后保护';
        message.replaceChildren(); body.disabled = false; save.disabled = false; save.textContent = '保存并保护';
        body.setCustomValidity('');
      }
      select.addEventListener('change', choose); choose();
      body.addEventListener('input', () => body.setCustomValidity(''));
      const form = node('form', { onsubmit: async event => {
        event.preventDefault();
        body.setCustomValidity(body.value.trim() ? '' : '正文不能为空');
        if (!body.reportValidity()) return;
        if (!payload) {
          payload = { reason: `用户在记忆管理页面编辑 ${labels[item.kind]}`, idempotency_key: crypto.randomUUID(),
            changes: [{ ref: item.ref, ...(entry.entry_id ? { entry_id: entry.entry_id } : {}),
              expected_revision: entry.edit_revision, old_text: entry.body, new_text: body.value,
              protect: true, allow_protected: !!entry.protected }] };
        }
        saving = true; save.disabled = true; cancel.disabled = true; body.disabled = true; select.disabled = true;
        message.textContent = '正在保存…';
        try {
          await client('memory/revise', 'POST', payload);
          if (!active || current !== version) return;
          element.close(); void onSaved?.(item.ref);
        } catch (error) {
          if (!active || current !== version) return;
          message.replaceChildren(node('p', { class: 'error' }, error.message));
          if (error.code === 'memory_edit_conflict') {
            const currentBody = error.result?.conflicts?.[0]?.current?.body;
            if (currentBody !== undefined) message.append(node('details', {}, node('summary', {}, '查看最新内容'),
              node('pre', { class: 'memory-full-text' }, currentBody)));
            message.append(button('重新读取', () => open(ref), { class: 'text-button' }));
          } else if (error.code === 'memory_edit_invalid') {
            payload = null; save.disabled = false; body.disabled = false; select.disabled = false;
          } else {
            // The outcome may be unknown: retries keep the exact same payload
            // and key, and editing stays frozen until the result is resolved.
            save.disabled = false; save.textContent = '重试保存';
          }
        } finally { saving = false; cancel.disabled = false; }
      } }, node('div', { class: 'memory-dialog-heading' }, node('h2', {}, `编辑${labels[item.kind] || '记忆'}`), badge),
      node('div', { class: 'memory-detail-body' }, item.entries.length > 1 ? field('选择条目', select) : null,
        field('正文', body), message, node('div', { class: 'memory-card-actions' }, cancel, save)));
      element.replaceChildren(form); body.focus();
    } catch (error) {
      if (active && current === version && error.name !== 'AbortError') {
        state(element, error.message, () => open(ref)); element.append(button('关闭', () => element.close()));
      }
    }
  }
  return { element, open, dispose() { active = false; version++; abort.abort(); element.close(); element.remove(); } };
}
