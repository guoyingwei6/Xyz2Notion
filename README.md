# Xyz2Notion

Xyz2Notion 是一个完全自托管的播客工作流：用 GitHub Actions 从小宇宙同步实际收听记录、待听播放列表、收藏、喜欢和播放进度，再写入用户自己的 Notion，并为符合条件的单集生成文字稿、摘要、章节和思维导图。

项目不依赖作者服务器、NotionHub 插件或激活服务。所有任务都运行在你自己的 GitHub Actions 中；凭证只通过 GitHub Secrets/Variables 注入。

## 更新日志

最近版本的重点变化：

- **2026-08-04**：五个 Episode 视图统一显示 Podcast、收听状态和 ASR 状态；
-  AI 总结/思维导图视图改用 Episode 作为数据源，点击 Name 直接打开单集页面；独立的思维导图数据库仍保留为内容存储层。
- **2026-08-04**：增强队列增加每两小时一次的遗留文字稿检查，避免转写完成后因事件遗漏而长期停在“已转写”。
- **2026-08-04**：`可重试失败` 按失败阶段自动回到对应队列；只从已保存检查点继续，最多累计重试 3 次，不重复已完成的转写或摘要。
- **2026-08-03**：统计日期统一按 UTC+8 计算，趋势图和热力图不再受 GitHub Runner UTC 日期影响。

完整的按日期更新记录见 [CHANGELOG.md](CHANGELOG.md)。以后每次功能、工作流或数据结构变更都会先在该文件追加记录。

## 三步开始

### 1. Fork 并配置凭证

Fork 本仓库，在 `Settings → Secrets and variables → Actions` 添加：

必需 Secrets：

- `XIAOYUZHOU_REFRESH_TOKEN`
- `NOTION_TOKEN`
- `NOTION_PAGE_ID`

启用完整 AI 链路还需要：

- `DASHSCOPE_API_KEY`：阿里云百炼 `paraformer-v1` 的 API Key
- `SILICONFLOW_API_KEY`：SiliconFlow 的免费 ASR/摘要 API Key

可选 Repository Variable：

- `XIAOYUZHOU_DEVICE_ID`：不填时由安装身份稳定派生
- `ASR_QUEUE_ENABLED`：日常转写队列开关
- `XYZ2NOTION_ENRICHMENT_QUEUE_ENABLED`：日常摘要/章节/脑图队列开关
- `ASR_BACKFILL_ACTIVE`、`XYZ2NOTION_ENRICHMENT_BACKLOG`：一次性存量队列开关，默认应保持关闭

当前版本不使用 `TINGWU_COOKIE`，也不需要为百炼额外配置 URL、模型名或 Workspace ID。百炼接口和模型由代码固定为中国内地通用端点与 `paraformer-v1`。详细配置见 [GitHub Actions 与 Secrets](docs/github-actions.md) 和 [百炼 ASR](docs/dashscope-asr.md)。

### 2. 初始化并同步

在 Fork 的 `Actions` 页面依次运行：

1. `Initialize Notion`，首次选择 `bootstrap`；
2. `Sync Podcast Metadata`，手动运行时输入精确确认语 `RUN_SAFE_INCREMENTAL_SYNC`。

在授权的空白 Notion 页面中，初始化会创建 9 个底层数据库，并按 8 个主页模块组织 18 个展示视图。日常运行是幂等的：只更新现有数据库/视图，不会再追加第二套主页布局，也不会清空用户内容。

同步包含实际播放历史、待听播放列表、收藏、喜欢、订阅播客和播放进度。统计只使用真实播放秒数；仅浏览、仅加入待听或仅收藏的单集不会被算入收听时长、收听天数、排行或热力图。

### 3. 让 AI 队列自动运行

先手动运行一次 `Transcribe Episode Queue` 和 `Enrich Transcribed Episodes`，确认自己的凭证和 Notion 页面可用。验收通过后保持两个日常队列开关开启即可。

日常链路是事件驱动的，不依赖“固定半小时后再启动”：

