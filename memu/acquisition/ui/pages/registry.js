import { chats } from './chats.js';
import { conversations } from './conversations.js';
import { files } from './files.js';
import { documents } from './documents.js';
import { media } from './media.js';
import { logs } from './logs.js';
import { segments } from './segments.js';

const page = (slug, title, adapter, view, group, provider) => ({ slug, title, adapter, view, group, provider });
export const pages = [
  page('qq', 'QQ', 'qq.napcat', chats, '聊天'),
  page('wechat', '微信', 'wechat.localdb', chats, '聊天'),
  ...[['chatgpt', 'ChatGPT'], ['claude', 'Claude'], ['gemini', 'Gemini'], ['deepseek', 'DeepSeek'], ['perplexity', 'Perplexity'], ['grok', 'Grok']]
    .map(([provider, title]) => page(`ai/${provider}`, title, 'browser.ai-history', conversations, 'AI', provider)),
  page('files', '文件', 'filesystem.typora', files, '文件与文档'),
  page('notion', 'Notion', 'notion.workspace', documents, '文件与文档'),
  page('feishu', '飞书', 'feishu.documents', documents, '文件与文档'),
  page('immich', 'Immich', 'immich.assets', media, '本地与媒体'),
  page('local-ai', '本地 AI', 'ai.local-logs', logs, '本地与媒体'),
  page('screen', '屏幕记录', 'screen.native', segments, '本地与媒体'),
];

export function matches(page, source) {
  return source.adapter_id === page.adapter && (!page.provider || (source.profile || source.provider) === page.provider);
}
