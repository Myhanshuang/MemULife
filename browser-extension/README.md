# 浏览器 AI 来源

这是默认未连接任何账号的 Chromium MV3 扩展。TypeScript 源码使用 Chrome 的平台 API，运行时不加载第三方脚本。六个网页平台的内部读取接口集中在 `src/providers.ts` 与 `src/http.ts`，每次请求经过只读 allowlist；页面观察器只发送会话 URL 的 dirty 提示，不复制页面文字、认证响应或任意网络流量。

## 构建与检查

需要 Node 24.11 或更新版本、Chromium 131 或更新版本。

```sh
npm ci --prefix browser-extension
npm test --prefix browser-extension
```

测试包含严格 TypeScript 检查、真实 IndexedDB API 事务模拟、分页与账号切换、Unicode/长整数、A→B→A、ACK 丢失、任务重启、分块摘要和完整回执校验。`node tools/build.mjs` 通过 Node 自带类型剥离生成 `dist/`；测试依赖和源码不进入扩展包。此命令单独运行浏览器扩展回归。

在浏览器扩展管理页打开开发者模式，选择“加载已解压的扩展程序”，目录为本仓库 `browser-extension/dist/`。首次点击扩展按钮打开设置，填写 MemULife 服务器地址并授权该站点；之后点击按钮打开来源管理网页，添加来源并选择平台，授权后检测账号，再连接所需工作空间。不会预先申请其他站点权限。Grok 与 Perplexity 还会请求 cookies 权限，仅读取该平台 profile 声明的一个会话 cookie，并在内存中计算账号隔离指纹；Cookie 原文不持久化、不发送给设备代理。DeepSeek 只读取已打开平台标签页的 `userToken` 键，令牌仅存在当前后台调用内存中。

ChatGPT 同名工作空间会显示各自的原生工作空间 ID。若最近打开的 ChatGPT 标签页是一条已有对话，检测会用限定只读接口核对该对话在各工作空间的可达性；只有唯一匹配才标记“当前打开的对话”。没有唯一证据时不推断当前工作空间，应先打开一条目标对话再重新检测。

## 网页操作，连接器读取

MemULife 0.6.2 支持服务器与浏览器位于不同设备。扩展选项中填写 HTTPS 服务地址，例如 `https://memory.example.com`，点击“保存并打开管理页”，登录后连接平台并选择导入。扩展安装在已登录平台账号的浏览器电脑上；该电脑无需运行 MemULife 后端。导入期间需保持浏览器运行并保持平台登录。

本机部署仍支持 `http://127.0.0.1:5073`。服务器地址只填写 Origin，不包含管理页面路径、令牌或账号密码。管理登录仍在网页完成，扩展不保存管理令牌。切换服务器不会自动转移已有来源凭据，需在目标管理页重新连接来源。
网页“添加来源”选择平台并检测账号，首次访问的浏览器授权在简短确认窗口完成；之后连接、
会话预览、消息选择、导入和记录查看均在网页进行。无需复制扩展 ID 或配对码。

构建从 `memu/acquisition/ui/` 复制共享设计样式和图标，请在完整仓库内构建。
管理页通过 content script 通信：本机入口静态注册，HTTPS 入口仅在用户授权后动态注册到配置的管理站点；后台同时校验确切 Origin（包括端口）。检测/连接必须同时有已认证
管理 API 颁发的限时凭证，且绑定平台与确切扩展 Origin。来源交付密钥不会返回网页。
同步/状态消息按服务 Origin 和来源 ID 限定，拒绝 iframe 与其他来源的调用。

平台读取保持只读 allowlist 和用户选择边界；提交导入/新建预览后立即唤醒，完成通知驱动页面更新。
例行检查仍为 150 秒。已有 0.4 配对继续可用；升级须重新加载扩展并刷新管理网页。
Gemini 首页和具体会话页均可读取；手动刷新从目录首页开始，完成通知按连接账号隔离。
管理页显示连接器实际错误，并可在账号选择右侧编辑连接名称。

完整会话使用 `memulife.browser-conversation.v2`，包含结构化原始消息和原始响应供审计。
Gemini 消息 ID 来自原生轮次/候选 ID，顺序从历史接口倒序恢复；时间只使用明确位置并校验范围。
轮次时间和候选记录时间不宣称为用户设备点击发送的精确时刻。详见 [解析依据](../docs/browser-content.md)。

## 设备桥接（兼容已有 Native 部署）

