import { node, button, checkbox, nameOf, metadata, dateFields, size } from '../dom.js';

export const logs = {
  search: '搜索日志文件', listTitle: '日志', layout: 'collection-workbench',
  renderList(ctx, items) {
    return items.map(item => node('div', { class: 'catalog-row', 'data-item': item.id }, checkbox(ctx, item),
      button(nameOf(item), () => ctx.focus(item), { class: 'row-name' }), node('span', { class: 'row-number' }, size(item.size_bytes))));
  },
  renderDetail(ctx, item) {
    const dates = dateFields(ctx, item);
    return node('section', { class: 'detail-content' }, node('h2', {}, nameOf(item).split('/').pop()),
      metadata([['客户端', ctx.source.profile], ['文件', item.id]]), dates.element,
      ctx.addButton(item, dates.value));
  },
};
