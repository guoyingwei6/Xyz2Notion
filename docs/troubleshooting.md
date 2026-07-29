# 故障排查与已知限制

## 先看哪里

Actions 摘要只显示聚合数量。具体单集状态在 Notion Episode 的 `ASR Status`、
`ASR Provider`、`Failure Reason` 和 `AI State File`。不要为了排错打印 Secret、
完整请求头或服务商响应正文。

## 常见问题

### `Process completed with exit code 2`

配置文件或必需凭证缺失。检查变量名大小写，尤其是：

- `XIAOYUZHOU_REFRESH_TOKEN`
- `NOTION_TOKEN`
- `NOTION_PAGE_ID`

### 小宇宙认证失败

必须复制 `X-Jike-Refresh-Token`，不是 Access Token、Cookie 或文章中误写的
`X-Jike-Access-Token`。重新登录后覆盖 GitHub Secret。Device ID 可不填；若频繁
切换设备环境，可固定 `XIAOYUZHOU_DEVICE_ID` Repository Variable。

### Notion 404/403

确认目标根页面和旧模板数据库都已授权给当前 Integration。更换 Token 后要重新
授权页面。页面 ID 可带或不带连字符，但不能填整个 OAuth 回调结果。

### 听悟一直排队

听悟是异步流程，通常需要多次 `Process Episode AI`。排队和处理中不会降级，以免
双重转写。若 Cookie 已过期或接口变化，会进入明确失败并在配置了 SiliconFlow 时
降级。

### SiliconFlow 失败

- 404：自动尝试下一个配置模型；
- 429/5xx：有限重试后保存为可重试失败；
- 音频过大：先由 FFmpeg 切片；
- 时间轴：免费接口只返回文本时，切片时间是粗粒度，不是逐句精确时间。

### 已有文字稿但没有摘要

通常是未配置 `DASHSCOPE_API_KEY`、摘要被关闭，或千问额度不可用。文字稿已经保存在
Notion 检查点中；补 Key 后再次运行，不会重复 ASR。

### 只重试失败单集

运行 `Retry Failed Episode AI` 工作流。它只处理 `FAILED_RETRYABLE`，不会重跑
正常、已发布或最终失败的单集。

### 强制重做一个单集

```bash
uv run xyz2notion redo-episode --eid <EID>
```

这会清空该单集的托管 AI 状态，但保留当前页面内容，直到新内容成功生成。

## 已知限制

- 听悟使用网页 Cookie 和内部接口，稳定性低于正式 API；
- SiliconFlow 免费 ASR 的说话人和逐句时间精度可能较低；
- 方言、多人重叠说话、背景音乐和远场录音效果取决于 Provider；
- 小宇宙接口没有公开稳定契约，字段变化时需更新适配；
- GitHub Actions 单 Job 有运行时长限制，长节目可能跨多次运行完成；
- Notion API 有速率和单请求块数量限制，长文字稿会分批写入；
- 当前版本不实现任何付费 ASR Provider；
- 真实视觉截图、真实六类音频质量和定时任务验收需要用户自己的账户凭证。
