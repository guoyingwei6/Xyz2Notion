# Changelog

本项目遵循 [Semantic Versioning](https://semver.org/)。

## Unreleased

- 新增只读权限的 `Xyz2Notion Maintenance` 手动工作流；
- GitHub-only 支持迁移 dry-run/确认应用、单集重做、统计和热力图重建；
- 所有 Notion 写工作流统一互斥，避免初始化、同步、AI 和维护并发修改页面。
- ASR 默认改为用户自己的百炼 `paraformer-v1` 免费额度，失败后降级
  SiliconFlow 免费 ASR 与本地 Whisper；
- 文字稿摘要继续使用用户自己的 `SILICONFLOW_API_KEY`，失败后回退到缓存的本地
  Qwen 模型；
- 自动 ASR 队列不再读取通义听悟 Cookie，听悟仅保留旧兼容/手动诊断入口。

## 0.1.0 - 2026-07-29

首个自主可控预览版：

- 小宇宙 Refresh Token 认证、订阅、历史、进度、里程和月度统计；
- 九个 Notion 数据库、12 个视图、首页统计、排行和年度热力图；
- 通义听悟 Cookie 优先、SiliconFlow 免费 ASR 自动降级；
- 千问结构化摘要、章节、重点、问答、术语、人物和脑图；
- 单集播放器、文字稿、SVG/原生脑图与用户笔记保护；
- Notion 私有检查点、GitHub Actions 定时推进和手动恢复；
- 旧 Podcast2Notion 模板原地迁移、dry-run 和作者服务依赖清理；
- 严格域名隔离、日志脱敏、Gitleaks 和 90% 测试覆盖率门。

真实账户端到端验收仍需要用户自行配置服务凭证。
