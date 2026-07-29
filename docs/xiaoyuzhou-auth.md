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

这些是小宇宙 App 的未公开接口，可能随时变化。客户端限制自动刷新次数、分页
页数和瞬时错误重试次数；Refresh Token 失效时会要求重新抓取，而不会无限循环
或输出服务器响应正文。
