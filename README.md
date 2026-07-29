# Xyz2Notion

Xyz2Notion 是一个完全自托管的开源工具，用 GitHub Actions 将小宇宙收听记录、
待听播放列表、收藏、播放进度、文字稿、AI 总结和思维导图同步到用户自己的 Notion。

项目不依赖作者服务器、NotionHub 插件或激活服务。用户自行提供小宇宙、
Notion 和可选语音识别服务的凭证，所有任务运行在用户自己的 GitHub Actions
中。

## 三步开始

### 1. Fork 并配置凭证

Fork 本仓库，在 `Settings → Secrets and variables → Actions` 至少添加：

- `XIAOYUZHOU_REFRESH_TOKEN`
- `NOTION_TOKEN`
- `NOTION_PAGE_ID`

转写和免费摘要建议再添加 `TINGWU_COOKIE` 和 `SILICONFLOW_API_KEY`。获取方法见
[GitHub Actions 与 Secrets](docs/github-actions.md)。

### 2. 初始化并同步

在 Fork 的 `Actions` 页面依次手动运行：

1. `Initialize Notion`
2. `Sync Podcast Metadata`

它会在已授权的空白 Notion 页面中创建九个数据库、14 个视图、统计关系和首页，
再同步播放历史、待听列表、收藏、进度、排行和热力图。

数据口径：

- `Episode · 全部` 只显示播放秒数大于 0 的单集；
- `Episode · 待听` 显示小宇宙播放列表，未播放时不参与统计；
- `Episode · 收藏` 显示小宇宙收藏，未播放时不参与统计；
- `Episode · 喜欢` 对应小宇宙 `isPicked`，与收藏是两个不同状态；
- 时长、天数、期数、排行和热力图只统计播放秒数大于 0 的记录。

### 3. 生成文字稿与 AI 内容

手动运行 `Process Episode AI`。元数据每天自动同步；AI 工作流在初期验收阶段仅
手动运行，避免未经确认连续处理大量节目。听悟 Cookie 明确失效时自动降级到
SiliconFlow；工作流中断后从用户自己 Notion 里的私有检查点继续。

只有满足以下条件的单集才会进入 AI 队列：

- 已收听至少 120 秒，或者已加入小宇宙收藏；
- 存在可访问的音频链接；
- 尚未发布文字稿，且不是“最终失败”；
- Notion 中的 `Skip AI` 未勾选。

不想处理某一期时，在 Notion 勾选 `Skip AI` 即可永久排除。已成功发布的单集不会
重复转写、摘要或生成思维导图。

> 建议第一次把 `config.yaml` 中的 `episodes_per_run` 设为 1，先用一个短单集验证。

## 项目状态

v0.1.0 已实现自主 Notion 模板、元数据与统计同步、听悟 Cookie → SiliconFlow
降级转写、SiliconFlow 免费摘要、脑图、迁移和可恢复的 GitHub Actions 编排。真实账户验收按
[实施 Checklist](outputs/Xyz2Notion项目实施Checklist.md)
继续记录；缺少用户凭证的项目不会用 Mock 冒充真实通过。

## 本地开发

需要 Python 3.12 和 [uv](https://docs.astral.sh/uv/)：

```bash
uv sync --locked --all-groups
uv run xyz2notion doctor
uv run ruff check .
uv run mypy src
uv run pytest
```

## 配置

复制 `config.example.yaml` 为 `config.yaml`，其中只保存免费模型顺序和
任务上限等公开配置：

```bash
cp config.example.yaml config.yaml
uv run xyz2notion config-check --config config.yaml
```

凭证只通过 GitHub Actions Secrets 或环境变量提供。兼容旧变量
`REFRESH_TOKEN`，但推荐迁移为 `XIAOYUZHOU_REFRESH_TOKEN`。

`XIAOYUZHOU_DEVICE_ID` 可以不填。省略时会根据
`XYZ2NOTION_INSTALLATION_ID`、`GITHUB_REPOSITORY` 或 Notion 页面 ID
稳定派生 UUID；显式填写则始终使用用户提供的设备 ID。

放入小宇宙 Refresh Token 后，可以执行不会输出 UID 或 Token 的认证检查：

```bash
uv run xyz2notion xiaoyuzhou-check
```

认证方式、Device ID 和只读接口说明见
[`docs/xiaoyuzhou-auth.md`](docs/xiaoyuzhou-auth.md)。

默认 ASR 顺序为通义听悟 Cookie → SiliconFlow，不实现任何付费 Provider。
SiliconFlow 音频切片、免费模型降级和时间轴精度说明见
[`docs/siliconflow-asr.md`](docs/siliconflow-asr.md)。

听悟 Cookie 的域名隔离、断点续查和降级规则见
[`docs/tingwu-cookie.md`](docs/tingwu-cookie.md)。文字稿的 SiliconFlow
免费结构化摘要、长文本分段和 Token 记录见
[`docs/ai-enrichment.md`](docs/ai-enrichment.md)。
单集页面的托管区边界、播放器、原生脑图、SVG 脑图和用户笔记保护机制见
[`docs/episode-page.md`](docs/episode-page.md)。
GitHub Secrets、五个运行工作流、调度时间和手动维护方法见
[`docs/github-actions.md`](docs/github-actions.md)。
旧 Podcast2Notion 模板的原地迁移、dry-run、单集重做和统计重建见
[`docs/migration.md`](docs/migration.md)。
配置字段见 [`docs/configuration.md`](docs/configuration.md)，成本边界见
[`docs/costs.md`](docs/costs.md)，故障处理与限制见
[`docs/troubleshooting.md`](docs/troubleshooting.md)，测试证据见
[`docs/qa-matrix.md`](docs/qa-matrix.md)。

## 可恢复运行

每个单集使用独立状态机记录发现、ASR 提交、转写、AI 增强和发布阶段。
状态以不可变 JSON 快照保存在用户自己的 Notion 文件属性中；GitHub Actions
中断后会从最后状态继续，而不是重新执行已经完成的步骤。仓库和 Actions
Artifact 都不会保存音频、文字稿或摘要。

## 初始化 Notion

授权空白页面并配置 `NOTION_TOKEN`、`NOTION_PAGE_ID` 后运行：

```bash
uv run xyz2notion notion-init
```

初始化器会幂等创建九个数据库、关系、公式、统计视图、Gallery 和首页布局，
且不会删除用户自行添加的字段、视图或笔记。完整说明见
[`docs/notion-template.md`](docs/notion-template.md)。

初始化完成后，同步订阅、收听过的播客、全部单集、播放进度和统计关系：

```bash
uv run xyz2notion sync-metadata
```

命令会先增量协调 Notion 结构，再按 Author ID、PID、EID 和周期键执行最小差异
upsert，并更新精确月统计、排行和当前年度热力图；不会覆盖未知属性、用户笔记
或单集页面块。统计口径见
[`docs/statistics.md`](docs/statistics.md)。

推进单集的转写、摘要和发布：

```bash
uv run xyz2notion process-ai --config config.yaml
```

只重试已经标记为“可重试失败”的单集：

```bash
uv run xyz2notion retry-failed --config config.yaml
```

## 安全原则

- 凭证只能发送到对应服务的精确域名。
- `malinkang.com`、`notionhub.app` 及其子域名在运行时明确禁止。
- 日志输出前必须经过统一脱敏。
- GitHub Actions 默认只有 `contents: read` 权限。
- 仓库不保存 Token、Cookie、API Key 或真实用户 Fixture。

完整说明见 [SECURITY.md](SECURITY.md)。

## 许可证

[MIT](LICENSE)
