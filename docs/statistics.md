# 统计口径与热力图

Xyz2Notion 会把统计结果与数据来源一起写入 Notion，避免把不同精度的数据混为
一谈。

## 口径

- `全部`总时长与 Podcast 排行来自小宇宙 `mileage` 原始秒数；
- 已结束历史月使用 `monthly-wrapped` 的 `playedSeconds` 和 `playedDays`；
- 当前月不使用可能尚未结算的月记，按 Episode 播放进度实时推导；
- 年统计由校正后的月统计汇总；
- ISO 周、日和当前月来自 Episode 的播放进度及最近播放时间；
- Podcast 数、Episode 数和日数按稳定 ID 去重；
- 每条周期记录写入 `Statistics Source`，值为 `mileage`、`monthly_wrapped`、
  `episodes` 或 `mixed`。

小宇宙当前只读接口没有提供每一天的精确增量。日/周热力图因此会把一个
Episode 当前累计进度归到它的最近播放日。这是透明标注的 Episode 推导值，
不是伪装成官方日统计。

## 热力图

热力图完全在 GitHub Actions Runner 本地生成：

- SVG 用于可审计的日期、秒数和等级数据；
- PNG 由 Python 标准库生成，作为 Notion 兼容展示版本；
- 0 表示当天无记录，正数按年度最大日时长映射到 1–4 级；
- PNG 通过 Notion 官方 File Upload API 上传，不依赖任何作者或第三方图片
  服务器；
- 图片 caption 保存年度和内容哈希。同年数据未变化时不重复上传；变化时只
  更新该年度的受管图片块，用户其他块不受影响。
