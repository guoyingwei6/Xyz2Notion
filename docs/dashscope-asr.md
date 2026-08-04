# 阿里云百炼 Paraformer ASR

Xyz2Notion 默认优先使用阿里云百炼录音文件识别模型 `paraformer-v1`，并在提交阶段按
`paraformer-v1 → paraformer-v2 → paraformer-mtl-v1` 依次 fallback。它通过
DashScope REST API 提交 Notion 已保存的公开音频 URL，等待异步任务完成后读取
转写 JSON，并保存为统一的文字稿检查点。

## 配置

在 GitHub Repository secrets 添加：

```text
DASHSCOPE_API_KEY
```

这里使用中国内地百炼的通用 API Key（不是 Token Plan/Coding Plan 专属 Key）。
项目不需要把 Key、URL 或模型写进 Secret：URL 和模型顺序由代码固定为国内通用端点与
三个 Paraformer 模型。

`config.example.yaml` 默认 ASR 顺序为：

```yaml
asr:
  provider_order:
    - dashscope
    - siliconflow
    - local_whisper
  dashscope_model: paraformer-v1
  dashscope_fallback_models:
    - paraformer-v2
    - paraformer-mtl-v1
```

项目只允许上述三个模型，避免误配付费或未知模型。Key 只会发送到
`dashscope.aliyuncs.com`。这些是代码允许的安全模型集合，不代表每个模型都一定有
免费额度；实际剩余额度以你的百炼控制台为准。启用百炼控制台的“仅使用免费额度”选项，
可避免额度耗尽后产生付费调用。

实际请求端点为：

- 提交：`POST https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription`
- 查询：`GET https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}`

阿里云目前还提供带 Workspace ID 的北京专属域名；官方说明现有
`dashscope.aliyuncs.com` 仍可正常使用，因此本项目不要求额外配置 Workspace ID。

## 降级规则

单集 ASR 顺序固定为：

1. 百炼 `paraformer-v1`；额度耗尽或模型不可用时依次尝试 `paraformer-v2`、`paraformer-mtl-v1`；
2. SiliconFlow 免费 ASR；
3. GitHub Actions 本地 `faster-whisper small`。

百炼模型 fallback 只针对提交阶段明确的额度耗尽或模型不可用错误；一旦某个模型已经
返回 task ID，后续轮询/解析失败不会再创建第二个百炼任务，而是进入已有的重试/外层
SiliconFlow 降级链路。三个百炼模型都不可用时，才继续尝试 SiliconFlow；SiliconFlow
仍不可用时才下载音频并运行本地 Whisper。

## 时间轴精度

如果百炼结果包含句子级时间戳，Notion 中 `ASR Quality` 会标为
`exact_timestamps`；若服务端只返回全文文本，则保存全文并把时间轴精度标为
`unknown`。无论哪种情况，摘要、章节和思维导图都继续使用已保存文字稿生成，
不会重复转写。
