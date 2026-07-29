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

## 安全原则

- 凭证只能发送到对应服务的精确域名。
- `malinkang.com`、`notionhub.app` 及其子域名在运行时明确禁止。
- 日志输出前必须经过统一脱敏。
- GitHub Actions 默认只有 `contents: read` 权限。
- 仓库不保存 Token、Cookie、API Key 或真实用户 Fixture。

完整说明见 [SECURITY.md](SECURITY.md)。

## 许可证

[MIT](LICENSE)
