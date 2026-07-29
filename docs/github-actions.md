# GitHub Actions 与 Secrets

Xyz2Notion 的定时任务只运行在用户自己 Fork 的 GitHub 仓库中。所有工作流仅有
`contents: read` 权限，不上传 Artifact，也不把音频、标题、EID、文字稿、摘要或
凭证写入运行摘要。

## 一次性配置

进入 Fork 仓库的 `Settings → Secrets and variables → Actions`。

在 **Repository secrets** 添加：

| 名称 | 必需 | 用途 |
| --- | --- | --- |
| `XIAOYUZHOU_REFRESH_TOKEN` | 是 | 小宇宙换取短期 Access Token |
| `NOTION_TOKEN` | 是 | 用户自己的 Notion Integration Token |
| `NOTION_PAGE_ID` | 是 | 已授权给 Integration 的空白根页面 ID |
| `NOTION_MIGRATION_PAGE_ID` | 否 | 旧模板副本页面；仅维护工作流迁移操作使用 |
| `TINGWU_COOKIE` | 否 | 优先使用用户已有的听悟网页额度 |
| `SILICONFLOW_API_KEY` | 建议 | 免费 ASR 降级与免费结构化摘要 |

在 **Repository variables** 可选添加：

| 名称 | 用途 |
| --- | --- |
| `XIAOYUZHOU_DEVICE_ID` | 固定小宇宙设备 UUID；省略时按仓库身份稳定派生 |

不要把任何 Secret 写入 `config.yaml`、Issue、Actions 输入或运行日志。

### 小宇宙 Refresh Token

打开 `https://xyzfm.link/login`，通过手机号或小宇宙 App 扫码完成统一身份认证。
登录后优先在浏览器开发者工具的
`Application → Cookies → namecard.xiaoyuzhoufm.com` 中查找
`x-jike-refresh-token`；若当前版本仍把令牌附加到 API 请求，也可在
`Network → Fetch/XHR → Headers` 中复制 `X-Jike-Refresh-Token`。
这里要的是 **Refresh Token 的值**，不是 Access Token，也不是整段 Cookie。

### Notion

创建自己的 Notion Integration，将目标空白页面授权给它。Integration Secret
填入 `NOTION_TOKEN`，页面 URL 中的页面 ID 填入 `NOTION_PAGE_ID`。无需使用作者
OAuth 回调或作者生成的 Token。

### 通义听悟 Cookie

登录 `https://tingwu.aliyun.com/` 后，从浏览器开发者工具中复制该站点请求使用的
完整 `Cookie` 请求头值并填入 `TINGWU_COOKIE`。Cookie 会过期，而且网页内部接口
可能变化；它只被发送到允许的阿里域名。详细边界见
[听悟 Cookie Provider](tingwu-cookie.md)。

### SiliconFlow

`SILICONFLOW_API_KEY` 来自用户自己的 SiliconFlow 账户，用于调用配置中的免费
ASR 和免费文本模型。项目只接受代码已核对的免费模型白名单，不使用 DashScope，
也不实现付费 ASR。免费模型清单可能调整，升级项目版本前应重新核对价格页。

## 工作流

| 工作流 | 触发 | 作用 |
| --- | --- | --- |
| `Initialize Notion` | 手动 | 幂等创建或修复九个数据库和首页 |
| `Sync Podcast Metadata` | 每天 00:17 UTC、手动 | 同步订阅、进度、统计和热力图 |
| `Process Episode AI` | 每 6 小时的第 43 分、手动 | 推进 ASR、摘要和发布状态机 |
| `Retry Failed Episode AI` | 手动 | 只恢复 `FAILED_RETRYABLE` 单集 |
| `Xyz2Notion Maintenance` | 手动 | 迁移、单集重做、统计或热力图重建 |

定时任务刻意避开整点。所有会写入 Notion 的工作流共用
`xyz2notion-runtime` concurrency group，避免迁移、初始化、元数据和 AI 同时改页。

首次使用顺序：

1. 在 `Actions` 手动运行 `Initialize Notion`；
2. 手动运行 `Sync Podcast Metadata`；
3. 手动运行 `Process Episode AI`；
4. 查看工作流摘要中的聚合计数，以及 Notion Episode 的 `ASR Status`。

如果 Fork 中存在公开的 `config.yaml`，AI 工作流使用它；否则使用
`config.example.yaml`。配置文件只能包含模型顺序、运行上限等非秘密设置。

## 维护工作流

`Xyz2Notion Maintenance` 提供五种手动操作：

- `migrate-dry-run`：只读检查旧模板，默认选项；
- `migrate`：应用原地迁移，必须同时勾选 `confirm_changes`；
- `redo-episode`：必须在 `episode_eid` 填写精确 EID；
- `rebuild-statistics`：重算全部统计；
- `rebuild-heatmap`：重建当前年度热力图。

如果设置 `NOTION_MIGRATION_PAGE_ID`，两个迁移操作使用该旧模板副本；其他操作始终
使用主 `NOTION_PAGE_ID`。工作流不会把页面 ID、EID 或迁移键写入运行摘要。

## 中断与重试

听悟的解析 ID、Task ID、文字稿和摘要检查点作为 JSON 文件保存在用户自己的
Notion Episode 中。每个外部 AI 阶段完成后都会先保存检查点。因此
Actions 被取消或超时后，下次运行继续查询或发布，不重复提交已完成的 ASR。

临时网络、限流或服务不可用会进入 `FAILED_RETRYABLE`。修复凭证或等待服务恢复后，
手动运行 `Retry Failed Episode AI`。认证失效、风控或听悟网页 Schema 变化属于
明确终态时，若配置了 SiliconFlow，则自动降级；正在排队或处理中的听悟任务不会
提前双跑。

## 轮换

- 小宇宙 Refresh Token 或听悟 Cookie 失效：在 GitHub Secret 中直接覆盖旧值；
- Notion Token 轮换：先把同一根页面授权给新 Integration，再替换 Secret；
- API Key 轮换：先创建新 Key、覆盖 Secret、验证工作流，再撤销旧 Key；
- 任何 Secret 疑似泄漏：立即在原服务撤销，不要只删除 GitHub 日志。
