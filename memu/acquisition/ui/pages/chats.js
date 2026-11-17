import { node, button, checkbox, nameOf, date, timestamp, dateFields, loading, state } from '../dom.js';

export const chats = {
  search: '搜索会话', listTitle: '会话', layout: 'chat-workbench',
  kinds: [['', '全部'], ['group', '群聊'], ['private', '私聊']],
  renderList(ctx, items) {
    return items.map(item => node('div', { class: 'conversation-row', 'data-item': item.id },
      checkbox(ctx, item), openConversation(ctx, item)));
  },
  renderDetail(ctx, item) {
    const dates = dateFields(ctx, item), timeline = node('div', { class: 'timeline', 'aria-label': '消息预览' });
    let cursor = '', request = 0;
    const more = button('更多消息', () => preview(true)); more.hidden = true;
    async function preview(append = false) {
      const generation = ++request;
      try {
        const range = dates.value();
        if (!append) { cursor = ''; loading(timeline); }
        more.disabled = true;
        const value = await ctx.preview(item, range, append ? cursor : '');
        if (!ctx.isFocused(item) || request !== generation) return;
        if (!append) timeline.replaceChildren();
        for (const message of value.items) timeline.append(node('article', { class: 'message-row' },
          node('div', { class: 'message-meta' }, node('strong', {}, message.actor?.display_name || message.actor?.source_actor_id || '未知成员'),
            node('time', { datetime: message.occurred_at || '' }, timestamp(message.occurred_at))),
          node('p', {}, message.text || '非文本消息'), message.truncated ? node('small', {}, '内容已截断') : null,
          message.attachment_count ? node('small', {}, `${message.attachment_count} 个附件`) : null));
        if (!timeline.childElementCount) state(timeline, value.next_cursor ? '当前页没有符合日期的消息' : '当前日期范围没有消息');
        cursor = value.next_cursor || ''; more.hidden = !cursor;
      } catch (error) { if (error.name !== 'AbortError' && ctx.isFocused(item) && request === generation) state(timeline, error.message, () => preview()); }
      finally { if (request === generation) more.disabled = false; }
    }
    dates.element.addEventListener('change', () => {
      request++; cursor = ''; more.hidden = true; state(timeline, '选择日期后预览消息');
    });
    state(timeline, '选择日期后预览消息');
    return node('section', { class: 'detail-content' }, node('h2', {}, nameOf(item)),
      dates.element, node('div', { class: 'actions' },
        button('预览消息', () => preview()), ctx.addButton(item, dates.value)),
      timeline, more);
  },
};

function openConversation(ctx, item) {
  const open = button('', () => ctx.focus(item), { class: 'conversation-open', 'aria-label': nameOf(item) });
  open.append(node('strong', {}, nameOf(item)), node('span', { class: 'row-meta' },
    node('span', {}, item.kind === 'group' ? '群聊' : item.kind === 'private' ? '私聊' : '会话'),
    item.message_count !== null && item.message_count !== undefined ? node('span', {}, `${item.message_count} 条`) : null,
    item.last_at ? node('time', { datetime: item.last_at }, date(item.last_at)) : null));
  return open;
}
