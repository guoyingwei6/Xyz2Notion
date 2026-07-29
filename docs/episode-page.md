# Episode 单集页面渲染

## 用户内容保护

Xyz2Notion 不调用 Notion 的 `erase_content`，也不会清空整个 Episode 页面。每个
单集只管理一个顶层 Toggle：

`🤖 Xyz2Notion 自动内容 · <STATE> · <CONTENT_HASH>`

程序只识别并处理此前由 Xyz2Notion 创建、且带有该前缀的根块。页面上其他段落、
图片、手工笔记、评论和用户新增块均不读取、不移动、不删除。

更新采用“先建后换”：

1. 在页面末尾创建 `BUILDING` 根块；
2. 写入全部播放器、摘要、脑图和文字稿；
3. 把新根块改为 `READY`；
4. 最后才把旧的 Xyz2Notion 根块移入 Notion 回收站。

如果中途失败，旧 `READY` 内容和用户笔记仍在；下次运行会重建未完成根块。Notion
官方的 Delete Block 也是移动到回收站，可从 Notion 恢复：

<https://developers.notion.com/reference/delete-a-block>

## 页面内容

程序托管区依次包含：

- 小宇宙外链 Audio block；
- 实际 ASR Provider、模型和时间轴精度；
- 全文摘要；
- 章节速览；
- 关键观点；
- 原文金句；
- 术语和人物；
- 问题回顾；
- 本地生成并上传到用户 Notion 的 SVG 脑图；
- Notion 原生嵌套列表脑图；
- 按时间和说话人组织的文字稿。

SiliconFlow 文字稿会明确显示 `coarse_timestamps` 警告；听悟逐句时间显示为
`exact_timestamps`。

## 脑图文件

SVG 由标准库在 GitHub Runner 本地生成，不请求外部脑图服务。文件通过 Notion
File Upload API 上传，再以 `file_upload` Image block 附加。Notion 官方当前支持
`.svg` 和 `image/svg+xml`：

- <https://developers.notion.com/guides/data-apis/working-with-files-and-media>
- <https://developers.notion.com/guides/data-apis/uploading-small-files>

文件名包含页面内容哈希。内容未变化时不会重复上传。

## 超长文字稿与限流

Rich Text 每段最多 2,000 字符，页面块通过 Notion 客户端按每批最多 100 个追加，
保证文字不丢失。每个批次独立处理 429、5xx 和 529 重试；若后续批次失败，
`BUILDING` 标记不会改为 `READY`，下次 Action 可以安全重建。

## 幂等

内容哈希覆盖音频 URL、完整 `TranscriptResult` 和 `SummaryResult`。页面已经存在
同哈希 `READY` 根块时：

- 不重复写块；
- 不重复上传脑图；
- 只清理可能由中断留下的其他 Xyz2Notion 托管根块；
- 用户内容保持原样。
