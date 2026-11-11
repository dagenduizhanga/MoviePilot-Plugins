# 运维脚本

## ssh-key-only.sh — SSH 只允许密钥登录

给 root 装上公钥，并关闭密码登录。可以重复运行；任何一步检查不通过都会自动回滚。

```bash
curl -fsSL https://raw.githubusercontent.com/dagenduizhanga/MoviePilot-Plugins/main/scripts/ssh-key-only.sh | bash -s -- 'ssh-ed25519 AAAA... 你的公钥'
```

公钥参数三选一：

| 参数 | 说明 |
| --- | --- |
| `'ssh-ed25519 AAAA...'` | 直接给公钥，比如从 Termius 复制的 |
| `gh:<GitHub用户名>` | 使用 `https://github.com/<用户名>.keys` 上的**全部**公钥 |
| `https://.../key.pub` | 从网址拉取 |

撤销（恢复系统原有的登录方式，已装的公钥保留）：

```bash
curl -fsSL https://raw.githubusercontent.com/dagenduizhanga/MoviePilot-Plugins/main/scripts/ssh-key-only.sh | bash -s -- --undo
```

**运行之后**：先别关当前窗口，新开一个窗口用密钥登录成功后再退出。万一登录不上，可以在当前窗口执行 `--undo`，或者用服务商网页上的 VNC 控制台（不走 SSH，仍可用密码登录）。

### 做了什么

1. 校验公钥格式（忘了替换占位符会直接报错退出），按指纹去重后追加到 `/root/.ssh/authorized_keys`。
2. 写入 `/etc/ssh/sshd_config.d/00-key-only.conf`，不改动 `sshd_config` 本身。文件名以 `00-` 开头，所以会排在 Ubuntu 云镜像自带的 `50-cloud-init.conf`（里面写着 `PasswordAuthentication yes`）之前生效。
3. 用 `sshd -t` 检查语法，再用 `sshd -T` 核对最终生效值；不通过就恢复原状，SSH 配置保持不变。
4. 重载 sshd（重载不会断开已有连接）。

需要 OpenSSH 8.2+（Debian 11+、Ubuntu 20.04+、RHEL/Rocky/Alma 9+）。更旧的系统不支持 `sshd_config.d`，脚本会检查出来并回滚，不会做任何改动。
