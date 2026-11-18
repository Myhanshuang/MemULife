import { connector } from './connector.js';
const errors = {
  memory_edit_conflict: '这条记忆已更新，请核对最新内容后重新编辑。',
  memory_edit_invalid: '保存失败，请检查正文并重新读取记忆。',
  napcat_webui_auth_failed: 'NapCat 登录失败，请检查 WebUI 登录密码。',
  napcat_webui_unsupported: '当前 NapCat 版本没有直连接口，请更新 NapCat 后重试。',
  napcat_qq_offline: 'NapCat 中的 QQ 尚未登录，请先登录 QQ。',
  napcat_2fa_required: '此 NapCat 开启了双重验证，当前直连还不支持该登录方式。',
  feishu_app_invalid: '请检查 App ID，应以 cli_ 开头。',
  feishu_redirect_invalid: '回调地址与当前管理页面不一致，请刷新后重新授权。',
  feishu_authorization_expired: '授权已过期或已使用，请重新连接飞书。',
  feishu_authorization_denied: '未完成飞书授权，请重新连接。',
  feishu_authorization_capacity: '正在进行的授权较多，请稍后重试。',
  feishu_authorization_failed: '飞书授权失败，请检查应用凭据和发布状态后重新连接。',
  feishu_authorization_scopes: '飞书没有返回续期凭证。请开通 offline_access 用户身份权限并启用刷新 user_access_token，发布应用后重新授权。',
  memory_query_invalid: '筛选条件无效或记忆已更新，请检查条件并刷新后重新查看。',
  memory_unavailable: '记忆服务暂不可用，请稍后刷新。',
  source_watch_active: '此来源已开启持续关注，请先关闭后再手动选择内容。',
  topic_invalid: '主题保存失败，请检查名称和关注范围。',
  config_invalid: '来源配置不完整或格式不正确，请检查必填项、账号及可浏览范围。',
  source_profile_unknown: '请选择受支持的来源格式。', source_namespace_required: '请填写来源名称。',
  source_credentials_invalid: '来源密钥格式无效，请检查后重新填写。',
  source_schema_invalid: '请选择包含 paths 的有效 OpenAPI JSON 文件，大小不能超过 8 MiB。',
  source_identity_requires_write: '当前身份没有来源写入权限。',
  extension_id_invalid: '扩展 ID 应为 32 位字母，请从 MemULife 扩展连接页复制。',
  pairing_invalid: '配对码已过期或与扩展不匹配，请重新生成。',
  pairing_capacity: '待配对请求较多，请稍后重新生成。',
  unauthorized: '管理密钥无效，请重新登录。', scope_denied: '当前密钥没有管理权限。',
  import_scope_denied: '所选范围已变化或不可访问，请重新浏览。', import_plan_stale: '来源范围已变化，请重新浏览并核对选择。',
  import_candidate_changed: '文件已修改，请重新核对范围。', import_selection_empty: '所选目录没有可导入文件。',
  import_already_active: '相同范围已有导入任务，请查看导入记录。', import_duplicate_scope: '同一对象不能重复出现在这批导入中。',
  import_binding_unavailable: '来源暂不可用，请检查连接。', import_candidate_unavailable: '来源内容暂不可用，请刷新。',
  import_preview_cursor_invalid: '预览已过期，请重新读取。', preview_cursor_invalid: '预览已过期，请重新读取。',
  account_binding_mismatch: '登录账号已变化，请重新连接来源。', auth_required: '来源凭据无效，请检查 API Key 或重新登录。',
  sync_already_exists: '此范围已开启持续更新。', import_subscription_control_required: '请在持续更新列表中控制此任务。',
  import_terminal: '任务已经结束，请刷新记录。', sync_terminal: '订阅已经取消。',
  import_access_changed: '来源权限已变化，请重新登录或刷新来源。',
  source_binding_mismatch: '来源账号绑定已变化，请检查连接。',
  invalid_request: '请检查所选范围与日期。',
  import_message_changed: '消息已变化，请重新读取并选择。',
  import_message_invalid: '消息选择不可用，请重新读取并选择。',
  import_message_scope_denied: '消息超出已选范围，请重新核对选择。',
  import_preview_expired: '预览已过期，请重新读取。',
  import_preview_scope_denied: '预览范围已变化，请重新读取。',
  import_preview_invalid: '预览不可用，请重新读取。',
  media_preview_unavailable: '此媒体暂时无法预览，原始引用仍保留。',
  memory_delete_changed: '内容或关联记忆已变化，请重新查看删除范围后确认。',
  media_preview_too_large: '媒体超过 16 MiB，请从关联证据中打开较短的音视频片段。',
  immich_preview_unsupported: '此连接暂不支持预览，请用 URL 和 API Key 重新添加 Immich 连接。',
  import_preview_capacity: '同时读取的消息较多，请稍后重试。',
  notion_cursor_expired: '目录已过期，请重新打开这一层。',
  notion_parent_changed: '页面范围已变化，请重新浏览。',
  notion_tree_budget_exceeded: '此层目录超过读取上限，请缩小连接范围。',
  notion_tree_capacity: '临时浏览内容较多，请稍后重试。',
  notion_tree_depth_exceeded: '目录层级过深，请直接连接需要的页面。',
  notion_tree_cycle: '页面层级异常，请检查 Notion 中的页面位置。',
  incompatible_schema: '来源格式已变化，当前版本暂不能读取。',
  source_url_invalid: '请填写完整的服务 URL，例如 https://photos.example.com 或 http://192.168.1.2:2283。',
  source_access_denied: '当前 API Key 没有读取权限，请在来源平台检查密钥权限。',
  source_unavailable: '来源接口不可用，请检查服务 URL 和版本。',
  source_offline: '暂时无法连接来源，请检查服务地址与网络。',
  account_scope_mismatch: '所选内容不属于当前连接账号，请重新选择。',
  incompatible_server_version: '服务版本与其 API 定义不一致，请更新 Immich 服务后刷新。',
  response_too_large: '文件超过当前读取大小上限，请调整来源配置或缩小文件。',
  pagination_loop: '来源分页异常，请稍后重试。',
  account_changed: '浏览器账号已变化，请重新连接来源。',
  permission_required: '请在浏览器扩展中允许访问此平台。',
  dormant_browser: '请打开已登录的平台页面。',
  preview_expired: '消息预览已过期，请重新读取。',
  preview_too_large: '此会话超过预览容量。',
  detail_budget_paused: '此会话超过预览容量。',
};

