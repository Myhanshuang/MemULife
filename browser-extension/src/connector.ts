/** Granted management page bridge. The background verifies the exact server origin. */
(() => {
  const loopback = location.protocol === 'http:' && ['localhost', '127.0.0.1'].includes(location.hostname) &&
    Boolean(location.port) && Number(location.port) >= 1024;
  if (window.top !== window || (!loopback && location.protocol !== 'https:') ||
      !/^\/manage\/imports(?:\/|$)/.test(location.pathname)) return;
  window.addEventListener('message', async event => {
    const request = event.data;
    if (event.source !== window || event.origin !== location.origin || request?.channel !== 'memu-connector-request' ||
        typeof request.id !== 'string' || request.id.length > 100 || !['hello', 'probe', 'connect', 'sync', 'status'].includes(request.action)) return;
    let reply;
    try { reply = await chrome.runtime.sendMessage({ type: 'web-connector', action: request.action, payload: request.payload }); }
    catch { reply = { ok: false, error: 'connector_reload_required' }; }
    window.postMessage({ channel: 'memu-connector-response', id: request.id, ...reply }, location.origin);
  });
  chrome.runtime.onMessage.addListener(message => {
    if (message?.type === 'connector-progress') window.postMessage({ channel: 'memu-connector-progress', source_ids: message.source_ids }, location.origin);
  });
})();
