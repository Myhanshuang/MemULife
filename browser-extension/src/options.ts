import { ORIGINS } from './http.ts';
import type { ProviderId } from './types.ts';
import { DEFAULT_SERVICE, configuredService, serviceURL, servicePermission } from './service.ts';
const names: Record<ProviderId, string> = { chatgpt: 'ChatGPT', claude: 'Claude', gemini: 'Gemini', deepseek: 'DeepSeek', perplexity: 'Perplexity', grok: 'Grok' };
const params = new URLSearchParams(location.search), nonce = params.get('authorize'), provider = params.get('provider') as ProviderId;
if (nonce && Object.hasOwn(names, provider)) {
  document.getElementById('server-settings')!.hidden = true;
  document.getElementById('title')!.textContent = `允许读取 ${names[provider]}`;
  document.getElementById('open-web')!.hidden = true;
  const button = document.getElementById('authorize') as HTMLButtonElement;
  button.hidden = false;
  button.onclick = async () => {
    button.disabled = true;
    try {
      const allowed = await chrome.permissions.request({ origins: [`${ORIGINS[provider]}/*`], permissions: provider === 'grok' || provider === 'perplexity' ? ['cookies'] : [] });
      const result = await chrome.runtime.sendMessage({ type: 'permission-result', nonce, allowed });
      if (!result?.ok) throw new Error('此连接请求已失效，请回网页重新连接。');
      document.getElementById('notice')!.textContent = allowed ? '授权成功。请回采集网页选择账号。' : '未授权。可以回网页重新发起连接。';
      if (allowed) window.close();
    } catch (error: any) { document.getElementById('notice')!.textContent = error.message; }
    finally { button.disabled = false; }
  };
} else {
  const input = document.getElementById('server-url') as HTMLInputElement;
  const button = document.getElementById('save-server') as HTMLButtonElement;
  const notice = document.getElementById('notice')!;
  const links = [...document.querySelectorAll<HTMLAnchorElement>('#open-web, .brand')];
  const updateLinks = (origin: string) => links.forEach(link => { link.href = `${origin}/manage/imports`; });
  void configuredService().then(value => { input.value = value ?? DEFAULT_SERVICE; updateLinks(input.value); });
  document.getElementById('server-settings')!.addEventListener('submit', async event => {
    event.preventDefault(); button.disabled = true;
    try {
      const origin = serviceURL(input.value.trim());
      const allowed = await chrome.permissions.request({ origins: [servicePermission(origin)] });
      if (!allowed) throw new Error('尚未授权连接此服务器。');
      const reply = await chrome.runtime.sendMessage({ type: 'configure-service', address: origin });
      if (!reply?.ok) throw new Error('服务器设置未保存，请重新加载扩展后重试。');
      updateLinks(origin); notice.textContent = '已保存，请在管理页登录并连接来源。';
      await chrome.tabs.create({ url: `${origin}/manage/imports` });
    } catch (error: any) {
      notice.textContent = error?.code === 'web_address_invalid' ? '请输入 HTTPS 服务器地址，或带端口的本机 HTTP 地址，不要包含路径或密钥。' : error.message;
    } finally { button.disabled = false; }
  });
}
