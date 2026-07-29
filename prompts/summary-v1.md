# summary-v1

`summary-v1` 是 Xyz2Notion 的首个结构化播客整理 Prompt 版本。运行时模板位于
`src/xyz2notion/enrichment/prompts.py`，本文件记录不可变的设计目标：

- 只依据文字稿，不补充外部事实；
- 输出严格 JSON，不输出 Markdown；
- 章节保留原始毫秒时间并按时间排序；
- 摘要、关键观点、原文金句、术语、人物和问题分字段保存；
- 思维导图为具有唯一 `node_id` 的递归树；
- 长文字稿先按时间和 Token 窗口分段，再合并结构化结果；
- 首次 JSON 不合格时只做一次 JSON 修复，不重新调用 ASR；
- Prompt 行为变化必须新建版本，不能原地改变 `summary-v1` 语义。
