import { connector, connectorError, onConnectorProgress } from './connector.js';
import { node, button, field, loading, state } from './dom.js';
import { Basket } from './state.js';
import { pages, matches } from './pages/registry.js';
import { workbench } from './workbench.js';
import { activity } from './activity.js';
import { icon, sourceIcon } from './icons.js';
import { sourceSetup } from './source-setup.js';
import { sourceEditor } from './source-editor.js';
import { sourceWatch } from './source-watch.js';

/** A host supplies its API client and router. No credential or browser storage. */
export async function mountImportPage(root, { client, route = '', params = new URLSearchParams(), catalog, onNavigate = () => {}, href = slug => `/manage/imports/${slug}` }) {
  const abort = new AbortController(), baskets = new Map();
  let active = true, version = 0, child = null, current = route;
  let stopConnection = () => {};
  let watch = null;
  catalog ||= await client('imports/sources', 'GET', undefined, abort.signal);
  const shell = node('div', { class: 'import-app' }), content = node('main', { class: 'import-content', id: 'import-content' });
  const navigation = node('nav', { class: 'source-nav', 'aria-label': '导入来源' });
  const menu = node('details', { class: 'mobile-menu' }, node('summary', {}, icon('grid'), '切换来源', icon('chevron')));
  const mobileNavigation = node('nav', { 'aria-label': '切换导入来源' });
  menu.append(mobileNavigation);
  const navigate = (slug, next = new URLSearchParams(), push = true) => {
    onNavigate(slug, next, push); menu.open = false; void show(slug, next);
  };
  function link(title, slug, extra = null) {
    return node('a', { href: href(slug), 'data-route': slug, onclick: event => {
      if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      event.preventDefault(); navigate(slug);
    } }, node('span', { class: 'nav-label' }, sourceIcon(slug === 'activity' ? 'history' : slug), node('span', {}, title)), extra);
  }
  function links(target) {
    target.append(link('全部来源', ''), link('导入记录', 'activity'));
    for (const group of [...new Set(pages.map(p => p.group))]) {
      target.append(node('span', { class: 'nav-group' }, group));
      for (const page of pages.filter(p => p.group === group)) {
        const count = catalog.sources.filter(s => matches(page, s) && s.enabled !== false).length;
        target.append(link(page.title, page.slug, count ? node('span', { class: 'nav-count', 'aria-label': `${count} 个来源` }, count) : null));
      }
    }
  }
  links(navigation); links(mobileNavigation);
  shell.append(navigation, menu, content); root.replaceChildren(shell);

  function addSource(page) {
    watch?.dispose(); watch = null;
    version++; stopConnection(); child?.dispose();
    child = sourceSetup({ client, page, onCancel: () => show(current, params), onSaved: async result => {
      const fresh = await client('imports/sources', 'GET', undefined, abort.signal);
      if (!active) return;
      catalog = fresh; navigation.replaceChildren(); mobileNavigation.replaceChildren(); links(navigation); links(mobileNavigation);
      const next = new URLSearchParams();
      if (result.source_id) next.set('source', result.source_id);
      navigate(result.slug, next);
    } });
    content.replaceChildren(child.element);
  }

  function editSource(page, source) {
    watch?.dispose(); watch = null;
    version++; stopConnection(); child?.dispose();
    child = sourceEditor({ client, page, source, onCancel: () => show(current, params), onSaved: refresh,
      onDeleted: async () => {
        baskets.delete(source.source_id); params = new URLSearchParams(); onNavigate(current, params, false);
        await refresh();
      } });
    content.replaceChildren(child.element);
  }

  async function show(slug, next = new URLSearchParams()) {
    if (!active) return;
    const generation = ++version; current = slug; params = next;
    stopConnection(); stopConnection = () => {}; child?.dispose(); child = null;
    watch?.dispose(); watch = null;
    for (const item of shell.querySelectorAll('[data-route]')) {
      item.dataset.route === slug ? item.setAttribute('aria-current', 'page') : item.removeAttribute('aria-current');
    }
    content.replaceChildren();
    if (slug === 'activity') {
      const names = new Map(catalog.sources.map(s => [s.source_id, s.namespace]));
      const followable = new Set(catalog.sources.filter(s => s.enabled !== false && catalog.profiles.some(p => p.adapter_id === s.adapter_id && p.continuous_updates === true)).map(s => s.source_id));
      child = activity(client, names, followable); content.append(child.element); return;
    }
    if (!slug) {
      const connected = catalog.sources.filter(s => s.enabled !== false).length;
      content.append(node('div', { class: 'page-heading' }, node('div', { class: 'heading-title' },
        node('h1', {}, '来源'), node('span', { class: 'count-badge' }, `${connected} 个已连接`)),
        node('div', { class: 'setup-actions' }, button('刷新', refresh, { icon: 'refresh' }),
          button('添加来源', () => addSource(), { class: 'primary', icon: 'plus' }))));
      for (const group of [...new Set(pages.map(p => p.group))]) {
        const list = node('div', { class: 'source-directory' });
        for (const page of pages.filter(p => p.group === group)) {
          const sources = catalog.sources.filter(s => matches(page, s) && s.enabled !== false);
          const card = link(page.title, page.slug, node('span', { class: sources.length ? 'connection-state connected' : 'connection-state' }, sources.length ? `${sources.length} 个来源` : '未连接'));
          card.className = 'source-card'; card.dataset.connected = String(Boolean(sources.length)); card.append(icon('arrow', 'card-arrow'));
          list.append(card);
        }
        content.append(node('section', { class: 'directory-group' }, node('h2', {}, group), list));
      }
      return;
    }
    const page = pages.find(p => p.slug === slug);
    if (!page) { state(content, '页面不存在', () => navigate('')); return; }
    const sources = catalog.sources.filter(s => matches(page, s) && s.enabled !== false);
    const heading = node('div', { class: 'page-heading' }, node('div', { class: 'heading-title' },
      node('span', { class: 'source-glyph' }, sourceIcon(page.slug)), node('h1', {}, page.title)));
    content.append(heading);
    const headingActions = node('div', { class: 'setup-actions' });
    heading.append(headingActions);
    headingActions.append(button('添加来源', () => addSource(page), { class: 'primary', icon: 'plus' }));
    if (!sources.length) {
      content.append(node('section', { class: 'empty-source setup-empty' }, node('span', { class: 'empty-glyph' }, sourceIcon(page.slug)),
        node('h2', {}, '尚未连接来源'),
        button('连接' + page.title, () => addSource(page), { class: 'primary', icon: 'plus' }))); return;
    }
    const sourceId = sources.some(s => s.source_id === params.get('source')) ? params.get('source') : sources[0].source_id;
    const picker = node('select', { 'aria-label': '来源账号', onchange: () => {
      const next = new URLSearchParams({ source: picker.value }); navigate(slug, next);
    } }, ...sources.map(s => node('option', { value: s.source_id }, s.namespace)));
    picker.value = sourceId;
    const selectedSource = sources.find(s => s.source_id === sourceId);
    const sourceToolbar = node('div', { class: 'source-toolbar' },
      node('div', { class: 'source-switch' }, field('切换已连接账号', picker),
        button('编辑连接', () => editSource(page, selectedSource), { icon: 'edit' })));
    content.append(sourceToolbar);
    const region = node('div', { class: 'source-content' });
    if (catalog.profiles.some(profile => profile.adapter_id === page.adapter && profile.continuous_updates)) {
      watch = sourceWatch(client, sourceId, page.adapter === 'browser.ai-history', locked => {
        region.inert = locked;
        region.setAttribute('aria-disabled', String(locked));
        region.classList.toggle('is-locked', locked);
      });
      sourceToolbar.append(watch.element);
    }
    if (page.adapter === 'browser.ai-history') {
      const message = node('p', { class: 'setup-hint', role: 'status', hidden: true });
      const connectionActions = node('div', { class: 'source-connection-actions' });
      sourceToolbar.append(connectionActions);
      const connectionError = error => error.message === connectorError('dormant_browser')
        ? '请打开已登录的平台页面后点击→' : error.message;
      let refreshing = false, statusVersion = 0;
      const isCurrent = () => active && generation === version;
      async function connectionStatus() {
        const sequence = ++statusVersion;
        try {
          const value = await connector('status', { source_id: sourceId }, 5000);
          if (!isCurrent() || sequence !== statusVersion) return;
          const binding = value.bindings?.find(row => row.source_id === sourceId);
          if (!binding) throw new Error('未找到当前账号的连接状态，请重新连接。');
          if (binding.last_error?.startsWith('attachment_')) {
            message.textContent = '附件读取失败，请查看导入记录'; message.hidden = false; message.classList.remove('error'); refreshing = false; return;
          }
          if (binding.last_error) throw new Error(connectorError(binding.last_error));
          message.classList.remove('error');
          if (refreshing) {
            const queued = binding.status === 'queued';
            message.textContent = queued ? '正在读取会话目录…' : '会话目录已刷新';
            message.hidden = false; refreshing = queued;
          } else { message.hidden = true; }
        } catch (error) {
          if (isCurrent() && sequence === statusVersion) {
            message.textContent = connectionError(error); message.hidden = false; message.classList.add('error'); refreshing = false;
          }
        }
      }
      stopConnection = onConnectorProgress(() => void connectionStatus(), sourceId);
      void connectionStatus();
      const refreshButton = button('刷新会话目录', async () => {
        statusVersion++; refreshing = true; message.classList.remove('error');
        message.textContent = '正在读取会话目录…'; message.hidden = false;
        try { await connector('sync', { source_id: sourceId }); }
        catch (error) { if (!isCurrent()) return; refreshing = false; message.textContent = connectionError(error); message.classList.add('error'); }
        message.hidden = false;
      }, { icon: 'refresh' });
      connectionActions.append(node('div', { class: 'source-refresh-action' }, message, refreshButton), button('断开连接', async () => {
        if (!confirm('断开此来源的扩展交付连接？已经导入的记忆会保留。')) return;
        try {
          await client(`imports/sources/${sourceId}/revoke-browser`, 'POST', {}, abort.signal);
          message.textContent = '连接已断开';
        } catch (error) { message.textContent = error.message; }
        message.hidden = false;
      }));
    }
    content.append(region); loading(region);
    try {
      const source = await client(`imports/sources/${sourceId}`, 'GET', undefined, abort.signal);
      if (!active || generation !== version) return;
      if (!source.binding_available) { state(region, '来源暂不可用，请检查连接', () => show(slug, params)); return; }
      if (!baskets.has(sourceId)) baskets.set(sourceId, new Basket());
      child = workbench({ client, page, source, basket: baskets.get(sourceId), params,
        updateURL: next => onNavigate(current, next, false), changed: () => navigate('activity') });
      region.replaceChildren(child.element);
    } catch (error) { if (active && generation === version && error.name !== 'AbortError') state(region, error.message, () => show(slug, params)); }
  }
  async function refresh() {
    const generation = ++version;
    try {
      const fresh = await client('imports/sources', 'GET', undefined, abort.signal);
      if (!active || generation !== version) return;
      catalog = fresh; navigation.replaceChildren(); mobileNavigation.replaceChildren(); links(navigation); links(mobileNavigation);
      await show(current, params);
    } catch (error) { if (active && error.name !== 'AbortError') state(content, error.message, refresh); }
  }
  void show(route, params);
  return { navigate: (slug, next) => show(slug, next), dispose() {
    active = false; version++; stopConnection(); watch?.dispose(); abort.abort(); child?.dispose(); baskets.clear(); shell.remove();
  } };
}
