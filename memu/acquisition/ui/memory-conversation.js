import { node } from './dom.js';

const roles = { user: '用户', assistant: '助手', system: '系统', tool: '工具', speaker: '发言人', unknown: '消息' };

export function conversation(item, compact = false) {
  const messages = item.display?.messages || [];
  const root = node('div', { class: 'memory-conversation', 'aria-label': '来源内容' });
  if (!messages.length) {
    if (item.body) root.append(node('div', { class: 'memory-message', 'data-role': 'document' },
      node('div', { class: 'memory-message-body' }, item.body + (compact && item.preview_truncated ? '…' : ''))));
    return root;
  }
  for (const message of compact ? messages.slice(0, 3) : messages) {
    const role = roles[message.role] ? message.role : 'unknown';
    const stamp = message.time && !Number.isNaN(Date.parse(message.time))
      ? new Date(message.time).toLocaleString('zh-CN', { hour12: false }) : '';
    root.append(node('div', { class: 'memory-message', 'data-role': role },
      node('div', { class: 'memory-message-heading' }, node('strong', {}, message.name || roles[role]),
        stamp ? node('time', { datetime: message.time }, stamp) : null, message.continued ? node('span', {}, '续') : null),
      node('div', { class: 'memory-message-body' }, message.body)));
  }
  return root;
}
