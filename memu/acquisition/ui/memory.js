import { node, button, field, loading, state, metadata } from './dom.js';
import { pages, matches } from './pages/registry.js';
import { icon, sourceIcon } from './icons.js';
import { topicsPage } from './topics.js';
import { mediaBrowser } from './memory-media.js';
import { conversation } from './memory-conversation.js';
import { memoryEditor } from './memory-editor.js';

const labels = { unit: 'Unit', source: 'Source', media: 'Source · 媒体', summary: 'Summary', day: '日 Summary', month: '月 Summary', runtime: '记忆主题' };
const options = (select, values, selected = '') => {
  select.replaceChildren(...values.map(([value, title]) => node('option', { value }, title)));
  select.value = selected;
};

export async function mountMemoryPage(root, { client, route = '', params = new URLSearchParams(), catalog, onNavigate = () => {} }) {
  const abort = new AbortController();
  let active = true, child = null;
  catalog ||= await client('imports/sources', 'GET', undefined, abort.signal);
  const shell = node('div', { class: 'import-app memory-app' });
  const content = node('main', { class: 'import-content', id: 'import-content' });
  const navigation = node('nav', { class: 'source-nav', 'aria-label': '记忆来源' });
  const mobile = node('nav', { 'aria-label': '切换记忆来源' });
  const menu = node('details', { class: 'mobile-menu' }, node('summary', {}, icon('grid'), '切换来源', icon('chevron')), mobile);
  const query = node('input', { type: 'search', maxlength: 1000, placeholder: '输入要查找的文字', 'aria-label': '搜索记忆文字' });
  const scopes = ['unit', 'source', 'summary', 'runtime'].map(value => node('input', { type: 'checkbox', value, checked: true }));
  const search = node('form', { class: 'memory-search', role: 'search', onsubmit: event => {
    event.preventDefault();
    if (!scopes.some(input => input.checked)) { scopes[0].setCustomValidity('至少选择一个搜索范围'); scopes[0].reportValidity(); return; }
    const next = new URLSearchParams({ query: query.value, layers: scopes.filter(input => input.checked).map(input => input.value).join(',') });
    navigate('search', next);
  } }, node('div', { class: 'memory-search-row' }, query, node('button', { type: 'submit', class: 'primary' }, icon('search'), '搜索')),
  node('fieldset', { class: 'memory-search-scopes' }, node('legend', {}, '搜索范围'), ...scopes.map(input => {
    input.addEventListener('change', () => scopes[0].setCustomValidity(''));
    return node('label', {}, input, labels[input.value]);
  })));
  function navigate(slug, next = new URLSearchParams()) { onNavigate(slug, next, true); menu.open = false; show(slug, next); }
  function link(title, slug, glyph = slug, extra = null) {
    return node('a', { href: '/manage/memory' + (slug ? '/' + slug : ''), 'data-route': slug, onclick: event => {
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      event.preventDefault(); navigate(slug);
    } }, node('span', { class: 'nav-label' }, sourceIcon(glyph), node('span', {}, title)), extra);
  }
  for (const nav of [navigation, mobile]) {
    nav.append(link('全部来源', '', 'grid'), node('span', { class: 'nav-group' }, '记忆整理'),
      link('日 / 月 Summary', 'summaries', 'history'), link('记忆主题', 'topics'));
    for (const group of [...new Set(pages.map(p => p.group))]) {
      nav.append(node('span', { class: 'nav-group' }, group));
      for (const page of pages.filter(p => p.group === group)) {
        const count = catalog.sources.filter(s => matches(page, s) && !s.deleted_at && s.enabled !== false).length;
        nav.append(link(page.title, page.slug, page.slug, count ? node('span', { class: 'nav-count' }, count) : null));
      }
    }
  }
  const region = node('div');
  content.append(region);
  shell.append(navigation, menu, content); root.replaceChildren(shell);
  function show(slug, next = new URLSearchParams()) {
    if (!active) return;
    params = new URLSearchParams(next); child?.dispose(); child = null;
    query.value = params.get('query') || '';
    const selected = (params.get('layers') || 'unit,source,summary,runtime').split(',');
    for (const input of scopes) input.checked = selected.includes(input.value);
    for (const a of shell.querySelectorAll('[data-route]')) a.dataset.route === slug ? a.setAttribute('aria-current', 'page') : a.removeAttribute('aria-current');
    if (slug === 'topics') {
      child = topicsPage(client);
      const heading = child.element.querySelector('.page-heading');
      heading.classList.add('memory-page-header');
      heading.insertBefore(search, heading.lastElementChild);
    }
    else if (!slug || ['summaries', 'search'].includes(slug) || pages.some(p => p.slug === slug)) {
      child = memoryRecords({ client, slug, params, catalog, search, update: next => onNavigate(slug, next, false) });
    } else { state(region, '页面不存在', () => navigate('')); return; }
    region.replaceChildren(child.element);
  }
  show(route, params);
  return { navigate: show, dispose() { active = false; abort.abort(); child?.dispose(); shell.remove(); } };
}

