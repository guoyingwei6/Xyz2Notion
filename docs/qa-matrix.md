# QA 与故障注入矩阵

## 离线场景样本

`tests/fixtures/transcript_cases.json` 包含六类不含真实用户数据的合成文字稿：

| 场景 | 离线验证范围 |
| --- | --- |
| 普通话访谈 | 多段文字和说话人进入统一模型 |
| 中英混合 | Unicode 与英文保持 |
| 方言明显 | 下游不改写原始 ASR 文本 |
| 多人对话 | 三个 Speaker 标签保持 |
| 背景音乐/远场 | 低质量来源仍可进入下游 |
| 超过 1 小时 | 按时长切块且末尾时间完整 |

这些 Fixture 只证明下游契约、分块、摘要和 Notion 渲染可处理六类结构，不代表真实
ASR 准确率。真实效果必须用用户自己的音频和 Provider 凭证验收。

## 故障注入

| 故障 | 自动化证据 | 期望 |
| --- | --- | --- |
| 小宇宙 Token 错误/过期 | `test_xiaoyuzhou_client.py` | 401/403 立即熔断，不二次刷新，不泄漏响应 |
| 小宇宙限速/风控 | `test_xiaoyuzhou_client.py` | 默认 20 请求预算、3 秒间隔、429 立即熔断 |
| Notion Token 错误 | `test_notion_client.py` | 分类为安全 Notion 错误 |
| 听悟 Cookie 过期 | `test_tingwu.py`, `test_ai_processor.py` | 熔断并降级 SiliconFlow |
| 听悟字段改变 | `test_tingwu.py` | `schema_changed`，不解析错误数据 |
| SiliconFlow 429 | `test_siliconflow.py` | 按 Retry-After 有限重试 |
| SiliconFlow 模型 404 | `test_siliconflow.py` | 尝试下一个免费模型 |
| SiliconFlow 最终失败 | `test_ai_processor.py` | 降级到 GitHub Actions 本地 Whisper |
| 本地 Whisper 契约 | `test_local_whisper.py` | 输出带时间戳的文字稿并安全处理空结果 |
| 音频 URL 失效/私网 | `test_audio_processing.py` | 下载前拒绝或安全失败 |
| Action 中途取消 | `test_state.py`, `test_ai_processor.py` | 从精确阶段恢复，不重复 ASR |
| Notion 429/529 | `test_notion_client.py` | 限速或过载退避重试 |
| AI JSON 无效 | `test_siliconflow_summary.py` | 只修复一次，再失败为 Schema 错误 |

## 每次发布质量门

```bash
uv lock --check
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest --cov --cov-report=term-missing
```

要求：Python 3.12、Mypy strict、覆盖率至少 90%、Gitleaks 通过、所有 Actions 固定
到提交 SHA。真实凭证、音频、文字稿和摘要不得成为 Fixture 或 Artifact。
