import { connector } from './connector.js';
import { node, button, field, loading } from './dom.js';
import { sourceIcon, icon } from './icons.js';
import { localAIPathHelp } from './local-ai-paths.js';
import { feishuAuthorization } from './feishu-authorization.js';

const profileNames = {
  'linux-markdown-v1': '本地文件', 'onebot11-napcat': 'NapCat / OneBot 11', 'napcat-webui': 'NapCat WebUI',
  'readonly-sqlite': '只读数据库', 'docx-v1': '飞书文档', 'api-2026-03-11': 'Notion API',
  'installed-openapi': 'Immich 服务', 'api-key': 'Immich API', codex: 'Codex', 'claude-code': 'Claude Code',
  'gemini-cli': 'Gemini CLI', opencode: 'OpenCode', cursor: 'Cursor',
  'wayland-portal-v1': 'Linux / Wayland', 'windows-wgc-v1': 'Windows',
  chatgpt: 'ChatGPT', claude: 'Claude', gemini: 'Gemini', deepseek: 'DeepSeek', perplexity: 'Perplexity', grok: 'Grok',
};
const labels = {
  root: '数据目录', base_url: '服务地址', ws_url: 'WebSocket 地址', account_id: '账号 ID',
  workspace_id: '工作空间 ID', principal_id: '飞书用户 ID', conversations: '可浏览的会话',
  folder_tokens: '文件夹 Token', document_ids: '文档 ID', wiki_spaces: '知识空间 ID',
  page_ids: '页面 ID', database_ids: '数据库 ID', data_source_ids: '数据源 ID',
  include_children: '包含子页面', include_comments: '包含评论',
  deployment_id: '部署标识', owner_id: '用户 ID', schema_path: 'OpenAPI 文件路径',
  schema_sha256: 'OpenAPI 文件 SHA-256', server_version: '服务器版本',
  album_ids: '相册 ID', all_assets: '浏览该用户的全部资产', visibilities: '可浏览的可见范围',
  include_trash: '包含回收站', original_mode: '原始媒体方式',
  device_id: '设备 ID', stream_id: '录屏流 ID', helper_path: '录屏助手路径',
  attachment_roots: '附件目录', database: '数据库路径', databases: '数据库路径列表',
  profile: '存储格式', snapshot_manifest: '快照清单路径', credential_file: 'OAuth 凭据文件路径',
  oauth: '使用 OAuth 凭据文件', download_media: '读取所选文档的媒体',
  media_hosts: '附件允许访问的主机', history_order: '历史消息顺序', revision_strategy: '文档版本策略',
  allow_insecure_http: '允许 HTTP 服务', debounce_ms: '文件稳定等待（毫秒）',
  scan_interval_seconds: '扫描间隔（秒）', max_file_bytes: '单文件上限（字节）',
  max_record_bytes: '单记录上限（字节）', max_files: '文件数量上限', max_requests: '请求数量上限',
  max_attachment_bytes: '附件上限（字节）', max_block_pages: '文档分页上限',
  overlap_seconds: '时间重叠（秒）', max_segment_bytes: '片段上限（字节）', max_index_bytes: '清单上限（字节）',
};
const advanced = new Set(['ws_url', 'attachment_roots', 'helper_path', 'history_order', 'media_hosts', 'profile',
  'snapshot_manifest', 'include_children', 'include_comments', 'download_media', 'include_trash', 'original_mode',
  'revision_strategy', 'overlap_seconds', 'allow_insecure_http', 'scan_interval_seconds', 'debounce_ms']);

