# 原始数据采集端

采集服务负责来源发现、内容选择、授权和素材交付，提供命令行、HTTP 接口与管理页面。
导入任务使用持久 outbox 和分页回执衔接记忆核心。

```text
来源发现 / 按需预览（仅有界内存）
  → 用户选择范围 / 冻结计划
  → 本地读取器或浏览器扩展提供原始 Snapshot/Page
  → 采集 outbox（选后持久化）
  → POST /v1/memory/write
  → Source → Digest（按需）→ Unit → 编码 / 召回 → 关系 → 提交 / 索引 → 后台视图
```

## 安装和运行

```bash
.venv/bin/python -m pip install -e '.[postgres,acquisition,test]'
.venv/bin/python -m memu core
# 在另一个终端
.venv/bin/python -m memu acquisition
```

核心负责模型调用和记忆仓库；采集服务通过核心 API 交付材料，通过 PostgreSQL 保存采集任务状态。
默认监听 `127.0.0.1:5073`，页面为 `/manage/imports`。使用 `.env` 中
`MEMU_ACCESS_OWNERS` 对应身份的 Bearer token 登录；token 只在页面内存中，不写浏览器存储。

顶部“记忆导入”保留来源设置、范围选择、持续关注和导入记录；“记忆管理”位于
`/manage/memory`，复用来源导航，查看已入库的 Unit / Source。可按来源连接、
原导入格式和日期范围筛选；日期使用仓库时区，包含起止两天，未知日期只在未限制日期时显示。
列表采用两列随内容高度变化的卡片，窄屏为一列。Unit 列表只展示正文和日期，
引用 ID、账户、原始对象和证据均放在详情中。聊天 Source 根据已有消息角色和时间显示为对话；
本地文件有原件时默认展示附件卡片，文件名、格式和字节保持原样；详情可切换查看提取文本。
聊天中已保存的非媒体附件也提供下载，图片和音视频保留预览、播放与下载入口。
Source 详情可以切换正文和已保存的原始导入内容（文本或 JSON）；没有独立原件时显示正文，
图片在列表和详情按需预览，已保存的音视频可加载播放器；Source 范围也包括原生媒体。
关联附件按同一次导入的原始版本匹配。部分删除过的 Source 不再开放整个原件或完整附件，
部分删除过的媒体只能打开未删除的片段。预览接口仅限 owner，不在 URL 中携带凭据。

Unit / Source 支持删除，先显示实际关联范围再确认。删除 Unit 时同时清除其事实证据链中
直接关联的 Source 片段，引用这些片段的其他记忆也会失效。删除沿用逐行置空，不压缩文件、
不移动其他行号。相关日/月 Summary、Runtime 和全局记忆立即隐藏旧内容并排队重建。
管理页面在删除落盘、重建任务入队后立即返回；索引同步和主题整理也由后台 worker 完成，
删除弹窗不等待模型或索引重建。后台工作保留在持久队列中，服务重启后继续处理。
删除预览与提交之间若内容变化，会拒绝旧确认，要求重新核对。

左侧“日 / 月 Summary”按日期查询日汇总或月汇总，月汇总按月份与日期范围的交集筛选；
“记忆主题”继续复用现有主题编辑。右上角搜索可多选 Unit、Source、Summary、记忆主题（Runtime），
按正文的连续文字匹配、区分大小写，不使用正则、模糊或语义搜索。浏览只读取已提交数据，
不调用模型；列表和长正文均分页，已删除或因删除而等待重建的内容不会显示。
各来源没有有效正文、也没有附件的审计占位（`audit_only=true`）不展示在列表、搜索和详情中，
导入时只保留审计回执，不再创建空 Source；已有原件、原生媒体或远程引用的附件仍正常保留。
历史清理仅处理带审计标记且正文为空或完全匹配系统占位文字的 Source，不按关键词删除正常原文。
清理沿用逐行置空和关联视图重建，保留原始审计数据，不移动其他内容的行号。

