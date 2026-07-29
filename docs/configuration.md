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
| `paid_enabled` | `false` | 付费 ASR 总开关；当前版本不实现付费 Provider |
| `paid_budget_cny` | `0` | 未显式设置正预算时拒绝启用付费 ASR |
| `siliconflow_models` | SenseVoiceSmall、TeleSpeechASR | 免费模型 404 时依次尝试 |

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
| `enabled` | `true` | 是否在没有听悟原生结果时调用千问摘要 |
| `model` | `qwen-flash` | DashScope 模型 |
| `prompt_version` | `summary-v1` | 版本化 Prompt |
| `chunk_tokens` | `24000` | 单块最大估算 Token |
| `chunk_minutes` | `30` | 单块最大时长 |
| `max_output_tokens` | `8192` | 单次最大输出 |
| 两项单价 | 可配置快照 | 只用于在 Notion 中估算费用 |

关闭摘要后，没有听悟原生摘要的单集会停在已转写状态，不会丢失文字稿，也不会重复
执行 ASR。

## `limits`

| 字段 | 默认值 | 说明 |
| --- | --- | --- |
| `episodes_per_run` | `3` | 每次 AI 工作流最多推进的单集数 |
| `asr_minutes_per_day` | `240` | 日预算策略上限 |
| `asr_minutes_per_month` | `3000` | 月预算策略上限 |
| `provider_poll_attempts` | `60` | 异步任务轮询上限 |

`episodes_per_run` 已由运行编排强制执行。分钟预算字段是公开策略边界；当前免费
Provider 不返回统一可结算用量，因此不要把它们当作服务商硬额度，仍需在用户自己的
服务商控制台设置用量告警。

## `state_file`

保留用于本地状态机兼容。GitHub Actions 的单集私有检查点实际存放在用户自己的
Notion `AI State File` 属性中，不提交到仓库。

## 环境变量

所有变量及必需性见 [GitHub Actions 与 Secrets](github-actions.md)。本地可通过
进程环境传入；`.env` 已被 Git 忽略，但仍不建议长期明文保存。
