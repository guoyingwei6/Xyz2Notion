---
created: 2026-07-29
updated: 2026-07-29
---

# Xyz2Notion：项目实施 Checklist

## 使用规则

- `[x]`：已完成并有证据。
- `[ ]`：未开始或正在进行。
- 每次只推进当前阶段中最靠前、依赖已满足的项目。
- 完成任务时必须同时记录代码、测试或截图证据。
- 阶段验收未通过，不进入下一阶段。
- 页面尽可能接近作者 Demo，但像素级一致不作为阻塞条件。
- 不使用作者插件、激活服务、运行服务器或私有运行包。
- 所有凭证只保存在用户自己的 GitHub Secrets。

## 已完成：前期调研与边界确认

- [x] 审计 `Podcast2Notion` 基础仓库。
- [x] 审计 `Podcast2NotionPro` 仓库。
- [x] 审计 PyPI `podcast2notion==0.2.5` 的实际运行代码。
- [x] 审计 `notionhub-runner` 对作者服务器的依赖。
- [x] 确认小宇宙使用 `X-Jike-Refresh-Token`，不是 Cookie。
- [x] 确认通义听悟网页转写使用 Cookie 和网页内部接口。
- [x] 确认 SiliconFlow 当前免费 ASR 模型与接口限制。
- [x] 明确 Cookie 优先、SiliconFlow 免费降级、付费 ASR 默认关闭。
- [x] 拆解作者 Notion Demo 的首页模块和统计结构。
- [x] 确认采用自主重建，不复制和再分发作者模板。
- [x] 确认运行环境仅为 GitHub Actions + Notion + 用户自己的服务凭证。

---

## P0：项目骨架与安全基线

目标：建立一个可以安全开发、测试和发布的全新开源仓库。

### 项目结构

- [x] 确定正式项目名称 `Xyz2Notion` 和 Python 包名 `xyz2notion`。
- [x] 确定许可证为 MIT。
- [x] 初始化 Git 仓库。
- [x] 创建 `pyproject.toml`，固定 Python 3.12。
- [x] 建立 `src/` 布局和 CLI 入口。
- [x] 建立 `tests/`、`schemas/`、`prompts/`、`assets/`。
- [x] 建立 `.github/workflows/`。
- [x] 创建 `.env.example`，只列变量名，不放真实值。
- [x] 添加 Ruff、Mypy、Pytest 配置。
- [x] 使用 `uv.lock` 锁定直接和间接依赖版本。

### 安全基线

- [x] 建立外部域名白名单，并按凭证类型隔离。
- [x] 默认 GitHub Token 权限设为 `contents: read`。
- [x] 不使用 `pull_request_target` 读取用户 Secrets。
- [x] 添加 Gitleaks Secret 泄漏扫描工作流。
- [x] 添加日志脱敏工具。
- [x] 添加测试：凭证不能发送到非白名单域名。
- [x] 添加测试：异常日志不能出现 Refresh Token、Cookie、API Key。
- [x] 禁止运行时请求 `malinkang.com`、`notionhub.app`。
- [x] 编写威胁模型和凭证轮换说明。

### P0 验收

