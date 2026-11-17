// Dirty hints contain a URL identity only. No DOM text, token, request or response capture.
(() => {
  const profiles: Record<string, [string, RegExp]> = {
    'chatgpt.com': ['chatgpt', /^\/c\/([\w-]+)/],
    'claude.ai': ['claude', /^\/chat\/([\w-]+)/],
    'gemini.google.com': ['gemini', /^(?:\/u\/\d+)?\/app\/([\w-]+)/],
    'www.perplexity.ai': ['perplexity', /^\/search\/([\w-]+)/],
    'chat.deepseek.com': ['deepseek', /^\/a\/chat\/s\/([\w-]+)/],
    'grok.com': ['grok', /^\/c\/([\w-]+)/],
  };
  const profile = profiles[location.hostname];
  if (!profile) return;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let last = 0;
  const dirty = () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      if (Date.now() - last < 10_000) return;
      const id = location.pathname.match(profile[1])?.[1];
      if (!id) return;
      last = Date.now();
      void chrome.runtime.sendMessage({ type: 'dirty', provider: profile[0], conversation_id: id }).catch(() => {});
    }, 2000);
  };
  new MutationObserver(dirty).observe(document.documentElement, { childList: true, subtree: true, characterData: true });
  addEventListener('popstate', dirty);
  dirty();
})();
