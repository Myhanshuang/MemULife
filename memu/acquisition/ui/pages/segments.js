import { node, button, checkbox, nameOf, metadata } from '../dom.js';

export const segments = {
  search: '搜索片段', listTitle: '已封存片段', layout: 'collection-workbench',
  renderList(ctx, items) {
    return items.map(item => node('div', { class: 'catalog-row', 'data-item': item.id }, checkbox(ctx, item),
      button(nameOf(item), () => ctx.focus(item), { class: 'row-name' }), node('span', { class: 'row-type' }, '已封存')));
  },
  renderDetail(ctx, item) {
    return node('section', { class: 'detail-content' }, node('h2', {}, '录屏片段'),
      metadata([['时间', nameOf(item)], ['片段', item.id]]), ctx.addButton(item));
  },
};
