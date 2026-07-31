# 通义听悟 Cookie Provider

## 定位

`tingwu_cookie` 用于优先消耗用户自己在通义听悟网页端已有的转写额度。它不经过
Xyz2Notion 作者服务器，也不把 Cookie 保存到 Notion、仓库、缓存或 Artifact。

听悟网页接口不是公开、稳定的 API，因此本 Provider 被视为“高收益但可失效”的
主通道；稳定降级通道是用户自己的 SiliconFlow API Key。

## 凭证和网络边界

GitHub Secret 名为 `TINGWU_COOKIE`。运行时只允许把该 Cookie 发送到：

- `tingwu.aliyun.com`

客户端使用当前网页的同源 `/api` 接口，并在附加请求头前再次校验 HTTPS 和精确
主机名。旧的 `qianwen.biz.aliyun.com` 与 `tw-efficiency.biz.aliyun.com`
已从允许列表移除。Cookie 不会发往小宇宙、Notion、SiliconFlow、
`malinkang.com`、`notionhub.app` 或任意重定向目标。异常只保留安全错误类别和
HTTP 状态码，不记录响应正文或请求头。

Cookie 的网页获取方式可能随通义页面变化。打开听悟“我的记录”，在浏览器开发者
工具的 Network → Fetch/XHR 中搜索 `getDirList` 或 `directory`，从
`https://tingwu.aliyun.com/api/directory/request?getDirList&c=web` 请求标头复制完整
`Cookie` 值。不要包含 `Cookie:` 前缀，也不要把值粘贴到 Issue、Actions 输入、
配置文件、日志或聊天，只放进仓库的 GitHub Actions Secret。

## 可恢复任务

听悟是异步工作流：

1. 查找或创建播客文件夹；
2. 按文件夹和标题查找已有任务；
3. 解析公网音频 URL；
4. 保存解析任务 ID；
5. 解析完成后提交转写并保存 Record/Task ID；
6. 后续 Action 按标题和 Task ID 查询状态；
7. 成功后读取逐句文字、说话人、摘要、章节、问答、思维导图和听悟笔记。

如果音频 URL 仍在解析，客户端返回 `source_parsing`，下一次 Action 必须携带已保存
的 `source_task_id` 继续查询。已找到的 Record 直接复用，不能再次提交。

## Fallback 规则

以下明确终态失败会打开当前客户端实例的熔断器，并允许切换 SiliconFlow：

- `authentication`：HTTP 401/403 或明确的登录/过期提示；
- `risk_control`：HTTP 412/418/451 或明确的风控/验证提示；
- `schema_changed`：网页端点 404、非 JSON 或关键字段缺失；
- 其他明确且不可恢复的网页 Provider 失败。

以下情况不立即降级：

- `source_parsing`、`submitted`、`processing`：任务仍在运行；
- HTTP 429、5xx、网络超时：先按上限重试并保存可恢复失败；
- 音频 URL 本身无法解析：标为 `invalid_input`，避免换 Provider 重复下载无效资源。

这样可以避免同一单集在听悟和 SiliconFlow 同时转写。

## 精度

听悟结果包含逐句开始时间和说话人。Xyz2Notion 将这些开始时间保留为毫秒时间轴，
后一条语句的开始时间作为前一条的区间结束时间，并把 `ASR Quality` 标为
`exact_timestamps`。最后一条只有精确开始时间时，其结束时间与开始时间相同。

网页接口变化时，元数据与统计同步仍可独立运行；只有 AI 处理分支会标记
`schema_changed` 并按 Provider 策略处理。
