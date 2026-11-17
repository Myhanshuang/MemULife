import { node } from './dom.js';
import { connector } from './connector.js';

export function sourceWatch(client, sourceId, browser = false, onState = () => {}) {
  const abort = new AbortController();
  let live = true, value = null, timer, generation = 0;
  const input = node('input', { type: 'checkbox', disabled: true, 'aria-label': '持续关注当前来源' });
  const hint = node('span', { class: 'setup-hint' }, '开启后自动导入此来源所有内容');
  const status = node('span', { class: 'source-watch-status setup-hint', role: 'status' }, '正在读取状态…');
  const element = node('div', { class: 'source-watch' },
    node('label', { class: 'source-watch-control' }, input, node('span', {}, '持续关注')), hint, status);
  function render(result) {
    value = result; input.checked = result.enabled; input.disabled = false;
    status.classList.toggle('error', Boolean(result.error_code));
    status.textContent = result.error_code ? `已暂停：${result.error_code}` : '';
    status.hidden = !status.textContent;
    element.title = browser ? '自动导入当前连接账号的全部内容及后续更新，包含附件；浏览器及平台登录需保持可用。'
      : '自动导入当前来源授权范围内的全部内容及后续更新，包含附件。';
    onState(result.enabled);
  }
  async function load() {
    const sequence = ++generation;
    try {
      const result = await client(`imports/sources/${sourceId}/watch`, 'GET', undefined, abort.signal);
      if (live && sequence === generation) render(result);
    } catch (error) { if (live && sequence === generation) { status.textContent = error.message; status.hidden = false; status.classList.add('error'); } }
    finally { if (live && sequence === generation) timer = setTimeout(load, 150000); }
  }
  input.addEventListener('change', async () => {
    const sequence = ++generation;
    const enabled = input.checked; input.disabled = true; clearTimeout(timer);
    onState(true);
    try {
      const result = await client(`imports/sources/${sourceId}/watch`, 'POST', { enabled }, abort.signal);
      if (!live || sequence !== generation) return;
      render(result);
      if (enabled && browser) {
        try { await connector('sync', { source_id: sourceId }); }
        catch { if (live && sequence === generation) { status.textContent = '等待浏览器连接'; status.hidden = false; } }
      }
    } catch (error) {
      if (live && sequence === generation) { input.checked = value?.enabled ?? false; input.disabled = false; onState(input.checked); status.textContent = error.message; status.hidden = false; status.classList.add('error'); }
    } finally { if (live && sequence === generation) timer = setTimeout(load, 150000); }
  });
  onState(true);
  void load();
  return { element, dispose() { live = false; clearTimeout(timer); abort.abort(); } };
}