export const errorMessage = code => {
  const feishu = /^feishu_oauth_(\d{1,10})$/.exec(code || '');
  if (feishu) {
    const messages = {
      20002: 'App Secret 无效，请从当前应用的凭证页面重新复制。',
      20010: '当前飞书账号没有使用此应用的权限，请检查应用可用范围。',
      20024: '授权码与 App ID 不匹配，请确认使用的是同一个应用。',
      20029: '回调地址不匹配，请将页面提供的完整回调地址加入此应用的重定向 URL。',
      20048: '应用不存在，请检查 App ID。',
      20049: '授权码的 PKCE 校验失败。请刷新管理页后重新授权；这不属于回调地址错误。',
      20003: '授权码无效或已使用，请重新授权。',
      20004: '授权码已过期，请重新授权。',
      20065: '授权码已经使用，请重新授权。',
      20071: '换取凭证时的回调地址与授权地址不一致，请刷新管理页后重新授权。',
      20069: '应用尚未启用，请检查发布状态。',
      20074: '请在飞书应用安全设置中启用刷新 user_access_token，并发布应用。',
    };
    return `飞书错误 ${feishu[1]}：${messages[feishu[1]] || '授权凭证校验失败，请重新授权后重试。'}`;
  }
  return errors[code] || '请求未完成，请检查来源后重试。';
};

export function createClient(getToken) {
  return async (path, method = 'GET', body, signal) => {
    const token = getToken();
    if (!token || /[^!-~]/.test(token)) {
      throw new Error('管理密钥格式不正确。请只填写 token 值，或选择管理凭据 JSON 文件。');
    }
    let response, value;
    try {
      response = await fetch(`/v1/manage/${path}`, { method, signal, cache: 'no-store',
        credentials: 'same-origin', headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    } catch (error) {
      if (error.name === 'AbortError') throw error;
      throw new Error(`管理请求未收到响应（${method} ${path.split('?')[0]}）。`);
    }
    try {
      value = await response.json();
    } catch (error) {
      if (error.name === 'AbortError') throw error;
      throw new Error(`管理接口响应不完整或不是 JSON（HTTP ${response.status}，${method} ${path.split('?')[0]}）。`);
    }
    if (!response.ok) {
      const error = new Error(response.status === 401 ? '管理登录凭据无效，请重新登录。' : errorMessage(value.code));
      error.code = value.code; error.result = value.result; throw error;
    }
    if (method === 'POST' && (path === 'imports/jobs' || path === 'imports/jobs/batch' || (path.endsWith('/preview') && value.status === 'pending' && !body?.cursor)) && body?.source_id !== null) {
      const source = body?.source_id || path.match(/^imports\/sources\/([^/]+)\/preview$/)?.[1];
      if (source) void connector('sync', { source_id: source }).catch(() => {});
    }
    return value;
  };
}
