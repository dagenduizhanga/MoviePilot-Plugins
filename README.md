# MoviePilot-Plugins

个人维护的 MoviePilot V2 第三方插件仓库。

## 插件列表

| 插件 | 说明 |
| --- | --- |
| [NodeSeek签到（过盾版）](plugins.v2/nodeseekcheckin/README.md) | NodeSeek 每日签到，直连被 Cloudflare 拦截时自动用 MoviePilot 内置浏览器过盾后再签到 |
| [Oyunfor 礼品卡购买](plugins.v2/oyunforgiftcard/README.md) | 在 oyunfor.com 自动购买 App Store 礼品卡（TL）：把币安付款码推给你，付款后推送兑换码；也支持余额、银行卡（3DS 转发）；默认演练模式 |

## 安装

1. 打开 MoviePilot → **插件** → **插件市场** → 右上角设置（或 **设定 → 插件 → 插件市场**），在仓库地址中追加一行：

   ```
   https://github.com/dagenduizhanga/MoviePilot-Plugins
   ```

   也可以用环境变量：在 `PLUGIN_MARKET` 里追加这个地址（多个地址用英文逗号分隔）。

2. 回到插件市场刷新，找到需要的插件安装。**NodeSeek签到（过盾版）** 安装时 MoviePilot 会自动安装依赖 `curl_cffi`。

3. 如果之前装过已停更的 `NodeSeek论坛签到`（madrays/nodeseeksign），请先停用或卸载，避免重复签到。

## 目录结构

```
plugins.v2/<插件ID小写>/   插件源码（MoviePilot V2）
package.v2.json           V2 插件市场索引
icons/                    插件图标
tests/                    离线测试（真实 MoviePilot V2 宿主 + 本地模拟 NodeSeek/Cloudflare）
tests/oyunforgiftcard/    Oyunfor 插件的离线测试（本地模拟 Oyunfor、支付网关与银行 3DS 页面）
```

## 免责声明

插件仅用于在自己的账号上完成签到或购买，请自行评估是否符合目标站点的使用规则，风险自负。