- [x] 空项目安装成功，`xyz2notion doctor` 返回正常。
- [x] Lint、格式、严格类型检查和 19 个测试在本地通过，覆盖率 95%。
- [x] GitHub Actions CI 通过：[运行 30417768808](https://github.com/guoyingwei6/Xyz2Notion/actions/runs/30417768808)。
- [x] 安全测试能拦截非白名单、跨服务、HTTP 和伪造子域名凭证请求。

P0 当前状态：已完成。公开仓库、质量检查、类型检查、19 个测试和 Gitleaks
均通过，P1 阶段门已打开。

---

## P1：配置、领域模型和状态机

目标：先定义稳定的数据契约，后续 Provider 和 Notion 不直接互相耦合。

### 配置

- [x] 定义 `config.yaml` Schema 和 `config.example.yaml`。
- [x] 定义环境变量加载和校验，凭证使用 `SecretStr`。
- [x] 支持旧 Secret `REFRESH_TOKEN` 迁移到 `XIAOYUZHOU_REFRESH_TOKEN`。
- [x] 支持可选 `XIAOYUZHOU_DEVICE_ID`。
- [x] 未填写 Device ID 时根据安装身份生成稳定 UUID。
- [x] 支持 ASR Provider 顺序配置，默认听悟 Cookie → SiliconFlow。
- [x] 付费 ASR 默认关闭且预算为 0，未显式预算时拒绝启用。
- [x] 配置单次 Episode 数、每日/月度 ASR 分钟数和轮询次数上限。

### 领域模型

- [x] 定义 Podcast 模型。
- [x] 定义 Episode 模型。
- [x] 定义 Author 模型。
- [x] 定义 ListeningPeriod 模型。
- [x] 定义 TranscriptResult 和 TranscriptSegment 统一模型。
- [x] 定义 SummaryResult、Chapter 和递归 MindmapNode。
- [x] 定义 Provider 错误类别、可重试判断和安全异常。
- [x] 定义幂等键：PID、EID、ASR Provider Task ID。

### 状态机

- [x] 实现 `DISCOVERED`。
- [x] 实现 `ASR_SUBMITTED`。
- [x] 实现 `ASR_RUNNING`。
- [x] 实现 `TRANSCRIBED`。
- [x] 实现 `ENRICHED`。
- [x] 实现 `PUBLISHED`。
- [x] 实现 `FAILED_RETRYABLE`，保存精确恢复状态。
- [x] 实现 `FAILED_FINAL`。
- [x] 对非法状态跳转、错误类别和失败状态一致性增加测试。

### P1 验收

- [x] 配置缺失或非法时给出明确错误。
- [x] 所有模型可以 JSON 序列化和反序列化。
- [x] 状态机使用原子 JSON 存储，中断后可以从保存状态继续。

P1 当前状态：已完成。GitHub CI
[运行 30418431127](https://github.com/guoyingwei6/Xyz2Notion/actions/runs/30418431127)
中的质量检查、CLI 冒烟测试和 Gitleaks 全部通过。

---

## P2：Notion 初始化器与自主模板

目标：从空白 Notion 页面自动创建项目所需结构。

### Notion 客户端

- [x] 接入 Notion API `2026-03-11`。
- [x] 实现限流、`Retry-After` 和指数退避。
- [x] 实现分页读取。
- [x] 实现幂等创建、查询和更新。
- [x] 实现长文本自动分块。

### 九个数据库

- [x] 创建 `Podcast`。
- [x] 创建 `Episode`。
- [x] 创建 `Author`。
- [x] 创建 `全部`。
- [x] 创建 `年`。
- [x] 创建 `月`。
- [x] 创建 `周`。
- [x] 创建 `日`。
- [x] 创建 `思维导图`。

### 属性与关系

- [x] 创建 Podcast 属性、PID 唯一键和作者关系。
- [x] 创建 Episode 属性、EID 唯一键和 Podcast 关系。
- [x] 创建 Episode 到全部、年、月、周、日的关系。
- [x] 创建年、月、周、日的 Rollup 和 Formula。
- [x] 创建播放进度百分比和环形进度 Formula。
- [x] 创建 ASR Provider、模型、状态、精度和失败原因字段。
- [x] 创建自动生成内容版本字段。
- [x] 创建思维导图到 Episode 的关系。

### 首页与视图

- [x] 创建页面封面和图标。
- [x] 创建左右分栏。
- [x] 创建菜单/目录。
- [x] 创建年度热力图占位块。
- [x] 创建总收听时长视图。
- [x] 创建年、月、周、日统计视图。
- [x] 创建收听时长排行榜。
- [x] 创建 Podcast Gallery。
- [x] 创建 Episode“全部”Gallery。
- [x] 创建 Episode“在听”Gallery。
- [x] 创建 Episode“听过”Gallery。
- [x] 创建 Episode“喜欢”Gallery。
- [x] 创建思维导图 Table。
- [x] 配置每个视图的过滤、排序、卡片封面和可见属性。

### P2 验收

- [x] 模拟空白页面一次初始化后无需手工建库。
- [x] 模拟连续初始化两次不产生重复数据库或重复视图。
- [x] 初始化器创建与公开 Demo 对应的主要模块。
- [x] 自动化测试确认用户新增块、属性和视图不会被删除。
- [ ] 使用真实 Notion 空白页面执行一次初始化并保存截图证据（等待
  `NOTION_TOKEN`、`NOTION_PAGE_ID`）。
- [ ] 在真实页面连续初始化两次，确认无重复数据库或视图（等待凭证）。
- [ ] 对真实页面做视觉验收并按截图微调布局（等待凭证）。

P2 当前状态：实现与模拟集成验收完成；真实 Notion 页面验收等待用户凭证，
不阻塞后续不依赖凭证的阶段。实现提交为 `3ecc86a`，GitHub CI
[运行 30418989624](https://github.com/guoyingwei6/Xyz2Notion/actions/runs/30418989624)
已通过。

---

## P3：小宇宙认证与 API 客户端

目标：安全获取用户自己的订阅、收听历史、进度和统计。

### 认证

- [x] 从 Secret 读取 `XIAOYUZHOU_REFRESH_TOKEN`。
- [x] 携带稳定 `X-Jike-Device-ID`。
- [x] 调用 `app_auth_tokens.refresh`。
- [x] 只在内存保存短期 Access Token。
- [x] Access Token 失效时自动刷新并重试。
- [x] Refresh Token 失效时输出可操作提示。
- [x] 认证请求和日志中不泄漏 Token。

### API

- [x] 实现订阅列表。
- [x] 实现播客累计里程。
- [x] 实现播放历史。
- [x] 实现单集列表。
- [x] 实现播放进度。
- [x] 实现个人 Profile/UID。
- [x] 实现历史月份 monthly-wrapped。
- [x] 实现分页和游标。
- [x] 保存最小测试 Fixture，删除所有真实身份信息。

### P3 验收

- [x] 模拟有效 Refresh Token 能换取 Access Token。
- [x] 模拟读取订阅、历史、进度和里程。
- [x] 无效 Token 不会死循环或泄漏响应内容。
- [x] 小宇宙接口契约测试通过。
- [ ] 使用真实 Refresh Token 完成 `xiaoyuzhou-check`（等待
  `XIAOYUZHOU_REFRESH_TOKEN`）。
- [ ] 使用真实账号抽样读取订阅、历史、进度和里程并保存脱敏证据（等待凭证）。

P3 当前状态：认证、自动刷新、七类只读接口、分页、脱敏 Fixture 和契约测试
已经实现；真实账号验收等待用户凭证，不阻塞后续离线开发。实现提交为
`01a7b5e`，GitHub CI
[运行 30419250447](https://github.com/guoyingwei6/Xyz2Notion/actions/runs/30419250447)
已通过。

---

## P4：元数据同步到 Notion

目标：稳定、幂等地同步 Author、Podcast 和 Episode。

### Author

- [x] 按用户 ID 或稳定键去重。
- [x] 同步名称和头像。
- [x] 建立 Author–Podcast 双向 Relation。

### Podcast

- [x] 按 PID upsert。
- [x] 同步标题、封面、简介和链接。
- [x] 同步累计收听秒数。
- [x] 同步最后更新时间。
- [x] 不覆盖用户手工字段。

### Episode

- [x] 按 EID upsert。
- [x] 同步标题、简介、封面、发布时间和音频 URL。
- [x] 同步喜欢状态。
- [x] 同步未听、在听、听过状态。
- [x] 同步总时长和收听进度。
- [x] 同步最近播放时间。
- [x] 建立 Podcast 双向 Relation。
- [x] 为已播放单集建立全部、年、月、周、日双向 Relation。
- [x] 新单集写入 AI 待处理状态。

### P4 验收

- [x] 模拟首次运行创建完整数据。
- [x] 模拟第二次运行不创建重复页面。
- [x] 自动化测试确认状态或进度变化只更新对应字段。
- [x] 自动化测试确认用户手工笔记、属性和页面块保持不变。
- [ ] 使用真实账号与真实 Notion 页面运行 `sync-metadata` 两次并保存脱敏证据
  （等待小宇宙与 Notion 凭证）。
- [ ] 抽样核对真实订阅、累计里程、收听状态、周期关系和 Rollup（等待凭证）。

P4 当前状态：订阅与历史播客发现、全部单集、进度合并、领域模型归一化和
Notion 最小差异 upsert 已实现并通过模拟端到端验收；真实数据验收等待凭证。
实现提交为 `054ef39`，GitHub CI
[运行 30419730194](https://github.com/guoyingwei6/Xyz2Notion/actions/runs/30419730194)
已通过。

---

## P5：统计、排行与热力图

目标：还原 Demo 中的统计体验和正确口径。

### 统计

- [x] 汇总全部累计收听时长。
- [x] 汇总每年播客数、单集数、天数和时长。
- [x] 汇总每月播客数、单集数、天数和时长。
- [x] 汇总 ISO 周统计。
- [x] 汇总每日统计。
- [x] 对历史月份使用 monthly-wrapped 精确值。
- [x] 当前月份使用 Episode 实时推导。
- [x] 月末后用 monthly-wrapped 校正。
- [x] 记录统计数据来源。
- [x] 计算 Podcast 收听时长排行榜。

### 热力图

- [x] 按每日收听秒数分 0–4 级。
- [x] 本地生成 GitHub Contribution 风格 SVG。
- [x] 生成 PNG 兼容版本。
- [x] 通过 Notion File Upload API 上传 PNG。
- [x] 使用年度和内容哈希只更新当前年度受管热力图块。
- [x] 不依赖作者热力图服务器。

### P5 验收

- [x] 自动化测试确认总时长等于播客里程汇总。
- [x] 自动化测试确认排行顺序按原始秒数正确排序。
- [x] 自动化测试确认热力图日期与每日数据一一对应。
- [x] 自动化测试确认同年相同内容不重复上传，变化时只更新一个受管块。
- [ ] 使用真实账号随机抽取历史月份，与小宇宙月记核对一致（等待凭证）。
- [ ] 在真实 Notion 页面确认 PNG 展示、统计 Formula 和视图排序（等待凭证）。

P5 当前状态：统计计算、来源标注、历史月校正、排行、SVG/PNG 渲染、Notion
文件上传和年度热力图幂等更新均已通过离线测试；真实月记与页面展示等待凭证。
实现提交为 `32dc88e`，GitHub CI
[运行 30420092816](https://github.com/guoyingwei6/Xyz2Notion/actions/runs/30420092816)
已通过。

---

## P6：SiliconFlow 免费 ASR 基线

目标：先完成稳定的官方免费 API 转写链路。

### 音频处理

- [x] 下载小宇宙音频到 Runner 临时目录。
- [x] 使用 FFprobe 检查格式、时长和体积。
- [x] 使用 FFmpeg 转为单声道、16 kHz、40 kbps。
- [x] 优先按静音切成 25–30 分钟片段，无静音时使用 28 分钟安全切点。
- [x] 每段保留 3 秒重叠。
- [x] Provider 返回或异常退出后删除临时目录，不保留音频 Artifact。

### ASR

- [x] 实现 SiliconFlow API 客户端。
- [x] 默认 `FunAudioLLM/SenseVoiceSmall`。
- [x] 支持 `TeleAI/TeleSpeechASR`。
- [x] 检查 1 小时和 50 MB 限制。
- [x] 实现 429 和 5xx 退避。
- [x] 实现模型下线自动切换。
- [x] 合并分片文字。
- [x] 去除重叠区重复文本。
- [x] 生成切片级粗粒度时间轴。
- [x] 标记 `ASR Quality = coarse_timestamps`。

### P6 验收

- [x] 自动化测试确认超过 1 小时音频会规划为 API 安全分片。
- [x] 自动化测试确认重叠区去重和粗粒度时间轴连续。
- [x] 自动化测试确认模型下线切换和 429/5xx 可恢复错误分类。
- [x] 纯模拟音频编排测试保证 GitHub Runner 未预装 FFmpeg 时覆盖率门仍通过。
- [ ] 使用真实 `SILICONFLOW_API_KEY` 转写一段约 20 分钟中文播客（等待凭证）。
- [ ] 使用真实超过 1 小时单集验证切片、合并和边界听感（等待凭证）。
- [ ] 接入总工作流状态机后验证失败状态的跨 Action 恢复（P10）。

P6 当前状态：下载安全、FFprobe/FFmpeg、静音切片、临时清理、免费 API
客户端、模型切换、合并去重和粗粒度时间轴已完成；真实 API 与长音频验收等待
SiliconFlow Key，跨 Action 恢复留在 P10 总编排验收。跨平台覆盖率修复后的
GitHub CI [运行 30420500565](https://github.com/guoyingwei6/Xyz2Notion/actions/runs/30420500565)
已通过。

---

## P7：通义听悟 Cookie Provider

目标：优先使用用户网页额度并取得完整听悟结果。

### 安全

- [x] 从 `TINGWU_COOKIE` Secret 读取 Cookie。
- [x] Cookie 只允许发送至明确的阿里云域名。
- [x] 禁止打印请求头、Cookie 和完整请求异常。
- [x] 实现 Cookie 健康检查。
- [x] 区分 `expired`、`risk_control`、`schema_changed`。

### 任务

- [x] 创建或查找播客文件夹。
- [x] 解析公网音频 URL。
- [x] 提交网页转写任务。
- [x] 保存听悟 Task ID。
- [x] 查询任务状态。
- [x] 已受理任务不重复提交。
- [x] 获取逐字稿、时间戳和说话人。
- [x] 获取全文摘要。
- [x] 获取章节速览。
- [x] 获取问答回顾。
- [x] 获取思维导图。
- [x] 获取听悟笔记。

### Fallback

- [x] 401/403 时熔断 Cookie Provider。
- [x] 404/字段变化时标记 schema changed。
- [x] 429/5xx 重试三次。
- [x] 明确失败后切换 SiliconFlow。
- [x] 处理中任务等待下一次 Action，不降级重复转写。

### P7 验收

- [ ] 有效 Cookie 能完成真实提交和结果读取（等待 `TINGWU_COOKIE`）。
- [x] 模拟错误 Cookie 能自动降级到 SiliconFlow。
- [x] 自动化测试确认日志和安全异常中不存在 Cookie。
- [x] 网页接口变化只影响 AI 分支，不会破坏元数据同步主链路。

P7 当前状态：听悟网页任务的提交、断点续查、结果读取、Cookie 熔断和
SiliconFlow 降级策略已实现，并通过完整模拟契约测试；真实网页额度验收等待用户
Cookie，不阻塞 P8。网页接口属于非公开契约，变更时会安全标记
`schema_changed`，不会把响应正文或 Cookie 写入日志。GitHub CI
[运行 30420776360](https://github.com/guoyingwei6/Xyz2Notion/actions/runs/30420776360)
已通过。

---

## P8：AI 摘要、章节和脑图

目标：不同 ASR Provider 的结果都能生成统一内容。

### 文本处理

- [x] 按时间段和 Token 窗口切分文字稿。
- [x] 处理超长文字稿。
- [x] 清理 ASR 噪声和事件标签。
- [x] 保留时间戳与原文映射。

### 千问

- [x] 版本化 Summary Prompt。
- [x] 定义 JSON Schema。
- [x] 生成全文摘要。
- [x] 生成章节标题和章节摘要。
- [x] 生成关键观点。
- [x] 生成金句。
- [x] 生成术语和人物。
- [x] 生成问题回顾。
- [x] 生成思维导图树。
- [x] 校验失败时只修复 JSON。
- [x] 记录模型、Prompt 版本和成本。
- [x] 听悟原生摘要和脑图完整时归一化复用，额外模型成本为 0。
- [x] 识别 `AllocationQuota.FreeTierOnly`，支持百炼“免费额度用完即停”。

### P8 验收

- [x] 同一文字稿重复生成的 JSON 字段和类型结构稳定。
- [x] JSON Schema 校验通过。
- [x] 章节时间不超出音频总时长。
- [x] 不因总结失败而重新扣 ASR 费用。
- [ ] 使用真实 `DASHSCOPE_API_KEY` 验证普通和超长文字稿（等待凭证）。
- [ ] 在百炼控制台确认“免费额度用完即停”并保存用户侧验收证据（等待用户操作）。

P8 当前状态：Provider 无关的清理/分段、听悟原生结果归一化、千问
`qwen-flash` 结构化生成、长文本 map/reduce、单次 JSON 修复、语义约束和
Token/费用记录已通过模拟验收；真实百炼调用等待用户 API Key。摘要模块只接受
已经持久化的 `TranscriptResult`，没有调用 ASR 的能力，因此摘要失败不会重复
转写。GitHub CI
[运行 30421110087](https://github.com/guoyingwei6/Xyz2Notion/actions/runs/30421110087)
已通过。

---

## P9：单集页面渲染

目标：生成可读、可维护且不覆盖用户内容的单集页面。

### 自动内容区

- [x] 创建 Notion Audio block。
- [x] 显示实际 ASR Provider 和精度。
- [x] 写入全文摘要。
- [x] 写入章节速览。
- [x] 写入关键观点和金句。
- [x] 写入问题回顾。
- [x] 写入原生嵌套思维导图。
- [x] 生成并上传脑图 SVG。
- [x] 写入按时间和说话人组织的文字稿。
- [x] SiliconFlow 结果明确标注粗粒度时间轴。

### 内容保护

- [x] 建立程序管理的根块。
- [x] 更新时只替换自动内容区。
- [x] 用户笔记区永不删除。
- [x] 重跑不会叠加重复内容。
- [x] Notion 429/529 后可以继续分块写入。
- [x] 采用 `BUILDING → READY` 先建后换，写入失败时保留旧页面。

### P9 验收

- [x] 模拟页面包含完整播放器、摘要、脑图和文字稿。
- [x] 自动化测试确认手工笔记在重复运行后仍保留。
- [x] 自动化测试确认超长文字稿可以完整写入。
- [ ] 在真实 Notion Episode 页面执行创建、更新和中断恢复（等待凭证）。
- [ ] 保存真实页面截图并按公开 Demo 微调排版（等待凭证）。

P9 当前状态：程序托管根块、先建后换、用户块保护、播放器、完整结构化内容、
本地 SVG 与原生嵌套脑图、长文字稿和 429/529 恢复均通过模拟验收；真实 Notion
页面和视觉微调等待凭证。更新只会把带 `🤖 Xyz2Notion 自动内容` 前缀的旧根块
移入 Notion 回收站，不会清空页面。

---

## P10：GitHub Actions 自动化

目标：无需 VPS，定时同步且故障后可继续。

### Workflow

- [x] `ci.yml`：Lint、类型检查、测试和安全扫描。
- [x] `init-notion.yml`：初始化自主 Notion 模板。
- [x] `sync-metadata.yml`：同步小宇宙元数据和统计。
- [x] `process-ai.yml`：推进 ASR、总结和发布状态机。
- [x] `retry-failed.yml` 或手动重试入口。
- [x] 调度避开整点高峰。
- [x] 配置 concurrency 防止重复运行。
- [x] 设置合理的 job timeout。
- [x] 为每次运行生成不含隐私的摘要。

### Secrets 文档

- [x] `XIAOYUZHOU_REFRESH_TOKEN`。
- [x] `XIAOYUZHOU_DEVICE_ID` 可选 Variable。
- [x] `NOTION_TOKEN`。
- [x] `NOTION_PAGE_ID`。
- [x] `TINGWU_COOKIE` 可选。
- [x] `SILICONFLOW_API_KEY`。
- [x] `DASHSCOPE_API_KEY` 条件必需。

### P10 验收

- [x] 新用户只按 README 添加配置即可运行。
- [ ] 手动运行和定时运行均成功。
- [x] 模拟工作流中断后下一次能从 Notion 检查点继续。
- [x] 自动化检查确认未配置 Artifact，仓库不提交私人状态、音频或完整文字稿。

P10 当前状态：四个只读权限运行工作流、错峰调度、互斥并发、超时、聚合摘要、
Notion 私有检查点和只重试可恢复失败的入口已完成；223 个测试通过，覆盖率
91.02%。真实手动/定时运行等待用户在 Fork 中配置 Secrets。

---

## P11：迁移、兼容与恢复

目标：允许原作者模板用户迁移，且系统可修复。

- [x] 识别作者旧模板的数据库名称。
- [x] 映射旧 PID/EID 和关系。
- [x] 原地采用已有 Podcast 和 Episode，不复制页面。
- [x] 保留页面 ID、页面链接、正文、未知属性和用户笔记。
- [x] 精确移除作者播放器、热力图和脑图服务 Embed。
- [x] 支持 Schema 版本检测和连续加法迁移。
- [x] 支持 dry-run。
- [x] 支持单集重做。
- [x] 支持统计重建。
- [x] 支持当前年度热力图重建。
- [x] 支持 Cookie Provider 单独禁用。
- [x] 支持所有 ASR Provider 暂停而继续同步元数据。

### P11 验收

- [x] 模拟旧模板迁移后 PID/EID 无重复；重复键时零写入停止。
- [x] 自动化测试确认用户笔记和已有页面 ID 不变。
- [x] 自动化测试确认只删除三个精确作者服务 Embed，主流程不再依赖它们。
- [ ] 使用真实旧模板副本执行 dry-run 和迁移并保存脱敏证据（等待 Notion 凭证）。

P11 当前状态：旧库签名识别、原地属性映射、重复键防护、用户内容保护、作者服务
Embed 清理、Schema v0→v1、dry-run、单集重做、统计/热力图重建和 Provider
暂停均已实现；242 个测试通过，覆盖率 90.18%。真实旧模板副本验收等待用户凭证。

---

## P12：端到端 QA 与发布

目标：用真实场景证明项目可以独立交付。

### 测试样本

- [ ] 普通话访谈。
- [ ] 中英混合。
- [ ] 方言明显。
- [ ] 多人对话。
- [ ] 背景音乐或远场录音。
- [ ] 超过 1 小时的长单集。

### 故障注入

- [ ] 小宇宙 Refresh Token 错误。
- [ ] Notion Token 错误。
- [ ] 听悟 Cookie 过期。
- [ ] 听悟内部接口字段改变。
- [ ] SiliconFlow 429。
- [ ] SiliconFlow 模型 404。
- [ ] 音频 URL 失效。
- [ ] GitHub Action 中途取消。
- [ ] Notion 429/529。
- [ ] AI JSON 无效。

### 发布

- [ ] README。
- [ ] 三步快速开始。
- [ ] 完整配置文档。
- [ ] 凭证获取和轮换文档。
- [ ] Notion 页面截图。
- [ ] 成本与免费策略说明。
- [ ] 隐私和安全说明。
- [ ] 已知限制。
- [ ] 故障排查。
- [ ] 迁移指南。
- [ ] Changelog。
- [ ] 正式 Release。

### P12 最终验收

- [ ] 不需要 VPS、插件、激活码或作者服务。
- [ ] 元数据同步、统计、转写、摘要、脑图全部可运行。
- [ ] Cookie 失败时自动使用免费 API。
- [ ] 付费 ASR 未显式开启时费用为 0。
- [ ] Notion 首页和单集页接近公开 Demo。
- [ ] 所有用户数据和凭证保持在用户自己的账户体系内。

---

## 当前执行位置

- 当前阶段：`P11 迁移、兼容与恢复`
- 下一项：完成 P11 全量测试与远端 CI，然后进入 P12 端到端 QA 与发布
- 开发原则：每完成一个 Checkbox，立即运行对应验证并更新任务状态