管理 API 为 `POST /v1/manage/memory`（`layers/query/adapter/profile/source_id/start/end/cursor/limit`）
及 `POST /v1/manage/memory/detail`（`ref/original/offset/revision`）、
`POST /v1/manage/memory/media`（`ref`，本地媒体上限 16 MiB，Immich 返回临时缩略图与原站链接），仅限 owner。
原始文件通过 `POST /v1/manage/memory/file`（`ref/file_id`）按已保存的引用读取，上限 32 MiB；
文件路径由服务端解析，任何部分被删除后不再提供该文件的完整原件。
采集服务转发到核心的 `/v1/memory/browse` 和 `/v1/memory/browse/detail`，不增加 MCP 工具。
删除使用 `POST /v1/manage/memory/delete-preview`（`ref`）与
`POST /v1/manage/memory/delete`（`ref/revision`），同样仅限 owner。
历史空审计清理使用核心的 `POST /v1/memory/browse/cleanup-audits`：空请求预览数量与受影响记忆，
核对后携带 `revision` 提交；仅限 owner。

| 配置 | 默认 / 含义 |
| --- | --- |
| `MEMU_ACQUISITION_ROOT` | `./adapter-state`；绑定、任务、订阅和已选择的原文待处理队列 |
| `MEMU_ACQUISITION_HOST` / `PORT` | `127.0.0.1` / `5073` |
| `MEMU_ACQUISITION_URL` | `http://127.0.0.1:5073`；CLI / native host 的连接地址 |
| `MEMU_ACQUISITION_WORKERS` | 默认 2，范围 1–8；并行处理独立导入任务，同一任务的游标串行推进 |
| `MEMU_DATABASE_URL` | 复用核心 PostgreSQL；存放采集任务、回执和 outbox 检查点 |
| `MEMU_CORE_URL` | 核心服务地址；采集端用此地址提交原始素材 |
| `MEMU_MCP_TOKENS` | 管理员及独立来源身份的 token 映射 |

同一采集目录只能启动一个进程。
来源配置中的根目录应使用服务所在机器上的绝对路径。网页填写的访问凭据保存到采集目录内
0600 的 `credentials.json`，控制配置只存生成的环境变量引用；来源列表及网页不会回显密钥。
命令行配置支持 adapter 的 `env:NAME` / `token_env` 引用。

## 在网页添加来源和导入

1. 打开 `http://127.0.0.1:5073/manage/imports` 并登录，点击“添加来源”。
2. 选择来源类型与格式，填写名称、目录或服务地址、账号范围和所需凭据。高级参数可展开设置。
3. 点击“连接并选择数据”（其他来源为“保存并选择数据”）。验证连接并保存配置后，页面显示可浏览的数据，尚未导入正文。
4. 选择文件、会话、文档或资产；按需预览、选择日期或消息，然后点击“核对范围”→“开始导入”。
5. 在“导入记录”查看完成状态。需要持续更新时，待一次性任务完成后再开启。

