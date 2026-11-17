import { node, button, field } from './dom.js';

export function feishuAuthorization(client, signal) {
  const redirect = location.origin + '/manage/imports/feishu/oauth/callback';
  const app = node('input', { required: true, autocomplete: 'off', placeholder: 'cli_…', 'aria-label': 'App ID', 'data-config-field': 'client_id' });
  const secret = node('input', { type: 'password', required: true, autocomplete: 'new-password', 'aria-label': 'App Secret', 'data-config-field': 'client_secret' });
  const callback = node('input', { value: redirect, readonly: true, 'aria-label': '回调地址' });
  const scopes = ['contact:user.base:readonly', 'drive:drive:readonly', 'docx:document:readonly', 'wiki:wiki:readonly', 'offline_access'];
  const copy = button('复制回调地址', async () => {
    try { await navigator.clipboard.writeText(redirect); copy.querySelector('.button-label').textContent = '已复制'; }
    catch { callback.focus(); callback.select(); }
  });
  const element = node('div', {}, node('div', { class: 'setup-grid' }, field('App ID', app), field('App Secret', secret)),
    node('details', { class: 'setup-advanced' }, node('summary', {}, '首次配置'),
      node('p', {}, '在飞书应用的安全设置中添加以下回调地址。'), field('回调地址', callback), copy,
      node('p', {}, '在权限管理中开通以下用户身份权限，并发布应用。'),
      node('ul', {}, ...scopes.map(scope => node('li', {}, node('code', {}, scope)))),
      node('a', { href: 'https://open.feishu.cn/app', target: '_blank', rel: 'noopener noreferrer' }, '打开飞书应用后台')));
  const status = node('div', { class: 'setup-actions', hidden: true, role: 'status' });
  let cancel = () => {};
  const disposed = () => cancel();
  signal.addEventListener('abort', disposed, { once: true });
  return { element, status,
    connect(name) {
      const popup = window.open('about:blank', '_blank', 'popup,width=680,height=800');
      if (!popup) return Promise.reject(new Error('请允许此页面打开授权窗口，然后重试。'));
      let state, channel, timer, done = false, completing = false;
      return new Promise((resolve, reject) => {
        const cleanup = () => {
          window.removeEventListener('message', message); channel?.close(); clearTimeout(timer);
          popup.close(); status.hidden = true; cancel = () => {};
        };
        const finish = (error, result) => {
          if (done) return;
          done = true; cleanup(); error ? reject(error) : resolve(result);
        };
        cancel = () => {
          if (completing) return;
          if (state) void client('imports/feishu/cancel', 'POST', { state }).catch(() => {});
          finish(new DOMException('Authorization cancelled', 'AbortError'));
        };
        async function receive(value) {
          if (done || completing || !state || value?.type !== 'memu-feishu-authorization' || value.state !== state) return;
          completing = true; clearTimeout(timer);
          status.replaceChildren(node('span', {}, '正在连接飞书…'));
          try {
            const result = await client('imports/feishu/complete', 'POST', { state, code: value.code, error: value.error }, signal);
            finish(null, result);
          } catch (error) { finish(error); }
        }
        function message(event) { if (event.origin === location.origin && event.source === popup) void receive(event.data); }
        window.addEventListener('message', message);
        status.hidden = false;
        status.replaceChildren(node('span', {}, '请在飞书窗口中确认授权'), button('取消授权', () => cancel()));
        timer = setTimeout(() => {
          if (state) void client('imports/feishu/cancel', 'POST', { state }).catch(() => {});
          finish(new Error('授权已过期，请重新连接。'));
        }, 600000);
        void client('imports/feishu/authorize', 'POST', {
          name, client_id: app.value.trim(), client_secret: secret.value, redirect_uri: redirect,
        }, signal).then(result => {
          secret.value = '';
          if (done) { void client('imports/feishu/cancel', 'POST', { state: result.state }).catch(() => {}); return; }
          state = result.state;
          channel = new BroadcastChannel('memu-feishu-' + state);
          channel.onmessage = event => { void receive(event.data); };
          popup.location.replace(result.authorization_url);
        }).catch(error => finish(error));
      });
    },
    dispose() { cancel(); secret.value = ''; signal.removeEventListener('abort', disposed); },
  };
}
