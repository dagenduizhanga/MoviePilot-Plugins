"""
安全录入页：账号、密码、Cookie、卡信息不经过 MoviePilot 的设置表单（那里会先把明文写进数据库），
而是在这个一次性链接打开的页面里提交，插件收到后直接加密保存。
"""
import html
import re
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .shop import parse_cookie, parse_expiry

# 字段：名称、说明、输入类型
FIELDS = [
    ("username", "Oyunfor 账号（邮箱）", "email"),
    ("password", "Oyunfor 密码", "password"),
    ("cookie", "Oyunfor Cookie", "textarea"),
    ("card_number", "卡号", "password"),
    ("card_expiry", "有效期 MM/YY", "text"),
    ("card_cvv", "CVV", "password"),
    ("card_holder", "持卡人姓名（拼音/英文）", "text"),
]
FIELD_NAMES = {key: label for key, label, _ in FIELDS}

SECURITY_HEADERS = {
    "Cache-Control": "no-store, max-age=0",
    "Pragma": "no-cache",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                               "frame-ancestors 'none'; base-uri 'none'",
}


def luhn_ok(number: str) -> bool:
    digits = [int(d) for d in number]
    total = 0
    for i, digit in enumerate(reversed(digits)):
        if i % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def mask_value(field: str, value: str) -> str:
    """设置页上显示的提示（不含可还原的信息）"""
    if not value:
        return ""
    if field == "username":
        name, _, domain = value.partition("@")
        return f"{name[:2]}***@{domain[:1]}***" if domain else f"{value[:2]}***"
    if field == "card_number":
        last4 = re.sub(r"\D", "", value)[-4:]
        return f"•••• {last4}"
    if field == "cookie":
        return f"{len(parse_cookie(value))} 项"
    if field == "card_holder":
        return f"{value[:1]}***"
    return "已保存"


def validate(field: str, raw: str, now: Optional[datetime] = None) -> Tuple[Optional[str], str]:
    """返回 (规范化后的值, 错误)"""
    value = (raw or "").strip()
    if field == "username":
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value) and not re.fullmatch(r"[\w.\-+]{3,64}", value):
            return None, "账号格式不对"
        return value, ""
    if field == "password":
        return (raw or ""), "" if raw else "密码为空"
    if field == "cookie":
        if not parse_cookie(value):
            return None, "Cookie 格式不对（应为 name=value; name2=value2）"
        return value, ""
    if field == "card_number":
        digits = re.sub(r"[\s-]", "", value)
        if not re.fullmatch(r"\d{12,19}", digits) or not luhn_ok(digits):
            return None, "卡号不对（没有通过校验位检查）"
        return digits, ""
    if field == "card_expiry":
        parsed = parse_expiry(value)
        if not parsed:
            return None, "有效期格式应为 MM/YY"
        month, year = int(parsed[0]), int(parsed[1])
        now = now or datetime.now()
        if (year, month) < (now.year, now.month):
            return None, "这张卡已经过期"
        return f"{parsed[0]}/{parsed[1][2:]}", ""
    if field == "card_cvv":
        if not re.fullmatch(r"\d{3,4}", value):
            return None, "CVV 应为 3~4 位数字"
        return value, ""
    if field == "card_holder":
        if not re.fullmatch(r"[A-Za-z][A-Za-z .'\-]{1,48}", value):
            return None, "持卡人姓名请用英文字母（与卡面一致）"
        return value.upper(), ""
    return None, "未知字段"


_STYLE = """
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif;
background:#f5f6fa;color:#1f2330;margin:0;padding:16px}
main{max-width:560px;margin:0 auto;background:#fff;border-radius:14px;padding:20px 20px 8px;
box-shadow:0 2px 12px rgba(0,0,0,.06)}
h1{font-size:20px;margin:0 0 4px}p.sub{color:#5b6070;margin:0 0 16px;font-size:14px;line-height:1.6}
fieldset{border:1px solid #e3e5ec;border-radius:10px;margin:0 0 14px;padding:10px 12px}
legend{font-weight:600;font-size:14px;padding:0 6px}
label{display:block;font-size:13px;color:#3c4150;margin:10px 0 4px}
input[type=text],input[type=password],input[type=email],textarea{width:100%;box-sizing:border-box;padding:10px;
border:1px solid #cfd3dd;border-radius:8px;font-size:15px}
textarea{min-height:80px;font-family:monospace;font-size:12px}
.row{display:flex;gap:10px}.row>div{flex:1}
.saved{font-size:12px;color:#1b7f3b;margin-left:6px}.clear{font-size:12px;color:#b3261e;margin-top:4px;display:block}
button{width:100%;padding:12px;border:0;border-radius:10px;background:#5b47e0;color:#fff;font-size:16px;margin:6px 0 14px}
.msg{padding:10px 12px;border-radius:8px;margin:0 0 14px;font-size:14px;line-height:1.6}
.ok{background:#e7f6ec;color:#1b5e20}.err{background:#fdecea;color:#8a1c12}.warn{background:#fff6e0;color:#7a4b00}
small{color:#6b7080}
"""