文件和本地 AI 填日志目录；录屏读取已有片段清单。
Notion 新连接只填集成密钥，自动绑定令牌所属用户，无需工作空间或页面 ID；
通过标题目录选择可访问的页面、数据库或数据源。飞书新连接填写 App ID 和 App Secret，点击“授权连接飞书”，
在飞书窗口中确认授权后自动识别用户，
在内容选择中浏览云空间文件夹、Docx 文档和知识库。目录查询不保存正文，导入仍严格限于明确选中的内容。
原有显式范围配置继续有效。飞书、Notion 需要来源服务授予访问权限。
Notion 和飞书的左侧目录保持固定，点击文档后在中间区域浏览下级内容。
Notion 页面支持导入前的正文预览和长文分页，只读取当前页面，不自动读取子页面或下载附件。
飞书的“首次配置”提供回调地址和所需用户身份权限；在飞书应用后台配置并发布一次即可。
网页使用带 state 和 PKCE 的 OAuth 授权，授权码只交换一次；App Secret 和刷新凭证保存在服务端私有文件中。
为避开飞书 v3 授权码交换的 PKCE 兼容问题，首次交换使用 v2 接口，自动续期使用兼容 v2 凭证的 v3 接口。
服务在访问凭证即将到期时刷新，并保存新的刷新凭证；有效期以 `expires_in` 和 `refresh_token_expires_in` 为准。
撤销授权或刷新凭证过期后需要重新授权。旧的手动 token 和 OAuth v2 连接继续兼容。
接口依据：[Notion 标题搜索](https://developers.notion.com/reference/post-search)、
[飞书文件夹清单](https://open.feishu.cn/document/server-docs/docs/drive-v1/folder/list)、
[飞书知识空间清单](https://open.feishu.cn/document/server-docs/docs/wiki-v2/space/list)。
授权与续期依据：[飞书 OAuth v3 刷新凭证](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/authentication-management/access-token/refresh-user-access-token-v3)。

Immich 新连接只需服务 URL 和 API Key；URL 可带或不带
`/api`。账号、服务器版本和 API 定义自动读取，无需填写 ID 或上传 OpenAPI 文件。
连接后可选择相册、进入相册浏览照片与视频，或打开全部照片与视频并逐项选择；支持拍摄日期范围。
点击左侧相册后，中间直接展示缩略图网格；视频展示封面和时长。日期筛选会更新预览，
可逐项勾选或全选已加载内容，也可用左侧勾选框选择整个相册。
API Key 需要 `user.read`、`album.read`、`asset.read`、`asset.edit.get`；
缩略图预览另需 `asset.view`，无需 `asset.download`。导入仅保存资产引用、描述和时间等元数据，
不会下载原件；旧配置中的 `original_mode=copy` 也按引用处理。管理页按需读取临时缩略图，
通过“在 Immich 中查看”打开原站照片或播放视频。引用导入不会将原件送入视觉模型，
因此整理出的文字以来源元数据为依据。
范围限当前账号的时间线及归档，排除回收站。
原有 `installed-openapi` 配置仍可读取。网页 AI 的账号连接步骤见下节。

本地文件目录不再限制扩展名。文本、代码、HTML、PDF 和 DOCX/XLSX/PPTX 可提取正文，图片、
音频和视频按媒体导入；PDF 文本提取使用本机 `pdftotext`（Poppler）。扫描 PDF、无法解析的格式、
损坏文件及空文件仍保存原件，并在导入记录标记未解析，不调用模型凭占位文字生成记忆。
原件及解析状态保存在来源元数据中；目录浏览不打开正文，提交时校验已选择文件的版本。
现有单文件大小预算仍生效（默认 16 MiB，可在高级选项调至 32 MiB）。
保留原件的请求还包含 Base64 传输开销；导入较大文件时应将
`MEMU_CORE_MAX_BODY_BYTES` 设置为 `100663296`（96 MiB），采集器每页只提交一个本地文件。

仍可使用命令行登记（可选）：

```bash
.venv/bin/python -m memu sources adapters
.venv/bin/python -m memu sources manifest filesystem.typora
.venv/bin/python -m memu sources add --from owner --source /已填写的/source.json
.venv/bin/python -m memu sources list --from owner
```

登记返回 `source_id`，只保存来源绑定，不读取正文。来源属于登记它的管理身份；其他身份不能
浏览、选择或控制该来源。可以通过 `--source-identity browser1` 为浏览器交付选择独立身份，
该身份须已经配置 token 与 write 权限，不能据此获得来源管理权限。

网页保留文件、飞书、Notion、Immich、本地 AI、录屏及六个网页 AI 平台的独立页面。
先浏览，再把需要的范围放入选择清单，核对后点“开始导入”。预览不是持久化授权。
本地 AI 只打开选中的文件；文件目录在计划时展开成有限的文件清单；新增文件不自动纳入。
聊天时间为 `[start,end)`，缺失时间不猜测；网页消息细选绑定原生 ID 和正文摘要，内容改变会拒绝旧选择。

需要纯命令行时：

```bash
.venv/bin/python -m memu sources candidates --from owner --source-id SOURCE_UUID
.venv/bin/python -m memu sources plan --from owner --source-id SOURCE_UUID \
  --selection /选择.json > /私有目录/plan.json
.venv/bin/python -m memu sources submit --from owner --source-id SOURCE_UUID --plan /私有目录/plan.json
.venv/bin/python -m memu sources jobs --from owner
```

`collect --source ... --selection ... --state ... --from ...` 可直接执行一次有界采集，不要求采集网页
服务。此时来源 JSON 需要 `source_instance_id`；文件的 selection 需要计划返回的冻结 `revisions`。
`collect` 最多处理 `--max-pages` 页，返回 `status=more` 可显式继续。使用 `--resume` 重放原待提交页。

## 浏览器扩展

```bash
npm ci --prefix browser-extension
npm test --prefix browser-extension
```

加载本仓库 `browser-extension/dist/` 为已解压 Chromium 扩展（MemULife 浏览器连接器 0.5.0）。
升级后在 Chrome 扩展管理页重新加载，再刷新采集网页。扩展按钮直接打开来源管理。

1. 打开已登录的 AI 平台页面。
2. 网页“添加来源”选择“网页 AI”和平台，点击“检测已登录账号”。首次使用时在浏览器弹窗中授权。
3. 选择检测到的账号，点击“连接并选择数据”。连接器 ID 和限时配对凭证由程序交换，无需复制。
4. 点击会话标题自动读取详情。选择完整会话或其中消息，核对后导入；提交会立即通知连接器。
5. 导入记录的“查看详情”显示所选范围、原始材料和生成的记忆。单一账号不显示账号切换控件。

网页操作仍需管理身份。自动握手限定确切网页 Origin、扩展 ID、平台与短期凭证；连接器不能接收
管理密钥。其他本机网页没有管理端生成的凭证就不能要求检测账号或建立连接。连接器只保留
平台授权窗口与网页入口，原始数据读取、断点与后台 150 秒检查能力保持不变。

完整会话现在保存有角色、原生 ID、正文和可用消息时间的可读 Source；原始网络响应保存在该
Source 的私有审计元数据中，不参与正文提炼。Gemini 的轮次时间与候选回复记录时间分别标明；
没有时间时保持未知，不用采集时间或会话更新时间填充。旧 Source 不被静默覆盖。
详细解析依据见 [浏览器正文与时间](browser-content.md)。

也可通过 Native Messaging 连接浏览器：

```bash
.venv/bin/python -m memu sources native-manifest \
  --root /绝对路径/adapter-state --source-id SOURCE_UUID \
  --extension-id EXTENSION_ID --output /私有目录/org.memu.acquisition.json
```

多个账号可重复 `--source-id`。生成器输出严格限定扩展 origin 的 manifest、启动脚本和路由配置；
按目标浏览器要求把 manifest 安装到 NativeMessagingHosts 目录。Linux Chrome 通常为
`~/.config/google-chrome/NativeMessagingHosts/`，Chromium 通常为 `~/.config/chromium/NativeMessagingHosts/`；
实际位置取决于浏览器发行版与配置目录。命令不自动修改浏览器。

native host 名称为 `org.memu.acquisition`，与浏览器扩展的连接配置一致。
生成的脚本使用当前解释器和 `MEMU_ENV_FILE` 加载运行配置。

扩展先上报目录元数据；没有服务端选择时不能提交正文。会话细选由消息校验器验证。
大请求经过有界内存分块；最终 `edge_durable` 回执表示所选原始素材已经进入采集 outbox，可以
回收扩展端副本，**不表示记忆已生成**。采集后台随后调用完整记忆写入流程，网页任务完成后才算
交接完成。分块中间回执不能回收原文。未知结果保留原 batch / digest / 幂等键。

## 录屏和附件

`native/wayland/` 与 `native/windows/` 保留原生采集助手源码；它们输出 sealed segment 文件。
网页录屏来源只发现已有清单、按选择读取原始片段，不隐式启动桌面捕获。Wayland 可用
`/usr/bin/python3 -m native.wayland --help` 查看依赖探测、自测与显式录屏命令。Windows 仍须在对应
平台构建与验收，不把 Linux 测试结果当作 Windows 设备验证。

UTF-8 文本 / JSON 原样送入 Source；附件文本保留独立标识，原始图片、音频、视频以字节提交，
模型处理发生在核心。文件附件只在选中 `include_attachments` 后读取授权根目录内的本地引用，
不下载网页链接；未获取的远程图片会计入附件缺失。不支持直接解析的二进制附件（例如 PDF）
保留原件在私有 material-metadata 文件中，并显示“原件已保存，正文尚未解析”。
源对象版本作为新的事件追加；删除通知不会自动清理已有记忆。

## 任务恢复和持续更新

来源页提供“持续关注”开关，作用于当前连接来源的授权范围，默认关闭。开启后先导入全部已有内容，
随后将新增或变更内容自动送入现有 Source 优先管线，始终开启附件选项；下方手动内容选择区同步锁定。
关闭后恢复手动选择。默认检查间隔 15 分钟，
浏览器目录继续遵循连接器的 150 秒唤醒和平台限流；浏览器和已登录的平台页面需保持可用。
“持续更新”仍是既有导入范围的更新；“持续关注”会发现新的会话和文件。暂停或删除来源停止后续提交，
不删除已接收的记忆。控制状态和逐对象指纹存于 PostgreSQL，未变化的内容不重复提交。

ChatGPT 附件需要重新加载 0.6.1 连接器。连接器按原生文件 ID 和会话 ID 下载原件，通过已有分块传输和
写入回执送入核心；文本附件合并到 Source，图片/音视频保留原始字节。单次附件原件总量最多 8 MiB，
还受 16 MiB 传输包上限约束。下载失败不会提交一个假装完整的纯文本会话。
导入记录显示已保存、未取回、未选入数量，以及正文未解析/附件完整性未核验提示。
浏览器读取或下载失败也写入对应导入记录，包含阶段、错误码和可用的 HTTP 状态，不记录下载地址或凭据。

2026-11-16 的实际附件审计：ChatGPT 测试会话缺少 3 张 JPEG；DeepSeek 测试会话缺少 2 个 Python
文件和 1 个 PDF。新连接器已接入 ChatGPT 下载；DeepSeek 和 Claude 的已识别附件会显式列为待下载，
其他浏览器格式的完整性仍待设备验证。部分来源的引用型路径尚未统一接上下载；
飞书已有下载实现；Immich 固定为引用模式。不要把勾选“含附件”视为所有平台附件均已取回。


整页素材均被核心持久接收（包括 `accepted` / `queued`）后推进来源游标，派生结果随后批量查询；全部完成后任务才显示完成。网络断线保留被冻结的待提交素材；网页“继续”和
命令行 `--resume` 是显式恢复，不重新读取变化后的素材。核心返回 `request_id` 的失败任务先经
所有者 `memory_manage retry` 处理。文件已经提交但索引待修复不重复追加。

一次性导入完成后才可单独开启持续更新，默认 900 秒，最低 150 秒。保留首次范围、固定结束时间
和消息摘要；目录新增文件、新会话均需新选择。失败会暂停等待处理。采集进程崩溃时正在运行的任务
暂停等待显式恢复；已排队且未开始的选择仍可执行。暂停 / 取消阻止新的页接受，已提交核心的页可能
继续完成，已写入记忆不会回滚删除。

采集控制状态存入 PostgreSQL 的 `memu_acquisition_records`，逐条回执存入 `memu_acquisition_receipts`；状态变更只更新对应记录。首次启动自动迁移原 `control.json` 和 outbox 检查点；事务提交后写入数据库身份标记 `control.postgres.json` 并移除已迁移的旧 JSON，不再全量重写状态文件。标记存在而数据库身份缺失时拒绝启动。冻结的原文暂存文件和凭据仍为 0600；原文在成功交接后从 outbox 清除，核心负责长期记忆保存。

Codex、Claude Code 和 Gemini CLI 导入复用隔离读取进程；当前压缩日志最多缓存 64 MiB 解码内容，文件标识、大小或修改时间变化即失效。读取进程有内存和逐请求超时限制，最多复用 256 次；持久游标仍由父进程 outbox 管理。该优化不扩大所选来源范围。
页面任务状态默认每 150 秒刷新，也支持用户主动刷新。

## 验证

```bash
.venv/bin/python -m pytest -q
npm test --prefix browser-extension
npm ci --prefix tests/ui
npm test --prefix tests/ui
```

Python 用合成数据覆盖读取器、来源选择、文件 / 浏览器交付、断点、核心全流程、媒体、权限和发布包。
浏览器扩展使用合成协议 / IndexedDB；网页交互覆盖独立 fixture API，并用真实采集服务和合成模型
验证“网页新增来源 → 选择文件 → 原文提交 → 记忆生成 → 导入完成”；还覆盖实际 Chromium 扩展
向本地采集服务配对和交付合成数据。来源兼容性与模型效果需要结合目标环境验证。
数据库测试需独立 `MEMU_TEST_POSTGRES_DSN`。
