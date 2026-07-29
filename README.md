# Xyz2Notion

Xyz2Notion 是一个完全自托管的开源工具，用 GitHub Actions 将小宇宙订阅、
收听记录、播放进度、文字稿、AI 总结和思维导图同步到用户自己的 Notion。

项目不依赖作者服务器、NotionHub 插件或激活服务。用户自行提供小宇宙、
Notion 和可选语音识别服务的凭证，所有任务运行在用户自己的 GitHub Actions
中。

## 当前状态

项目处于早期开发阶段。P0 已建立项目骨架、CLI、安全域名白名单、日志脱敏、
单元测试和 CI；业务同步功能将按
[实施 Checklist](outputs/Xyz2Notion项目实施Checklist.md)
逐阶段实现。

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

复制 `config.example.yaml` 为 `config.yaml`，其中只保存 ASR 顺序、预算开关和
任务上限等可公开配置：

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

默认 ASR 顺序为通义听悟 Cookie → SiliconFlow，付费 DashScope 默认关闭且
预算为 0。

## 可恢复运行

每个单集使用独立状态机记录发现、ASR 提交、转写、AI 增强和发布阶段。
状态以原子 JSON 文件保存；GitHub Actions 中断后会从最后状态继续，而不是
重新执行已经完成的步骤。

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

## 安全原则

- 凭证只能发送到对应服务的精确域名。
- `malinkang.com`、`notionhub.app` 及其子域名在运行时明确禁止。
- 日志输出前必须经过统一脱敏。
- GitHub Actions 默认只有 `contents: read` 权限。
- 仓库不保存 Token、Cookie、API Key 或真实用户 Fixture。

完整说明见 [SECURITY.md](SECURITY.md)。

## 许可证

[MIT](LICENSE)
