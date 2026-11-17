import { node } from './dom.js';

// Defaults checked against upstream docs / source; file locations match our readers.
// Codex: https://developers.openai.com/codex/config-advanced/
// Claude Code: https://code.claude.com/docs/en/settings
// Gemini CLI: https://geminicli.com/docs/cli/session-management/
// OpenCode: https://github.com/anomalyco/opencode/blob/v1.2.27/packages/opencode/src/global/index.ts
// Cursor: https://forum.cursor.com/t/agents-panel-right-sidebar-lost-session-list-no-way-to-restore/153481/4
const paths = {
  codex: {
    linux: '~/.codex', windows: '%USERPROFILE%\\.codex',
    linuxExample: '/home/你的用户名/.codex', windowsExample: 'C:\\Users\\你的用户名\\.codex',
    files: ['sessions/**/*.jsonl', 'archived_sessions/**/*.jsonl'],
    note: '如果设置了 CODEX_HOME，请填写它指向的目录。',
  },
  'claude-code': {
    linux: '~/.claude', windows: '%USERPROFILE%\\.claude',
    linuxExample: '/home/你的用户名/.claude', windowsExample: 'C:\\Users\\你的用户名\\.claude',
    files: ['projects/<项目目录编码>/**/*.jsonl'],
    note: '如果设置了 CLAUDE_CONFIG_DIR，请填写它指向的目录。',
  },
  'gemini-cli': {
    linux: '~/.gemini', windows: '%USERPROFILE%\\.gemini',
    linuxExample: '/home/你的用户名/.gemini', windowsExample: 'C:\\Users\\你的用户名\\.gemini',
    files: ['tmp/<项目标识>/chats/session-*.json', 'tmp/<项目标识>/chats/session-*.jsonl'],
    note: '填写用户主目录下的 .gemini；会话按项目保存在 tmp 内。',
  },
  opencode: {
    linux: '~/.local/share/opencode', windows: '%USERPROFILE%\\.local\\share\\opencode',
    linuxExample: '/home/你的用户名/.local/share/opencode', windowsExample: 'C:\\Users\\你的用户名\\.local\\share\\opencode',
    files: ['opencode.db', 'storage/{session,message,part}/**/*.json（旧版）'],
    note: '如果设置了 XDG_DATA_HOME，请填写其下的 opencode 目录。数据库路径留空时读取 opencode.db；自定义文件名可在下方填写。',
  },
  cursor: {
    linux: '~/.config/Cursor', windows: '%APPDATA%\\Cursor',
    linuxExample: '/home/你的用户名/.config/Cursor', windowsExample: 'C:\\Users\\你的用户名\\AppData\\Roaming\\Cursor',
    files: ['User/globalStorage/state.vscdb', 'User/workspaceStorage/<工作区标识>/state.vscdb'],
    note: '这里读取 Cursor 编辑器的聊天数据库。使用自定义用户数据目录时，请填写实际目录；数据库路径列表留空可自动查找上述文件。',
  },
};

export function localAIPathHelp(profile, name, control) {
  const info = paths[profile];
  if (!info) return null;
  control.placeholder = `例如 ${info.linuxExample} 或 ${info.windowsExample}`;
  control.setAttribute('aria-describedby', 'local-ai-path-help');
  const platforms = [['Linux', info.linux, info.linuxExample], ['Windows', info.windows, info.windowsExample]];
  const directories = platforms.map(([os, path, example]) => node('div', {},
    node('dt', {}, os), node('dd', {}, node('code', {}, path),
      node('small', {}, '完整路径示例：', node('code', {}, example)))));
  return node('aside', { class: 'local-ai-path-help', id: 'local-ai-path-help', 'aria-label': `${name} 默认数据目录` },
    node('h3', {}, `${name} 默认数据目录`),
    node('dl', { class: 'local-ai-path-platforms' }, directories),
    node('p', {}, '数据目录填写上面的根目录；会话文件位于其下：'),
    node('ul', { class: 'local-ai-path-files' }, info.files.map(path => node('li', {}, node('code', {}, path)))),
    node('p', {}, info.note),
    node('p', {}, '~ 和 %USERPROFILE% 表示运行 Agent 的用户主目录；%APPDATA% 通常是用户主目录下的 AppData\\Roaming。请替换为实际绝对路径后填写。'),
    node('p', {}, '路径必须能被采集服务访问。远程或容器部署时，请填写复制或挂载后的路径，并保留上述子目录结构。CLI 在 WSL 内运行时，按 Linux 路径查找。'));
}
