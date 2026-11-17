import { node, button, checkbox, nameOf, date, metadata, loading, state } from '../dom.js';

const kinds = { page_ids: '页面', database_ids: '数据库', data_source_ids: '数据源', document_ids: '文档', folder_tokens: '文件夹', wiki_spaces: '知识库' };
export const documents = {
  search: '搜索文档', listTitle: '文档', layout: 'collection-workbench', catalogParent: '.',
  kindsFor: page => page.slug === 'notion'
    ? [['', '全部'], ['page_ids', '页面'], ['database_ids', '数据库'], ['data_source_ids', '数据源']]
    : [['', '全部'], ['document_ids', '文档'], ['folder_tokens', '文件夹'], ['wiki_spaces', '知识库']],
  renderList(ctx, items) {
    return items.map(item => node('div', { class: 'catalog-row', 'data-item': item.id }, checkbox(ctx, item),
      button(nameOf(item), () => ctx.focus(item), { class: 'row-name' }),
      node('span', { class: 'row-type' }, kinds[item.kind] || '文档'),
      node('span', { class: 'row-date' }, item.updated_at ? date(item.updated_at) : '')));
  },
  renderDetail(ctx, item) {
    const scopes = { page_ids: '此页面', database_ids: '此数据库中的条目', data_source_ids: '此数据源中的条目',
      document_ids: '此文档', folder_tokens: '此文件夹中的文档', wiki_spaces: '此知识库中的文档' };
    const controller = new AbortController(), trail = ctx.trail();
    ctx.onDetailDispose(() => controller.abort());
    const content = node('div', { class: 'document-content' });
    const tabs = node('div', { class: 'filter-tabs document-tabs', role: 'group', 'aria-label': '文档内容' });
    const canPreview = ctx.source.preview && item.kind === 'page_ids';
    let mode = canPreview ? 'body' : 'children', generation = 0, cursor = '', busy = false;
    const more = button('加载更多', () => load(true)); more.hidden = true;
    const current = version => !controller.signal.aborted && version === generation;
    async function load(append = false) {
      if (busy && append) return;
      const version = ++generation, selectedMode = mode;
      busy = true; more.disabled = true;
      if (!append) { cursor = ''; more.hidden = true; loading(content); }
      try {
        const value = selectedMode === 'body'
          ? await ctx.preview(item, {}, append ? cursor : '', { signal: controller.signal })
          : await ctx.children(item, append ? cursor : '', { signal: controller.signal });
        if (!current(version)) return;
        if (!append) content.replaceChildren();
        else content.querySelector('.region-state')?.remove();
        if (selectedMode === 'body') {
          let body = content.querySelector('.document-body');
          if (!body) { body = node('div', { class: 'document-body' }); content.append(body); }
          for (const row of value.items) body.append(document.createTextNode(row.text || ''));
        } else {
          for (const child of value.items) content.append(node('div', { class: 'catalog-row', 'data-item': child.id },
            checkbox(ctx, child), button(nameOf(child), () => ctx.focus(child, [...trail, item]), { class: 'row-name' }),
            node('span', { class: 'row-type' }, kinds[child.kind] || '文档')));
        }
        cursor = value.next_cursor || ''; more.hidden = !cursor;
        if (!content.textContent.trim()) state(content, selectedMode === 'body' ? '暂无正文' : '没有下级内容');
      } catch (error) {
        if (current(version) && error.name !== 'AbortError') {
          if (append) ctx.error(error);
          else state(content, error.message, () => load());
        }
      } finally { if (current(version)) { busy = false; more.disabled = false; } }
    }
    function switchMode(value) {
      mode = value;
      for (const tab of tabs.children) tab.setAttribute('aria-pressed', String(tab.dataset.mode === mode));
      void load();
    }
    if (canPreview) tabs.append(button('正文', () => switchMode('body'), { 'data-mode': 'body', 'aria-pressed': String(mode === 'body') }));
    if (item.can_expand) tabs.append(button('下级内容', () => switchMode('children'), { 'data-mode': 'children', 'aria-pressed': String(mode === 'children') }));
    if (canPreview || item.can_expand) void load();
    return node('section', { class: 'detail-content document-detail' },
      trail.length ? node('nav', { class: 'breadcrumbs', 'aria-label': ctx.page.title + ' 路径' },
        ...trail.map((part, index) => button(nameOf(part), () => ctx.focus(part, trail.slice(0, index)))),
        node('span', { 'aria-current': 'page' }, nameOf(item))) : null,
      node('h2', {}, nameOf(item)),
      metadata([['类型', kinds[item.kind] || '文档'], ['范围', item.scope_description || scopes[item.kind] || '此文档'], ['更新', item.updated_at && date(item.updated_at)]]),
      node('div', { class: 'actions' }, ctx.addButton(item)), tabs, content, more,
      node('details', { class: 'object-identity' }, node('summary', {}, '标识'), node('p', {}, item.id)));
  },
};
