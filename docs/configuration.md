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
| `provider_order` | `tingwu_cookie`, `siliconflow` | ASR 优先级；空数组表示暂停新 ASR |
| `siliconflow_models` | SenseVoiceSmall、TeleSpeechASR | 免费白名单模型 404 时依次尝试 |

只禁用 Cookie：

```yaml
asr:
  provider_order:
    - siliconflow
```

暂停所有新 ASR：

```yaml
asr:
  provider_order: []
```

## `summary`

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `enabled` | `true` | 是否在没有听悟原生结果时调用免费摘要 |
| `siliconflow_models` | Qwen3-8B、Qwen2.5-7B-Instruct | 免费白名单摘要模型回退顺序 |
| `prompt_version` | `summary-v1` | 版本化 Prompt |
| `chunk_tokens` | `24000` | 单块最大估算 Token |
| `chunk_minutes` | `30` | 单块最大时长 |
| `max_output_tokens` | `8192` | 单次最大输出 |

配置验证仅接受当前版本核对过的两个免费 ASR 模型和两个免费摘要模型。免费模型
全部限流或不可用时保存可重试失败，不自动切换付费模型。

关闭摘要后，没有听悟原生摘要的单集会停在已转写状态，不会丢失文字稿，也不会重复
执行 ASR。

## `limits`

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `episodes_per_run` | `3` | 每次 AI 工作流最多推进的单集数 |
| `asr_minutes_per_day` | `240` | 每日计划处理分钟上限 |
| `asr_minutes_per_month` | `3000` | 每月计划处理分钟上限 |
| `provider_poll_attempts` | `60` | 异步任务轮询上限 |

`episodes_per_run` 已由运行编排强制执行。分钟字段只控制任务吞吐量，不涉及金额、
计费估算或付费模型。

## AI 处理门槛

`Process Episode AI` 只处理已收听至少 120 秒、存在音频链接、尚未发布文字稿且
未被标记为最终失败的单集。Notion 的 Episode 数据库包含 `Skip AI` 复选框；
勾选后该单集不会进入自动转写、摘要和思维导图队列。

已进入异步转写阶段的单集仍会在后续工作流中继续轮询，直至成功、可重试失败或
最终失败。已经发布的单集不会重复调用服务。

## `state_file`

保留用于本地状态机兼容。GitHub Actions 的单集私有检查点实际存放在用户自己的
Notion `AI State File` 属性中，不提交到仓库。

## 环境变量

所有变量及必需性见 [GitHub Actions 与 Secrets](github-actions.md)。本地可通过
进程环境传入；`.env` 已被 Git 忽略，但仍不建议长期明文保存。
