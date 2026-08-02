# AI 摘要、章节和思维导图

## 内容来源

Xyz2Notion 只消费已经写入 Notion 检查点的文字稿，并统一生成 `SummaryResult`。
转写与增强是两条独立队列：ASR 完成后才进入摘要、章节和脑图阶段，增强失败不会
重新下载音频或再次调用 ASR。

在候选门槛不变的前提下，ASR 队列的优先级固定为：收藏 → 喜欢 → 听过 → 在听 → 待听。
同一单集同时有多个标记时按更高优先级处理。喜欢、待听本身不会绕过 120 秒播放门槛；
只有收藏可以在未播放时进入候选。

摘要失败只改变 AI 增强状态，不会重新下载音频或再次调用 ASR。

Notion 页面发布发生临时错误时，处理器会把失败安全记录为可重试状态，恢复点保持在
`ENRICHED`。人工重试只会继续写入音频、思维导图、摘要、章节和文字稿，不会重新调用
ASR 或摘要模型；发布器仍采用先完成新托管内容、再归档旧托管内容的顺序。

发布完成后，同一份脑图还会按 Episode EID 幂等写入独立的“思维导图”数据库，
并建立 `Episode` 关系。旧版本已发布但缺少独立脑图记录时，可运行 Notion-only
工作流的 `reconcile-published-ai`；它每次最多读取两期已有 Notion 状态文件，
只核对文字稿、摘要和页面托管块，补写脑图行以及转写/总结完成时间，不调用 ASR、
摘要模型或小宇宙。

主页底部的“转写与总结”模块提供两个独立视图：`转写文本`按转写完成时间倒序，
`AI总结与思维导图`按总结完成时间倒序。摘要、章节和思维导图是同一次增强流程的并列
输出，不会重复调用模型；打开视图中的 Episode 页面可查看全文转写和全部 AI 内容。

## 默认免费模型

摘要、章节和思维导图固定按以下顺序生成：

1. SiliconFlow 免费模型 `Qwen/Qwen3-8B`；
2. GitHub Actions 本地 `Qwen3-1.7B-Q4_K_M`。

远程客户端只接受 `Qwen/Qwen3-8B`，不会因误填而调用其他远程模型或付费模型。
免费政策属于服务商外部状态，未来可能变化：

- <https://siliconflow.cn/pricing>
- <https://docs.siliconflow.cn/cn/userguide/rate-limits/rate-limit-and-upgradation>

SiliconFlow 限流、暂时不可用、下线，或“原始生成 + 一次 JSON 修复”后仍不符合
Schema 时，才启动本地模型。本地模型和 `llama-cpp-python` 运行时均进入
GitHub Actions 缓存；有效缓存会直接复用，只有首次运行、缓存被 GitHub 回收或
完整性校验失败时才重新下载。本地模型文件固定版本、大小和 SHA256，校验通过后
才原子替换缓存文件。

两个通道都失败后才保存失败状态。每个 `SummaryResult` 仍记录最终成功的实际模型、
Prompt 版本和远程尝试累计的输入/输出 Token，估算费用固定为 0。

## 长文字稿

文字稿处理过程：

1. 删除独立的音乐、掌声、片头等事件标签，保留普通正文；
2. 保留每句原始开始时间和说话人；
3. 按配置的 Token 上限和时间窗口切分，超长单句也做有界切分；
4. 每段先生成同一 Schema 的中间结果；
5. 再用一次结构化调用去重合并；
6. 校验章节顺序、音频总时长和脑图节点 ID 唯一性。

Token 数在调用前使用保守的中英混合估算，实际用量记录以 API 返回的 `usage`
为准。

## 输出 Schema

`summary-v1` 固定输出：

- 全文摘要；
- 章节标题、毫秒开始时间和章节摘要；
- 关键观点；
- 原文金句；
- 术语；
- 人物；
- 问题回顾；
- 递归思维导图。

Prompt 设计记录见 [`prompts/summary-v1.md`](../prompts/summary-v1.md)。结构或语义
变化必须创建新版本。

## JSON 修复

调用使用 SiliconFlow 官方兼容端点：

`https://api.siliconflow.cn/v1/chat/completions`

并设置 `response_format={"type":"json_object"}`。远程模型的首次结果若有
JSON 语法、字段类型、缺失字段、章节越界或脑图 ID 重复，只允许额外执行一次
JSON 修复；修复 Prompt 不会重新调用 ASR，也不访问音频。修复后仍不合格时，
切换本地 Qwen3，再允许一次本地 JSON 修复；仍失败才保存 `schema_changed`。
远程 Qwen3 使用 `enable_thinking=true`；本地 Qwen3 使用约束 JSON 和
`/no_think`，优先保证结构化输出稳定。
