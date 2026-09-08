# 阿里云百炼免费额度转写

Xyz2Notion 支持七个录音文件转写模型，默认候选顺序为
`paraformer-v2 → fun-asr → fun-asr-mtl → qwen-audio-3.0-asr-flash-filetrans →
qwen3-asr-flash-filetrans → paraformer-v1 → paraformer-mtl-v1`。它通过
DashScope REST API 提交 Notion 已保存的公开音频 URL，等待异步任务完成后读取
转写 JSON，并保存为统一的文字稿检查点。

## 配置

在 GitHub Repository secrets 添加：

```text
DASHSCOPE_API_KEY
```

这里使用中国内地百炼的通用 API Key（不是 Token Plan/Coding Plan 专属 Key）。
项目不需要把 Key、URL 或模型写进 Secret：URL 和模型顺序由代码固定为国内通用端点与
上述录音文件模型。Sambert 是语音合成模型，不属于转写候选。

`config.example.yaml` 默认 ASR 顺序为：

```yaml
asr:
  provider_order:
    - dashscope
  dashscope_model: paraformer-v2
  dashscope_fallback_models:
    - fun-asr
    - fun-asr-mtl
    - qwen-audio-3.0-asr-flash-filetrans
    - qwen3-asr-flash-filetrans
    - paraformer-v1
    - paraformer-mtl-v1
  dashscope_free_tier_confirmed_models: []
```

**默认确认列表为空，因此不提交新的百炼转写任务。** 在百炼控制台逐个开启
“免费额度用完即停”后，只把已经确认开启的模型名加入
`dashscope_free_tier_confirmed_models`。这是账户所有者的显式确认，不是代码对控制台
开关或剩余额度的实时验证；本地配置不能代替服务端计费保护，也不能防止开关后来被关闭。
没有该保护开关的模型不要加入确认列表。默认不启用 SiliconFlow ASR 或本地 Whisper
作为额度耗尽后的替代路径。

模型白名单仅限制接口兼容范围，**不代表永久免费**。实际额度、共享关系和有效期以
账户控制台为准。Key 只会发送到 `dashscope.aliyuncs.com`。

实际请求端点为：

- 提交：`POST https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription`
- 查询：`GET https://dashscope.aliyuncs.com/api/v1/tasks/{task_id}`

阿里云目前还提供带 Workspace ID 的北京专属域名；官方说明现有
`dashscope.aliyuncs.com` 仍可正常使用，因此本项目不要求额外配置 Workspace ID。

## 降级规则

单集 ASR 行为：

1. 跳过未确认免费保护的候选模型，不向这些模型发送请求；
2. 提交时明确返回额度耗尽或模型不可用错误，才尝试下一个已确认的模型；
3. 免费额度全部耗尽时返回 `free_quota_paused`，不消耗失败重试次数，也不调用其他 ASR
   服务；额度恢复后可再次由正常队列尝试；
4. 敏感内容失败直接停止，不跨模型或服务商重试。

百炼模型 fallback 只针对提交阶段明确的额度耗尽或模型不可用错误；一旦某个模型已经
返回 task ID，后续轮询/解析失败不会再创建第二个百炼任务。提交结果不明确时保留
不确定状态，禁止自动重投。确认列表后来清空时，已有任务仍可轮询并读取结果，
不会重复提交或丢弃已保存的文字稿。

Fun-ASR 和 Qwen-Audio 文件模型使用 `input.file_urls`、`output.results`；
Qwen3 文件模型使用 `input.file_url`、`output.result`，客户端分别适配。
官方接口参考：

- <https://help.aliyun.com/zh/model-studio/fun-asr-recorded-speech-recognition-http-api>
- <https://help.aliyun.com/zh/model-studio/qwen-asr-api-reference>

## 时间轴精度

如果百炼结果包含句子级时间戳，Notion 中 `ASR Quality` 会标为
`exact_timestamps`；若服务端只返回全文文本，则保存全文并把时间轴精度标为
`unknown`。无论哪种情况，摘要、章节和思维导图都继续使用已保存文字稿生成，
不会重复转写。