function memoryRecords({ client, slug, params, catalog, search, update }) {
  const page = pages.find(p => p.slug === slug), summary = slug === 'summaries', searching = slug === 'search';
  const abort = new AbortController();
  let active = true, version = 0, cursor = null, detailVersion = 0, loaded = 0;
  let listMedia = mediaBrowser(client), detailMedia = mediaBrowser(client);
  let layer = params.get('layer') || (summary ? 'day' : 'unit');
  if (!(summary ? ['day', 'month'] : ['unit', 'source']).includes(layer)) layer = summary ? 'day' : 'unit';
  const list = node('div', { class: 'memory-records', 'aria-live': 'polite' });
  const count = node('span', { class: 'count-badge' });
  const detail = node('dialog', { class: 'memory-dialog', 'aria-label': '记忆详情' });
  const detailBody = node('div', { class: 'memory-detail-body' });
  const deletion = node('dialog', { class: 'memory-dialog memory-delete-dialog', 'aria-label': '删除记忆' });
  const editor = memoryEditor(client, () => { detail.close(); return load(); });
  let deleteVersion = 0, deleting = false;
  deletion.addEventListener('cancel', event => { if (deleting) event.preventDefault(); });
  deletion.addEventListener('close', () => { deleteVersion++; });
  detail.append(node('div', { class: 'memory-dialog-heading' }, node('h2', {}, '记忆详情'), button('关闭', () => detail.close())), detailBody);
  detail.addEventListener('close', () => { detailVersion++; detailMedia.dispose(); });
  const more = button('加载更多', () => load(true)); more.hidden = true;
  const source = node('select', { 'aria-label': '来源连接' });
  const available = catalog.sources.filter(s => !s.deleted_at && (!page || matches(page, s)));
  if (params.get('source') && !available.some(s => s.source_id === params.get('source'))) {
    params.delete('source'); update(params);
  }
  options(source, [['', '全部连接'], ...available.map(s => [s.source_id, s.namespace + (s.enabled === false ? '（已断开）' : '')])], params.get('source') || '');
  const format = node('select', { 'aria-label': '原导入格式' });
  const initialFormats = [...new Set(available.map(s => s.profile))].filter(Boolean).sort();
  options(format, [['', '全部导入格式'], ...initialFormats.map(v => [v, v])], params.get('profile') || '');
  if (page?.provider) { options(format, [[page.provider, page.provider]], page.provider); format.disabled = true; }
  const start = node('input', { type: 'date', 'aria-label': '开始日期', value: params.get('start') || '' });
  const end = node('input', { type: 'date', 'aria-label': '结束日期', value: params.get('end') || '' });
  const filters = node('form', { class: 'memory-filters', onsubmit: event => { event.preventDefault(); apply(); } },
    ...(!summary ? [field('来源连接', source), field('原导入格式', format)] : []), field('开始日期', start), field('结束日期', end),
    node('button', { type: 'submit' }, '筛选'), button('重置', () => {
      source.value = ''; format.value = ''; start.value = ''; end.value = ''; apply();
    }, { class: 'text-button' }));
  const tabs = node('div', { class: 'filter-tabs', role: 'group', 'aria-label': summary ? '汇总周期' : '记忆层级' });
  if (!searching) for (const value of summary ? ['day', 'month'] : ['unit', 'source']) tabs.append(button(labels[value], () => {
    layer = value; params.set('layer', layer); update(params); renderTabs(); void load();
  }, { 'data-layer': value }));
  function renderTabs() { for (const tab of tabs.children) tab.setAttribute('aria-pressed', String(tab.dataset.layer === layer)); }
  renderTabs();
  const title = searching ? '搜索结果' : summary ? '日 / 月 Summary' : page?.title || '全部记忆';
  const element = node('section', { class: 'memory-page' },
    node('div', { class: 'page-heading memory-page-header' },
      node('div', { class: 'memory-heading-main' }, node('div', { class: 'heading-title' }, node('h1', {}, title), count), tabs),
      search, button('刷新', () => load(), { icon: 'refresh' })),
    filters, list, more, detail, deletion, editor.element);
  function apply() {
    end.setCustomValidity(start.value && end.value && start.value > end.value ? '结束日期不能早于开始日期' : '');
    if (!end.reportValidity()) return;
    for (const [key, value] of [['source', source.value], ['profile', format.value], ['start', start.value], ['end', end.value]]) {
      value ? params.set(key, value) : params.delete(key);
    }
    update(params); void load();
  }
  end.addEventListener('input', () => end.setCustomValidity(''));
  start.addEventListener('input', () => end.setCustomValidity(''));
  const originName = origin => {
    const source = catalog.sources.find(s => s.source_id === origin.source_instance_id);
    return (source?.namespace || origin.source_namespace) + (source?.deleted_at ? '（来源已删除）' : '');
  };
  const dateLabel = item => item.date_start ? item.date_start + (item.date_end && item.date_end !== item.date_start ? ' 至 ' + item.date_end : '') : '日期未知';
  function card(item) {
    const unit = item.kind === 'unit';
    return node('article', { class: 'topic-card memory-card', 'data-ref': item.ref },
      node('div', { class: 'topic-heading' }, node('span', { class: 'status-badge' }, labels[item.kind] || item.kind),
        item.protected || item.protected_count ? node('span', { class: 'status-badge memory-protected' }, '已保护') : null,
        node('span', { class: 'memory-card-date' }, dateLabel(item)), item.pending_update ? node('span', { class: 'muted' }, '等待更新') : null),
      !unit && item.display?.mode !== 'attachment' && (item.display?.title || item.scope) ? node('h2', {}, item.display?.title || item.scope) : null,
      item.display?.mode === 'attachment' ? null : item.kind === 'source' ? conversation(item, true)
        : item.body ? node('p', { class: 'memory-preview' }, item.body + (item.preview_truncated ? '…' : '')) : null,
      !unit ? listMedia.gallery(item.media, true) : null,
      node('div', { class: 'memory-card-actions' }, button('查看详情', () => openDetail(item.ref)),
        ...(['unit', 'day', 'month', 'runtime'].includes(item.kind) ? [button('编辑', () => editor.open(item.ref))] : []),
        ...(['unit', 'source', 'media'].includes(item.kind) ? [button('删除', () => confirmDelete(item), { class: 'text-button danger' })] : [])));
  }
  async function confirmDelete(item) {
    const current = ++deleteVersion;
    loading(deletion); if (!deletion.open) deletion.showModal();
    try {
      const plan = await client('memory/delete-preview', 'POST', { ref: item.ref }, abort.signal);
      if (!active || current !== deleteVersion) return;
      const message = node('p', { role: 'status', class: 'error' });
      const cancel = button('取消', () => deletion.close());
      const submit = button('确认删除', async () => {
        deleting = true; submit.disabled = true; cancel.disabled = true; message.textContent = '正在删除…';
        try {
          await client('memory/delete', 'POST', { ref: item.ref, revision: plan.revision });
          if (!active) return;
          deletion.close(); detail.close(); await load();
        } catch (error) {
          if (active) { message.textContent = error.message; submit.disabled = true;
            message.append(button('重新查看删除范围', () => confirmDelete(item), { class: 'text-button' })); }
        } finally { deleting = false; cancel.disabled = false; }
      }, { class: 'danger-button' });
      deletion.replaceChildren(node('div', { class: 'memory-detail-body' }, node('h2', {}, '删除这条记忆？'),
        node('p', { class: 'memory-delete-excerpt' }, item.body?.slice(0, 220) || item.display?.title || labels[item.kind]),
        node('p', {}, `将删除 ${plan.counts.unit} 条 Unit、${plan.counts.source} 段 Source${plan.counts.media ? `、${plan.counts.media} 个媒体引用` : ''}。`),
        message, node('div', { class: 'memory-card-actions' }, cancel, submit)));
      cancel.focus();
    } catch (error) {
      if (active && current === deleteVersion && error.name !== 'AbortError') {
        state(deletion, error.message, () => confirmDelete(item)); deletion.append(button('关闭', () => deletion.close()));
      }
    }
  }
  async function load(append = false) {
    const current = ++version; more.disabled = true;
    if (!append) { listMedia.dispose(); listMedia = mediaBrowser(client); cursor = null; loaded = 0; count.textContent = ''; more.hidden = true; loading(list); }
    const request = { layers: searching ? (params.get('layers') || 'unit,source,summary,runtime').split(',') : [layer],
      query: searching ? params.get('query') || '' : '', adapter: page?.adapter || '', profile: page?.provider || params.get('profile') || '',
      source_id: params.get('source') || '', start: params.get('start') || '', end: params.get('end') || '', limit: 30,
      cursor: append ? cursor || '' : '' };
    try {
      const result = await client('memory', 'POST', request, abort.signal);
      if (!active || current !== version) return;
      if (!append) list.replaceChildren();
      list.querySelector('.region-state')?.remove();
      list.append(...result.items.map(card)); loaded += result.items.length;
      count.textContent = `已加载 ${loaded} 条`; cursor = result.next_cursor; more.hidden = !cursor;
      if (!loaded) state(list, '没有符合条件的记忆数据');
      const selected = format.value;
      if (!page?.provider) options(format, [['', '全部导入格式'], ...[...new Set([...initialFormats, ...(result.formats || []), selected].filter(Boolean))].sort().map(v => [v, v])], selected);
    } catch (error) {
      if (active && current === version && error.name !== 'AbortError') {
        if (!append) state(list, error.message, () => load());
        else { const notice = node('div'); state(notice, error.message, () => { notice.remove(); void load(true); }); list.append(notice); }
      }
    } finally { if (current === version) more.disabled = false; }
  }
  async function openDetail(ref, original = false, extracted = false) {
    detailMedia.dispose(); detailMedia = mediaBrowser(client);
    const current = ++detailVersion; loading(detailBody); if (!detail.open) detail.showModal();
    try {
      const item = await client('memory/detail', 'POST', { ref, original }, abort.signal);
      if (!active || current !== detailVersion) return;
      const attachment = item.display?.mode === 'attachment';
      const asConversation = item.kind === 'source' && !original && !attachment;
      const missingOriginal = original && !item.original_available;
      const body = missingOriginal ? node('p', {}, '暂无原始导入内容')
        : asConversation ? conversation(item) : node('pre', { class: 'memory-full-text' }, item.body);
      const detailMore = button('继续读取正文', async () => {
        detailMore.disabled = true;
        try {
          const next = await client('memory/detail', 'POST', { ref, original, offset: item.next_offset, revision: item.revision }, abort.signal);
          if (!active || current !== detailVersion) return;
          if (asConversation) body.append(...conversation(next).childNodes);
          else body.append(document.createTextNode(next.body));
          item.next_offset = next.next_offset; detailMore.hidden = item.next_offset === null;
        } catch (error) { if (active && current === detailVersion && error.name !== 'AbortError') status.textContent = error.message; }
        finally { detailMore.disabled = false; }
      });
      detailMore.hidden = missingOriginal || item.next_offset === null || (attachment && !extracted);
      const status = node('p', { class: 'muted', role: 'status' });
      const view = node('div', { class: 'filter-tabs', role: 'group', 'aria-label': '内容格式' });
      if (attachment) view.append(button('附件', () => openDetail(ref), { 'aria-pressed': String(!extracted) }),
        button('提取文本', () => openDetail(ref, false, true), { 'aria-pressed': String(extracted) }));
      else if (item.kind === 'source') view.append(button('对话 / 正文', () => openDetail(ref), { 'aria-pressed': String(!original) }),
        button('原始导入内容', () => openDetail(ref, true), { 'aria-pressed': String(original) }));
      const evidence = node('div', { class: 'memory-evidence' });
      for (const fact of item.facts || []) evidence.append(button(fact, () => openDetail(fact), { class: 'text-button' }));
      detailBody.replaceChildren(metadata([['引用', item.ref], ['类型', labels[item.kind] || item.kind], ['日期', dateLabel(item)],
        ['账户 / 来源', (item.origins || []).map(originName).join('、')], ['写入账户', item.source],
        ['来源连接 ID', [...new Set((item.origins || []).map(o => o.source_instance_id))].join('、')],
        ['原始对象 ID', [...new Set((item.origins || []).map(o => o.object_id))].join('、')],
        ['原导入格式', [...new Set((item.origins || []).map(o => o.source_profile))].join('、')]]),
      view, status, !(attachment && extracted) ? detailMedia.gallery(item.media) : null,
      ...((item.body || missingOriginal) && (!attachment || extracted) ? [body] : []), detailMore,
      ...(item.kind === 'media' && !item.media?.length ? [node('p', {}, '暂无可用媒体')] : []),
      ...(item.facts?.length ? [node('h3', {}, '关联证据 / Source'), evidence] : []),
      ...(['unit', 'day', 'month', 'runtime'].includes(item.kind) ? [node('div', { class: 'memory-card-actions' },
        item.protected || item.protected_count ? node('span', { class: 'status-badge memory-protected' }, '已保护') : null,
        button('编辑', () => editor.open(item.ref)))] : []),
      ...(['unit', 'source', 'media'].includes(item.kind) ? [node('div', { class: 'memory-card-actions' },
        button('删除这条记忆', () => confirmDelete(item), { class: 'text-button danger' }))] : []));
    } catch (error) { if (active && current === detailVersion && error.name !== 'AbortError') state(detailBody, error.message, () => openDetail(ref, original)); }
  }
  void load();
  return { element, dispose() { active = false; version++; detailVersion++; deleteVersion++; abort.abort(); listMedia.dispose(); detailMedia.dispose(); detail.close(); deletion.close(); editor.dispose(); element.remove(); } };
}
