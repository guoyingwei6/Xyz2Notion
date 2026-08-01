# 完整配置

`config.yaml` 只保存可公开的策略，不保存任何 Token、Cookie 或 API Key。可从
`config.example.yaml` 复制：

```bash
cp config.example.yaml config.yaml
uv run xyz2notion config-check --config config.yaml
```

## `asr`

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `provider_order` | `dashscope`, `siliconflow`, `local_whisper` | ASR 优先级；空数组表示暂停新 ASR |
| `dashscope_model` | `paraformer-v1` | 百炼录音文件识别模型；仅允许该免费模型 |
| `siliconflow_models` | SenseVoiceSmall、TeleSpeechASR | 免费白名单模型 404 时依次尝试 |
| `local_whisper_model` | `small` | 最终本地兜底；仅允许 `tiny`、`base`、`small` |

语音转文字默认优先使用百炼免费额度，不依赖网页 Cookie：

```yaml
asr:
  provider_order:
    - dashscope
    - siliconflow
    - local_whisper
```

暂停所有新 ASR：

```yaml
asr:
  provider_order: []
```

## `summary`

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 是否在已有文字稿后调用免费摘要 |
| `siliconflow_models` | Qwen3-8B | 唯一允许的远程免费摘要模型 |
| `local_qwen_fallback` | `true` | 远程失败后启用 GitHub Actions 本地 Qwen3-1.7B |
| `prompt_version` | `summary-v1` | 版本化 Prompt |
| `chunk_tokens` | `24000` | 单块最大估算 Token |
| `chunk_minutes` | `30` | 单块最大时长 |
| `max_output_tokens` | `8192` | 单次最大输出 |

配置验证仅接受当前版本核对过的两个免费 ASR 模型和一个免费摘要模型。远程摘要
失败后只降级到本地 Qwen3-1.7B，不自动切换其他远程模型或付费模型。本地模型与
运行时由 GitHub Actions 缓存，正常情况下不会每次下载。

关闭摘要后，单集会停在已转写状态，不会丢失文字稿，也不会重复执行 ASR。

## `limits`

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `episodes_per_run` | `4` | 每次 AI 工作流最多推进的单集数 |
| `asr_minutes_per_day` | `240` | 每日计划处理分钟上限 |
| `asr_minutes_per_month` | `3000` | 每月计划处理分钟上限 |
| `provider_poll_attempts` | `60` | 异步任务轮询上限 |

`episodes_per_run` 已由运行编排强制执行。分钟字段只控制任务吞吐量，不涉及金额、
计费估算或付费模型。

## AI 处理门槛

`Transcribe Episode Queue` 和 `Enrich Transcribed Episodes` 只处理已收听至少 120 秒或已收藏、存在音频链接、尚未发布
文字稿且未被标记为最终失败的单集。收藏会绕过 120 秒门槛；仅加入待听播放列表
不会自动转写。Notion 的 Episode 数据库包含 `Skip AI` 复选框，勾选后仍可排除
任何单集。

收听统计与 AI 门槛相互独立：待听或收藏但播放秒数为 0 的单集可以显示和转写，
但不会增加收听时长、天数、期数、节目数、排行或热力图。

已进入异步转写阶段的单集仍会在后续工作流中继续轮询，直至成功、可重试失败或
最终失败。已经发布的单集不会重复调用服务。

## `state_file`

保留用于本地状态机兼容。GitHub Actions 的单集私有检查点实际存放在用户自己的
Notion `AI State File` 属性中，不提交到仓库。

## 环境变量

所有变量及必需性见 [GitHub Actions 与 Secrets](github-actions.md)。本地可通过
进程环境传入；`.env` 已被 Git 忽略，但仍不建议长期明文保存。
