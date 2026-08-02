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
- 总时长、年/月/周/日、排行、Podcast Gallery、五个 Episode Gallery，以及
  “转写文本”和“AI总结与思维导图”两个 AI 输出视图；
- 原创封面、页面图标、双栏入口和年度热力图占位块。

## 幂等与用户内容

初始化器采用增量协调：

- 主页布局仅在显式传入 `--create-home` 且目标页为空或只含数据层时创建；
- 日常 `notion-init`、元数据同步和 AI 工作流永不追加主页普通布局；
- 按父页面和精确数据库名称复用九个数据库；
- 按数据源、首页父页面和视图名称复用视图；
- 只添加缺失的 Xyz2Notion 管理属性，不重写已有 Formula、Relation 或选项；
- 不传 `null` 删除未知属性；
- 不删除任何数据库、视图、页面块或用户笔记；
- 首页标记或稳定布局锚点已存在时不重复创建布局。

因此重复执行不会生成第二套数据库或视图，用户添加的字段、块和视图会保留。

“转写文本”和“AI总结与思维导图”与五个 Episode 状态标签分开，分别按
`转写完成时间`、`总结完成时间`倒序展示。摘要、章节和思维导图是在保存文字稿后一次
增强流程中的并列输出，打开关联 Episode 页面即可查看完整内容。
