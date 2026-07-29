# 安全策略

## 威胁模型

Xyz2Notion 会处理小宇宙 Refresh Token、Notion Token、通义听悟 Cookie 和
第三方 ASR API Key。主要风险包括：

1. 凭证被错误发送到非目标域名；
2. 异常响应、调试日志或测试快照泄漏凭证；
3. GitHub Actions 在不可信 PR 上读取 Secrets；
4. 依赖或 Action 版本漂移造成供应链风险；
5. 网页 Cookie 失效或被服务端撤销。

当前安全边界：

- 每类凭证拥有独立的精确域名白名单；
- HTTP 和包含用户名/密码的 URL 一律拒绝；
- 作者服务域名被显式列入禁止清单；
- 日志使用统一脱敏函数；
- CI 不使用 `pull_request_target`，权限默认 `contents: read`；
- GitHub Actions 固定到提交 SHA；
- Gitleaks 扫描仓库和提交内容。

## Secrets 配置

仅在个人 fork 的 `Settings → Secrets and variables → Actions` 中保存凭证。
不要把真实值写入 `.env`、Issue、Actions 日志或测试 Fixture。

## 凭证轮换

- 小宇宙：Refresh Token 失效或疑似泄漏后，退出相关登录会话并重新登录获取。
- Notion：在 Notion 集成设置中撤销旧 Token，创建新 Token，并重新授权目标页面。
- 通义听悟：退出全部网页会话后重新登录，更新 `TINGWU_COOKIE`。
- SiliconFlow：在服务商控制台删除旧 Key，创建权限最小的新 Key。

轮换后只更新 GitHub Secret，不要在提交历史中保存旧值。若凭证曾被提交，
仅删除文件并不够，必须先撤销凭证，再清理 Git 历史。

## 报告漏洞

请通过仓库的私密安全报告渠道提交，不要在公开 Issue 中附带任何凭证或原始
请求头。
