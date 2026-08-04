# 故障排查与已知限制

## 先看哪里

Actions 摘要只显示聚合数量。转写状态在 Notion Episode 的 `ASR Status`、`ASR Provider`，
摘要/章节/思维导图状态在 `增强状态`、`增强 Provider`；检查点仍在 `Failure Reason` 和
`AI State File`。不要为了排错打印 Secret、完整请求头或服务商响应正文。

## 常见问题

### `Process completed with exit code 2`

配置文件或必需凭证缺失。检查变量名大小写，尤其是：

- `XIAOYUZHOU_REFRESH_TOKEN`
- `NOTION_TOKEN`
- `NOTION_PAGE_ID`

### 小宇宙认证失败

打开 `https://xyzfm.link/login` 重新登录，必须复制
`X-Jike-Refresh-Token` 的值，不是 Access Token、整段 Cookie 或文章中误写的
`X-Jike-Access-Token`。重新登录后覆盖 GitHub Secret。Device ID 可不填；若频繁
切换设备环境，可固定 `XIAOYUZHOU_DEVICE_ID` Repository Variable。

如果账号出现封禁、风控、401、403 或 429，不要立即更新 Token 后反复重试。
先在 GitHub Actions 禁用 `Sync Podcast Metadata` 并等待账号恢复。客户端会在
第一次上述响应时立即熔断；恢复后也只能手动运行一次安全增量同步进行验证。

### Notion 404/403

确认目标根页面和旧模板数据库都已授权给当前 Integration。更换 Token 后要重新
授权页面。页面 ID 可带或不带连字符，但不能填整个 OAuth 回调结果。

### 百炼 ASR 没有完成

百炼录音文件识别是异步流程，队列会保存任务 ID 并在后续运行继续轮询。只有任务进入
明确失败状态才会降级到 SiliconFlow；SiliconFlow 失败后才使用本地 Whisper，避免
同一音频重复提交。检查 Notion Episode 的 `ASR Status`、`ASR Provider` 和
`Failure Reason`，不要重复手动提交同一单集。

### SiliconFlow 失败

- 404：自动尝试下一个配置模型；
- 429/5xx：有限重试后保存为可重试失败；
- 音频过大：先由 FFmpeg 切片；
- 时间轴：免费接口只返回文本时，切片时间是粗粒度，不是逐句精确时间。

SiliconFlow ASR 最终失败时会自动转到本地 `faster-whisper small`。首次使用需要
下载公开模型，日志暂时停留在下载或加载阶段不一定表示卡死。摘要仍依赖
SiliconFlow 文本接口；若整个 Key 或网络不可用，本地文字稿检查点会保留，接口恢复后
继续摘要，不会重复转写。

### 已有文字稿但没有摘要

通常是未配置 `SILICONFLOW_API_KEY`、摘要被关闭，或免费文本模型正在限流。文字稿
已经保存在 Notion 检查点中；补 Key 或稍后重试不会重复 ASR。

### 只重试失败单集

重新运行 `Transcribe Episode Queue` 或 `Enrich Transcribed Episodes`。它们只消费
对应阶段的 `FAILED_RETRYABLE` 状态，不会重跑正常、已发布或最终失败的单集。

### 强制重做一个单集

```bash
uv run xyz2notion redo-episode --eid <EID>
```

这会清空该单集的托管 AI 状态，但保留当前页面内容，直到新内容成功生成。

## 已知限制

- SiliconFlow 免费 ASR 的说话人和逐句时间精度可能较低；
- 方言、多人重叠说话、背景音乐和远场录音效果取决于 Provider；
- 小宇宙接口没有公开稳定契约，字段变化时需更新适配；
- GitHub Actions 单 Job 有运行时长限制，长节目可能跨多次运行完成；
- Notion API 有速率和单请求块数量限制，长文字稿会分批写入；
- 当前版本不实现任何付费 AI Provider；
- 真实视觉截图、真实六类音频质量和定时任务验收需要用户自己的账户凭证。