export function sourceSetup({ client, page, onSaved, onCancel }) {
  const abort = new AbortController();
  let live = true, busy = false, adapter, profile, readConfig, schemaText, formVersion = 0;
  let feishu;
  const element = node('section', { class: 'setup-card', 'aria-label': '添加来源' });
  const errors = node('p', { class: 'error', role: 'alert', hidden: true });
  const name = node('input', { name: 'source_name', required: true, maxlength: 200, placeholder: '例如：工作笔记、个人账号' });
  const selector = node('select', { 'aria-label': '来源类型' });
  const profileSelect = node('select', { 'aria-label': '来源格式' });
  const configRegion = node('div', { class: 'setup-fields' });
  const fields = node('fieldset', { class: 'setup-fieldset' });
  const save = button('保存并选择数据', () => {}, { type: 'submit', class: 'primary', icon: 'arrow' });
  const form = node('form', {}, fields, errors);
  const heading = node('div', { class: 'setup-heading' }, node('span', { class: 'source-glyph' }, sourceIcon(page?.slug || 'grid')),
    node('div', {}, node('h2', {}, '添加来源')));
  const typeFields = node('div', { class: 'setup-grid' }, field('来源类型', selector), field('来源格式', profileSelect));
  const nameField = field('来源名称', name);
  fields.append(typeFields, nameField, configRegion,
    node('div', { class: 'setup-footer' },
      node('div', { class: 'setup-actions' }, button('取消', onCancel), save)));
  element.append(heading, form);
  loading(configRegion);

  function buildObject(schema, prefix = '') {
    const basic = node('div', { class: 'setup-grid' });
    const extra = node('details', { class: 'setup-advanced' }, node('summary', {}, '高级选项'));
    const optional = node('div', { class: 'setup-grid' }); extra.append(optional);
    const readers = [];
    for (const [key, spec] of Object.entries(schema.properties || {})) {
      if (key === 'provider' || key === 'backend' || key === 'allow_foreground_actions') continue;
      if (adapter.adapter_id === 'wechat.localdb' && key === 'conversations') continue;
      const path = prefix + key, secret = adapter.secret_fields?.[path];
      const required = (schema.required || []).includes(key);
      const label = secret || (adapter.adapter_id === 'qq.napcat' && profile.id === 'napcat-webui' && key === 'base_url' ? 'NapCat WebUI 地址' : labels[key]) || key;
      let control, read;
      if (spec.type === 'object') {
        const nested = buildObject(spec, path + '.');
        const enabled = node('input', { type: 'checkbox' });
        const nestedFields = node('fieldset', { class: 'setup-fieldset', disabled: true, hidden: true }, nested.element);
        enabled.addEventListener('change', () => { nestedFields.disabled = !enabled.checked; nestedFields.hidden = !enabled.checked; });
        control = node('div', { class: 'setup-wide' }, field(label, enabled), nestedFields);
        read = credentials => enabled.checked ? nested.read(credentials) : undefined;
      } else if (spec.type === 'boolean' || typeof spec.const === 'boolean') {
        control = node('input', { type: 'checkbox', checked: spec.default === true });
        read = () => control.checked;
      } else if (spec.enum || spec.items?.enum) {
        const values = key === 'original_mode' ? ['reference'] : spec.enum || spec.items.enum;
        control = node('select', { multiple: spec.type === 'array', required });
        if (!required && spec.type !== 'array' && spec.default === undefined) control.append(node('option', { value: '' }, '使用默认值'));
        for (const value of values) control.append(node('option', { value }, ({ timeline: '时间线', archive: '归档', hidden: '隐藏', locked: '锁定', copy: '原始媒体', reference: '仅引用', newest_first: '由新到旧', oldest_first: '由旧到新' })[value] || value));
        if (spec.default !== undefined) control.value = spec.default;
        if (key === 'visibilities') control.value = 'timeline';
        read = () => spec.type === 'array' ? [...control.selectedOptions].map(v => v.value) : control.value || undefined;
      } else if (spec.type === 'array') {
        control = node('textarea', { rows: 3, required, placeholder: key === 'conversations' && spec.items?.type === 'object'
          ? 'group:群号\nprivate:好友 QQ 号' : '每行填写一项' });
        read = () => {
          const values = control.value.split(/\r?\n/).map(v => v.trim()).filter(Boolean);
          if (!values.length) return required ? [] : undefined;
          if (spec.items?.type === 'object') return values.map(value => {
            const match = /^(group|private):([^\s:]+)$/.exec(value);
            if (!match) throw new Error('QQ 会话请按 group:群号 或 private:QQ号填写，每行一项。');
            return { type: match[1], id: match[2] };
          });
          return [...new Set(values)];
        };
      } else {
        control = node('input', { type: secret ? 'password' : spec.type === 'integer' ? 'number' : 'text',
          required, autocomplete: secret ? 'new-password' : 'off',
          min: spec.minimum, max: spec.maximum, minlength: spec.minLength,
          value: spec.default ?? '', placeholder: key === 'root' ? '/绝对路径/数据目录' : key === 'base_url' ? 'https://你的服务地址' : '' });
        read = credentials => {
          const value = secret ? control.value : control.value.trim();
          if (!value) return undefined;
          if (secret) { credentials[path] = value; return undefined; }
          if (key === 'root' && !value.startsWith('/') && !/^[A-Za-z]:[\\/]/.test(value)) throw new Error('数据目录请填写服务所在机器上的绝对路径。');
          return spec.type === 'integer' ? Number(value) : value;
        };
      }
      control.setAttribute('data-config-field', path);
      if (spec.type !== 'object') control.setAttribute('aria-label', label + (required ? ' *' : ''));
      let container = spec.type === 'object' ? control : field(label + (required ? ' *' : ''), control);
      if (adapter.adapter_id === 'ai.local-logs' && path === 'root') {
        container = node('div', { class: 'setup-wide' }, container,
          localAIPathHelp(profile.id, profileNames[profile.id] || profile.id, control));
      }
      if (spec.type === 'array' || spec.type === 'object') container.classList.add('setup-wide');
      (key.startsWith('max_') || advanced.has(key) || (adapter.adapter_id === 'feishu.documents' && key === 'base_url') ? optional : basic).append(container);
      readers.push([key, read]);
    }
    return { element: node('div', {}, basic, optional.children.length ? extra : null), read(credentials) {
      return Object.fromEntries(readers.map(([key, read]) => [key, read(credentials)]).filter(([, value]) => value !== undefined));
    } };
  }

  function renderProfile() {
    feishu?.dispose(); feishu?.status.remove(); feishu = null;
    const version = ++formVersion;
    profile = adapter.profiles.find(p => p.id === profileSelect.value);
    const simpleImmich = adapter.adapter_id === 'immich.assets' && profile.id === 'api-key';
    const simpleConnection = ['qq.napcat', 'notion.workspace', 'feishu.documents'].includes(adapter.adapter_id);
    typeFields.hidden = (simpleImmich || simpleConnection) && page?.adapter === adapter.adapter_id;
    nameField.hidden = simpleImmich;
    name.required = !simpleImmich;
    heading.querySelector('.source-glyph').replaceChildren(sourceIcon(adapter.slug === 'ai' ? 'ai/' + profile.id : adapter.slug));
    errors.hidden = true;
    schemaText = undefined;
    save.querySelector('.button-label').textContent = simpleConnection || adapter.adapter_id === 'browser.ai-history' ? '连接并选择数据' : '保存并选择数据';
    if (adapter.adapter_id === 'feishu.documents') {
      feishu = feishuAuthorization(client, abort.signal);
      configRegion.replaceChildren(feishu.element);
      form.append(feishu.status);
      save.querySelector('.button-label').textContent = '授权连接飞书';
      readConfig = () => ({});
    } else if (adapter.adapter_id === 'browser.ai-history') {
      let selectedAccounts = [], ticket;
      const accounts = node('select', { required: true, 'aria-label': '选择平台账号' }, node('option', { value: '' }, '请先检测账号'));
      const status = node('p', { class: 'setup-hint', role: 'status' }, '');
      const detect = button('检测已登录账号', async () => {
        detect.disabled = true; status.textContent = '正在连接…';
        try {
          const hello = await connector('hello');
          ticket = await client('imports/browser-pairings', 'POST', { provider: profile.id,
            name: name.value.trim() || profileNames[profile.id], extension_id: hello.extension_id }, abort.signal);
          const result = await connector('probe', { provider: profile.id, code: ticket.code });
          if (!live || version !== formVersion) return;
          selectedAccounts = result.accounts;
          accounts.replaceChildren(...selectedAccounts.map((account, i) => node('option', { value: String(i) }, account.label)));
          status.textContent = selectedAccounts.length ? `已检测到 ${selectedAccounts.length} 个账号` : '没有检测到账号，请先打开平台页面并登录。';
        } catch (error) { if (live && version === formVersion) status.textContent = error.message; }
        finally { detect.disabled = false; }
      }, { icon: 'search' });
      configRegion.replaceChildren(detect, field('平台账号', accounts), status);
      readConfig = () => {
        const account = selectedAccounts[Number(accounts.value)];
        if (!ticket || !account) throw new Error('请先检测并选择平台账号。');
        return { code: ticket.code, account };
      };

    } else {
      const built = buildObject(profile.schema);
      configRegion.replaceChildren(built.element);
      if (adapter.adapter_id === 'wechat.localdb') configRegion.append(node('p', { class: 'setup-hint' },
        '连接后展示快照中的全部可读会话，再勾选需要导入的会话和日期范围。'));
      if (adapter.adapter_id === 'immich.assets' && profile.id === 'installed-openapi') {
        const upload = node('input', { type: 'file', accept: '.json,application/json', 'aria-label': '上传 Immich OpenAPI 文件' });
        const feedback = node('small', { class: 'setup-hint', role: 'status' });
        const schemaPath = configRegion.querySelector('[data-config-field="schema_path"]');
        const schemaHash = configRegion.querySelector('[data-config-field="schema_sha256"]');
        upload.addEventListener('change', async () => {
          const file = upload.files[0]; schemaText = undefined;
          errors.hidden = true;
          schemaPath.disabled = schemaHash.disabled = false;
          try {
            if (!file) return;
            if (file.size > 8 * 1024 * 1024) throw new Error('API 定义文件不能超过 8 MiB。');
            const value = await file.text();
            const parsed = JSON.parse(value);
            if (!parsed || typeof parsed !== 'object' || !parsed.paths || typeof parsed.paths !== 'object' || Array.isArray(parsed.paths)) throw new Error('请选择有效的 OpenAPI JSON 文件。');
            if (!live || formVersion !== version || upload.files[0] !== file) return;
            schemaText = value; schemaPath.disabled = schemaHash.disabled = true;
            feedback.textContent = `已选择 ${file.name}`;
          } catch (error) { if (live && formVersion === version) { errors.textContent = error.message; errors.hidden = false; upload.value = ''; } }
        });
        configRegion.prepend(node('div', { class: 'setup-schema-upload' }, field('上传 OpenAPI 文件', upload), feedback));
      }
      readConfig = credentials => {
        const config = built.read(credentials);
        if (adapter.adapter_id === 'screen.native') config.backend = profile.id === 'wayland-portal-v1' ? 'wayland_portal' : 'windows_wgc';
        return config;
      };
    }
  }
  let adapters = [];
  function renderAdapter() {
    adapter = adapters.find(a => a.adapter_id === selector.value);
    profileSelect.replaceChildren(...adapter.profiles.map(p => node('option', { value: p.id }, profileNames[p.id] || p.id)));
    if (page?.provider && adapter.profiles.some(p => p.id === page.provider)) profileSelect.value = page.provider;
    if (!name.value) name.value = page?.title || adapter.name;
    renderProfile();
  }
  selector.addEventListener('change', renderAdapter);
  profileSelect.addEventListener('change', renderProfile);
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (busy || !readConfig) return;
    errors.hidden = true;
    try {
      const credentials = {}, config = readConfig(credentials);
      busy = true; fields.disabled = true;
      if (adapter.adapter_id === 'feishu.documents') {
        const result = await feishu.connect(name.value.trim());
        if (live) await onSaved({ ...result, slug: 'feishu' });
      } else if (adapter.adapter_id === 'browser.ai-history') {
        const result = await connector('connect', { provider: profile.id, code: config.code, account: config.account });
        if (live) await onSaved({ source_id: result.source_id, slug: 'ai/' + profile.id });
      } else {
        const result = await client('imports/sources', 'POST', { source: {
          adapter_id: adapter.adapter_id, source_profile: profile.id,
          source_namespace: adapter.adapter_id === 'immich.assets' ? 'Immich' : name.value.trim(), config,
        }, credentials, ...(schemaText === undefined ? {} : { schema_text: schemaText }) }, abort.signal);
        if (live) await onSaved({ ...result, slug: adapter.slug });
      }
    } catch (error) {
      if (live && error.name !== 'AbortError') { errors.textContent = error.message; errors.hidden = false; }
    } finally { busy = false; fields.disabled = false; }
  });
  void client('imports/setup', 'GET', undefined, abort.signal).then(result => {
    if (!live) return;
    adapters = result.adapters;
    selector.replaceChildren(...adapters.map(a => node('option', { value: a.adapter_id }, a.name)));
    if (page) selector.value = page.adapter;
    renderAdapter();
  }).catch(error => { if (live && error.name !== 'AbortError') { configRegion.replaceChildren(); errors.textContent = error.message; errors.hidden = false; } });
  return { element, dispose() { live = false; feishu?.dispose(); abort.abort(); element.querySelectorAll('input[type=password]').forEach(input => { input.value = ''; }); } };
}
