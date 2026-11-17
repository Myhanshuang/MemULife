import { node, button, checkbox, nameOf, date, dateFields, loading, state } from '../dom.js';
import { interval } from '../state.js';

function duration(value) {
  return typeof value === 'string' ? value.replace(/\.\d+$/, '').replace(/^00:/, '') : '';
}

export const media = {
  search: '搜索相册', listTitle: '相册', layout: 'collection-workbench media-workbench', catalogParent: '.',
  renderList(ctx, items) {
    return items.map(item => {
      const check = checkbox(ctx, item);
      check.querySelector('input').addEventListener('change', () => { if (!ctx.isFocused(item)) ctx.focus(item); });
      return node('div', { class: 'catalog-row', 'data-item': item.id }, check,
        button(nameOf(item), () => ctx.focus(item), { class: 'row-name' }),
        node('span', { class: 'row-number' }, Number.isFinite(item.asset_count) ? `${item.asset_count} 项` : '全部'));
    });
  },
  renderDetail(ctx, album) {
    const dates = dateFields(ctx, album);
    const gallery = node('div', { class: 'media-gallery', 'aria-label': '照片与视频预览' });
    const notice = node('p', { class: 'media-preview-notice', role: 'status', hidden: true });
    const count = node('span', { class: 'muted', role: 'status' });
    let alive = true, generation = 0, cursor = '', controller, rows = [], busy = false, viewer;
    const cards = new Map();

    function covered(asset) {
      return [album.id, 'all_assets'].some(id => {
        const selection = ctx.basket.get(id);
        if (!selection) return false;
        const { start, end } = interval(selection.range);
        const stamp = Date.parse(asset.taken_at);
        return (!start && !end) || (Number.isFinite(stamp) && (!start || stamp >= Date.parse(start)) && (!end || stamp < Date.parse(end)));
      });
    }
    function updateSelection() {
      for (const asset of rows) {
        const card = cards.get(asset.id), inherited = covered(asset), selected = inherited || ctx.basket.has(asset.id);
        card.input.checked = selected; card.input.disabled = inherited;
        card.element.classList.toggle('is-selected', selected);
        card.element.title = inherited ? '已包含在所选相册中' : '';
      }
      selectAll.disabled = busy || !rows.some(asset => !covered(asset) && !ctx.basket.has(asset.id));
      deselectAll.disabled = busy || !rows.some(asset => ctx.basket.has(asset.id));
    }
    function show(asset) {
      viewer?.remove();
      const close = button('关闭预览', () => viewer.close(), { class: 'text-button' });
      viewer = node('dialog', { class: 'media-viewer', 'aria-label': nameOf(asset) },
        node('div', { class: 'media-viewer-heading' }, node('strong', {}, nameOf(asset)), close),
        node('img', { src: asset.thumbnail, alt: nameOf(asset) }),
        node('p', { class: 'muted' }, asset.media_type === 'VIDEO' ? `视频封面${duration(asset.duration) ? ' · ' + duration(asset.duration) : ''}` : date(asset.taken_at)));
      viewer.addEventListener('close', () => viewer?.remove());
      viewer.addEventListener('click', event => { if (event.target === viewer) viewer.close(); });
      document.body.append(viewer); viewer.showModal();
    }
    function tile(asset) {
      const input = node('input', { type: 'checkbox', 'aria-label': `选择 ${nameOf(asset)}`, onchange: () => ctx.toggle(asset, input.checked) });
      const picture = asset.thumbnail
        ? node('img', { src: asset.thumbnail, alt: nameOf(asset), loading: 'lazy', decoding: 'async' })
        : node('span', { class: 'media-placeholder' }, '预览暂不可用');
      const thumb = button('', () => show(asset), { class: 'media-thumb', 'aria-label': `预览 ${nameOf(asset)}`, disabled: !asset.thumbnail });
      thumb.replaceChildren(picture);
      if (asset.media_type === 'VIDEO') thumb.append(node('span', { class: 'media-video-badge' }, '视频', duration(asset.duration) ? ' · ' + duration(asset.duration) : ''));
      const element = node('article', { class: 'media-tile', 'data-asset': asset.id },
        thumb, node('label', { class: 'selection-check media-select' }, input),
        node('div', { class: 'media-caption' }, node('span', { title: nameOf(asset) }, nameOf(asset)),
          node('time', { datetime: asset.taken_at || '' }, date(asset.taken_at))));
      cards.set(asset.id, { element, input });
      return element;
    }
    const selectAll = button('全选已加载', () => {
      try { ctx.basket.addMany(rows.filter(asset => !covered(asset)).map(item => ({ item }))); }
      catch (error) { ctx.error(error); }
    }, { class: 'text-button' });
    const deselectAll = button('取消已加载', () => ctx.basket.removeMany(rows.map(asset => asset.id)), { class: 'text-button' });
    const more = button('加载更多', () => load(true), { class: 'text-button' }); more.hidden = true;

    async function load(append = false) {
      const version = ++generation;
      controller?.abort(); controller = new AbortController(); busy = true; notice.hidden = true;
      if (!append) { rows = []; cards.clear(); cursor = ''; more.hidden = true; count.textContent = ''; loading(gallery); }
      more.disabled = true; updateSelection();
      try {
        const result = await ctx.preview(album, dates.value(), append ? cursor : '', { signal: controller.signal, limit: 24 });
        if (!alive || !ctx.isFocused(album) || generation !== version) return;
        let grid = gallery.querySelector('.media-gallery-grid');
        if (!grid) { grid = node('div', { class: 'media-gallery-grid' }); gallery.replaceChildren(grid); }
        for (const asset of result.items) {
          if (cards.has(asset.id)) continue;
          rows.push(asset); grid.append(tile(asset));
        }
        cursor = result.next_cursor || ''; more.hidden = !cursor;
        count.textContent = `已加载 ${rows.length} 项`;
        if (result.warnings?.includes('immich_preview_permission_required')) {
          notice.textContent = '缩略图预览需要 asset.view 权限，请在 Immich 中为此 API Key 勾选后重试。'; notice.hidden = false;
        } else if (rows.some(asset => asset.preview_error)) {
          notice.textContent = '部分缩略图暂不可用，仍可选择对应内容。'; notice.hidden = false;
        }
        if (!rows.length) state(gallery, cursor ? '当前页没有符合日期的内容，可继续加载' : '当前日期范围没有照片或视频');
      } catch (error) {
        if (alive && generation === version && error.name !== 'AbortError') state(gallery, error.message, () => load());
      } finally {
        if (alive && generation === version) { busy = false; more.disabled = false; updateSelection(); }
      }
    }
    dates.element.addEventListener('change', () => { void load(); });
    const retry = button('刷新预览', () => load(), { class: 'text-button', icon: 'refresh' });
    ctx.onSelectionChange(updateSelection);
    ctx.onDetailDispose(() => { alive = false; generation++; controller?.abort(); viewer?.remove(); });
    void load();
    return node('section', { class: 'detail-content media-detail' },
      node('div', { class: 'media-detail-heading' }, node('h2', {}, nameOf(album)), retry),
      node('p', { class: 'setup-hint' }, '仅导入资源引用和描述等元数据，原件保留在 Immich。'),
      dates.element,
      node('div', { class: 'media-gallery-toolbar' }, count, node('div', { class: 'actions' }, selectAll, deselectAll)),
      notice, gallery, more);
  },
};
