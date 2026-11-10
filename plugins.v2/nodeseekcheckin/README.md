# NodeSeek签到（过盾版）

适用于 **MoviePilot V2** 的 NodeSeek 每日签到插件。

旧的 `NodeSeek论坛签到`（madrays/nodeseeksign）已于 2026-09 下架停更，常见报错是
`API签到出错: requests non-JSON/non-200`——它的三个请求方式都被 Cloudflare 拦了，而且没有带 NodeSeek
现在要求的 `x-csrf-challenge` 请求头。本插件针对这两点重写。

## 工作方式

签到接口：`POST https://www.nodeseek.com/api/attendance?random=true|false`

插件按顺序尝试三条通道，前一条被 Cloudflare 拦截才会用下一条：

| 顺序 | 通道 | 做法 | 开销 |
| --- | --- | --- | --- |
| 1 | 直连 | `curl_cffi` 模拟最新 Chrome 的 TLS/HTTP2 指纹和成套请求头，直接调签到接口 | 3 个 HTTP 请求 |
| 2 | 浏览器过盾 | 用 MoviePilot 自带的仿真浏览器（v2.12+ 为 CloakBrowser，旧版为 Playwright）打开签到页，等 Cloudflare 质询通过（需要时会点击 Turnstile 验证框），然后在页面里发起签到请求 | 启动一次浏览器，约 10~60 秒 |
| 3 | FlareSolverr（可选） | 让 FlareSolverr 过盾拿到 `cf_clearance`，再直连签到 | 需要自备 FlareSolverr |

浏览器过盾成功后，插件会把 `cf_clearance`、浏览器 UA 和站点下发的新 Cookie 缓存下来，
**第二天优先带着它们直连**，通常不需要再开浏览器；缓存失效了才重新过盾。

## 配置说明

| 配置 | 说明 |
| --- | --- |
| NodeSeek Cookie | 浏览器登录 nodeseek.com → F12 → 网络 → 任意请求 → 请求头里的 `Cookie` 整段复制 |
| 试试手气 | 开：随机鸡腿；关：固定 5 个 |
| 签到周期 | 5 位 cron，默认 `17 8 * * *` |
| 签到通道 | 默认「自动」。也可以强制只用某一条通道，便于排查 |
| 随机延迟上限 | 定时触发后随机等待 0~N 秒再签到 |
| 失败重试次数 / 间隔 | 失败后自动重试。定时触发的失败在重试用尽前不会发通知；「立即运行一次」的结果每次都会通知 |
| 使用代理 / 自定义代理 | 默认不走代理。开启后三条通道都走代理（留空则用 MoviePilot 的系统代理） |
| 过盾等待上限 | 浏览器等待 Cloudflare 质询的最长时间，默认 90 秒 |
| 浏览器无头模式 | 默认有头（Docker 里是虚拟显示），更容易过盾；起不来会自动退回无头 |
| FlareSolverr 地址 | 留空使用 MoviePilot 系统设置里的 `FLARESOLVERR_URL`；都没配则不启用该通道 |
| 自定义 User-Agent | 一般留空。只有当你粘贴的 Cookie 里带 `cf_clearance` 时，才填获取该 Cookie 的那个浏览器的 UA |

远程命令：在消息渠道发送 `/nodeseek_sign` 可手动签到并收到结果。

## 能不能过盾，取决于什么

需要先讲清楚：**没有任何方案能保证 100% 通过 Cloudflare**，结果主要取决于出口 IP 的信誉。

- 家宽 IP 直连：通常第 1 条通道就够了。
- 机房/VPS IP 或被标记过的代理：直连大概率被拦，要靠浏览器通道；IP 信誉太差时浏览器也会卡在 "Just a moment"。
- `cf_clearance` 与 **出口 IP + UA** 绑定。所以开代理时三条通道必须走同一个出口；FlareSolverr 也必须和 MoviePilot 用同一个出口 IP。
- 签到只需要 Cookie。Cookie 过期后需要你重新登录获取（NodeSeek 的登录有 Turnstile + 设备指纹，本插件不做自动登录）。

## 排查

所有步骤都会写进插件日志（插件卡片 → 查看日志）。常见情况：

| 现象 | 原因与处理 |
| --- | --- |
| 通知「Cookie失效」 | 重新登录 NodeSeek 复制 Cookie |
| 直连 `被 Cloudflare 拦截`，浏览器通道成功 | 正常，说明当前出口 IP 直连过不了，插件已自动降级；之后会复用缓存 |
| 浏览器通道 `N 秒内未能通过 Cloudflare 质询` | 出口 IP 信誉差。看插件数据目录下的 `last_challenge.png` 截图；换出口 IP/代理，或把等待上限调大 |
| `出口 IP 被 Cloudflare 直接封禁` | 这个 IP 被站点直接拒绝，只能换 IP |
| `无法启动浏览器` | MoviePilot 的浏览器环境有问题（非官方镜像常见）。可改用「仅直连」或 FlareSolverr |
| 表单里提示 `curl_cffi 未安装` | 依赖没装上：在插件市场重装本插件，或在容器里执行 `pip install "curl_cffi>=0.16.0"` 后重启 |

## 测试

`tests/` 里是离线测试：在真实的 MoviePilot V2 后端源码里加载插件，用本地模拟的
NodeSeek 接口 + Cloudflare 质询页（含需要点击的 Turnstile）跑通所有分支。

```bash
export MP_HOST_DIR=/path/to/MoviePilot   # v2 分支源码，依赖与 app/helper 资源文件已就绪
xvfb-run -a python -m pytest tests/test_plugin.py
xvfb-run -a python -m pytest tests/test_host_integration.py   # 需单独进程运行
```

模拟的质询只能验证插件的流程与降级逻辑，不能代表真实 Cloudflare 的判定强度。

## 免责声明

仅用于在自己的账号上完成每日签到。请自行评估是否符合 NodeSeek 的使用规则，风险自负。
