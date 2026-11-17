# 浏览器正文与时间

完整会话的 Source 使用结构化消息生成可读 Markdown：角色、原生消息 ID、消息时间、原文。
原始响应仍保留在 Source 的 `metadata.memulife.transport_audit` 中。协议帧、重复富文本树和
计数不会混入模型正文；不是把 AI 的回复当成来源摘要。所有素材继续走公共记忆写入流程。

Gemini 响应的 `turn[0]` 是 `[conversation_id, turn_id]`，按两个字段分别解析会话和轮次身份。
用户正文位于 `turn[2][0][0]`，候选回复正文位于 `turn[3][0][i][1][0]`，不递归搜集字符串。
历史接口按新到旧返回，分页合并后恢复轮次顺序；同一轮次内用户在前，回复候选保留各自 ID。

时间策略：
- Gemini 的 `turn[4]` 是 `[seconds, nanoseconds]` 轮次时间，仅标为平台轮次时间。
- `turn[3][2]` 中按 `[3]` 候选 ID 匹配、读取 `[4]` 的时间对，标为候选回复记录时间。
- 时间对必须是有限整数，纳秒须在 `[0, 1e9)`。候选不匹配或结构缺失时保留 null。
- 预览和消息摘要使用规范 UTC 毫秒格式；更高精度原值留在原始响应审计数据中。
- 其余平台继续从消息自己的 create_time / created_at / inserted_at / createTime 等原生字段读取。
- 不拿目录更新时间、导出时间或当前时间填补消息时间。部分消息无日期的整段会话仍归档为未知时间。

协议结构参考：

- [OmniExporter Gemini adapter](https://github.com/joganubaid/omniexporter/blob/e31fcdeae49f7368c6596db27cbde8384c218d2e/src/adapters/gemini-adapter.js)：说明用户/候选正文位置与轮次时间对。该实现部分兜底使用当前时间，这里不采用。
- [hstry Gemini provider](https://github.com/byteowlz/hstry/blob/b8719cf630ec15a8ef6b9fa6f1a629be9aa53fbf/extension/providers/gemini.js)：分页后恢复轮次顺序。它将目录更新时间用于消息，本实现不采用该时间填充方式。
- [Gemini-API chat_mixin](https://github.com/HanaokaYuzu/Gemini-API/blob/f14269ab3271f7d481191610195bd8c9ffee02e4/src/gemini_webapi/components/chat_mixin.py)：原生轮次 ID 与候选结构。
- [gemini-history-exporter](https://github.com/peter-tiktinsky/gemini-history-exporter/blob/ca77d20d5e0576639af1420cc90b0e417f5ed80f/gemini_export.py)：全响应时间极值只适合近似会话活动范围，不能当逐条消息发送时间。

已生成记忆的更正通过统一修订流程提交，保持原始证据和稳定定位符。

## 原文与处理批次

完整 Source 及原始响应审计先持久化，再进入 digest / Unit 流程。切分只生成处理材料，
不把切分后的换行或摘要写回原文。`MEMU_TASK_BATCH_CHARS` 默认为 `32000`
字符；计算包含材料 JSON 转义、定位符及消息身份信息，不代表模型上下文 token 数。

浏览器来源在采集时记录消息起始行、原生 ID、角色与时间。分批按消息顺序贪心装入，
放不下下一条完整消息时结束当前批次。单条消息超过预算时，依次优先段落、换行、句末、
空白；没有适合边界才按字符切开。所有片段按顺序拼接能够完整恢复进入处理流程的原文，
不截掉尾部、不省略超长消息。每个片段携带所属消息信息。

`ref` 保持原有 Source 行定位；`source_slice` 保存它相对于指定原始行段的字符偏移
（Unicode 字符，0 起始，start 包含、end 不包含）。超长单行的多个片段可以共享行定位，
但使用不同的偏移与正文摘要作为持久处理标识，避免断点恢复跳过同一行的后续片段。
已有 Gemini 原文可从审计响应重建消息边界，只有重建结果匹配当前 Source 才使用该边界。

处理长十六进制、Base64 或 checkpoint 编码时，Source 仍保留完整原文。digest 与 Unit
记录来源提供、询问或明确解析该数据的行为、可见类型及上下文，通过 facts 回到原文；
不把长编码串逐字抄入摘要，也不自行解码或猜测其中内容。输入材料的 `body` 与输出条目
的 `body` 在提示词中明确区分。普通自然语言的事实、限定和关键原话继续保留。

对 Source 分片中连续至少 1024 个十六进制字符的编码串，模型输入视图以标记替换，
并附 `opaque_data`：类型、字符数、UTF-8 原文 SHA-256、原始行定位与字符起止偏移。
仅修改传给模型的副本，Source、原始响应、分片计划与断点标识均保持完整；周围自然语言
和短编码示例不替换。模型不能据此猜测编码内部内容。分片 digest 的输出引用由 schema
限定为该分片的 `item.ref`，避免混用偏移基准的整段定位符或生成网页式 `#L` 后缀。