def _page(title: str, body: str) -> str:
    return (f"<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            f"<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            f"<meta name=\"robots\" content=\"noindex,nofollow\"><title>{html.escape(title)}</title>"
            f"<style>{_STYLE}</style></head><body><main>{body}</main></body></html>")


def render_form(token: str, saved: Dict[str, str], messages: List[Tuple[str, str]], http_warning: bool) -> str:
    """saved: 字段 -> 打码提示"""
    notes = "".join(f'<div class="msg {kind}">{html.escape(text)}</div>' for kind, text in messages)
    if http_warning:
        notes += ('<div class="msg warn">当前是 http 连接：提交的内容在局域网里是明文传输的。'
                  '只在自己家的网络里打开这个页面；从外网访问请给 MoviePilot 配置 https。</div>')

    def field(key: str) -> str:
        label = FIELD_NAMES[key]
        kind = dict((k, t) for k, _, t in FIELDS)[key]
        hint = saved.get(key)
        placeholder = "已加密保存，留空不修改" if hint else ""
        badge = f'<span class="saved">✓ 已保存 {html.escape(hint)}</span>' if hint else ""
        clear = (f'<label class="clear"><input type="checkbox" name="clear_{key}" value="1"> 删除已保存的{html.escape(label)}'
                 f'</label>') if hint else ""
        attrs = f'name="{key}" autocomplete="off" autocapitalize="off" spellcheck="false" placeholder="{placeholder}"'
        if kind == "textarea":
            control = f"<textarea {attrs}></textarea>"
        else:
            extra = ' inputmode="numeric"' if key in ("card_number", "card_cvv") else ""
            control = f'<input type="{kind}" {attrs}{extra}>'
        return f"<div><label>{html.escape(label)}{badge}</label>{control}{clear}</div>"

    body = f"""<h1>🔐 Oyunfor 安全录入</h1>
<p class="sub">这里填写的内容直接用 AES-256-GCM 加密保存，不经过 MoviePilot 的设置数据库，也不会回显。
留空的项保持不变。这个链接只能提交一次，10 分钟内有效。</p>{notes}
<form method="post" action="secure" autocomplete="off">
<input type="hidden" name="t" value="{html.escape(token)}">
<fieldset><legend>账号（Cookie 失效时才用账号密码登录）</legend>
{field("cookie")}<small>浏览器登录 oyunfor.com → F12 → 网络 → 任意请求 → 请求头里的 Cookie 整段复制</small>
{field("username")}{field("password")}</fieldset>
<fieldset><legend>银行卡（只在付款方式选「银行卡」时使用）</legend>
{field("card_number")}<div class="row">{field("card_expiry")}{field("card_cvv")}</div>{field("card_holder")}
<small>卡号会做校验位检查；建议用限额低的卡或虚拟卡。</small></fieldset>
<button type="submit">加密保存</button></form>"""
    return _page("Oyunfor 安全录入", body)


def render_result(saved: Dict[str, str], messages: List[Tuple[str, str]]) -> str:
    notes = "".join(f'<div class="msg {kind}">{html.escape(text)}</div>' for kind, text in messages)
    rows = "".join(f"<li>{html.escape(FIELD_NAMES[key])}：{html.escape(hint)}</li>"
                   for key, hint in saved.items() if hint)
    body = f"""<h1>🔐 Oyunfor 安全录入</h1>{notes}
<p class="sub">现在已加密保存的内容：</p><ul>{rows or "<li>（无）</li>"}</ul>
<p class="sub">这个链接已失效。要再修改，请回到插件设置页重新打开「安全录入」。可以关闭本页了。</p>"""
    return _page("Oyunfor 安全录入", body)


def render_error(text: str) -> str:
    return _page("Oyunfor 安全录入", f'<h1>🔐 Oyunfor 安全录入</h1><div class="msg err">{html.escape(text)}</div>'
                                    '<p class="sub">请回到 MoviePilot 插件设置页，重新打开「安全录入」链接。</p>')