1. 每天 05:17（UTC+8）运行一次受限的小宇宙增量同步；
2. 元数据同步成功后自动触发转写队列；
3. 转写工作流成功后自动触发增强队列；即使没有新的转写成功，每两小时也会检查遗留的“已转写”文字稿；
4. 增强队列只读取 Notion 已保存的文字稿，不再访问小宇宙或调用 ASR。

如果某期进入 `可重试失败`，它不会单独启动第三条任务，而是在下一次相关队列运行时自动重试：
上一步成功后触发的事件运行会立即接续；摘要/发布阶段没有事件时由每两小时的增强兜底运行接手。ASR 阶段失败回到下一次日常转写事件（存量开关开启时也会被每两小时的转写存量队列接手），摘要/发布阶段失败回到增强队列；每次运行仍最多处理 2 期。

日常每次最多处理 2 期；ASR 两期之间至少等待 60 秒。每个工作流都带有 GitHub Actions 并发锁，前一步未完成时不会并行启动下一次。每两小时的存量调度仍保留在 YAML 中，但由存量开关控制；本项目当前默认关闭，不会与日常链路争抢任务。

ASR 固定按以下顺序降级：

1. 阿里云百炼 `paraformer-v1`；
2. SiliconFlow 免费 ASR；
3. GitHub Actions 本地 `faster-whisper small`。

摘要、章节和思维导图固定使用：

1. SiliconFlow 免费 `Qwen/Qwen3-8B`；
2. 缓存的 GitHub Actions 本地 `Qwen3-1.7B-Q4_K_M`。

本地模型只在远程服务不可用时兜底。ASR 工作流为本地音频兜底安装 FFmpeg；摘要工作流复用缓存的本地模型和 `llama-cpp-python` 运行时，不重复下载完整模型。

AI 候选必须同时满足基础条件：

- 已播放至少 120 秒，或已加入小宇宙收藏，或已标记为喜欢；
- Notion 中存在可访问的音频地址；
- 尚未成功发布文字稿/增强内容，且不是最终失败状态；
- `Skip AI` 未勾选。

满足门槛后，转写队列和后续摘要/脑图队列都按以下顺序处理：**收藏 → 喜欢 → 听过 → 在听 → 待听**。
一个单集同时命中多个状态时取更高优先级。这个顺序只决定候选先后，不会放宽
“播放至少 120 秒、收藏或喜欢”的门槛；收藏或喜欢的单集即使从未播放也会进入候选。

已成功发布的单集不会重复 ASR、摘要或思维导图。不想处理某一期时，在 Notion 勾选 `Skip AI` 即可排除。

## Notion 数据与界面

### Episode 的五个标签

主页只保留以下五个 Episode 视图，顺序固定为：

`待听｜在听｜听过｜喜欢｜收藏`

- **待听**：小宇宙播放列表中的单集；可以尚未播放，不计入统计。
- **在听**：已经开始播放，但尚未播放到结束位置的单集。
- **听过**：播放到节目末尾附近（允许最后约 15 秒上报误差）或小宇宙标记为已完成。
- **喜欢**：小宇宙 `isPicked`，并且确实有播放秒数；它和收藏是两个独立状态。
- **收藏**：小宇宙 `isFavorited`；可以尚未播放，收藏本身不会增加收听统计，但会进入 AI 候选。

Episode 底层数据库仍保留完整属性、文字稿状态、播放进度、人工笔记和 AI 检查点；主页不再使用容易混淆的“全部”标签。

### 转写与总结

主页底部的“转写与总结”模块不改变上面的五个小宇宙状态标签，只有两个 AI 输出视图：

- **转写文本**：列出已经完成转写的单集，包括摘要失败但文字稿已保存的“可重试失败”单集，按 `转写完成时间` 从新到旧排序；打开单集页面即可查看完整文字稿。
- **AI总结与思维导图**：直接列出 Episode 数据库中已经完成增强的单集，按 `总结完成时间` 从新到旧排序；点击 `Name` 就能打开该单集页面，摘要、章节和思维导图属于同一次增强流程，会在同一单集页面中一起发布。

