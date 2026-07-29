# SiliconFlow 免费 ASR

Xyz2Notion 的稳定降级 Provider 使用 SiliconFlow 官方
[`/v1/audio/transcriptions`](https://docs.siliconflow.cn/en/api-reference/audio/create-audio-transcriptions)
接口。官方接口当前限制单文件不超过 1 小时和 50 MB，支持：

1. `FunAudioLLM/SenseVoiceSmall`（默认）；
2. `TeleAI/TeleSpeechASR`（模型下线时自动切换）。

这两个模型在 SiliconFlow
[价格页](https://siliconflow.cn/pricing)当前均标为免费，但免费政策属于外部状态，
未来可能变化；用户始终使用自己的 `SILICONFLOW_API_KEY`。

## 音频处理

每个 Episode 在独立临时目录中处理：

1. 只允许 HTTPS 公网音频 URL，并限制重定向和下载体积；
2. FFprobe 读取真实容器、编码、时长和体积；
3. FFmpeg 转成单声道、16 kHz、40 kbps MP3；
4. 在每个 25–30 分钟窗口内选择最接近 28 分钟的静音结束点；
5. 没有合适静音时使用 28 分钟安全切点；
6. 相邻片段保留 3 秒重叠；
7. 每段再次检查 1 小时和 50 MB 限制；
8. Provider 返回后退出临时目录，源音频、标准化音频和分片全部删除。

GitHub Actions 不上传音频 Artifact。

## 结果精度

SiliconFlow 端点只返回整段文本，不返回逐字时间戳。Xyz2Notion 会：

- 对重叠区执行最长后缀/前缀去重；
- 以分片边界生成粗粒度时间段；
- 将 `timing_quality` 和 Notion `ASR Quality` 标为
  `coarse_timestamps`；
- 用音频分片内容与实际模型生成稳定 Provider Task ID；
- 把 429、5xx 和网络失败转换为可恢复 Provider 错误，不打印 API Key 或完整
  服务响应。
