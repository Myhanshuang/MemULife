import { ORIGINS } from './http.ts';
import type { ProviderId } from './types.ts';
const names: Record<ProviderId, string> = { chatgpt: 'ChatGPT', claude: 'Claude', gemini: 'Gemini', deepseek: 'DeepSeek', perplexity: 'Perplexity', grok: 'Grok' };
const params = new URLSearchParams(location.search), nonce = params.get('authorize'), provider = params.get('provider') as ProviderId;
if (nonce && Object.hasOwn(names, provider)) {
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
}