底层的独立“思维导图”数据库仍保留，用于存储可检索的脑图 JSON、Mermaid 和 Episode 关系；它不是第二条 AI 任务链，也不会再单独占用主页展示位。

### 统计口径

- 每日趋势：最近 7 天；
- 每周趋势：最近 1 个月；
- 每月趋势：最近 1 年；
- 年度趋势：全部年份；
- 年/月/周/日明细表：保留全量历史，作为图表的统计来源；
- Podcast 排行显示收听小时，底层仍保存精确的播放秒数；
- 热力图只标记有真实播放的日期。

统计完全基于 Notion 已保存的 Episode 和增量账本。首次启用时会把当前数据固定为基线，后续只累计新增播放秒数，不会重复计算旧的收听时长，也不会为统计额外请求小宇宙。日期按 UTC+8（Asia/Shanghai/Asia/Taipei）解释，不会把 GitHub Runner 的 UTC 日期当作本地日期。

Podcast 封面在同步后会分批本地化到 Notion；已有的 Notion 内部封面不会被外链覆盖。已确认的旧零播放页面采用可恢复的 Notion 归档，受保护的待听、收藏、喜欢或 AI 页面不会被清理。

## 小宇宙同步安全边界

每次元数据同步都受硬限制保护：

- 单次最多 20 个小宇宙请求；
- 请求之间至少间隔 3 秒；
- 列表最多读取 1 页、25 条；
- 播放进度最多查询 25 个 EID；
- 播放列表单集补全最多 3 条，缺失 Podcast 补全最多 2 条；
- 遇到 401、403 或 429 立即熔断，不刷新 Token、不重试、不继续抓取。

项目不会按月份遍历全历史，也不会因为本次增量没有看到某条旧记录就删除 Notion 页面。完整认证说明见 [`docs/xiaoyuzhou-auth.md`](docs/xiaoyuzhou-auth.md)。

## 配置与本地开发

需要 Python 3.12 和 [uv](https://docs.astral.sh/uv/)：

```bash
uv sync --locked --all-groups
uv run xyz2notion doctor
uv run ruff check .
uv run mypy src
uv run pytest
```

公开配置复制自 `config.example.yaml`：

```bash
cp config.example.yaml config.yaml
uv run xyz2notion config-check --config config.yaml
```

配置文件只保存公开的模型顺序和任务上限；Token、Cookie、API Key 只能通过环境变量或 GitHub Secrets 提供。旧版的 `REFRESH_TOKEN` 仍兼容，但新安装推荐使用 `XIAOYUZHOU_REFRESH_TOKEN`。

本地运行安全的 ASR 队列：

```bash
uv run xyz2notion process-asr --config config.yaml --mode incremental --limit 2
```

摘要和脑图只消费 Notion 中已经保存的文字稿：

```bash
uv run python -m xyz2notion.orchestration.enrichment_queue \
  --config config.yaml --mode normal --limit 2
```

更多说明：

- [GitHub Actions 与 Secrets](docs/github-actions.md)
- [小宇宙认证与 Device ID](docs/xiaoyuzhou-auth.md)
- [百炼 Paraformer ASR](docs/dashscope-asr.md)
- [SiliconFlow ASR](docs/siliconflow-asr.md)
- [本地 Whisper 兜底](docs/local-whisper.md)
- [摘要、章节与思维导图](docs/ai-enrichment.md)
- [单集页面与人工笔记](docs/episode-page.md)
- [Notion 模板与迁移](docs/notion-template.md)
- [统计实现](docs/statistics.md)
- [故障处理](docs/troubleshooting.md)

## 可恢复运行

每个单集都有独立的发现、ASR 提交、转写、AI 增强和发布检查点，检查点保存在用户自己的 Notion 页面属性中。工作流中断后会从最后一个完成阶段继续，不会重复已完成的 ASR 或摘要。GitHub Actions 日志只输出聚合状态，不输出 Token、Cookie、音频 URL、节目标题、EID 或文字稿正文。

## 许可证

[MIT](LICENSE)
