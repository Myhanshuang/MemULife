const params = new URLSearchParams(location.search);
const state = params.get('state');
const result = { type: 'memu-feishu-authorization', state, code: params.get('code'), error: params.get('error') };
history.replaceState(null, '', location.pathname);
if (state && /^[A-Za-z0-9_-]{40,64}$/.test(state) && (result.code || result.error)) {
  const channel = new BroadcastChannel('memu-feishu-' + state);
  channel.postMessage(result);
  window.opener?.postMessage(result, location.origin);
  channel.close();
  document.querySelector('#status').textContent = '已返回授权结果，请回到原来的管理页面。此窗口可以关闭。';
} else {
  document.querySelector('#status').textContent = '授权信息不完整，请回到管理页面重新连接。';
}
