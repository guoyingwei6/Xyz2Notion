# 小宇宙认证与只读接口

Xyz2Notion 不使用小宇宙 Cookie。它从 GitHub Secret
`XIAOYUZHOU_REFRESH_TOKEN` 读取 `X-Jike-Refresh-Token`，调用：

```text
POST https://api.xiaoyuzhoufm.com/app_auth_tokens.refresh
```

换取短期 `X-Jike-Access-Token`。Access Token 和接口可能返回的轮换
Refresh Token 都只保存在本次进程的内存中，不写入磁盘、缓存或日志。

## Device ID

`X-Jike-Device-ID` 用来让同一个安装保持稳定的设备身份。可以显式设置
`XIAOYUZHOU_DEVICE_ID`；未设置时，配置层会基于仓库/安装身份生成稳定 UUID。
它不是登录凭证，但频繁变化可能触发重新认证或风控，所以不应每次随机生成。

## 最小权限与接口

Refresh Token 只发送给刷新端点，其他请求只携带短期 Access Token。客户端当前
只实现同步需要的只读接口：

- 订阅：`/v1/subscription/list`
- 累计里程：`/v1/mileage/list`
- 播客详情：`/v1/podcast/get`
- 单集：`/v1/episode/list`
- 收听历史：`/v1/episode-played/list-history`
- 播放进度：`/v1/playback-progress/list`
- Profile：`/v1/profile/get`
- 历史月报：`/v1/monthly-wrapped/get`

这些是小宇宙 App 的未公开接口，可能随时变化，也可能触发账号风控。客户端采用
不可绕过的保守限制：

- 单次进程最多发送 20 个小宇宙 HTTP 请求，刷新 Access Token 也计数；
- 任意两个请求的开始时间至少间隔 3 秒；
- 列表默认只取 1 页、最多 25 条；
- 播放列表单集详情最多补 3 条，缺失 Podcast 最多补 2 条；
- 播放进度最多查询 25 个 EID；
- 401、403、429 立即打开熔断器，本次运行不刷新、不重试、不继续；
- 5xx 或传输错误最多重试 1 次。

工作流默认只允许手动运行。账号出现异常、认证失败或风控提示时，不应立刻更换
Token 反复测试；先保持工作流禁用并等待账号恢复。

## 同步内容与统计边界

一次安全增量同步会合并以下数据：

- 订阅播客、作者和最近播放历史；
- 已保存的播放进度与 `Last Played At`；
- 播放列表（待听）及其顺序；
- 收藏（`isFavorited`）和喜欢（`isPicked`）。

待听、收藏和喜欢是三个独立标记。单集可以尚未播放就出现在待听或收藏视图中，
但只有 `Played Seconds > 0` 的单集才进入收听时长、收听天数、Podcast 排行和热力图。
浏览过但没有播放进度的单集不会被当作“听过”。

同步不会遍历全历史月份，也不会因为本次有限快照中没有出现某条旧记录就删除 Notion
页面。播放列表最多补全 3 条单集详情，缺失 Podcast 最多补全 2 条，播放进度最多查询
25 个 EID；超过上限的内容留到后续增量运行。
