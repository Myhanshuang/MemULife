import { node, button, field, state, loading } from './dom.js';
import { memoryEditor } from './memory-editor.js';

export function topicsPage(client) {
  const abort = new AbortController();
  let active = true, version = 0;
  const list = node('div', { class: 'topic-list' });
  const memory = memoryEditor(client, refresh);
  const content = node('section', { class: 'topics-page' },
    node('div', { class: 'page-heading' }, node('h1', {}, '记忆主题'),
      node('div', { class: 'setup-actions' }, button('刷新', refresh, { icon: 'refresh' }),
        button('添加主题', () => edit(), { class: 'primary', icon: 'plus' }))), list, memory.element);

  function edit(topic = {}) {
    const current = ++version;
    const name = node('input', { value: topic.key || '', required: true, maxlength: 80, readonly: !!topic.key });
    const scope = node('textarea', { rows: 4, maxlength: 1200 }); scope.value = topic.description || '';
    const message = node('div', { class: 'notice error', role: 'alert', hidden: true });
    const save = node('button', { type: 'submit', class: 'primary' }, '保存');
    const form = node('form', { class: 'topic-editor', onsubmit: async event => {
      event.preventDefault(); save.disabled = true; message.hidden = true;
      try {
        await client('topics', 'POST', { key: name.value.trim(), description: scope.value.trim() }, abort.signal);
        if (active && current === version) await refresh();
      } catch (error) {
        if (active && current === version && error.name !== 'AbortError') {
          message.textContent = error.message; message.hidden = false;
        }
      } finally { save.disabled = false; }
    } }, field('主题名称', name), field('关注范围', scope), message,
      node('div', { class: 'setup-actions' }, button('取消', refresh), save));
    list.replaceChildren(form); name.focus();
  }

  async function refresh() {
    const current = ++version; loading(list);
    try {
      const result = await client('topics', 'GET', undefined, abort.signal);
      if (!active || current !== version) return;
      list.replaceChildren();
      if (!result.topics.length) state(list, '暂无跟踪主题');
      for (const topic of result.topics) {
        const status = { queued: '等待更新', running: '更新中', failed: '更新失败', done: '已更新' }[topic.state] || topic.state;
        list.append(node('article', { class: 'topic-card' },
          node('div', { class: 'topic-heading' }, node('h2', {}, topic.key),
            topic.protected_count ? node('span', { class: 'status-badge memory-protected' }, '已保护') : null,
            node('span', { class: 'status-badge', 'data-state': topic.state === 'done' ? 'completed' : topic.state }, status),
            button('编辑主题', () => edit(topic))),
          node('dl', { class: 'topic-meta' }, node('dt', {}, '来源'), node('dd', {}, topic.origin === 'user' ? '用户指定' : '自动发现'),
            node('dt', {}, '记录'), node('dd', {}, String(topic.unit_count))),
          node('div', { class: 'topic-scope' }, node('h3', {}, '关注范围'), node('p', {}, topic.description)),
          node('div', { class: 'topic-body' }, node('h3', {}, '当前记忆'), node('p', {}, topic.body || '暂无记录')),
          topic.ref && topic.body ? node('div', { class: 'memory-card-actions' },
            button('编辑记忆', () => memory.open(topic.ref))) : null));
      }
      if (result.discovery.state === 'failed') list.prepend(node('div', { class: 'notice error', role: 'alert' },
        '主题发现失败', button('重试', async event => {
          event.currentTarget.disabled = true;
          try { await client('topics/retry', 'POST', {}, abort.signal); if (active) await refresh(); }
          catch (error) { if (active && error.name !== 'AbortError') state(list, error.message, refresh); }
        })));
    } catch (error) {
      if (active && current === version && error.name !== 'AbortError') state(list, error.message, refresh);
    }
  }
  void refresh();
  return { element: content, dispose() { active = false; version++; abort.abort(); memory.dispose(); } };
}
