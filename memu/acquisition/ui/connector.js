const errors = { connector_unavailable: '未检测到连接器。请加载新版 MemULife 扩展并刷新此页面。',
  connector_reload_required: '连接器已更新，请刷新此页面。', permission_required: '尚未授权读取该平台，请重新连接并确认授权。',
  connector_upgrade_required: '请在浏览器扩展页重新加载新版 MemULife，再重试导入。',
  attachment_download_failed: '附件原件下载失败，导入尚未完成；请保持平台页面登录后重试。',
  attachment_source_unavailable: '平台上的附件原件已不可用，导入尚未完成。',
  attachment_access_denied: '平台拒绝访问附件原件，导入尚未完成。',
  attachment_download_url_missing: '平台未返回附件下载地址，导入尚未完成。',
  attachment_not_ready: '平台仍在准备附件，稍后自动重试。',
  attachment_too_large: '附件超出当前单次传输容量，导入尚未完成。',
  attachment_budget_exceeded: '本次附件总量超出传输容量，请缩小导入范围。',
  pairing_invalid: '连接请求已过期，请重新检测账号。',
  dormant_browser: '请打开已登录的平台页面后重试。', account_changed: '平台账号已变化，请重新检测账号。',
  auth_required: '请先在平台页面登录。', scope_denied: '连接已失效，请重新连接来源。' };
export const connectorError = code => errors[code] || `连接器未完成读取：${code || '未知错误'}`;
export function connector(action, payload = {}, timeout = action === 'hello' ? 1500 : 120000) {
  return new Promise((resolve, reject) => {
    const id = crypto.randomUUID();
    const finish = () => { clearTimeout(timer); removeEventListener('message', receive); };
    const receive = event => {
      if (event.source !== window || event.origin !== location.origin || event.data?.channel !== 'memu-connector-response' || event.data.id !== id) return;
      finish(); const reply = event.data;
      if (reply.ok) resolve(reply.result);
      else reject(new Error(connectorError(reply.error)));
    };
    const timer = setTimeout(() => { finish(); reject(new Error(errors.connector_unavailable)); }, timeout);
    addEventListener('message', receive);
    window.postMessage({ channel: 'memu-connector-request', id, action, payload }, location.origin);
  });
}
export function onConnectorProgress(callback, sourceId) {
  const receive = event => {
    if (event.source !== window || event.origin !== location.origin || event.data?.channel !== 'memu-connector-progress') return;
    if (sourceId && Array.isArray(event.data.source_ids) && !event.data.source_ids.includes(sourceId)) return;
    callback();
  };
  addEventListener('message', receive); return () => removeEventListener('message', receive);
}
