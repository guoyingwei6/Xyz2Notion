# 成本与免费策略

Xyz2Notion 本身免费、MIT 开源，不收激活费，也不经过项目作者的服务器。

## 默认费用边界

- GitHub Actions：使用用户自己的 GitHub 账户额度；
- Notion API：使用用户自己的 Integration；
- 小宇宙：只读取用户自己的订阅、历史和进度；
- 听悟 Cookie：优先消耗用户网页账户已有额度；
- SiliconFlow：使用同一个用户 Key 调用免费 ASR 和免费摘要模型；
- 本地 Whisper：使用 GitHub Actions CPU，只在前两条 ASR 通道失败时运行；
- 本地 Qwen3-1.7B：使用 GitHub Actions CPU，只在 SiliconFlow 摘要失败时运行；
- 付费 Provider：不实现，配置和客户端只接受已核对的免费模型白名单。

“免费模型”或“免费额度”由服务商决定，可能随时调整。Xyz2Notion 不承诺永久免费，
也不会绕过配额、风控或付费规则。

## 防止重复费用

听悟解析 ID、ASR Task ID、文字稿和摘要在每个外部 AI 边界后先保存到 Notion。
工作流取消、超时或网络失败后从检查点继续。听悟任务仍在排队或处理时不会同时调用
SiliconFlow；只有认证失效、风控、网页 Schema 变化等明确终态才降级。
SiliconFlow ASR 最终失败后才运行本地 Whisper；成功检查点会阻止重复转写。
SiliconFlow 摘要失败后才运行本地 Qwen3。两个本地模型及其运行时使用 GitHub
Actions 缓存，缓存命中时不重复下载；缓存被回收或校验失败时才重新获取。

免费摘要保存输入 Token、输出 Token、实际模型和 Prompt 版本，估算费用记录为 0。

## 建议

1. 初次把 `episodes_per_run` 设为 1；
2. 在 SiliconFlow 控制台确认当前候选模型仍标为免费；
3. 先用一个短单集验证，再逐步增加；
4. 不希望产生任何摘要 API 调用时，把 `summary.enabled` 设为 `false`；
5. 不希望产生任何新 ASR 调用时，把 `asr.provider_order` 设为空数组。
