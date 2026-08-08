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
| `DASHSCOPE_API_KEY` | 建议 | 百炼 Paraformer 优先 ASR；内部顺序为 v1 → v2 → mtl-v1 |
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

### 阿里云百炼 Paraformer

`DASHSCOPE_API_KEY` 来自用户自己的阿里云百炼账号，用于优先调用
Paraformer 录音文件识别额度。项目只把该 Key 发送到
`dashscope.aliyuncs.com`，并且只允许 `paraformer-v1`、`paraformer-v2` 和
`paraformer-mtl-v1` 作为自动 ASR 模型，顺序固定为 v1 → v2 → mtl-v1。
请使用中国内地百炼的通用 API Key；不需要另设 URL、Workspace ID 或模型 Secret。
当前代码使用 `POST /api/v1/services/audio/asr/transcription` 提交，使用
`GET /api/v1/tasks/{task_id}` 查询异步任务。

### SiliconFlow

`SILICONFLOW_API_KEY` 来自用户自己的 SiliconFlow 账户，用于调用配置中的免费
ASR 降级模型和免费文本摘要模型。项目只接受代码已核对的免费模型白名单，
不实现付费 ASR。免费模型清单可能调整，升级项目版本前应重新核对价格页。

## 工作流

| 工作流 | 触发 | 作用 |
| --- | --- | --- |
| `Initialize Notion` | 手动 | `bootstrap` 首次建首页；`initialize` 日常只修复数据库和视图 |
| `Sync Podcast Metadata` | 每天 05:17（UTC+8）+ 手动确认 | 安全增量同步最近播放历史、待听、收藏和进度 |
| `Transcribe Episode Queue` | 元数据同步成功后；存量每 2 小时 | 只推进到“已转写”；日常每次最多 2 期且两期相隔 60 秒 |
| `Enrich Transcribed Episodes` | 转写队列成功后；每 2 小时检查一次遗留文字稿 | 只消费既有文字稿；日常每次最多 2 期，生成摘要、章节、思维导图并发布 |
| `Retry Failed Episode AI` | 每 2 小时；也可手动 | 先处理 `人工请求重试`，再处理普通 `可重试失败`；每阶段最多 2 期 |
| `Xyz2Notion Maintenance` | 手动 | 迁移、单集重做、统计或热力图重建 |
| `Xyz2Notion Notion-only Repair` | 手动 | 只读盘点 AI/封面/零播放存量，或分批修复封面与已发布脑图；归档已确认的 legacy 零播放页面 |
| `Audit Notion View Configurations` | 手动 | 只读检查托管视图的配置项总数、可见列数、合法/未知属性和视图归属 |

元数据工作流每天 UTC 21:17（UTC+8 次日 05:17）自动运行一次；手动运行仍必须输入
`RUN_SAFE_INCREMENTAL_SYNC`。自动与手动运行都受 20 请求、3 秒间隔、单页
25 条及 401/403/429 立即熔断保护。每次元数据同步完成后会自动本地化最多
10 张新出现的 Podcast 外链封面；已有 Notion 内部封面不会被外链覆盖，超过
10 张的积压会由后续每日任务继续处理；
仓库所有会写入 Notion 的工作流共用
`xyz2notion-runtime` concurrency group，避免迁移、初始化、元数据和 AI 同时改页。
AI 定时任务不包含 `XIAOYUZHOU_REFRESH_TOKEN`。转写队列只读取 Notion 已保存的
音频地址并停在“已转写”。当前默认 ASR 队列按
`DashScope paraformer-v1 -> paraformer-v2 -> paraformer-mtl-v1 -> SiliconFlow -> 本地 Whisper` 降级；增强队列不接收
小宇宙或任何 ASR 凭证，只消费 Notion 已保存文字稿。存量模式两条队列均每两小时最多 2 期；转写队列两期之间
固定等待 60 秒。存量清空后将两个 backlog Variable 改为 `false`，之后元数据同步
成功才启动日常转写；增强队列在转写成功后立即启动，并每两小时额外检查一次遗留的“已转写”文字稿，
不受固定半小时窗口影响。`Retry Failed Episode AI` 会按保存的 `resume_state` 回到原失败阶段，
先消费 Episode 上勾选的 `人工请求重试`，再处理普通 `可重试失败`；即使没有失败，也可以用它把普通单集插入优先队列：待处理从 ASR 开始，已转写从增强开始，已增强从发布开始；排队中或转写中的任务只查询保存的检查点，不会重复提交。ASR 成功后由增强工作流事件立即接续。最终失败的单集也能通过这个复选框重新打开：有文字稿但没有摘要时从增强开始，已有摘要时从发布开始，没有文字稿时从 ASR 开始。复选框被消费后自动取消，不需要改代码。每次队列最多处理 2 期，
同一期累计重试 3 次后转为最终失败。

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

## Notion 视图配置审计、修复与重建

### 先审计，不要先同步

`Audit Notion View Configurations` 工作流只需要 `NOTION_TOKEN` 和 `NOTION_PAGE_ID`，不会访问小宇宙，也不会修改 Notion。
等价的本地命令是：

