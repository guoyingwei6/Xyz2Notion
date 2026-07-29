# 成本与免费策略

Xyz2Notion 本身免费、MIT 开源，不收激活费，也不经过项目作者的服务器。

## 默认费用边界

- GitHub Actions：使用用户自己的 GitHub 账户额度；
- Notion API：使用用户自己的 Integration；
- 小宇宙：只读取用户自己的订阅、历史和进度；
- 听悟 Cookie：优先消耗用户网页账户已有额度；
- SiliconFlow：调用用户账户可用的免费 ASR 模型；
- DashScope：只有缺少听悟原生摘要时才调用用户自己的 Key；
- 付费 ASR：默认关闭，预算为 0，当前版本不实现付费 Provider。

“免费模型”或“免费额度”由服务商决定，可能随时调整。Xyz2Notion 不承诺永久免费，
也不会绕过配额、风控或付费规则。

## 防止重复费用

听悟解析 ID、ASR Task ID、文字稿和摘要在每个可计费边界后先保存到 Notion。
工作流取消、超时或网络失败后从检查点继续。听悟任务仍在排队或处理时不会同时调用
SiliconFlow；只有认证失效、风控、网页 Schema 变化等明确终态才降级。

千问摘要保存输入 Token、输出 Token、模型、Prompt 版本和按配置单价计算的估算费用。
估算不是账单，应以服务商控制台为准。

## 建议

1. 初次把 `episodes_per_run` 设为 1；
2. 在 SiliconFlow 和 DashScope 控制台设置额度告警；
3. 先用一个短单集验证，再逐步增加；
4. 不希望产生任何摘要 API 调用时，把 `summary.enabled` 设为 `false`；
5. 不希望产生任何新 ASR 调用时，把 `asr.provider_order` 设为空数组。
