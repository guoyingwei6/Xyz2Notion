# 从 Podcast2Notion 旧模板迁移

Xyz2Notion 采用原地、增量迁移。旧 Podcast、Episode 和 Author 行不会复制或移动，
所以原 Notion 页面 ID、页面链接、正文、用户笔记和未知自定义属性保持不变。

## 迁移前

1. 在 Notion 中复制一份旧根页面作为人工备份；
2. 把旧根页面及其数据库授权给自己的 Notion Integration；
3. 配置 `NOTION_TOKEN` 和旧根页面的 `NOTION_PAGE_ID`；
4. 先执行只读检查：

```bash
uv run xyz2notion migrate --dry-run
```

输出只包含扫描、计划更新和旧服务嵌入数量，不打印播客名、单集名、PID 或 EID。
如果发现重复 PID/EID，命令会停止且不写入任何页面；应先在 Notion 中人工判断
哪个重复行需要保留。

确认后执行：

```bash
uv run xyz2notion migrate
```

也可以完全不在本地接触 Token：把旧模板副本 ID 保存为 GitHub Secret
`NOTION_MIGRATION_PAGE_ID`，运行 `Xyz2Notion Maintenance`，先选择
`migrate-dry-run`，确认聚合结果后再选择 `migrate` 并勾选 `confirm_changes`。

## 自动识别和映射

迁移器识别旧数据库名：

- `Podcast`
- `Episode`
- `Author` 或 `作者`
- `全部`、`年`、`月`、`周`、`日`
- `思维导图`

同时检查旧字段签名，避免仅凭同名就接管无关数据库。主要映射包括：

| 旧字段 | 新字段 |
| --- | --- |
| `播客` / `Pid` | `Name` / `PID` |
| `标题` / `Eid` | `Name` / `EID` |
| `音频` / `时长` / `收听进度` | `Audio URL` / `Duration Seconds` / `Played Seconds` |
| `状态` / `喜欢` / `日期` | `Listening Status` / `Liked` / `Last Played At` |
| `作者` | `Authors` |
| `语音转文字状态=Done` | `ASR Status=已发布` |

已有的新字段值优先，迁移器不会用旧字段覆盖它。旧的 `Done` 单集会被视为已发布，
避免自动重复转写；需要重新生成时使用单集重做命令。

## 旧作者服务

迁移只删除 URL 主机精确匹配以下域名的 `embed` 块：

- `heatmap.malinkang.com`
- `notion-music.malinkang.com`
- `mindmap.malinkang.com`

其他 Embed、段落、Callout、标题、嵌套笔记、属性和页面均不删除。之后的播放器、
热力图和脑图由 Xyz2Notion 使用 Notion 原生块及用户自己上传的 SVG 生成。

## 恢复入口

只重做一个单集的 AI 流程：

```bash
uv run xyz2notion redo-episode --eid <EID>
```

GitHub-only 用法是在 `Xyz2Notion Maintenance` 选择 `redo-episode` 并填写
`episode_eid`。

该命令只清空 Xyz2Notion 管理的 AI 状态属性，不删除当前页面内容。下一次
`Transcribe Episode Queue` 和 `Enrich Transcribed Episodes` 会重新生成，并在成功
后原子替换 Xyz2Notion 托管区。

重建统计或当前年度热力图：

```bash
uv run xyz2notion rebuild-statistics
uv run xyz2notion rebuild-heatmap
```

临时跳过百炼，只使用 SiliconFlow：

```yaml
asr:
  provider_order:
    - siliconflow
```

暂停所有新 ASR、但继续运行元数据和统计同步：

```yaml
asr:
  provider_order: []
```

已经保存到 `TRANSCRIBED` 的单集仍可继续摘要和发布；尚未开始 ASR 的单集返回
`paused`，不会被误记为最终失败。

## Schema 版本

首页托管标记记录工作区 Schema 版本。当前为 v1。迁移命令先读取版本并计算连续的
加法迁移路径；若页面 Schema 比当前程序更新，会拒绝降级。初始化器只添加缺失字段、
关系、视图和标记，不删除未知字段或用户内容。
