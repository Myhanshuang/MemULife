import { icon } from './icons.js';

export function node(tag, attrs = {}, ...children) {
  const element = document.createElement(tag);
  if (tag === 'button') element.type = 'button';
  for (const [key, value] of Object.entries(attrs)) {
    if (key === 'class') element.className = value;
    else if (key.startsWith('on')) element.addEventListener(key.slice(2), value);
    else if (value !== null && value !== undefined && value !== false) {
      if (['value', 'checked', 'disabled', 'hidden'].includes(key)) element[key] = value;
      else element.setAttribute(key, value === true ? '' : value);
    }
  }
  element.append(...children.flat().filter(c => c !== null && c !== undefined));
  return element;
}

export const button = (label, action, attrs = {}) => {
  const { icon: glyph, ...attributes } = attrs;
  return node('button', { ...attributes, onclick: action }, glyph ? icon(glyph) : null, node('span', { class: 'button-label' }, label));
};
export const field = (label, control) => node('label', { class: 'field' }, node('span', {}, label), control);
export const nameOf = item => item.title || item.name || item.id;
export const date = value => value && !Number.isNaN(Date.parse(value))
  ? new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' }).format(new Date(value)) : '暂无日期';
export const timestamp = value => value && !Number.isNaN(Date.parse(value))
  ? new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' }).format(new Date(value)) : '';
export const size = bytes => Number.isFinite(bytes) ? (bytes < 1024 ? `${bytes} B` : bytes < 1048576 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1048576).toFixed(1)} MB`) : '';

export function state(region, label, retry) {
  region.replaceChildren(node('div', { class: 'region-state', role: 'status' },
    node('span', { class: 'empty-glyph' }, icon(retry ? 'refresh' : 'inbox')),
    node('p', {}, label), retry ? button('重试', retry, { icon: 'refresh' }) : null));
}

export function loading(region) {
  region.replaceChildren(node('div', { class: 'skeleton', role: 'status', 'aria-label': '正在读取' },
    ...Array.from({ length: 5 }, () => node('div', { 'aria-hidden': 'true' }))));
}

export function checkbox(ctx, item) {
  return node('label', { class: 'selection-check' }, node('input', { type: 'checkbox', 'aria-label': `选择 ${nameOf(item)}`,
    'data-selection': item.id, checked: ctx.basket.has(item.id), onchange: event => {
      try { ctx.toggle(item, event.target.checked); }
      catch (error) { event.target.checked = false; ctx.error(error); }
    } }));
}

export function metadata(entries) {
  const list = node('dl', { class: 'metadata' });
  for (const [key, value] of entries) if (value !== null && value !== undefined && value !== '') {
    list.append(node('dt', {}, key), node('dd', {}, String(value)));
  }
  return list;
}

export function dateFields(ctx, item) {
  const range = ctx.range(item);
  const start = node('input', { type: 'date', name: 'start', value: range.start || '', 'aria-label': '开始日期' });
  const end = node('input', { type: 'date', name: 'end', value: range.end || '', 'aria-label': '结束日期' });
  const changed = () => {
    end.setCustomValidity(start.value && end.value && start.value > end.value ? '结束日期不能早于开始日期' : '');
    ctx.setRange(item, { start: start.value, end: end.value });
  };
  start.addEventListener('change', changed); end.addEventListener('change', changed);
  return { element: node('div', { class: 'date-fields' }, field('开始日期', start), field('结束日期', end)),
    value: () => { if (!end.reportValidity()) throw new Error('请检查日期范围。'); return { start: start.value, end: end.value }; } };
}
