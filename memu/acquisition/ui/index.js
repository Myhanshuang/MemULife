import { createClient } from './client.js';
import { mountImportPage } from './page.js';
import { mountMemoryPage } from './memory.js';

const $ = id => document.getElementById(id);
let token = '', application = null, generation = 0, workspace = '';
const client = createClient(() => token);
const route = () => {
  const match = location.pathname.match(/^\/manage\/(imports|memory)(?:\/(.*?))?\/?$/);
  const value = { workspace: match?.[1] || 'imports', slug: match?.[2] || '' };
  if (value.workspace === 'imports' && value.slug === 'topics') value.workspace = 'memory';
  return value;
};
function onNavigate(slug, params, push) {
  if (new URLSearchParams(location.search).get('embed') === '1') params.set('embed', '1');
  const path = '/manage/' + workspace + (slug ? '/' + slug : '') + (params.size ? '?' + params : '');
  history[push ? 'pushState' : 'replaceState'](null, '', path);
}
async function mount(catalog) {
  const version = ++generation, next = route();
  application?.dispose(); application = null; workspace = next.workspace;
  for (const link of $('workspace-nav').querySelectorAll('a')) {
    link.dataset.workspace === workspace ? link.setAttribute('aria-current', 'page') : link.removeAttribute('aria-current');
  }
  const params = new URLSearchParams(location.search);
  onNavigate(next.slug, params, false);
  const mounted = await (workspace === 'memory' ? mountMemoryPage : mountImportPage)($('import-root'), {
    client, catalog, route: next.slug, params, onNavigate,
  });
  if (version !== generation) { mounted.dispose(); return false; }
  application = mounted;
  return true;
}
async function login(value) {
  const version = ++generation; token = value.trim(); $('login-error').hidden = true; $('login-submit').disabled = true;
  try {
    const catalog = await client('imports/sources');
    if (version !== generation) return;
    if (!await mount(catalog)) return;
    $('login').hidden = true; $('import-root').hidden = false; $('logout').hidden = false; $('workspace-nav').hidden = false;
    $('token').value = '';
  } catch (error) {
    token = ''; $('login-error').textContent = error.message; $('login-error').hidden = false;
  } finally { $('login-submit').disabled = false; }
}
async function switchPage() {
  if (!token) return;
  const next = route();
  if (next.workspace === workspace && application) application.navigate(next.slug, new URLSearchParams(location.search));
  else {
    const version = ++generation;
    try {
      const catalog = await client('imports/sources');
      if (version === generation && token) await mount(catalog);
    } catch (error) { if (version === generation) $('import-root').textContent = error.message; }
  }
}
$('workspace-nav').addEventListener('click', event => {
  const link = event.target.closest('a');
  if (!link || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
  event.preventDefault();
  if (link.dataset.workspace === workspace) return;
  const embed = new URLSearchParams(location.search).get('embed') === '1' ? '?embed=1' : '';
  history.pushState(null, '', link.getAttribute('href') + embed); void switchPage();
});
$('login-form').addEventListener('submit', event => { event.preventDefault(); void login($('token').value); });
$('credentials').addEventListener('change', async event => {
  try {
    const file = event.target.files[0]; if (!file) return;
    if (file.size > 65536) throw new Error('请选择工作区的凭据文件。');
    const data = JSON.parse(await file.text());
    if (typeof data.token !== 'string') throw new Error('凭据文件不含管理密钥。');
    await login(data.token);
  } catch (error) { $('login-error').textContent = error.message; $('login-error').hidden = false; }
  finally { event.target.value = ''; }
});
$('logout').addEventListener('click', () => {
  generation++; application?.dispose(); application = null; token = ''; workspace = '';
  $('login').hidden = false; $('import-root').hidden = true; $('logout').hidden = true; $('workspace-nav').hidden = true; $('token').value = '';
});
addEventListener('popstate', () => { void switchPage(); });
if (new URLSearchParams(location.search).get('embed') === '1') document.body.classList.add('embedded');
