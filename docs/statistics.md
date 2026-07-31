# 统计口径与热力图

Xyz2Notion 会把统计结果与数据来源一起写入 Notion，避免把不同精度的数据混为
一谈。

## Notion 基线与增量口径

首次启用时，Xyz2Notion 只在 Notion 内建立统计基线：

- 现有 `全部`、年、月、周、日和 Podcast 总时长原样保留；
- 每个已有 Episode 的当前 `Played Seconds` 记为基线，不再次计入总时长；
- 基线版本最后写入“全部”记录。中途失败时不会提前启用半成品基线。

以后每天同步完成后，统计器只读取 Notion 已保存的 Episode：

- `新增秒数 = Played Seconds - Episode 基线 - 已记账增量`；
- 新增秒数按 `Last Played At` 写入 Episode 自己的日期账本；
- 总时长、年/月/周/日、Podcast 排行和热力图都由“原基线 + 日期账本”确定性重算；
- 重试不会重复累加，同一播放进度重复同步时新增秒数为 0；
- 播放进度倒退时不扣减历史，也不会在重新超过已记账高水位前重复计数；
- 技术属性只用于恢复与计算，不加入主页展示视图。

这一阶段不会为了统计调用小宇宙历史、月记或里程接口。小宇宙当前只读接口也没有
提供逐日播放事件，因此一次同步中新发现的增量统一归到该 Episode 最新的
`Last Played At` 日期。

## 热力图

热力图完全在 GitHub Actions Runner 本地生成：

- SVG 用于可审计的日期、秒数和等级数据；
- PNG 由 Python 标准库生成，作为 Notion 兼容展示版本；
- 0 表示当天无记录，正数按年度最大日时长映射到 1–4 级；
- PNG 通过 Notion 官方 File Upload API 上传，不依赖任何作者或第三方图片
  服务器；
- 图片 caption 保存年度和内容哈希。同年数据未变化时不重复上传；变化时只
  更新该年度的受管图片块，用户其他块不受影响。

`rebuild-statistics` 和 `rebuild-heatmap` 同样只读取 Notion，不加载
`XIAOYUZHOU_REFRESH_TOKEN`。
