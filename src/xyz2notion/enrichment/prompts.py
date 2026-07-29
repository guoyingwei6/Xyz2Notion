"""Versioned prompts for podcast transcript enrichment."""

from __future__ import annotations

PROMPT_VERSION = "summary-v1"

SYSTEM_PROMPT = """你是严谨的中文播客编辑。只能依据输入文字稿整理内容，不得补充
文字稿中不存在的事实。必须输出一个 JSON 对象，不要输出 Markdown 代码围栏或解释。
章节时间使用输入中的毫秒时间，金句必须是原文摘录，人物和术语去重。"""

FULL_PROMPT = """请把下面的播客文字稿整理成指定 JSON Schema：

要求：
1. summary 是完整但简洁的全文摘要；
2. chapters 按时间升序，start_ms 不得超过音频总时长；
3. highlights 是关键观点，quotes 是原文金句；
4. terms 是需要解释的术语，people 是明确出现的人物；
5. questions 是节目回答的重要问题；
6. mindmap 是以节目主题为根节点的树，每个 node_id 在树内唯一；
7. 所有字段都必须出现，缺少内容时使用空数组；
8. 只输出 JSON。

音频总时长：{duration_ms} 毫秒
JSON Schema：
{schema}

文字稿：
{transcript}
"""

CHUNK_PROMPT = """这是长播客的第 {index}/{count} 段。先把本段整理为中间 JSON，
保留原始毫秒时间，不要生成段外事实。只输出 JSON。

JSON Schema：
{schema}

本段范围：{start_ms}-{end_ms} 毫秒
文字稿：
{transcript}
"""

SYNTHESIS_PROMPT = """把下面多个分段摘要合并成一份完整播客结果。去重但不要丢失
重要观点、原文金句、人物、术语和问题；章节按 start_ms 排序且不得超过
{duration_ms} 毫秒。只输出符合 Schema 的 JSON。

JSON Schema：
{schema}

分段摘要：
{digests}
"""

REPAIR_PROMPT = """下面是一次不符合 JSON Schema 的模型输出。只修复 JSON 的语法、
字段类型和缺失字段，不改写内容，不补充新事实。只输出修复后的 JSON。

JSON Schema：
{schema}

待修复输出：
{invalid}
"""