Native Messaging host 名称为 `org.memu.acquisition`。本地配置须使用 `adapter_id=browser.ai-history`，`source_profile` 为下表的平台 ID，`config` 中的 `provider/account_id/workspace_id` 与来源绑定中的值完全一致。配置还必须限定 `extension_origin=chrome-extension://<扩展ID>/`。Host manifest 的 `allowed_origins` 必须同样限定这个扩展 ID。Linux 的启动脚本以如下命令运行，并转发 Chrome 传入的扩展 origin 参数：

```sh
python -m memu native-host --config /absolute/private/browser-source.json
```

先运行本仓库 `memu acquisition`，通过 `memu sources add` 登记浏览器来源。
随后运行 `memu sources native-manifest --root /私有采集目录 --source-id 来源UUID --extension-id 扩展ID --output /输出/org.memu.acquisition.json`。
多个绑定可重复提供 `--source-id`；生成的路由仅包含明确指定的来源。将输出 manifest 放入目标
浏览器的 NativeMessagingHosts 目录。完整步骤见 [采集端指南](../docs/acquisition.md)。
Host 按 provider/account/workspace 三项匹配；使用当前项目 `.env` 中对应 `source_identity` 的 token
连接采集服务。生成器只输出配置文件，不会修改浏览器设置或连接真实账号。

扩展发现目录并在内存中上报会话 ID/locator/revision，用户先在管理后端创建所选会话任务，扩展再领取 import_job_id 和范围。没有导入选择或显式预览请求时不请求详情、不建立正文任务。后台 alarm 为 150 秒；相邻平台请求至少间隔 1 秒、每账号并发为 1。offset 列表完成后重读首页；页面变化只为已选会话建立提示，不点击、滚动或切换分支。

## 数据与故障行为

- IndexedDB 保存绑定与分页控制游标；仅已获导入选择的会话保存详情任务、原始响应与 outbox。未选目录条目只在内存处理。读取前及返回后再次取得后端选择，并在写事务中核对任务，防止迟到响应写入新选择。
- 来源的失败详情单独重试。429 完整尊重 `Retry-After`，401、挑战页、身份变化、未知结构均明确报告。404/410 或列表缺项不生成删除。
- 详情响应原样保存为字符串；长整数和高精度时间游标不会先经过 JavaScript Number 截断。不存在的消息时间与角色不补造。
- 每条请求使用持久 UUID。outbox 按观察顺序交付；早期批次失败会阻止较晚观察越过它。只在相同请求 ID、batch ID 且 `durable_level=edge_durable` 的最终回执之后回收原文。
- 超过 1 MiB 的 native 请求分为 192 KiB 块，最多 86 块、16 MiB 整体上限，并核验整个 UTF-8 请求的 SHA-256。分块经 native 转交后端的有界内存；不写 native-staging。中间回执 storage=volatile，不能释放 outbox；重启后重发所有块。最终后端核对实际会话与任务后才写入 MemULife 的持久待处理队列。
- 一个 detail 最多累积 500 页或 8 MiB 原始响应，以控制组装与传输占用。到达预算不会把来源标为完成。

升级必须在扩展管理页重新加载构建后的 dist。旧版自动采集数据保留，不冒充用户选择；无 import_job_id 的旧 outbox 不再自动传输。新版后端拒绝旧版无任务请求。候选目录显示来源实际提供的标题、时间与数量，缺失时保留 ID；独立平台选择页面在 `/manage/imports/ai/<平台>`。网页自动连接需要扩展 0.5.0 及配套后端，升级后须重新加载扩展。

## 会话内消息选择

管理页打开会话时创建预览请求并通知连接器读取；连接器也会在例行唤醒时领取尚未处理的预览。只有唤醒入口设置 `claim_previews=true`，采集期间的权限复查不会领取其他预览。预览请求本身、详情响应、分页中间结果与消息正文都只在内存存在，不进入 IndexedDB、outbox 或设备队列。后端按原请求、账号、管理权限和配置再次检查响应。

六个平台将可见文本投影为 `id/role/text/occurred_at`；没有原生角色或时间时保持 unknown/null。原生消息 ID 优先，缺少 ID 时使用内容与结构位置摘要，结构移动或内容变化会导致旧选择拒绝导入。纯工具、附件和不透明块不是可选文本消息，完整原文需求仍使用整段会话模式。预览最多 500 条可见文本消息、900 KiB 消息传输；读取最多 50 页和 8 MiB 原始响应，超过预算明确失败，不返回伪完整的截断列表。

