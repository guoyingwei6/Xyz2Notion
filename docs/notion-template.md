# Xyz2Notion Notion 自主模板

Xyz2Notion 不复制或分发第三方模板。初始化器使用 Notion 公共 API
`2026-03-11`，从用户授权的空白页面创建数据库、关系、公式、视图和首页布局。

官方 API 依据：

- [Create a database](https://developers.notion.com/reference/create-database)
- [Working with views](https://developers.notion.com/guides/data-apis/working-with-views)
- [API version changes](https://developers.notion.com/reference/changes-by-version)

## 初始化

1. 创建 Notion Internal Integration，并打开读取、插入、更新内容能力。
2. 把目标空白页面连接给该 Integration。
3. 在 GitHub Actions Secrets 中设置 `NOTION_TOKEN` 和 `NOTION_PAGE_ID`。
4. 本地或工作流执行：

```bash
uv run xyz2notion notion-init --create-home
```

也可以显式指定页面：

```bash
uv run xyz2notion notion-init --create-home --page-id YOUR_PAGE_ID
```

不要在命令行中传入 Token。Token 只从环境变量读取。

## 自动创建的内容

- 独立的 `Xyz2Notion 数据层` 页面；
- `Author`、`Podcast`、`Episode`、`全部`、`年`、`月`、`周`、`日`、
  `思维导图` 九个数据库；
- Podcast、Episode、周期统计和思维导图之间的 Relation；
- 收听秒数、单集数量和小时数 Rollup/Formula；
- 播放百分比和环形进度 Formula；
- ASR Provider、模型、任务 ID、状态、精度、失败原因、内容版本、转写完成时间和总结完成时间；
- `增强 Provider`、`增强状态`：摘要、章节和思维导图共用的独立处理通道与状态；
- 总时长、年/月/周/日、排行、Podcast Gallery、五个 Episode Gallery，以及
  “转写文本”和“AI总结与思维导图”两个 AI 输出视图；
- 原创封面、页面图标、双栏入口和年度热力图占位块。

主页展示层按 8 个模块组织，共 18 个视图；底层仍只有上述 9 个数据库。初始化时会复用
已有数据库和视图，不会因为重复运行而再创建一套主页。

## 幂等与用户内容

初始化器采用增量协调：

- 主页布局仅在显式传入 `--create-home` 且目标页为空或只含数据层时创建；
- 日常 `notion-init`、元数据同步和 AI 工作流永不追加主页普通布局；
- 按父页面和精确数据库名称复用九个数据库；
- 按数据源、首页父页面和视图名称复用视图；
- 只添加缺失的 Xyz2Notion 管理属性，不重写已有 Formula、Relation 或选项；
- 不传 `null` 删除未知属性；
- 不删除数据库、页面块、用户笔记或 Episode 数据属性；仅清理已明确由代码托管的旧展示视图和视图配置；
- 首页标记或稳定布局锚点已存在时不重复创建布局。

因此重复执行不会生成第二套数据库或视图，用户添加的字段、块和视图会保留。

“转写文本”和“AI总结与思维导图”与五个 Episode 状态标签分开，分别按
`转写完成时间`、`总结完成时间`倒序展示。后一个视图使用 `增强状态=已完成`，并显示
`增强 Provider`；摘要、章节和思维导图是在保存文字稿后一次增强流程中的并列输出，
打开关联 Episode 页面即可查看完整内容。

## 主页视图与状态含义

主页的 Episode 模块只保留以下五个标签页，顺序固定为：

`待听｜在听｜听过｜喜欢｜收藏`

- **待听**：来自小宇宙播放列表的单集，可以尚未播放，不计入统计；
- **在听**：已经有播放进度，但尚未播放到节目末尾；
- **听过**：播放到总时长末尾附近（允许约 15 秒上报误差）或被小宇宙标记为完成；
- **喜欢**：`isPicked`，同时要求有实际播放秒数；
- **收藏**：`isFavorited`，可以尚未播放，但会进入 AI 候选。

主页底部的“转写与总结”模块包含两个输出视图：

- **转写文本**：按 `转写完成时间` 从新到旧显示已保存文字稿；文字稿已保存但增强失败的
  单集仍会显示，打开 Name 可直接进入 Episode 页面；
- **AI 总结与思维导图**：按 `总结完成时间` 从新到旧显示 `增强状态=已完成` 的单集，
  同时显示 `增强状态` 和 `增强 Provider`。

`ASR Status`/`ASR Provider` 只表示语音转写；`增强状态`/`增强 Provider` 只表示摘要、
章节和思维导图共用的增强流程。独立的“思维导图”数据库是 JSON、Mermaid 和 Episode
关系的内容存储层，不是另一条任务队列。

两个 AI 输出视图会由代码做安全协调：例行初始化会删除 Notion 中已经不存在的历史属性 ID 和
重复项，保留仍存在于 Episode 数据库中的合法字段，并补齐系统需要的默认字段。默认字段如下：

- **转写文本**：`Name`、`人工请求重试`、`ASR Status`、`ASR Provider`、`转写完成时间`；
- **AI总结与思维导图**：`Name`、`人工请求重试`、`Podcast`、`增强状态`、`增强 Provider`、
  `总结完成时间`、`Content Version`。

你可以直接在 Notion 的 Episode 数据库和这两个视图中增加合法属性；下一次初始化会保留它们。
清理后的配置如果确实超过 Notion API 的 100 项上限，程序会在发出更新请求前明确停止并提示减少
视图列，不会静默丢掉你新增的字段。这只改变视图显示配置，不删除 Episode 数据库中的字段、页面
或 AI 内容。其他普通 Episode、Podcast、统计视图仍保留用户手动调整的属性显示、列顺序、画廊
卡片布局或图表展示配置。
