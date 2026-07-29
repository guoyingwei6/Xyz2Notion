# GitHub Actions 本地 Whisper 兜底

Xyz2Notion 的固定 ASR 顺序是：

1. 通义听悟 Cookie；
2. SiliconFlow 免费 ASR API；
3. GitHub Actions CPU 上的本地 `faster-whisper small`。

本地模型是最终兜底，不会抢在听悟或 SiliconFlow 前运行。它不需要新的 API Key；
只有前两条通道均不可用时，才从公开模型仓库下载模型并在当前临时 Runner 中推理。
Runner 结束后，下载的音频、模型运行目录和文字稿临时文件都会随虚拟机销毁。

## 为什么选择 small

`tiny` 和 `base` 下载更小、速度更快，但中文长播客更容易出现漏字和同音错误。
`small` 在 GitHub 标准 CPU Runner 上仍可运行，同时比前两者更适合作为中文兜底。
允许的模型名称限制在 `tiny`、`base`、`small`，避免误配大型模型耗尽运行时间。

```yaml
asr:
  provider_order:
    - tingwu_cookie
    - siliconflow
    - local_whisper
  local_whisper_model: small
```

## 边界

- 本地 Whisper 只负责音频转文字，不生成摘要、章节或思维导图。
- 摘要仍调用用户自己的 SiliconFlow 免费文本模型。
- 如果 SiliconFlow 整个账户或网络不可用，本地文字稿会先写入 Notion 检查点；
  摘要阶段等待后续重试，不会重新转写。
- 本地模型使用 CPU，长节目耗时可能明显高于 API，因此 AI 工作流上限为 180 分钟，
  且验收阶段每次最多推进 2 期。
- 本地 Provider 的错误只保存分类和异常类型，不记录音频 URL、标题或原始响应。