选择清单保存每条消息的 ID 和 `SHA-256(JSON.stringify([id, role, text, occurred_at]))`，不保存预览正文。消息级任务重新在内存读取来源，摘要全部匹配后才将所选消息投影为 `memulife.browser-messages.v1` 并进入持久 outbox。原始响应、未选文字、附件和未知元数据不会混入这个格式；失败只记录稳定错误码。事务内还会检查选择摘要是否变化，拒绝给消息任务写入完整分页响应。完整会话任务继续使用原有原文分页与断点规则。

## 当前验证范围

| 平台 ID | 已实现 | 仍需真实环境准入 |
|---|---|---|
| `chatgpt` | 在已打开的 ChatGPT 标签页内读取限定接口，原生用户/工作空间绑定、offset 列表、完整 mapping DAG、全部节点及非文本原文 | 需保持对应账号标签页打开；特殊/归档/项目 scope、图片原件、目标网页版本仍未验收 |
| `claude` | 账号与组织绑定、列表与 tree 详情、工具/未知块原文、eventual 后续复查 | 目标版本树字段、Artifacts/附件原件、特殊 scope |
| `gemini` | 从已打开的 Gemini 标签页识别 `/app` 或 `/u/N/app`，在对应页面会话内读取限定 RPC、多个 batchexecute frame、列表/详情 opaque 分页、候选原文 | 需保持对应账号标签页打开；稳定原生 principal、分支与媒体仍未验收；当前绑定会话指纹，变更需重新配对 |
| `perplexity` | UUID/slug 分离、列表分页、100 条之后的详情分页、引用原文 | 真实服务分页终止条件、原生 principal、附件；当前仅会话绑定 |
| `deepseek` | 精确 `(updated_at,id)` 游标、无 cache_version 完整读取、所有 fragments 原文 | pinned scope、真实账号字段、附件；MERGE 会保留旧数据并重试完整读取，不冒充 REPLACE |
| `grok` | 60 条有界发现、已知历史 ID 轮转、response DAG、每 50 个响应分批且校验缺失 | 全历史发现、原生 principal、附件；当前仅会话绑定 |

完整会话模式保留附件引用与未知结构的原文，当前不下载媒体原件，不把 metadata-only 标为已备份。消息模式仅保存所选可见文本，排除附件、工具 JSON 和未知结构。可读材料包含逐条消息的角色和已知时间；工具、附件和分支原始结构保留在审计响应中。已在独立 Chromium 配置中装载扩展，验证本机配对和合成数据完整交付；尚无真实平台账号的实机验收，因此 profile 状态为 `implemented / fixture_verified`，`device_verified=false`；账号整体覆盖始终明确为 partial。

## 维护边界

`providers.ts` 只负责账户、请求与完整会话格式；`messages.ts` 负责消息投影与规范摘要；`sync.ts` 只调度；`store.ts` 只处理事务；`native.ts` 处理 Native 交接，`connection.ts` 处理限定来源的本机 HTTP 配对与交付。身份、时间和保留策略不放入 UI。平台格式变化时更新对应 profile 与去标识 fixture，不放宽解析为“成功空列表”。运行错误只输出稳定错误码，不打印响应、URL 中的认证参数、Cookie 或令牌。

请求结构依据交付规格 A.5/A.6，以及本轮读取的原始实现：

- [Chromium Native Messaging](https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging)、[service worker 生命周期](https://developer.chrome.com/docs/extensions/develop/concepts/service-workers/lifecycle)。
- hstry 固定提交 `c7ea955878223abb11dbdc91ddcdc2428b280a1b` 的 [ChatGPT](https://github.com/byteowlz/hstry/blob/c7ea955878223abb11dbdc91ddcdc2428b280a1b/extension/providers/chatgpt.js)、[Claude](https://github.com/byteowlz/hstry/blob/c7ea955878223abb11dbdc91ddcdc2428b280a1b/extension/providers/claude.js)、[Gemini](https://github.com/byteowlz/hstry/blob/c7ea955878223abb11dbdc91ddcdc2428b280a1b/extension/providers/gemini.js)、[Perplexity](https://github.com/byteowlz/hstry/blob/c7ea955878223abb11dbdc91ddcdc2428b280a1b/extension/providers/perplexity.js)。
- OmniExporter 的 [DeepSeek](https://github.com/joganubaid/omniexporter/blob/main/src/adapters/deepseek-adapter.js) 与 [Grok](https://github.com/joganubaid/omniexporter/blob/main/src/adapters/grok-adapter.js)，本轮读取内容摘要记录在兼容性 profile。请求知识独立重写，未复制上游 UI、调度器或许可代码。
