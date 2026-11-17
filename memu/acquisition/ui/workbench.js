import { onConnectorProgress } from './connector.js';
import { node, button, field, nameOf, loading, state } from './dom.js';
import { interval, rangeFor, scopeBatches } from './state.js';
import { icon } from './icons.js';

export function workbench({ client, page, source, basket, params, updateURL, changed }) {
  const view = page.view, abort = new AbortController(), drafts = new Map();
  let detailDispose = () => {};
  let detailChanged = () => {}, catalog = { breadcrumbs: [] }, detailTrail = [];
  let active = true, request = 0, focusVersion = 0, focused = null, items = [], cursor = '', plans = null, pending = false, catalogLoading = false;
  const root = node('div', { class: `workbench ${view.layout}` });
  const notice = node('p', { class: 'notice', role: 'status', 'aria-live': 'polite', hidden: true });
  const list = node('div', { class: 'candidate-list', 'aria-label': view.listTitle });
  const detail = node('div', { class: 'candidate-detail', 'aria-label': '选择详情' });
  const basketRows = node('div', { class: 'basket-rows' });
  const count = node('strong', {}, `已选 ${basket.rows.size} 项`);
  const planRegion = node('div', { class: 'plan-region', 'aria-live': 'polite' });
  const more = button('加载更多', () => load(true)); more.hidden = true;
  const attachments = node('input', { type: 'checkbox', name: 'attachments', onchange: () => { if (!pending) renderBasket(); } });
  const attached = field('包含附件', attachments);
  attached.classList.add('check-field');
  const error = value => { if (!active || value.name === 'AbortError') return; notice.textContent = value.message || String(value); notice.hidden = false; notice.classList.add('error'); };
  const ctx = {
    source, page, basket, params, error,
    catalog: () => catalog,
    trail: () => detailTrail,
    onSelectionChange(fn) { detailChanged = fn; },
    onDetailDispose(fn) { detailDispose = fn; },
    range: item => rangeFor(item.id, drafts, basket, params),
    setRange(item, range) {
      if (pending || !active) return;
      drafts.set(item.id, { ...range }); focusVersion++;
      if (basket.has(item.id)) { basket.get(item.id).range = { ...range }; basket.touch(); }
      setParam('start', range.start); setParam('end', range.end); updateURL(params);
    },
    select(item, range = ctx.range(item)) { if (pending || !active) return; try { basket.add(item, range); } catch (e) { error(e); } },
    selectMessages(item, messages) { if (pending || !active) return; basket.add(item, {}, messages); },
    remove(item) { if (!pending && active) basket.remove(item.id); },
    addButton(item, getRange = () => ctx.range(item)) {
      return button(basket.has(item.id) ? '已选择' : '加入选择', () => {
        try { ctx.select(item, getRange()); } catch (e) { error(e); }
      }, { 'data-add-selection': item.id, disabled: basket.has(item.id) });
    },
    toggle(item, checked) { if (pending || !active) return; checked ? basket.add(item, ctx.range(item)) : basket.remove(item.id); },
    isFocused: item => active && focused?.id === item.id,
    focus(item, trail = []) {
      if (pending || !active) return;
      const range = ctx.range(item);
      if (focused) drafts.set(focused.id, ctx.range(focused));
      drafts.set(item.id, { ...range });
      focused = item; detailTrail = trail; focusVersion++;
      setParam('focus', item.id); setParam('start', range.start); setParam('end', range.end);
      updateURL(params); detailDispose(); detailDispose = () => {}; detailChanged = () => {}; detail.replaceChildren(view.renderDetail(ctx, item));
      for (const row of list.querySelectorAll('[data-item]')) row.classList.toggle('is-focused', row.dataset.item === item.id);
    },
    directory(parent) {
      if (pending || !active) return;
      params.set('parent', parent); params.delete('cursor'); params.delete('focus'); params.delete('start'); params.delete('end'); params.delete('query'); params.delete('kind'); search.value = '';
      focused = null; focusVersion++; detailDispose(); detailDispose = () => {}; detailChanged = () => {}; updateURL(params); renderExtra();
      for (const b of types.children) b.setAttribute('aria-pressed', String(b.dataset.kind === ''));
      state(detail, page.slug === 'notion' ? '选择页面或条目' : '选择文件'); void load();
    },
    async preview(item, range, nextCursor, options = {}) {
      if (pending || !active) throw new DOMException('Preview unavailable', 'AbortError');
      const version = focusVersion;
      const value = await client(`imports/sources/${source.source_id}/preview`, 'POST', {
        selection: { scopes: [item.id], ...interval(range) }, cursor: nextCursor, limit: options.limit || 30,
      }, options.signal ? AbortSignal.any([abort.signal, options.signal]) : abort.signal);
      if (!active || focused?.id !== item.id || version !== focusVersion) throw new DOMException('Expired preview', 'AbortError');
      return value;
    },
    async children(item, nextCursor = '', options = {}) {
      if (pending || !active) throw new DOMException('Preview unavailable', 'AbortError');
      const version = focusVersion;
      const query = new URLSearchParams({ parent: item.id, limit: '100' });
      if (nextCursor) query.set('cursor', nextCursor);
      const value = await client(`imports/sources/${source.source_id}/candidates?${query}`, 'GET', undefined,
        options.signal ? AbortSignal.any([abort.signal, options.signal]) : abort.signal);
      if (!active || focused?.id !== item.id || version !== focusVersion) throw new DOMException('Expired preview', 'AbortError');
      return value;
    },
  };
  const search = node('input', { type: 'search', name: 'query', value: params.get('query') || '', 'aria-label': view.search, placeholder: '名称或标识' });
  const searchForm = node('form', { class: 'search-row', onsubmit: event => { event.preventDefault(); searchNow(); } },
    field(view.search, node('span', { class: 'search-input' }, icon('search'), search)), button('搜索', () => searchNow(), { icon: 'arrow', class: 'icon-button', title: '搜索' }));
  let searchTimer;
  function searchNow() { clearTimeout(searchTimer); if (pending || !active) return; params.delete('cursor'); setParam('query', search.value.trim()); updateURL(params); void load(); }
  search.addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(searchNow, 300); });
  const types = node('div', { class: 'filter-tabs', role: 'group', 'aria-label': '类型' });
  const kindOptions = view.kindsFor?.(page) || view.kinds || [];
  for (const [value, label] of kindOptions) types.append(button(label, () => {
    if (pending || !active) return;
    setParam('kind', value); params.delete('cursor'); updateURL(params);
    for (const b of types.children) b.setAttribute('aria-pressed', String(b.dataset.kind === value)); void load();
  }, { 'data-kind': value, 'aria-pressed': String((params.get('kind') || '') === value) }));
  const extra = node('div', { class: 'catalog-extra' });
  function renderExtra() { extra.replaceChildren(...(view.toolbar ? [view.toolbar(ctx)] : [])); }
  renderExtra();
  const loadedCount = node('span', { class: 'count-badge', 'aria-label': '已加载数量' }, '0');
  const selectAll = button('全选已加载', () => {
    if (pending || catalogLoading || !active) return;
    notice.hidden = true;
    try { basket.addMany(items.map(item => ({ item, range: ctx.range(item) }))); }
    catch (e) { error(e); }
  }, { class: 'text-button', title: '选择当前筛选下已加载的项目，保留已有日期和消息范围' });
  const deselectAll = button('取消全选', () => {
    if (!pending && !catalogLoading && active) basket.removeMany(items.map(item => item.id));
  }, { class: 'text-button', title: '只取消当前列表中的选择' });
  const bulkSelection = node('div', { class: 'catalog-selection', role: 'group', 'aria-label': '批量选择' }, selectAll, deselectAll);
  const listPanel = node('section', { class: 'catalog-panel', 'aria-label': `${page.title}${view.listTitle}` },
    node('div', { class: 'catalog-heading' }, node('strong', {}, view.listTitle), loadedCount), searchForm, types, extra, bulkSelection, list, more);
  const review = button('核对范围', prepare, { class: 'primary', icon: 'arrow' });
  const summary = node('aside', { class: 'selection-panel', role: 'region', 'aria-label': '已选范围' },
    node('div', { class: 'selection-heading' }, count, button('清空', () => { if (!pending) basket.clear(); }, { class: 'text-button' })),
    basketRows, attached, review, planRegion, node('span', { class: 'selection-privacy' }, icon('lock'), '只采集所选范围'));
  root.append(notice, listPanel, detail, summary);
  state(detail, view === page.view && page.group === '聊天' ? '选择一个会话' : '选择一项查看范围');
  function setParam(name, value) { value ? params.set(name, value) : params.delete(name); }
  function renderBulkSelection() {
    const selected = items.filter(item => basket.has(item.id)).length;
    const allSelected = items.length > 0 && selected === items.length;
    selectAll.disabled = pending || catalogLoading || !items.length || allSelected;
    deselectAll.disabled = pending || catalogLoading || !selected;
    selectAll.querySelector('.button-label').textContent = allSelected ? '已全选' : '全选已加载';
  }
  function setPending(value) {
    pending = value;
    if (value) clearTimeout(searchTimer);
    listPanel.inert = value; detail.inert = value; summary.inert = value;
    root.setAttribute('aria-busy', String(value));
    review.disabled = value || !basket.rows.size; attachments.disabled = value;
    renderBulkSelection();
  }

  function renderBasket() {
    plans = null; planRegion.replaceChildren(); review.hidden = false; count.textContent = `已选 ${basket.rows.size} 项`;
    basketRows.replaceChildren();
    for (const { item, range, messages } of [...basket.rows.values()].slice(0, 100)) basketRows.append(node('div', { class: 'basket-item' },
      node('div', {}, node('strong', {}, nameOf(item)), node('small', {}, messages ? `${Object.keys(messages).length} 条消息`
        : range.start || range.end ? `${range.start || '不限开始'} 至 ${range.end || '持续至今'}` : page.group === 'AI' ? '完整会话' : item.scope_description || '全部范围')),
      button('移除', () => { if (!pending) basket.remove(item.id); }, { class: 'text-button', 'aria-label': `移除 ${nameOf(item)}` })));
    if (basket.rows.size > 100) basketRows.append(node('p', { class: 'muted' }, `另有 ${basket.rows.size - 100} 项已选，可在左侧列表取消选择。`));
    if (!basket.rows.size) basketRows.append(node('p', { class: 'muted' }, '尚未选择'));
    for (const input of root.querySelectorAll('[data-selection]')) {
      input.checked = basket.has(input.dataset.selection);
      input.indeterminate = Boolean(basket.get(input.dataset.selection)?.messages);
      input.disabled = input.indeterminate;
    }
    for (const button of detail.querySelectorAll('[data-add-selection]')) {
      button.disabled = basket.has(button.dataset.addSelection); button.textContent = button.disabled ? '已选择' : '加入选择';
    }
    for (const row of list.querySelectorAll('[data-item]')) row.classList.toggle('is-selected', basket.has(row.dataset.item));
    review.disabled = pending || !basket.rows.size; attachments.disabled = pending;
    renderBulkSelection();
    detailChanged();
  }
  basket.changed = renderBasket; renderBasket();

  async function load(append = false) {
    if (pending || !active) return;
    const generation = ++request;
    notice.hidden = true;
    catalogLoading = true;
    if (!append) { items = []; cursor = ''; loadedCount.textContent = '0'; loading(list); }
    renderBulkSelection();
    more.disabled = true;
    const query = new URLSearchParams({ limit: '100', parent: view.catalogParent || params.get('parent') || '.' });
    for (const key of ['query', 'kind']) if (params.get(key)) query.set(key, params.get(key));
    const next = append ? cursor : params.get('cursor'); if (next) query.set('cursor', next);
    try {
      const value = await client(`imports/sources/${source.source_id}/candidates?${query}`, 'GET', undefined, abort.signal);
      if (!active || generation !== request) return;
      catalog = value; renderExtra();
      if (!append) { items = []; list.replaceChildren(); }
      else list.querySelector('.region-state')?.remove();
      items.push(...value.items); loadedCount.textContent = String(items.length); list.append(...view.renderList(ctx, value.items));
      cursor = value.next_cursor || ''; more.hidden = !cursor;
      if (!items.length) state(list, params.get('query') ? '没有匹配项' : '暂无可选内容');
      if (value.warnings?.length) {
        notice.textContent = value.metadata_available === false ? '名称暂不可用，当前显示已配置范围'
          : value.warnings.includes('metadata_budget_exceeded') ? '部分名称尚未读取，可继续加载' : '部分名称暂不可用，当前保留已配置范围';
        notice.hidden = false; notice.classList.remove('error');
      }
      for (const row of list.querySelectorAll('[data-item]')) row.classList.toggle('is-selected', basket.has(row.dataset.item));
      for (const input of list.querySelectorAll('[data-selection]')) {
        input.indeterminate = Boolean(basket.get(input.dataset.selection)?.messages); input.disabled = input.indeterminate;
      }
      if (!focused && params.get('focus')) { const item = items.find(i => i.id === params.get('focus')); if (item) ctx.focus(item); }
    } catch (e) {
      if (active && generation === request && e.name !== 'AbortError') {
        if (append) error(e);
        else state(list, e.message, () => load());
      }
    }
    finally {
      if (generation === request) { catalogLoading = false; more.disabled = false; renderBulkSelection(); }
    }
  }

  async function prepare() {
    if (pending) return;
    const revision = basket.revision, included = attachments.checked;
    try {
      const selections = basket.selections(included);
      setPending(true); review.setAttribute('aria-busy', 'true'); notice.hidden = true;
      const batches = scopeBatches(selections), prepared = [];
      for (const [index, batch] of batches.entries()) {
        planRegion.replaceChildren(node('span', { class: 'muted' }, `正在核对范围 ${index + 1} / ${batches.length}`));
        const value = await client('imports/plan-batch', 'POST', { source_id: source.source_id, selections: batch }, abort.signal);
        if (!active || revision !== basket.revision || attachments.checked !== included) return;
        prepared.push(...value.plans.map((plan, i) => ({ ...plan, sourceScopes: batch[i].scopes })));
      }
      const scopes = prepared.flatMap(plan => plan.selection.scopes);
      if (new Set(scopes).size !== scopes.length) throw new Error('所选范围包含重复项目，请取消重叠的文件或会话范围。');
      plans = prepared; review.hidden = true;
      const total = plans.reduce((n, p) => n + p.scope_count, 0);
      const start = button('开始导入', () => submit(start, revision), { class: 'primary', icon: 'arrow' });
      planRegion.replaceChildren(node('strong', {}, `${total} 项待导入`), node('span', { class: 'muted' }, '原始素材将经完整记忆流程处理'), start,
        button('返回选择', () => { if (!pending) renderBasket(); }, { class: 'text-button' }));
    } catch (e) { if (active) planRegion.replaceChildren(); error(e); }
    finally { setPending(false); review.removeAttribute('aria-busy'); }
  }

  async function submit(start, revision) {
    if (!plans || pending || revision !== basket.revision) return;
    const submitted = basket.snapshot(), batches = scopeBatches(plans, plan => plan.selection);
    let confirmed = 0, jobs = 0;
    setPending(true); start.disabled = true; start.setAttribute('aria-busy', 'true');
    try {
      for (const [index, batch] of batches.entries()) {
        planRegion.replaceChildren(node('span', { role: 'status' }, `正在提交 ${index + 1} / ${batches.length}，已确认 ${confirmed} 项`));
        const value = await client('imports/jobs/batch', 'POST', { source_id: source.source_id,
          plans: batch.map(({ selection, config_hash }) => ({ selection, config_hash })) }, abort.signal);
        if (!active) return;
        const accepted = new Set(batch.flatMap(plan => plan.sourceScopes));
        basket.complete(new Map([...submitted].filter(([id]) => accepted.has(id))));
        confirmed += batch.reduce((n, plan) => n + plan.scope_count, 0); jobs += value.jobs.length;
        review.hidden = true;
      }
      planRegion.replaceChildren(node('strong', {}, `已提交 ${confirmed} 项，创建 ${jobs} 个导入任务`), button('查看导入记录', changed));
    } catch (e) {
      error(e);
      if (active) {
        plans = null; review.hidden = false;
        planRegion.replaceChildren(node('strong', {}, `已确认提交 ${confirmed} 项`),
          node('span', { class: 'muted' }, '提交已停止，未确认的项目仍在选择中。请先查看导入记录，核对最后一次提交结果后再继续。'),
          button('查看导入记录', changed));
      }
    }
    finally { setPending(false); start.disabled = false; start.removeAttribute('aria-busy'); }
  }
  const stopProgress = onConnectorProgress(() => { if (page.group === 'AI' && active && !pending) void load(); }, source.source_id);
  void load();
  return { element: root, dispose() { stopProgress(); detailDispose(); active = false; abort.abort(); clearTimeout(searchTimer); basket.changed = () => {}; detail.replaceChildren(); } };
}
