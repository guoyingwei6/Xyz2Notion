# AI 摘要、章节和思维导图

## 两条内容来源

Xyz2Notion 最终统一生成 `SummaryResult`，但避免不必要的模型调用：

1. 听悟结果同时具有全文摘要和思维导图时，直接把原生摘要、章节、问答和脑图
   归一化，额外 Token 和估算成本均为 0；
2. SiliconFlow 只有文字稿，或听悟结果不完整时，调用用户自己的
   `DASHSCOPE_API_KEY`，由千问生成统一结构。

摘要失败只改变 AI 增强状态，不会重新下载音频或再次调用 ASR。

## 默认模型和费用边界

默认模型为 `qwen-flash`。阿里云官方当前说明该模型支持 100 万上下文和结构化
输出：

- <https://help.aliyun.com/zh/model-studio/qwen-flash>
- <https://help.aliyun.com/en/model-studio/qwen-structured-output>

`config.example.yaml` 中的价格是 2026-07-29 北京地域、输入不超过 128K Token
时的公开价格快照：输入 0.15 元/百万 Token、输出 1.5 元/百万 Token。价格字段
可配置；每个 `SummaryResult` 记录模型、Prompt 版本、输入/输出 Token 和估算成本。

百炼免费额度不是永久承诺。官方当前规则是北京地域新用户通常获得有效期 90 天的
模型独立额度；认证账户若未开启“免费额度用完即停”，额度耗尽后可能转为按量
计费：

- <https://help.aliyun.com/zh/model-studio/new-free-quota/>
- <https://help.aliyun.com/zh/model-studio/model-usage-statistics>

建议用户在百炼控制台开启“免费额度用完即停”。接口返回
`AllocationQuota.FreeTierOnly` 时，Xyz2Notion 将其标为 `quota_exhausted`，不会
继续重试制造费用。

## 长文字稿

文字稿处理过程：

1. 删除独立的音乐、掌声、片头等事件标签，保留普通正文；
2. 保留每句原始开始时间和说话人；
3. 按配置的 Token 上限和时间窗口切分，超长单句也做有界切分；
4. 每段先生成同一 Schema 的中间结果；
5. 再用一次结构化调用去重合并；
6. 校验章节顺序、音频总时长和脑图节点 ID 唯一性。

Token 数在调用前使用保守的中英混合估算，实际计费记录以 API 返回的 `usage`
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

调用使用官方兼容端点：

`https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions`

并设置 `response_format={"type":"json_object"}`。首次结果若有 JSON 语法、
字段类型、缺失字段、章节越界或脑图 ID 重复，只允许额外执行一次 JSON 修复；
修复 Prompt 不会重新调用 ASR，也不访问音频。第二次仍不合格则保存
`schema_changed` 失败，等待人工或后续重试。
