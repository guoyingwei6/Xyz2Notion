# AI 摘要、章节和思维导图

## 两条内容来源

Xyz2Notion 最终统一生成 `SummaryResult`，但避免不必要的模型调用：

1. 听悟结果同时具有全文摘要和思维导图时，直接把原生摘要、章节、问答和脑图
   归一化，不再调用文本模型；
2. SiliconFlow 只有文字稿，或听悟结果不完整时，调用用户自己的
   `SILICONFLOW_API_KEY`，由免费文本模型生成统一结构。

摘要失败只改变 AI 增强状态，不会重新下载音频或再次调用 ASR。

Notion 页面发布发生临时错误时，处理器会把失败安全记录为可重试状态，恢复点保持在
`ENRICHED`。人工重试只会继续写入音频、思维导图、摘要、章节和文字稿，不会重新调用
ASR 或摘要模型；发布器仍采用先完成新托管内容、再归档旧托管内容的顺序。

发布完成后，同一份脑图还会按 Episode EID 幂等写入独立的“思维导图”数据库，
并建立 `Episode` 关系。旧版本已发布但缺少独立脑图记录时，可运行 Notion-only
工作流的 `reconcile-published-ai`；它每次最多读取两期已有 Notion 状态文件，
只核对文字稿、摘要和页面托管块并补写脑图行，不调用 ASR、摘要模型或小宇宙。

## 默认免费模型

默认按顺序尝试：

1. `Qwen/Qwen3-8B`；
2. `Qwen/Qwen2.5-7B-Instruct`。

两个模型当前在 SiliconFlow 价格页标为免费。配置和客户端仅接受这两个模型 ID，
不会因用户误填而调用其他模型。免费政策属于服务商外部状态，未来可能变化：

- <https://siliconflow.cn/pricing>
- <https://docs.siliconflow.cn/cn/userguide/rate-limits/rate-limit-and-upgradation>

免费模型限流、暂时不可用、下线，或“原始生成 + 一次 JSON 修复”后仍不符合
Schema 时，按候选顺序切换到下一个免费模型。全部候选均失败后才保存失败状态，
不会切换到付费模型。每个 `SummaryResult` 仍记录最终成功的实际模型、Prompt
版本和所有尝试累计的输入/输出 Token，估算费用固定为 0。

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

并设置 `response_format={"type":"json_object"}`。每个免费模型的首次结果若有
JSON 语法、字段类型、缺失字段、章节越界或脑图 ID 重复，只允许额外执行一次
JSON 修复；修复 Prompt 不会重新调用 ASR，也不访问音频。当前模型修复后仍不
合格时切换下一个免费模型；所有免费模型都失败后才保存 `schema_changed`。
`enable_thinking=false` 仅对官方明确支持该参数的 Qwen3 模型发送；切换到
Qwen2.5 时会省略该字段，避免 fallback 请求因模型不支持的参数被 HTTP 400 拒绝。
