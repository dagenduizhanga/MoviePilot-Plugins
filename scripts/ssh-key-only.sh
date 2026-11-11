#!/usr/bin/env bash
# SSH 仅允许密钥登录：安装 root 公钥 + 关闭密码登录。可重复运行，失败自动回滚。
#
#   curl -fsSL https://raw.githubusercontent.com/dagenduizhanga/MoviePilot-Plugins/main/scripts/ssh-key-only.sh | bash -s -- <参数>
#
# 参数（任选一个）：
#   'ssh-ed25519 AAAA... 备注'   直接给公钥（多行即多个）
#   gh:<GitHub用户名>             使用 https://github.com/<用户名>.keys 上的全部公钥
#   https://example.com/key.pub   从网址拉取公钥
#   --undo                        撤销：删除本脚本的配置，恢复系统原有登录方式（已装公钥保留）
#
# 只写入 /etc/ssh/sshd_config.d/00-key-only.conf，不改 sshd_config 本身。
# 需要 OpenSSH 8.2+（Debian 11+ / Ubuntu 20.04+ / RHEL 9+）；旧系统会自动回滚，不做任何改动。
set -euo pipefail

DROP=/etc/ssh/sshd_config.d/00-key-only.conf
AUTH=/root/.ssh/authorized_keys

die() { echo "✗ $*" >&2; exit 1; }
fingerprint() { ssh-keygen -lf - <<<"$1" 2>/dev/null | awk '{print $2}'; }

reload_sshd() {
    sshd -t || die "sshd 配置检查失败，未重载"
    systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null ||
        service ssh reload 2>/dev/null || service sshd reload 2>/dev/null ||
        die "配置已写入但重载失败，请手动重载 sshd"
}

main() {
    [[ $EUID -eq 0 ]] || die "请用 root 运行"
    [[ $# -eq 1 ]] || die "用法：ssh-key-only.sh <'ssh-ed25519 AAAA...' | gh:用户名 | https://... | --undo>"

    if [[ $1 == --undo ]]; then
        rm -f "$DROP"
        reload_sshd
        echo "✓ 已撤销，SSH 恢复为系统原有配置"
        return
    fi

    # 1. 获取并校验公钥：全部有效才继续
    local src k t b fp have prev kv eff
    local -a keys=()
    case $1 in
        gh:*)                 src=$(curl -fsSL "https://github.com/${1#gh:}.keys") || die "拉取失败：$1" ;;
        http://* | https://*) src=$(curl -fsSL "$1") || die "拉取失败：$1" ;;
        *)                    src=$1 ;;
    esac
    while IFS= read -r k; do
        k=${k%$'\r'}
        [[ -z ${k//[[:space:]]/} || $k == \#* ]] && continue
        read -r t b _ <<<"$k"
        [[ -n $(fingerprint "$k") ]] || die "无效公钥：$t ${b:0:16}…（是不是忘了替换占位符？）"
        keys+=("$k")
    done <<<"$src"
    ((${#keys[@]})) || die "没有拿到任何公钥"

    # 2. 安装公钥：按指纹去重，备注不同也不会重复添加
    install -d -m 700 /root/.ssh
    touch "$AUTH" && chmod 600 "$AUTH"
    have=$(ssh-keygen -lf "$AUTH" 2>/dev/null | awk '{print $2}' || true)
    for k in "${keys[@]}"; do
        fp=$(fingerprint "$k")
        grep -qxF "$fp" <<<"$have" && continue
        printf '%s\n' "$k" >>"$AUTH"
        have+=$'\n'$fp
    done
    if command -v restorecon >/dev/null; then restorecon -R /root/.ssh; fi

    # 3. 写入配置；00- 前缀保证排在 50-cloud-init.conf 等之前（sshd 取第一次出现的值）
    prev=$(cat "$DROP" 2>/dev/null || true)
    install -d /etc/ssh/sshd_config.d
    cat >"$DROP" <<'EOF'
# 由 ssh-key-only.sh 生成；删除本文件并重载 sshd 即可恢复原有登录方式
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
ChallengeResponseAuthentication no
PermitRootLogin prohibit-password
EOF

    # 4. 校验语法和最终生效值，不通过就回滚
    rollback() {
        if [[ -n $prev ]]; then printf '%s\n' "$prev" >"$DROP"; else rm -f "$DROP"; fi
        die "$1，已回滚，SSH 配置未改动"
    }
    eff=$(sshd -T 2>&1) || rollback "sshd 配置检查失败：$eff"
    for kv in "pubkeyauthentication yes" "passwordauthentication no" \
              "kbdinteractiveauthentication no" "permitrootlogin without-password"; do
        grep -qx "$kv" <<<"$eff" ||
            rollback "生效值不是「$kv」（sshd_config 可能没有 Include sshd_config.d，或被更早的配置覆盖）"
    done
    reload_sshd

    echo "✓ 已启用密钥登录、关闭密码登录。root 当前的授权公钥："
    ssh-keygen -lf "$AUTH" | sed 's/^/    /'
    echo "⚠ 先别关当前窗口：新开一个窗口用密钥登录成功后再退出。"
}

# 整个脚本解析完才执行，curl 下载中断时不会跑半截
main "$@"
