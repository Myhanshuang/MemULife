import { node, button, checkbox, nameOf, size, metadata } from '../dom.js';

export const files = {
  search: '搜索当前目录', listTitle: '文件', layout: 'collection-workbench',
  kinds: [['', '全部'], ['directory', '目录'], ['file', '文件']],
  toolbar(ctx) {
    const path = ctx.params.get('parent') || '.';
    const parts = path === '.' ? [] : path.split('/');
    return node('nav', { class: 'breadcrumbs', 'aria-label': '文件路径' },
      button('根目录', () => ctx.directory('.')),
      ...parts.map((part, index) => button(part, () => ctx.directory(parts.slice(0, index + 1).join('/')))));
  },
  renderList(ctx, items) {
    return items.map(item => node('div', { class: 'catalog-row file-row', 'data-item': item.id }, checkbox(ctx, item),
      node('span', { class: 'file-kind', 'aria-hidden': 'true' }, item.kind === 'directory' ? '目录' : item.format || '文件'),
      button(nameOf(item), () => item.kind === 'directory' ? ctx.directory(item.id) : ctx.focus(item), { class: 'row-name' }),
      node('span', { class: 'row-number' }, size(item.size_bytes))));
  },
  renderDetail(ctx, item) {
    return node('section', { class: 'detail-content' }, node('h2', {}, nameOf(item)),
      metadata([['路径', item.id], ['大小', size(item.size_bytes)], ['格式', item.format || item.media_type || '文件']]),
      ctx.addButton(item));
  },
};