```bash
uv run xyz2notion audit-view-configurations --details
```

输出中的几个数字含义不同：

- `configuration.properties`：Notion 视图内部保存的属性配置项总数，包含 `visible=false` 的项；它不是页面上当前可见的列数。
- `visible`：配置项中 `visible=true` 的数量，才是当前实际显示的列数。
- `known` / `unknown`：这些配置项的属性 ID 是否还能在当前数据源找到。`unknown` 不为 0，或同一个 ID 重复出现，说明视图配置有历史残留。
- `view_id` / `parent_database_id`：用于确认具体是哪一个 view、哪一个根页面 linked database 容器，不代表 Episode 数据库 ID。

Notion API 对 `configuration.properties` 数组本身有最多 100 项的校验。这是视图配置请求的结构上限，不是 Education Plus 账号的数据库行数、页面块数或字段数配额。因此，Notion 界面看起来只有几列时，历史残留 ID、重复项或隐藏项仍可能让 API 收到 101 项。

### 正常修复路径

1. 先运行上面的只读审计，并保存 Actions 摘要中的 `view_id`、`parent_database_id`、`known` 和 `unknown`。
2. 如果只是视图属性残留，运行 `Initialize Notion`，选择 `initialize`。程序只对有残留、重复或格式异常的 table/gallery view 显式清空 `configuration.properties`，再写入清理后的配置；合法的用户字段和系统默认字段会保留，view ID 不会反复变化。
3. 如果根页面中出现重复或失效的 linked database 容器，先运行 `audit-dashboard` 核对精确数量，再由维护者确认后运行 `rebuild-dashboard`。这个操作会归档指定的根页面 linked database block 并重建托管视图，不删除数据页、数据库、字段、Episode 页面、文字稿、摘要或思维导图。工作流要求确认字符串严格匹配：

   ```text
   ARCHIVE_<expected_count>_LINKED_DATABASE_BLOCKS
   ```

   例如本次实际核对并执行的是 4 个容器：`ARCHIVE_4_LINKED_DATABASE_BLOCKS`。这里的 4 是根页面 block 数，不是 Episode 数据库字段数；重建后当前代码生成 18 个托管 view。

4. 修复后再次运行 `audit-view-configurations --details`。本次 2026-08-08 的最终审计结果为：Episode 相关配置项 `known=42`、`unknown=0`，`configuration.properties=42`；实际可见列按不同 view 为 5/6/7。42 是当前数据源的合法属性配置总数（包括标题字段 `Name`），不是 42 个显示列。

### 以后增加属性

可以继续在 Episode 数据库增加新属性，也可以在 view 中把合法属性加入显示配置。下次 `initialize` 会保留仍存在于数据源中的合法自定义属性；只在数据库里新建字段不会被程序强行显示。程序会在发出 Notion 请求前检查清理后的配置数量，如果确实超过 100 项就明确失败并提示减少该 view 的配置项，不会静默隐藏或丢弃新属性。

本次线上修复和验证记录：[`Initialize Notion` 归档/重建](https://github.com/guoyingwei6/Xyz2Notion/actions/runs/31245983402)、[代码 CI](https://github.com/guoyingwei6/Xyz2Notion/actions/runs/31246356714)、[最终初始化与审计](https://github.com/guoyingwei6/Xyz2Notion/actions/runs/31246474176)。

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

百炼的任务 ID、文字稿和摘要检查点作为 JSON 文件保存在用户自己的 Notion Episode
中。每个外部 AI 阶段完成后都会先保存检查点。因此 Actions 被取消或超时后，下次
运行继续查询或发布，不重复提交已完成的 ASR。若远程 ASR 进入明确失败状态，队列才
按固定顺序降级到 SiliconFlow，再降级到本地 Whisper；不会重复提交已经完成的阶段。

临时网络、限流或服务不可用会进入 `FAILED_RETRYABLE`。系统按保存的恢复点自动把它送回
转写或增强队列：ASR 阶段失败不会进入摘要队列，摘要/发布阶段失败也不会重新转写。
下一次相关事件运行会重试；独立失败队列每两小时检查两个阶段，也可以手动运行对应工作流。百炼认证、
配额或服务暂时不可用时，自动降级到 SiliconFlow；SiliconFlow ASR 仍失败时，最后
才在 Runner CPU 上运行本地 Whisper；
本地通道不需要新增 Secret。

### 人工请求优先处理或重试

Episode 数据库只有一个人工开关：`人工请求重试`。它既可以把普通单集插入优先队列，也可以
重开失败状态，优先级高于普通失败重试。系统会读取该单集最新的 `AI State File`，根据当前
检查点自动选择 ASR、增强或发布阶段；处理结束后通过 Notion API 将复选框改回未勾选。

## 轮换

- 小宇宙 Refresh Token 失效：在 GitHub Secret 中直接覆盖旧值；
- Notion Token 轮换：先把同一根页面授权给新 Integration，再替换 Secret；
- API Key 轮换：先创建新 Key、覆盖 Secret、验证工作流，再撤销旧 Key；
- 任何 Secret 疑似泄漏：立即在原服务撤销，不要只删除 GitHub 日志。
