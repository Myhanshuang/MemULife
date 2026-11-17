import { onConnectorProgress } from '../connector.js';
import { node, button, checkbox, nameOf, date, timestamp, metadata, loading, state } from '../dom.js';
import { errorMessage } from '../client.js';

const messageTime = value => value ? new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' }).format(new Date(value)) : '时间未知';
const roles = { user: '用户', assistant: '助手', system: '系统', tool: '工具', unknown: '未知角色' };
const sameMessages = (a, b) => Boolean(a && Object.keys(a).length === Object.keys(b).length && Object.entries(b).every(([id, digest]) => a[id] === digest));

export const conversations = {
  search: '搜索会话', listTitle: '会话', layout: 'collection-workbench',
  renderList(ctx, items) {
    return items.map(item => {
      const check = checkbox(ctx, item);
      check.title = '选择完整会话';
      check.querySelector('input').setAttribute('aria-label', `选择完整会话 ${nameOf(item)}`);
      return node('div', { class: 'catalog-row', 'data-item': item.id }, check,
        button(nameOf(item), () => ctx.focus(item), { class: 'row-name' }),
        node('time', { class: 'row-date', datetime: item.updated_at || item.created_at || '' }, date(item.updated_at || item.created_at)),
        node('span', { class: 'row-number' }, Number.isFinite(item.message_count) ? `${item.message_count} 条` : ''));
    });
  },
  renderDetail(ctx, item) {
    let mode = ctx.basket.has(item.id) && !ctx.basket.get(item.id).messages ? 'whole' : 'messages';
    let request = 0, cursor = '', pendingCursor = '', fetching = false;
    const chosen = new Map(Object.entries(ctx.basket.get(item.id)?.messages || {})), known = new Map();
    const body = node('div', { class: 'message-picker' });
    const modes = node('div', { class: 'filter-tabs selection-mode', role: 'group', 'aria-label': '会话导入范围' });
    const wholeButton = button('选择完整会话', () => ctx.select(item));
    const list = node('div', { class: 'message-selection-list', 'aria-label': '会话消息' });
    const status = node('p', { class: 'inline-note', role: 'status', 'aria-live': 'polite', hidden: true });
    const count = node('strong', {}, '已勾选 0 条');
    const read = button('读取消息', () => readMessages(false));
    const more = button('更多消息', () => readMessages(true)); more.hidden = true;
    const apply = button('加入选择', () => {
      try { ctx.selectMessages(item, Object.fromEntries(chosen)); } catch (error) { ctx.error(error); }
    });
    const selectLoaded = button('全选已加载', () => {
      for (const message of known.values()) chosen.set(message.id, message.digest);
      draftChanged();
    });
    const clear = button('取消勾选', () => { chosen.clear(); draftChanged(); }, { class: 'text-button' });
    const controls = node('div', { class: 'message-selection-controls' }, count, node('div', { class: 'actions' }, selectLoaded, clear, apply));

    function refresh() {
      count.textContent = `已勾选 ${chosen.size} 条`;
      for (const input of list.querySelectorAll('[data-message]')) {
        input.checked = chosen.has(input.dataset.message); input.disabled = mode === 'whole'; input.parentElement.hidden = mode === 'whole';
      }
      const selected = ctx.basket.get(item.id)?.messages;
      const saved = sameMessages(selected, Object.fromEntries(chosen));
      apply.textContent = saved ? '已选择' : selected ? '应用选择' : '加入选择';
      apply.disabled = fetching || !chosen.size || saved;
      selectLoaded.disabled = fetching || !known.size; clear.disabled = fetching || !chosen.size;
      read.disabled = fetching; more.disabled = fetching;
      wholeButton.disabled = ctx.basket.has(item.id) && !selected;
      wholeButton.textContent = wholeButton.disabled ? '已选择完整会话' : '选择完整会话';
    }
    function draftChanged() {
      const saved = ctx.basket.get(item.id)?.messages;
      if (saved && !sameMessages(saved, Object.fromEntries(chosen))) ctx.remove(item);
      refresh();
    }
    function renderMode() {
      for (const control of modes.children) control.setAttribute('aria-pressed', String(control.dataset.mode === mode));
      if (mode === 'whole') body.replaceChildren(wholeButton, node('div', { class: 'actions' }, read), status, list, more);
      else body.replaceChildren(node('div', { class: 'actions' }, read), status, controls, list, more);
      refresh();
    }
    for (const [value, title] of [['messages', '选择消息'], ['whole', '完整会话']]) modes.append(button(title, () => {
      if (mode === value) return;
      mode = value; request++; fetching = false;
      ctx.remove(item); renderMode();
      if (!known.size) queueMicrotask(() => { if (ctx.isFocused(item)) void readMessages(false); });
    }, { 'data-mode': value }));
    ctx.onSelectionChange(() => {
      const row = ctx.basket.get(item.id);
      if (row && !row.messages && mode !== 'whole') { mode = 'whole'; request++; fetching = false; renderMode(); }
      else refresh();
    });

    async function readMessages(append) {
      if (fetching) return;
      const generation = ++request;
      fetching = true; refresh(); status.hidden = true;
      if (!append && !pendingCursor) loading(list);
      try {
        const value = await ctx.preview(item, {}, append ? cursor : pendingCursor);
        if (!ctx.isFocused(item) || generation !== request) return;
        if (value.status === 'pending') {
          pendingCursor = value.request_id; read.textContent = '刷新消息';
          state(list, '正在读取会话…'); more.hidden = true; return;
        }
        if (value.status !== 'ready') throw new Error(errorMessage(value.error_code));
        if (!append) { list.replaceChildren(); known.clear(); }
        pendingCursor = ''; read.textContent = '重新读取';
        let stale = false;
        for (const message of value.items) {
          if (chosen.has(message.id) && chosen.get(message.id) !== message.digest) { chosen.delete(message.id); stale = true; }
          if (known.has(message.id)) continue;
          known.set(message.id, message);
          const check = node('input', { type: 'checkbox', 'data-message': message.id,
            'aria-label': `选择消息 ${known.size}`, checked: chosen.has(message.id), onchange: event => {
              event.target.checked ? chosen.set(message.id, message.digest) : chosen.delete(message.id); draftChanged();
            } });
          list.append(node('article', { class: 'selectable-message' }, node('label', { class: 'selection-check' }, check),
            node('div', { class: 'message-content' }, node('div', { class: 'message-meta' },
              node('strong', {}, roles[message.role] || roles.unknown), node('time', { datetime: message.occurred_at || '', title: ctx.page.provider === 'gemini' ? (message.role === 'user' ? '平台轮次时间' : '平台候选回复记录时间') : '平台消息时间' }, messageTime(message.occurred_at))),
            node('p', {}, message.text || '非文本消息'))));
        }
        cursor = value.next_cursor || ''; more.hidden = !cursor;
        if (!cursor) for (const id of chosen.keys()) if (!known.has(id)) { chosen.delete(id); stale = true; }
        if (stale) { ctx.remove(item); status.textContent = '有消息已变化，请重新核对选择'; status.hidden = false; }
        if (!known.size) state(list, '没有可选择的消息');
      } catch (error) {
        if (error.name === 'AbortError' || !ctx.isFocused(item) || generation !== request) return;
        pendingCursor = ''; read.textContent = '重新读取';
        status.textContent = error.message; status.hidden = false;
        if (!known.size) state(list, '消息读取未完成');
      } finally { if (generation === request) { fetching = false; refresh(); } }
    }
    state(list, '正在读取会话…'); renderMode();
    const stop = onConnectorProgress(() => { if (pendingCursor && ctx.isFocused(item)) void readMessages(false); }, ctx.source.source_id);
    ctx.onDetailDispose?.(stop);
    queueMicrotask(() => { if (ctx.isFocused(item)) void readMessages(false); });
    return node('section', { class: 'detail-content' }, node('h2', {}, nameOf(item)),
      metadata([['更新', item.updated_at && date(item.updated_at)],
        ['创建', item.created_at && date(item.created_at)], ['消息', item.message_count]]), modes, body,
      node('details', { class: 'object-identity' }, node('summary', {}, '会话标识'), node('p', {}, item.id)));
  },
};
