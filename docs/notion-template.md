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
uv run xyz2notion notion-init
```

也可以显式指定页面：

```bash
uv run xyz2notion notion-init --page-id YOUR_PAGE_ID
```

不要在命令行中传入 Token。Token 只从环境变量读取。

## 自动创建的内容

- 独立的 `Xyz2Notion 数据层` 页面；
- `Author`、`Podcast`、`Episode`、`全部`、`年`、`月`、`周`、`日`、
  `思维导图` 九个数据库；
- Podcast、Episode、周期统计和思维导图之间的 Relation；
- 收听秒数、单集数量和小时数 Rollup/Formula；
- 播放百分比和环形进度 Formula；
- ASR Provider、模型、任务 ID、状态、精度、失败原因和内容版本；
- 总时长、年/月/周/日、排行、Podcast Gallery、四个 Episode Gallery 和
  思维导图视图；
- 原创封面、页面图标、双栏入口和年度热力图占位块。

## 幂等与用户内容

初始化器采用增量协调：

- 按父页面和精确数据库名称复用九个数据库；
- 按数据源、首页父页面和视图名称复用视图；
- 只添加或更新 Xyz2Notion 管理的属性；
- 不传 `null` 删除未知属性；
- 不删除任何数据库、视图、页面块或用户笔记；
- 首页标记已存在时不重复创建布局。

因此重复执行不会生成第二套数据库或视图，用户添加的字段、块和视图会保留。
