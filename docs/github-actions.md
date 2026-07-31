# GitHub Actions 与 Secrets

Xyz2Notion 的任务只运行在用户自己 Fork 的 GitHub 仓库中。所有工作流仅有
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
| `ASR_QUEUE_ENABLED` | 设为 `true` 后允许“只转写”定时队列运行 |
| `ASR_BACKFILL_ACTIVE` | 首次存量排空期间设为 `true`；确认 `remaining=0` 后改为 `false` |
| `XYZ2NOTION_ENRICHMENT_QUEUE_ENABLED` | 设为 `true` 后允许“只增强”定时队列运行 |
| `XYZ2NOTION_ENRICHMENT_BACKLOG` | 首次存量增强期间设为 `true`；确认 `remaining=0` 后改为 `false` |

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

登录 `https://tingwu.aliyun.com/` 并进入“我的记录”后，从浏览器开发者工具中
找到 `/api/directory/request?getDirList&c=web` 请求，复制其完整 `Cookie` 请求头值并
填入 `TINGWU_COOKIE`。Cookie 会过期，而且网页内部接口可能变化；它只被发送到
`tingwu.aliyun.com`。详细边界见
[听悟 Cookie Provider](tingwu-cookie.md)。

### SiliconFlow

`SILICONFLOW_API_KEY` 来自用户自己的 SiliconFlow 账户，用于调用配置中的免费
ASR 和免费文本模型。项目只接受代码已核对的免费模型白名单，不使用 DashScope，
也不实现付费 ASR。免费模型清单可能调整，升级项目版本前应重新核对价格页。

## 工作流

| 工作流 | 触发 | 作用 |
| --- | --- | --- |
| `Initialize Notion` | 手动 | `bootstrap` 首次建首页；`initialize` 日常只修复数据库和视图 |
| `Sync Podcast Metadata` | 每天 05:17（UTC+8）+ 手动确认 | 安全增量同步最近播放历史、待听、收藏和进度 |
| `Transcribe Episode Queue` | 存量每 2 小时；日常 05:47（UTC+8） | 只推进到“已转写”；每次最多 2 期且两期相隔 60 秒 |
| `Enrich Transcribed Episodes` | 存量每 2 小时；日常 06:37（UTC+8） | 只消费既有文字稿；生成摘要、章节、思维导图并发布 |
| `Process Episode AI` | 手动验收 | 兼容入口；正式拆队后保持手动禁用 |
| `Retry Failed Episode AI` | 每日 + 手动 | 每次最多恢复 2 个 `FAILED_RETRYABLE` 单集，累计重试 3 次后停止 |
| `Xyz2Notion Maintenance` | 手动 | 迁移、单集重做、统计或热力图重建 |
| `Xyz2Notion Notion-only Repair` | 手动 | 只读盘点 AI/封面/零播放存量，或分批修复封面与已发布脑图 |
| `Check Tingwu Authentication` | 手动 | 只发起一次只读目录请求验证 Cookie，不提交音频、不消耗转写额度 |

元数据工作流每天 UTC 21:17（UTC+8 次日 05:17）自动运行一次；手动运行仍必须输入
`RUN_SAFE_INCREMENTAL_SYNC`。自动与手动运行都受 20 请求、3 秒间隔、单页
25 条及 401/403/429 立即熔断保护。每次元数据同步完成后会自动本地化最多
10 张新出现的 Podcast 外链封面；已有 Notion 内部封面不会被外链覆盖，超过
10 张的积压会由后续每日任务继续处理；
仓库所有会写入 Notion 的工作流共用
`xyz2notion-runtime` concurrency group，避免迁移、初始化、元数据和 AI 同时改页。
AI 定时任务不包含 `XIAOYUZHOU_REFRESH_TOKEN`。转写队列只读取 Notion 已保存的
音频地址并停在“已转写”；增强队列不接收听悟、小宇宙或任何 ASR 凭证，只消费
Notion 已保存文字稿。存量模式两条队列均每两小时最多 2 期；转写队列两期之间
固定等待 60 秒。存量清空后将两个 backlog Variable 改为 `false`，之后转写每天
05:47、增强每天 06:37 仅处理新增检查点。可重试失败每日最多 2 期，同一期累计
重试 3 次后转为最终失败。

首次使用顺序：

1. 在 `Actions` 手动运行 `Initialize Notion`，首次选择 `bootstrap`；
2. 手动运行 `Sync Podcast Metadata`；
3. 各手动运行一次 `Transcribe Episode Queue` 和
   `Enrich Transcribed Episodes`，每次只选 1 期；
4. 验收成功后启用两条 Queue Variable；首次将两个 backlog Variable 设为
   `true`，存量清空后改为 `false`；
5. 查看工作流摘要中的聚合计数，以及 Notion Episode 的 `ASR Status`。

如果 Fork 中存在公开的 `config.yaml`，AI 工作流使用它；否则使用
`config.example.yaml`。配置文件只能包含模型顺序、运行上限等非秘密设置。

## 维护工作流

`Xyz2Notion Maintenance` 保留五种手动操作入口：

- `migrate-dry-run`：只读检查旧模板，默认选项；
- `migrate`：应用原地迁移，必须同时勾选 `confirm_changes`；
- `redo-episode`：必须在 `episode_eid` 填写精确 EID；
- `rebuild-statistics`：从 Notion Episode 基线与增量账本幂等重算统计；
- `rebuild-heatmap`：从 Notion 日统计重新生成热力图。

两项操作只接收 Notion Secret，不接收或访问小宇宙凭证。首次运行建立基线并原样
保留现有总时长；后续运行只累计基线之后新增加的播放秒数。

如果设置 `NOTION_MIGRATION_PAGE_ID`，两个迁移操作使用该旧模板副本；其他操作始终
使用主 `NOTION_PAGE_ID`。工作流不会把页面 ID、EID 或迁移键写入运行摘要。

## 中断与重试

听悟的解析 ID、Task ID、文字稿和摘要检查点作为 JSON 文件保存在用户自己的
Notion Episode 中。每个外部 AI 阶段完成后都会先保存检查点。因此
Actions 被取消或超时后，下次运行继续查询或发布，不重复提交已完成的 ASR。

听悟在途检查点只能查询已有记录，禁止再次解析或提交。若列表暂时看不到刚提交的
记录，则保持排队等待；若发现多条同名记录且无法通过已保存的真实记录 ID 唯一匹配，
以 `ambiguous_record` 安全暂停，绝不猜测、重复提交或提前降级。

临时网络、限流或服务不可用会进入 `FAILED_RETRYABLE`。修复凭证或等待服务恢复后，
手动运行 `Retry Failed Episode AI`。认证失效、风控或听悟网页 Schema 变化属于
明确终态时，若配置了 SiliconFlow，则自动降级；正在排队或处理中的听悟任务不会
提前双跑。SiliconFlow ASR 仍失败时，最后才在 Runner CPU 上运行本地 Whisper；
本地通道不需要新增 Secret。

## 轮换

- 小宇宙 Refresh Token 或听悟 Cookie 失效：在 GitHub Secret 中直接覆盖旧值；
- Notion Token 轮换：先把同一根页面授权给新 Integration，再替换 Secret；
- API Key 轮换：先创建新 Key、覆盖 Secret、验证工作流，再撤销旧 Key；
- 任何 Secret 疑似泄漏：立即在原服务撤销，不要只删除 GitHub 日志。
