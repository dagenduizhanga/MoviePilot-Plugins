"""
Oyunfor 站点自动化（Playwright 同步接口）

站点页面结构无法离线获取，所以这里的定位全部采用「多候选文本/属性启发式」，
并允许用户在插件设置里用 JSON 覆盖任意一步的选择器。
每一步都会保存截图和页面源码，便于对照真实页面调整。

本模块不依赖 MoviePilot，便于单独测试。
"""
import json
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urljoin, urlparse

BASE_URL = "https://www.oyunfor.com"
# App Store 礼品卡分类页与商品页地址规律（来自公开的商品链接）
CATEGORY_PATH = "/apple-store/apple-store-itunes-gift-card"
PRODUCT_SLUG = "{amount}-tl-tutarinda-app-store-itunes-hediye-karti"
# 订单页候选地址（找不到「Siparişlerim」链接时依次尝试）
ORDERS_PATHS = ("/hesabim/siparislerim", "/siparislerim", "/hesabim/siparis-gecmisi", "/hesabim/siparisler",
                "/uye/siparislerim", "/hesabim")
LOGIN_PATHS = ("/giris", "/giris-yap", "/uye-girisi", "/login", "/hesabim/giris")

# App Store / iTunes 兑换码：16 位，以 X 开头；也兼容 4-4-4-4 分组写法
APPLE_CODE_RE = re.compile(r"(?<![A-Z0-9])X[A-Z0-9]{15}(?![A-Z0-9])")
APPLE_CODE_GROUPED_RE = re.compile(r"(?<![A-Z0-9])(X[A-Z0-9]{3})[- ]([A-Z0-9]{4})[- ]([A-Z0-9]{4})[- ]([A-Z0-9]{4})(?![A-Z0-9])")

# ---------------------------------------------------------------- 文本启发式
# 所有模式都作用于「折叠后」的文本：土耳其字母转 ASCII、小写、合并空白。
TEXT_PATTERNS: Dict[str, List[str]] = {
    "buy_now": [r"^hemen al\b", r"^hemen satin al\b", r"^satin al$", r"^buy now\b"],
    "add_to_cart": [r"^sepete ekle\b", r"^add to (cart|basket)\b", r"sepete ekle"],
    "go_cart": [r"^sepete git\b", r"^sepeti (goruntule|onayla)\b", r"^sepetim\b", r"^sepet\b", r"^(go to |view )?cart\b"],
    "checkout": [r"^odemeye gec\b", r"^odeme adimina gec\b", r"^alisverisi tamamla\b", r"^siparisi tamamla\b",
                 r"^satin al\b", r"^devam et\b", r"^devam\b", r"^checkout\b", r"^proceed\b"],
    "pay_balance": [r"bakiye ile (ode|odeme|satin al)", r"^(hesap )?bakiye(m|si|niz)?\b", r"bakiyemden", r"\bbakiye\b",
                    r"\bbalance\b"],
    "pay_card": [r"kredi ?(karti)? ?/ ?banka karti", r"kredi karti", r"banka karti", r"kart ile (ode|odeme)",
                 r"credit ?/ ?debit", r"credit card", r"debit card"],
    "remove_item": [r"^sil$", r"^kaldir$", r"^urunu (sil|kaldir)$", r"^sepeti (bosalt|temizle)$", r"^tumunu (sil|kaldir)$",
                    r"^remove$", r"^delete$", r"^empty cart$", r"^clear cart$"],
    "pay_submit": [r"^odemeyi tamamla\b", r"^odemeyi onayla\b", r"^odeme yap\b", r"^guvenli odeme\b", r"^ode\b",
                   r"^siparisi (tamamla|onayla|olustur)\b", r"^satin al\b", r"^satin almayi tamamla\b", r"^onayla\b",
                   r"^tamamla\b", r"^pay\b", r"^complete (order|payment|purchase)\b", r"^place order\b",
                   r"^confirm (order|payment)\b"],
    "confirm_dialog": [r"^evet\b", r"^onayla\b", r"^onayliyorum\b", r"^tamam\b", r"^devam\b", r"^yes\b", r"^confirm\b",
                       r"^ok$"],
    "reveal_code": [r"kodu? (goster|gor|al)\b", r"kodlari (goster|gor)\b", r"^goster$", r"^show (code|pin)\b",
                    r"^reveal\b", r"epin(i)? goster"],
    "orders_link": [r"^siparislerim$", r"^siparis gecmisi$", r"^siparislerim\b", r"^my orders$", r"^orders$"],
    "order_detail": [r"^detay(lar)?$", r"^detay(i)? (gor|goruntule|goster)\b", r"^goruntule$", r"^incele$",
                     r"siparis detay", r"kodu? (goster|gor)\b", r"^details?$", r"^view$"],
    "login_link": [r"^giris yap$", r"^giris$", r"^uye girisi$", r"^giris yap ?/ ?uye ol$", r"^login$", r"^sign in$"],
    "login_submit": [r"^giris yap$", r"^giris$", r"^oturum ac$", r"^login$", r"^sign in$", r"^devam$"],
    "otp_submit": [r"^(gonder|onayla|dogrula|devam|devam et|tamam|ileri|submit|confirm|continue|verify|ok|next)\b"],
}
# 这些文字说明元素不是「付款方式选项」，而是充值/保存卡之类的入口
PAYMENT_EXCLUDE = [r"yukle", r"top ?up", r"\bload\b", r"kaydet", r"\bsave\b", r"ekle$", r"talep", r"cek(im)?\b"]
# 付款方式选项可能出现的元素（不含普通 <a>，避免点到页头链接）
OPTION_SELECTOR = ("label,input[type=radio],[role=radio],[role=tab],li[data-value],[data-payment],[data-method],"
                   "[data-type],button,div[class*=payment i],div[class*=odeme i],div[class*=method i],"
                   "li[class*=payment i],li[class*=odeme i],a[class*=payment i],a[class*=odeme i],a[class*=tab i]")
BUTTON_SELECTOR = "button,input[type=submit],input[type=button],a,[role=button]"
DIALOG_SCOPE = ("[role=dialog],[role=alertdialog],.modal.show,.modal.in,.swal2-popup,.sweet-alert,.jconfirm-box,"
                ".popup.active,.mfp-content,.fancybox-content")
SUCCESS_TEXT = [
    r"siparis(iniz)? (basariyla )?(alindi|olusturuldu|tamamlandi|onaylandi)",
    r"odeme(niz)? (basariyla )?(alindi|tamamlandi|basarili)", r"odeme islemi(niz)? basari",
    r"satin alma islemi(niz)? (basariyla )?tamamlandi", r"isleminiz basariyla (tamamlandi|gerceklesti)",
    r"tesekkur ederiz", r"order (has been )?(placed|completed|confirmed)", r"payment (was )?successful",
    r"thank you for your (order|purchase)",
]
FAIL_TEXT = [
    r"odeme(niz)? (basarisiz|alinamadi|reddedildi|gerceklestirilemedi)", r"islem(iniz)? (basarisiz|reddedildi)",
    r"yetersiz bakiye", r"bakiye(niz)? yetersiz", r"kart(iniz)? (reddedildi|gecersiz)",
    r"3d (dogrulama|secure)? ?(islemi)? basarisiz", r"dogrulama basarisiz", r"islem onaylanmadi",
    r"payment (failed|declined)", r"insufficient (balance|funds)", r"transaction (failed|declined)",
    r"authentication failed",
]
SUCCESS_URL = r"(basarili|success|tamamlandi|tesekkur|siparis-?(onay|tamam|basari)|order-?(complete|success|confirm)|odeme-?basari)"
FAIL_URL = r"(basarisiz|odeme-?hata|payment-?(fail|error)|/fail)"
OUT_OF_STOCK_TEXT = [r"stokta yok", r"\btukendi\b", r"stok(ta)? kalmadi", r"out of stock", r"sold out",
                     r"gecici olarak temin edilemiyor", r"stok bekleniyor"]
LOGGED_IN_TEXT = [r"^cikis( yap)?$", r"^hesabim$", r"^siparislerim$", r"^bakiye yukle$", r"^profilim$",
                  r"^logout$", r"^my account$", r"^hesap ayarlari$"]
LOGGED_OUT_TEXT = [r"^giris yap$", r"^uye ol$", r"^kayit ol$", r"^giris$", r"^giris yap ?/ ?uye ol$",
                   r"^login$", r"^sign in$", r"^register$"]
CAPTCHA_HOSTS = ("google.com/recaptcha", "recaptcha.net", "hcaptcha.com", "challenges.cloudflare.com")
CHALLENGE_TITLES = ("just a moment", "bir dakika", "lutfen bekleyin", "checking your browser", "attention required",
                    "access denied", "請稍候", "请稍候")

# 页面上能用于显示的「状态」
OUT_SUCCESS = "success"
OUT_FAILED = "failed"
OUT_CANCELLED = "cancelled"
OUT_TIMEOUT = "timeout"
OUT_UNKNOWN = "unknown"

_TR_MAP = str.maketrans({"ı": "i", "İ": "i", "ş": "s", "Ş": "s", "ğ": "g", "Ğ": "g", "ü": "u", "Ü": "u",
                         "ö": "o", "Ö": "o", "ç": "c", "Ç": "c", "â": "a", "Â": "a", "î": "i", "û": "u"})


def fold(text: Optional[str]) -> str:
    """土耳其字母转 ASCII、小写、合并空白，供启发式匹配"""
    value = (text or "").translate(_TR_MAP).lower()
    value = value.replace("̇", "")
    return re.sub(r"\s+", " ", value).strip()


def parse_number(raw: str) -> Optional[float]:
    """解析 1.234,56 / 1,234.56 / 250 / 250,00 这类金额数字"""
    text = (raw or "").strip().replace(" ", "").replace(" ", "")
    if not text:
        return None
    if "," in text and "." in text:
        if text.rfind(",") > text.rfind("."):
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", "")
    elif "," in text:
        head, _, tail = text.rpartition(",")
        text = f"{head.replace(',', '')}.{tail}" if len(tail) in (1, 2) else text.replace(",", "")
    elif "." in text:
        parts = text.split(".")
        if len(parts) > 2 or len(parts[-1]) == 3:
            text = text.replace(".", "")
    try:
        return float(text)
    except ValueError:
        return None


_MONEY_RE = re.compile(
    r"(?:₺|\btl\b|\btry\b)\s*(\d{1,3}(?:[.,\s]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)"
    r"|(\d{1,3}(?:[.,\s]\d{3})*(?:[.,]\d{1,2})?|\d+(?:[.,]\d{1,2})?)\s*(?:₺|\btl\b|\btry\b)",
    re.I)


def find_money(text: Optional[str]) -> List[float]:
    """找出文本里所有带 TL/₺ 的金额"""
    values = []
    for match in _MONEY_RE.finditer(text or ""):
        number = parse_number(match.group(1) or match.group(2) or "")
        if number is not None:
            values.append(number)
    return values


def amount_pattern(amount: int) -> re.Pattern:
    """匹配面额，如 250 TL / 250,00 ₺ / 250-tl，避免 250 误中 2500"""
    return re.compile(rf"(?<![\d.,]){amount}(?:[.,]0{{1,2}})?\s*-?\s*(?:tl|₺|try)(?![a-z0-9])", re.I)


def extract_codes(text: Optional[str], custom: Optional[str] = None) -> List[str]:
    """从文本中提取兑换码（去重保序）"""
    upper = (text or "").upper()
    found: List[str] = []
    if custom:
        try:
            for match in re.finditer(custom, text or ""):
                value = match.group(1) if match.groups() else match.group(0)
                value = (value or "").strip()
                if value and value not in found:
                    found.append(value)
        except re.error:
            pass
    for match in APPLE_CODE_RE.finditer(upper):
        if match.group(0) not in found:
            found.append(match.group(0))
    for match in APPLE_CODE_GROUPED_RE.finditer(upper):
        value = "".join(match.groups())
        if value not in found:
            found.append(value)
    return found


def mask_code(code: str) -> str:
    if len(code) <= 8:
        return code[:2] + "*" * max(0, len(code) - 2)
    return f"{code[:4]}{'*' * (len(code) - 8)}{code[-4:]}"


def parse_cookie(cookie_str: Optional[str]) -> Dict[str, str]:
    """解析浏览器复制的 Cookie 字符串"""
    text = (cookie_str or "").strip()
    if text.lower().startswith("cookie:"):
        text = text[7:]
    result: Dict[str, str] = {}
    for part in re.split(r"[;\r\n]+", text):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name, value = name.strip(), value.strip()
        if name:
            result[name] = value
    return result


def parse_expiry(raw: Optional[str]) -> Optional[Tuple[str, str]]:
    """把 MM/YY、MM/YYYY、MMYY 解析为 ('MM', 'YYYY')"""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 4:
        month, year = digits[:2], "20" + digits[2:]
    elif len(digits) == 6:
        month, year = digits[:2], digits[2:]
    else:
        return None
    if not 1 <= int(month) <= 12:
        return None
    return month, year


# ---------------------------------------------------------------- 页面脚本
_JS_FOLD = r"""
const fold = s => (s || '').replace(/[ıİI]/g, 'i').replace(/[şŞ]/g, 's').replace(/[ğĞ]/g, 'g').replace(/[üÜ]/g, 'u')
    .replace(/[öÖ]/g, 'o').replace(/[çÇ]/g, 'c').replace(/[âÂ]/g, 'a').toLowerCase().replace(/\s+/g, ' ').trim();
const visible = el => {
    if (!el || !el.getBoundingClientRect) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const st = getComputedStyle(el);
    return !(st.visibility === 'hidden' || st.display === 'none' || parseFloat(st.opacity || '1') === 0);
};
const mark = el => {
    let id = el.getAttribute('data-mpx');
    if (!id) { id = 'm' + Math.random().toString(36).slice(2, 10); el.setAttribute('data-mpx', id); }
    return id;
};
"""

JS_FIND_CLICKABLE = "([patterns, opts]) => {" + _JS_FOLD + r"""
    opts = opts || {};
    const regs = patterns.map(p => new RegExp(p, 'i'));
    const excludes = (opts.exclude || []).map(p => new RegExp(p, 'i'));
    const sel = opts.selector || 'a,button,input[type=submit],input[type=button],input[type=radio],[role=button],[role=radio],[role=tab],label,[onclick],li[data-value],[data-payment],[data-method]';
    const roots = opts.scope ? Array.from(document.querySelectorAll(opts.scope)).filter(visible) : [document];
    const maxLen = opts.maxLen || 60;
    let best = null;
    for (const root of roots) {
        for (const el of root.querySelectorAll(sel)) {
            if (!visible(el)) {
                if (!(el.tagName === 'INPUT' && el.type === 'radio')) continue;
            }
            const disabled = !!el.disabled || el.getAttribute('aria-disabled') === 'true' || /\bdisabled\b/.test(el.className || '');
            if (disabled && !opts.allowDisabled) continue;
            let raw = el.innerText || el.value || '';
            if (!/[a-z0-9]/.test(fold(raw))) raw = el.getAttribute('aria-label') || el.getAttribute('title') || raw;
            if (el.tagName === 'INPUT' && el.type === 'radio') {
                const lab = (el.id && document.querySelector('label[for="' + CSS.escape(el.id) + '"]')) || el.closest('label');
                raw = (lab ? lab.innerText : '') + ' ' + (el.value || '');
            }
            raw = raw || el.getAttribute('aria-label') || el.getAttribute('title') || '';
            const text = fold(raw);
            if (!text || text.length > maxLen) continue;
            if (excludes.some(r => r.test(text))) continue;
            if (opts.skipMarked && el.getAttribute('data-mpx-skip')) continue;
            for (let i = 0; i < regs.length; i++) {
                if (regs[i].test(text)) {
                    const score = i * 1000 + text.length;
                    if (!best || score < best.score) best = {el, score, text, disabled};
                    break;
                }
            }
        }
    }
    if (!best) return null;
    return {id: mark(best.el), text: best.text, tag: best.el.tagName.toLowerCase(), disabled: best.disabled,
            href: best.el.getAttribute('href') || ''};
}
"""

JS_PAGE_TEXT = "() => {" + _JS_FOLD + r"""
    const body = document.body;
    return {text: body ? (body.innerText || '').slice(0, 30000) : '', title: document.title || '', url: location.href};
}
"""

JS_INPUT_VALUES = r"""
() => Array.from(document.querySelectorAll('input,textarea')).filter(el => el.type !== 'password' && el.type !== 'hidden')
    .map(el => el.value || '').filter(v => v && v.length < 80).join('\n')
"""

JS_FIND_LINKS = "([hrefPart]) => {" + _JS_FOLD + r"""
    const out = [];
    for (const a of document.querySelectorAll('a[href]')) {
        const href = a.href || '';
        if (hrefPart && href.indexOf(hrefPart) < 0) continue;
        const text = fold((a.innerText || '') + ' ' + (a.getAttribute('title') || '') + ' ' + (a.getAttribute('aria-label') || ''));
        out.push({href, text: text.slice(0, 200), visible: visible(a)});
    }
    return out.slice(0, 500);
}
"""

JS_PRODUCT_INFO = "([priceSel]) => {" + _JS_FOLD + r"""
    const title = (() => {
        const h1 = Array.from(document.querySelectorAll('h1')).find(visible);
        return h1 ? h1.innerText : (document.title || '');
    })();
    const prices = [];
    const push = (v, src) => { if (v) prices.push({v: String(v).slice(0, 80), src}); };
    if (priceSel) { for (const el of document.querySelectorAll(priceSel)) { if (visible(el)) push(el.innerText || el.getAttribute('content'), 'custom'); } }
    for (const el of document.querySelectorAll('[itemprop=price]')) push(el.getAttribute('content') ? el.getAttribute('content') + ' TL' : el.innerText, 'itemprop');
    const meta = document.querySelector('meta[property="product:price:amount"]');
    if (meta) push(meta.getAttribute('content') + ' TL', 'meta');
    for (const el of document.querySelectorAll('[class*=price i],[class*=fiyat i],[id*=price i],[id*=fiyat i]')) {
        if (!visible(el)) continue;
        if (/(old|eski|del|strike|indirimsiz|list)/i.test(el.className || '')) continue;
        if (el.closest('del,s,strike')) continue;
        if (el.querySelector('[class*=price i],[class*=fiyat i]')) continue;
        push(el.innerText, 'class');
    }
    const body = document.body ? document.body.innerText : '';
    return {title, prices: prices.slice(0, 40), text: body.slice(0, 20000)};
}
"""

JS_FIND_TOTAL = "() => {" + _JS_FOLD + r"""
    const labels = /(genel toplam|odenecek tutar|toplam tutar|odenecek|toplam|grand total|order total|total)/;
    const out = [];
    for (const el of document.querySelectorAll('div,li,tr,p,span,td,th,dt,dd,strong,b,label')) {
        if (!visible(el)) continue;
        const t = fold(el.innerText || '');
        if (!t || t.length > 120 || !labels.test(t)) continue;
        out.push(el.innerText.replace(/\s+/g, ' ').trim());
    }
    return out.slice(0, 60);
}
"""

JS_FIND_INPUT = "([kind, overrideSel]) => {" + _JS_FOLD + r"""
    const labelText = el => {
        let t = '';
        try {
            if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) t += ' ' + l.innerText; }
            const pl = el.closest('label'); if (pl) t += ' ' + pl.innerText;
            const prev = el.previousElementSibling; if (prev && prev.tagName === 'LABEL') t += ' ' + prev.innerText;
        } catch (e) {}
        return t;
    };
    const desc = el => fold([el.name, el.id, el.getAttribute('placeholder'), el.getAttribute('aria-label'),
        el.getAttribute('autocomplete'), el.getAttribute('data-name'), el.getAttribute('formcontrolname'),
        typeof el.className === 'string' ? el.className : '', labelText(el)].join(' '));
    const rules = {
        email: {ac: /^(email|username)$/, re: /(e-?posta|email|mail|kullanici adi|username|uye adi|telefon veya e-?posta)/, not: /(sifre|password)/, types: /^(email|text|)$/},
        password: {ac: /current-password/, re: /(sifre|parola|password)/, not: /(tekrar|confirm|yeni)/, types: /^password$/},
        number: {ac: /^cc-number$/, re: /(card.?num|cardnumber|kart.?(no|num)|cc.?num|\bpan\b|kart numarasi|card number|ccno|cardno|kredi karti numarasi)/, not: /(cvv|cvc|holder|sahibi|month|year|expir|son kullanma)/, types: /^(text|tel|number|)$/},
        holder: {ac: /^cc-name$/, re: /(holder|owner|card.?name|kart (sahibi|uzerindeki)|ad ?soyad|isim|name on card|cc.?name|cardname|nameoncard)/, not: /(number|numara|cvv|cvc|e-?posta|email)/, types: /^(text|)$/},
        expiry: {ac: /^cc-exp$/, re: /(expir|exp.?date|son kullanma|\bskt\b|mm ?\/ ?yy|aa ?\/ ?yy|ay ?\/ ?yil|valid thru|gecerlilik)/, not: /(month|year|cvv|cvc)/, types: /^(text|tel|)$/},
        month: {ac: /^cc-exp-month$/, re: /(exp.?month|expmonth|month|\bay\b|cc.?month|\bmm\b)/, not: /(year|yil|cvv)/, types: /^(text|tel|number|select-one|)$/},
        year: {ac: /^cc-exp-year$/, re: /(exp.?year|expyear|year|\byil\b|cc.?year|\byy(yy)?\b)/, not: /(month|cvv)/, types: /^(text|tel|number|select-one|)$/},
        cvv: {ac: /^cc-csc$/, re: /(cvv|cvc|cvc2|cv2|\bcsc\b|guvenlik kodu|security code|card.?code)/, not: /(number|numara|name|isim)/, types: /^(text|tel|number|password|)$/},
        quantity: {ac: /^$/, re: /(adet|quantity|\bqty\b|miktar|piece)/, not: /(sepet)/, types: /^(text|tel|number|)$/},
    };
    if (overrideSel) {
        for (const el of document.querySelectorAll(overrideSel)) { if (visible(el)) return {id: mark(el), tag: el.tagName.toLowerCase(), type: el.type || '', maxlength: el.maxLength || -1, how: 'custom'}; }
    }
    const rule = rules[kind];
    if (!rule) return null;
    let best = null;
    for (const el of document.querySelectorAll('input,select')) {
        if (!visible(el) || el.disabled || el.readOnly) continue;
        const type = (el.tagName === 'SELECT') ? 'select-one' : (el.getAttribute('type') || '').toLowerCase();
        if (!rule.types.test(type)) continue;
        const ac = (el.getAttribute('autocomplete') || '').toLowerCase();
        const d = desc(el);
        let score = null;
        if (rule.ac.source !== '^$' && rule.ac.test(ac)) score = 0;
        else if (rule.re.test(d) && !rule.not.test(d)) score = 1;
        if (score === null) continue;
        if (!best || score < best.score) best = {el, score};
    }
    if (!best) return null;
    const el = best.el;
    return {id: mark(el), tag: el.tagName.toLowerCase(), type: el.type || '', maxlength: el.maxLength || -1, how: best.score === 0 ? 'autocomplete' : 'attr'};
}
"""

JS_VERIFY_STATE = "([otpSel]) => {" + _JS_FOLD + r"""
    const text = fold(document.body ? document.body.innerText : '').slice(0, 8000);
    const verifyText = /(3d ?secure|3-d secure|\b3ds\b|dogrulama|tek kullanimlik|one[- ]?time|\botp\b|verification|\bsms\b|authenticat|authoris|authoriz|approve|digital token|digibank|secure ?code|id check|visa secure|safekey|guvenlik kodu|onay kodu|sifre)/.test(text);
    const appText = /(approve|authoris|authoriz|push notification|digital token|digibank|banking app|mobile app|in-app|uygulama(niz|si)? (uzerinden|ile|dan)|mobil uygulama|check your (phone|device|app)|open your)/.test(text);
    const errorText = /(hatali|yanlis|incorrect|invalid|wrong|gecersiz|tekrar deneyin|try again|did not match|eslesmedi)/.test(text);
    const cardRe = /(card|kart|cc-|cvv|cvc|cvc2|expir|son kullanma|month|year|holder|sahibi|\bpan\b|kupon|coupon|indirim|promo|referans|reference|search|arama|e-?posta|email|telefon numarasi|phone number|posta ?kodu|postal|zip|adet|quantity|qty)/;
    const otpRe = /(otp|sms|code|kod|sifre|password|passcode|\bpin\b|token|verif|dogrula|onay)/;
    let otp = null, inputs = 0;
    const consider = [];
    if (otpSel) { for (const el of document.querySelectorAll(otpSel)) { if (visible(el)) { consider.push({el, score: -1}); } } }
    for (const el of document.querySelectorAll('input')) {
        const type = (el.getAttribute('type') || 'text').toLowerCase();
        if (!/^(text|tel|number|password)$/.test(type)) continue;
        if (!visible(el) || el.disabled || el.readOnly) continue;
        inputs++;
        const d = fold([el.name, el.id, el.getAttribute('placeholder'), el.getAttribute('aria-label'), el.getAttribute('autocomplete'), typeof el.className === 'string' ? el.className : ''].join(' '));
        if (cardRe.test(d) && !/(otp|sms|one-time-code)/.test(d)) continue;
        let score = null;
        if (/one-time-code/.test((el.getAttribute('autocomplete') || '').toLowerCase()) || otpRe.test(d)) score = 0;
        else if (el.maxLength >= 4 && el.maxLength <= 10) score = 1;
        else score = 2;
        consider.push({el, score});
    }
    consider.sort((a, b) => a.score - b.score);
    if (consider.length && (verifyText || consider[0].score <= 0)) {
        const c = consider[0];
        if (c.score <= 1 || consider.length === 1) otp = {id: mark(c.el), score: c.score};
    }
    return {verifyText, appText, errorText, otp, inputs, title: document.title || '', snippet: text.slice(0, 240), url: location.href};
}
"""

JS_CAPTCHA = r"""
() => !!document.querySelector('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], iframe[src*="challenges.cloudflare.com"], .g-recaptcha, .h-captcha, .cf-turnstile')
"""

JS_DIALOG_OPEN = "() => {" + _JS_FOLD + r"""
    const sel = '[role=dialog],[role=alertdialog],.modal.show,.modal.in,.modal[style*="display: block"],.swal2-popup,.sweet-alert,.jconfirm-box,.popup.active,.mfp-content,.fancybox-content';
    const els = Array.from(document.querySelectorAll(sel)).filter(visible);
    return els.length ? fold(els.map(e => e.innerText || '').join(' ')).slice(0, 400) : '';
}
"""

JS_CHALLENGE = r"""
() => {
    if (typeof window._cf_chl_opt !== 'undefined') { return true; }
    return !!document.querySelector('#challenge-form, #challenge-running, #cf-challenge-running, #challenge-stage, #cf-please-wait, #challenge-error-text');
}
"""


# ---------------------------------------------------------------- 数据结构
@dataclass
class CardInfo:
    """银行卡信息（只在内存中传递）"""
    number: str = ""
    holder: str = ""
    expiry: str = ""
    cvv: str = ""
    saved_last4: str = ""
    use_saved: bool = True

    @property
    def has_manual(self) -> bool:
        return bool(re.sub(r"\D", "", self.number)) and bool(parse_expiry(self.expiry)) and bool(self.cvv)


@dataclass
class Product:
    """商品页信息"""
    url: str
    title: str = ""
    price: Optional[float] = None
    in_stock: Optional[bool] = None
    face_value: Optional[int] = None


@dataclass
class PurchaseRequest:
    """一次购买请求"""
    amount: Optional[int] = None
    product_url: str = ""
    quantity: int = 1
    pay_mode: str = "balance"  # balance / card
    dry_run: bool = True
    max_total: float = 0.0
    card: CardInfo = field(default_factory=CardInfo)
    verify_timeout: int = 300
    known_codes: Sequence[str] = ()


@dataclass
class PurchaseResult:
    """一次购买的结果"""
    status: str
    message: str
    product: Optional[Product] = None
    total: Optional[float] = None
    balance: Optional[float] = None
    codes: List[str] = field(default_factory=list)
    order_url: str = ""
    paid: bool = False
    verification: str = ""
    steps: List[str] = field(default_factory=list)


class FlowError(Exception):
    """流程中断（附带结果状态）"""

    def __init__(self, status: str, message: str, paid: bool = False):
        super().__init__(message)
        self.status = status
        self.message = message
        self.paid = paid


class Interaction:
    """插件与流程之间的交互接口（3DS 验证码转发、取消）"""

    def verification_needed(self, kind: str, detail: str, attempt: int, screenshot: Optional[Path]) -> None:
        """需要用户完成验证：kind = otp（输入验证码）/ app（在银行 App 确认）/ unknown"""

    def verification_feedback(self, text: str) -> None:
        """验证过程中的提示（如验证码错误）"""

    def poll(self) -> Optional[str]:
        """非阻塞取用户输入：数字验证码 / 'ok' / 'cancel'；没有输入返回 None"""
        return None

    def cancelled(self) -> bool:
        return False


class StepRecorder:
    """每一步的截图与页面源码，保存在插件数据目录，便于排查和适配"""

    def __init__(self, directory: Optional[Path], log: Callable[[str], None]):
        self.directory = directory
        self.log = log
        self.index = 0
        self.steps: List[str] = []
        self.last_screenshot: Optional[Path] = None
        if directory:
            directory.mkdir(parents=True, exist_ok=True)

    def note(self, text: str):
        self.steps.append(text)
        self.log(text)
        if self.directory:
            try:
                with open(self.directory / "steps.log", "a", encoding="utf-8") as handle:
                    handle.write(f"{time.strftime('%H:%M:%S')} {text}\n")
            except Exception:  # noqa
                pass

    def snap(self, page: Any, name: str, html: bool = True, mask: Optional[List[Any]] = None) -> Optional[Path]:
        if not self.directory or page is None:
            return None
        self.index += 1
        stem = f"{self.index:02d}-{re.sub(r'[^a-zA-Z0-9_-]+', '_', name)[:40]}"
        shot = self.directory / f"{stem}.jpg"
        try:
            kwargs: Dict[str, Any] = {"path": str(shot), "type": "jpeg", "quality": 60, "full_page": True,
                                      "timeout": 15000}
            if mask:
                kwargs["mask"] = mask
            page.screenshot(**kwargs)
            self.last_screenshot = shot
        except Exception as err:
            self.log(f"截图失败（{name}）：{str(err)[:120]}")
            shot = None
        if html:
            try:
                frames = [frame.url for frame in page.frames]
                header = "<!-- url: %s\n frames:\n %s\n-->\n" % (page.url, "\n ".join(frames))
                (self.directory / f"{stem}.html").write_text(header + sanitize_html(page.content()), encoding="utf-8")
                for i, frame in enumerate(page.frames[1:6], start=1):
                    try:
                        if not frame.url or frame.url.startswith("about:"):
                            continue
                        (self.directory / f"{stem}.frame{i}.html").write_text(
                            f"<!-- url: {frame.url} -->\n" + sanitize_html(frame.content()), encoding="utf-8")
                    except Exception:  # noqa
                        continue
            except Exception as err:
                self.log(f"保存页面源码失败（{name}）：{str(err)[:120]}")
        return shot


def sanitize_html(content: str) -> str:
    """保存页面源码前去掉输入框里的值和像卡号的长数字，避免敏感信息落盘"""
    text = re.sub(r'(<input\b[^>]*?\bvalue=")([^"]*)(")', r"\1***\3", content or "", flags=re.I)
    text = re.sub(r"(<input\b[^>]*?\bvalue=')([^']*)(')", r"\1***\3", text, flags=re.I)
    return re.sub(r"(?<!\d)\d(?:[ -]?\d){12,18}(?!\d)", "[redacted]", text)


# ---------------------------------------------------------------- 站点操作
class OyunforShop:
    """在一个浏览器页面上完成 Oyunfor 的各个步骤"""

    def __init__(self, context: Any, page: Any, base_url: str = BASE_URL, log: Callable[[str], None] = print,
                 recorder: Optional[StepRecorder] = None, overrides: Optional[Dict[str, Any]] = None,
                 nav_timeout: int = 60, pause_scale: float = 1.0):
        self.context = context
        self.page = page
        self.base_url = base_url.rstrip("/")
        self.host = urlparse(self.base_url).hostname or ""
        self.log = log
        self.recorder = recorder or StepRecorder(None, log)
        self.overrides = overrides or {}
        self.nav_timeout = nav_timeout
        self.pause_scale = pause_scale
        # 填过卡信息的 frame 与输入框（截图时遮挡）
        self.card_frame: Any = None
        self.card_masks: List[Any] = []

    # ------------------------------------------------------------ 基础
    def note(self, text: str):
        self.recorder.note(text)

    def snap(self, name: str, html: bool = True) -> Optional[Path]:
        # 只遮挡还在页面上的卡信息输入框（支付页跳走后旧的 frame 已失效，带着它截图会失败）
        live = []
        for locator in self.card_masks:
            try:
                if locator.count():
                    live.append(locator)
            except Exception:  # noqa
                continue
        return self.recorder.snap(self.page, name, html=html, mask=live or None)

    def pause(self, low: float = 0.6, high: float = 1.4):
        seconds = random.uniform(low, high) * self.pause_scale
        if seconds > 0:
            try:
                self.page.wait_for_timeout(int(seconds * 1000))
            except Exception:  # noqa
                time.sleep(seconds)

    def url(self, path: str) -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        return urljoin(self.base_url + "/", path.lstrip("/"))

    def on_site(self, url: Optional[str] = None) -> bool:
        host = urlparse(url or self.page.url).hostname or ""
        return bool(host) and (host == self.host or host.endswith("." + self.host.split(".", 1)[-1]))

    def override_selectors(self, key: str) -> List[str]:
        value = self.overrides.get(key)
        if not value:
            return []
        if isinstance(value, str):
            return [value]
        return [str(item) for item in value if item]

    def adopt_new_page(self):
        """点击可能打开新标签页（如支付网关），自动切换到最新的页面"""
        try:
            pages = [p for p in self.context.pages if not p.is_closed()]
        except Exception:  # noqa
            return
        if pages and pages[-1] is not self.page:
            self.page = pages[-1]
            self.note(f"切换到新打开的页面：{self.page.url[:100]}")

    def goto(self, path: str, what: str = "") -> None:
        target = self.url(path)
        try:
            self.page.goto(target, wait_until="domcontentloaded", timeout=self.nav_timeout * 1000)
        except Exception as err:
            self.note(f"打开 {what or target} 时出现异常（继续）：{str(err)[:160]}")
        passed, reason = self.wait_challenge(self.nav_timeout)
        if not passed:
            self.snap("challenge")
            raise FlowError("blocked", reason)
        try:
            self.page.wait_for_load_state("load", timeout=15000)
        except Exception:  # noqa
            pass

    def page_text(self, frame: Any = None) -> Dict[str, str]:
        target = frame or self.page
        for _ in range(3):
            try:
                data = target.evaluate(JS_PAGE_TEXT)
                if isinstance(data, dict):
                    return data
            except Exception:  # noqa
                time.sleep(0.5)
        return {"text": "", "title": "", "url": ""}

    def find(self, key: str, frame: Any = None, **opts) -> Optional[Dict[str, Any]]:
        """按覆盖选择器 → 文本启发式的顺序找可点击元素"""
        target = frame or self.page
        for selector in self.override_selectors(key):
            try:
                locator = target.locator(selector).first
                if locator.count() and locator.is_visible():
                    marker = locator.evaluate("el => { if (!el.getAttribute('data-mpx')) "
                                              "el.setAttribute('data-mpx', 'c' + Math.random().toString(36).slice(2, 10)); "
                                              "return el.getAttribute('data-mpx'); }")
                    return {"id": marker, "text": f"custom:{selector}", "tag": "custom", "disabled": False}
            except Exception:  # noqa
                continue
        patterns = opts.pop("patterns", None) or TEXT_PATTERNS.get(key) or []
        if not patterns:
            return None
        try:
            return target.evaluate(JS_FIND_CLICKABLE, [patterns, opts])
        except Exception as err:
            self.log(f"查找元素 {key} 失败：{str(err)[:120]}")
            return None

    def click(self, found: Dict[str, Any], frame: Any = None, what: str = "") -> bool:
        target = frame or self.page
        locator = target.locator(f'[data-mpx="{found["id"]}"]').first
        try:
            locator.scroll_into_view_if_needed(timeout=5000)
        except Exception:  # noqa
            pass
        for force in (False, True):
            try:
                locator.click(timeout=10000, force=force)
                self.note(f"点击「{what or found.get('text')}」")
                return True
            except Exception as err:
                last = err
        try:
            locator.evaluate("el => el.click()")
            self.note(f"点击「{what or found.get('text')}」（脚本点击）")
            return True
        except Exception:  # noqa
            self.note(f"点击「{what or found.get('text')}」失败：{str(last)[:120]}")
            return False

    def find_and_click(self, key: str, what: str, frame: Any = None, **opts) -> bool:
        found = self.find(key, frame=frame, **opts)
        if not found:
            return False
        return self.click(found, frame=frame, what=f"{what}（{found.get('text')}）")

    def settle(self, timeout: float = 8.0):
        """等待点击后的跳转/请求结束"""
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=int(timeout * 1000))
        except Exception:  # noqa
            pass
        try:
            self.page.wait_for_load_state("networkidle", timeout=int(timeout * 1000))
        except Exception:  # noqa
            pass
        self.adopt_new_page()

    # ------------------------------------------------------------ Cloudflare
    def _challenge_state(self) -> Tuple[Optional[bool], str]:
        try:
            title = self.page.title() or ""
        except Exception:  # noqa
            return None, ""
        low = fold(title)
        if any(keyword in low for keyword in CHALLENGE_TITLES):
            return True, title
        try:
            return bool(self.page.evaluate(JS_CHALLENGE)), title
        except Exception:  # noqa
            return None, title

    def wait_challenge(self, timeout: int) -> Tuple[bool, str]:
        start = time.time()
        seen = False
        clear_hits = 0
        last_click = 0.0
        title = ""
        while time.time() - start < timeout:
            state, title = self._challenge_state()
            if state is False:
                clear_hits += 1
                if clear_hits >= 2 or not seen:
                    if seen:
                        self.note(f"已通过 Cloudflare 质询（{time.time() - start:.0f} 秒）")
                    return True, ""
            elif state:
                clear_hits = 0
                if not seen:
                    self.note(f"遇到 Cloudflare 质询（{title}），等待通过，最长 {timeout} 秒")
                seen = True
                low = fold(title)
                if "attention required" in low or "access denied" in low:
                    return False, f"出口 IP 被 Cloudflare 拒绝（{title}），请更换网络或代理"
                if time.time() - start > 5 and time.time() - last_click > 7 and self._click_turnstile():
                    last_click = time.time()
            try:
                self.page.wait_for_timeout(1000)
            except Exception:  # noqa
                time.sleep(1)
        return False, f"{timeout} 秒内未能通过 Cloudflare 质询（页面标题：{title or '未知'}）"

    def _click_turnstile(self) -> bool:
        try:
            for frame in self.page.frames:
                if "challenges.cloudflare.com" not in (frame.url or ""):
                    continue
                box = frame.frame_element().bounding_box()
                if box and box.get("width", 0) > 40 and box.get("height", 0) > 20:
                    x = box["x"] + min(30.0, box["width"] * 0.12)
                    y = box["y"] + box["height"] / 2
                    self.page.mouse.click(x, y, delay=random.randint(60, 140))
                    self.note("已尝试点击 Turnstile 验证框")
                    return True
        except Exception:  # noqa
            pass
        return False

    # ------------------------------------------------------------ 登录
    def is_logged_in(self) -> Optional[bool]:
        for selector in self.override_selectors("logged_in"):
            try:
                if self.page.locator(selector).first.count():
                    return True
            except Exception:  # noqa
                continue
        if self.find("logged_in", patterns=LOGGED_IN_TEXT, maxLen=30):
            return True
        if self.find("logged_out", patterns=LOGGED_OUT_TEXT, maxLen=30):
            return False
        # 菜单可能折叠着：再看链接地址
        try:
            return self.page.evaluate(r"""() => {
                const links = Array.from(document.querySelectorAll('a[href]')).map(a => (a.getAttribute('href') || '').toLowerCase());
                if (links.some(h => /(cikis|logout|signout|siparislerim|hesabim)/.test(h))) return true;
                if (links.some(h => /(\/giris|\/login|uye-girisi|\/kayit|register)/.test(h))) return false;
                return null;
            }""")
        except Exception:  # noqa
            return None

    def login(self, username: str, password: str, interaction: Optional[Interaction] = None,
              verify_timeout: int = 180) -> Tuple[bool, str]:
        """账号密码登录（尽力而为：遇到图形验证码直接放弃）"""
        opened = False
        override = self.overrides.get("login_url")
        if override:
            self.goto(override, "登录页")
            opened = True
        elif self.find_and_click("login_link", "登录入口", maxLen=30):
            self.settle()
            opened = bool(self._find_input("password"))
        if not opened:
            for path in LOGIN_PATHS:
                self.goto(path, "登录页")
                if self._find_input("password"):
                    opened = True
                    break
        password_input = self._find_input("password")
        if not password_input:
            self.snap("login-page")
            return False, "没有找到登录表单"
        email_input = self._find_input("email")
        if not email_input:
            self.snap("login-page")
            return False, "没有找到账号输入框"
        self._fill(email_input, username, "账号")
        self._fill(password_input, password, "密码", secret=True)
        captcha = False
        try:
            captcha = bool(self.page.evaluate(JS_CAPTCHA))
        except Exception:  # noqa
            pass
        self.snap("login-filled", html=False)
        submit = (self.find("login_submit", maxLen=30, scope="form:has(input[type=password])", selector=BUTTON_SELECTOR)
                  or self.find("login_submit", maxLen=30, selector="button,input[type=submit],[role=button]"))
        if not submit or not self.click(submit, what=f"登录（{submit.get('text')}）"):
            try:
                self.page.locator(f'[data-mpx="{password_input["id"]}"]').press("Enter")
            except Exception:  # noqa
                return False, "没有找到登录按钮"
        self.settle()
        self.pause(1.5, 2.5)
        state = self.is_logged_in()
        if state:
            return True, "登录成功"
        # 登录后要求短信验证码
        if interaction:
            verify = self._verify_frame_state()
            if verify and verify[1].get("otp"):
                ok = self._relay_codes(interaction, verify_timeout, purpose="登录验证")
                if ok:
                    self.settle()
                    if self.is_logged_in():
                        return True, "登录成功（已验证）"
        self.snap("login-result")
        if captcha:
            return False, "登录页有图形验证码，插件无法自动登录，请在浏览器登录后更新 Cookie"
        text = fold(self.page_text().get("text"))
        if re.search(r"(hatali|yanlis|gecersiz|incorrect|invalid)", text):
            return False, "账号或密码错误"
        return False, "登录后仍未检测到登录状态"

    # ------------------------------------------------------------ 信息读取
    def read_balance(self) -> Optional[float]:
        """读取页面上显示的账户余额（尽力而为）"""
        selectors = self.override_selectors("balance")
        texts: List[str] = []
        for selector in selectors:
            try:
                texts.append(self.page.locator(selector).first.inner_text(timeout=3000))
            except Exception:  # noqa
                continue
        if not texts:
            data = self.page_text()
            raw = data.get("text") or ""
            for line in raw.splitlines():
                if re.search(r"bakiye|balance", fold(line)) and find_money(line):
                    texts.append(line)
            if not texts:
                for match in re.finditer(r"(bakiye|balance)[^\n]{0,40}", raw, re.I):
                    texts.append(match.group(0))
                # 余额常在「Bakiye」下一行
                lines = raw.splitlines()
                for i, line in enumerate(lines):
                    if re.search(r"^\s*(bakiye(m|niz)?|balance)\s*:?\s*$", fold(line)) and i + 1 < len(lines):
                        texts.append(lines[i + 1])
        for text in texts:
            values = find_money(text)
            if values:
                return values[0]
        return None

    def resolve_product(self, amount: Optional[int], product_url: str = "") -> Product:
        """打开目标商品页"""
        if product_url:
            self.goto(product_url, "商品页")
        else:
            if not amount:
                raise FlowError("failed", "没有指定面额或商品链接")
            url = self._find_product_link(amount)
            self.goto(url, "商品页")
        product = self.inspect_product()
        if amount and product.face_value and product.face_value != amount:
            self.snap("product-mismatch")
            raise FlowError("failed", f"商品页面额为 {product.face_value} TL，与要买的 {amount} TL 不符，已停止")
        if amount and not product.face_value:
            product.face_value = amount
        return product

    def _find_product_link(self, amount: int) -> str:
        category = self.overrides.get("category_url") or CATEGORY_PATH
        self.goto(category, "礼品卡分类页")
        self.snap("category")
        pattern = amount_pattern(amount)
        slug = re.compile(rf"/{amount}-tl[-/]", re.I)
        try:
            links = self.page.evaluate(JS_FIND_LINKS, ["/apple-store/"]) or []
        except Exception:  # noqa
            links = []
        fallback = None
        for link in links:
            href = link.get("href") or ""
            if href.rstrip("/").endswith(CATEGORY_PATH):
                continue
            if pattern.search(link.get("text") or ""):
                self.note(f"在分类页找到 {amount} TL 商品：{href}")
                return href
            if fallback is None and slug.search(href):
                fallback = href
        if fallback:
            self.note(f"按链接规律找到 {amount} TL 商品：{fallback}")
            return fallback
        guess = f"{CATEGORY_PATH}/{PRODUCT_SLUG.format(amount=amount)}"
        self.note(f"分类页没有找到 {amount} TL 商品，按地址规律直接打开：{guess}")
        return guess

    def inspect_product(self) -> Product:
        price_sel = ",".join(self.override_selectors("price")) or None
        try:
            info = self.page.evaluate(JS_PRODUCT_INFO, [price_sel]) or {}
        except Exception:  # noqa
            info = {}
        title = (info.get("title") or "").strip()
        product = Product(url=self.page.url, title=title[:200])
        face = re.search(r"(?<![\d.,])(\d{2,5})(?:[.,]0{1,2})?\s*(?:tl|₺|try)(?![a-z0-9])", fold(title))
        if face and re.search(r"(app ?store|itunes|apple)", fold(title)):
            product.face_value = int(face.group(1))
        for item in info.get("prices") or []:
            raw = str(item.get("v") or "")
            values = find_money(raw)
            if not values and (item.get("src") != "class" or re.fullmatch(r"\s*[\d.,]+\s*", raw)):
                values = [parse_number(raw)]
            values = [v for v in values if v]
            if values:
                product.price = values[0]
                break
        text = fold(info.get("text"))
        if any(re.search(p, text) for p in OUT_OF_STOCK_TEXT):
            product.in_stock = False
        elif self.find("buy_now", maxLen=40) or self.find("add_to_cart", maxLen=40):
            product.in_stock = True
        self.note(f"商品：{product.title or '（无标题）'}；价格：{product.price if product.price is not None else '未识别'}；"
                  f"库存：{ {True: '有', False: '无', None: '未知'}[product.in_stock] }")
        return product

    def read_total(self) -> Optional[float]:
        for selector in self.override_selectors("total"):
            try:
                values = find_money(self.page.locator(selector).first.inner_text(timeout=3000))
                if values:
                    return values[0]
            except Exception:  # noqa
                continue
        try:
            lines = self.page.evaluate(JS_FIND_TOTAL) or []
        except Exception:  # noqa
            lines = []
        # 优先「Genel Toplam / Ödenecek Tutar」，其次「Toplam」
        ranked = sorted(lines, key=lambda s: (0 if re.search(r"(genel toplam|odenecek)", fold(s)) else 1, len(s)))
        for line in ranked:
            values = find_money(line)
            if values:
                return values[-1]
        return None

    # ------------------------------------------------------------ 下单
    def set_quantity(self, quantity: int) -> bool:
        if quantity <= 1:
            return True
        found = self._find_input("quantity")
        if not found:
            return False
        try:
            locator = self.page.locator(f'[data-mpx="{found["id"]}"]').first
            locator.fill(str(quantity))
            locator.dispatch_event("change")
            self.note(f"数量设为 {quantity}")
            return True
        except Exception as err:
            self.note(f"设置数量失败：{str(err)[:100]}")
            return False

    def open_cart(self) -> bool:
        """打开购物车页，返回是否像购物车"""
        cart = self.overrides.get("cart_url")
        if cart:
            self.goto(cart, "购物车")
        elif self.find_and_click("go_cart", "购物车", maxLen=30, exclude=[r"ekle"]):
            self.settle()
        else:
            self.goto("/sepet", "购物车")
        return self._looks_like_cart()

    def clear_cart(self) -> int:
        """清空购物车（尽力而为），避免演练或失败留下的商品混进本次订单"""
        if not self.open_cart():
            self.note("没有找到购物车页面，跳过清空购物车")
            return 0
        removed = 0
        icon_sel = ("button[class*=remove i],a[class*=remove i],button[class*=delete i],a[class*=delete i],"
                    "button[class*=trash i],a[class*=trash i],[data-action*=remove i],[data-action*=delete i]")
        for _ in range(12):
            found = self.find("remove_item", maxLen=25)
            if not found:
                try:
                    locator = self.page.locator(icon_sel).first
                    if locator.count() and locator.is_visible():
                        marker = locator.evaluate("el => { el.setAttribute('data-mpx', 'r' + Math.random().toString(36).slice(2, 9)); return el.getAttribute('data-mpx'); }")
                        found = {"id": marker, "text": "删除图标"}
                except Exception:  # noqa
                    found = None
            if not found:
                break
            if not self.click(found, what=f"移出购物车（{found.get('text')}）"):
                break
            removed += 1
            self.pause(0.8, 1.4)
            confirm = self.find("confirm_dialog", maxLen=20, scope=DIALOG_SCOPE)
            if confirm:
                self.click(confirm, what=f"确认删除（{confirm.get('text')}）")
            self.settle(6)
        if removed:
            self.note(f"购物车已清理 {removed} 次")
            self.snap("cart-cleared")
        return removed

    def start_checkout(self, quantity: int) -> None:
        """商品页 → （购物车）→ 结算页"""
        if quantity > 1 and not self.set_quantity(quantity):
            raise FlowError("failed", "商品页没有找到数量输入框，无法一次购买多张（可改为分多次购买）")
        if self.find_and_click("buy_now", "立即购买", maxLen=40):
            pass
        elif self.find_and_click("add_to_cart", "加入购物车", maxLen=40):
            pass
        else:
            self.snap("product-no-button")
            raise FlowError("failed", "商品页没有找到「Hemen Al / Sepete Ekle」按钮（可能缺货、未登录或页面结构不同）")
        self.settle()
        self.pause(1.0, 2.0)
        if not self._looks_like_checkout() and not self._looks_like_cart():
            # 还停在商品页（地址可能多了参数）：可能弹出了「已加入购物车」对话框
            found = (self.find("checkout", maxLen=40, scope=DIALOG_SCOPE)
                     or self.find("go_cart", maxLen=40, scope=DIALOG_SCOPE, exclude=[r"ekle"]))
            if found:
                self.snap("added-dialog")
                self.click(found, what=f"对话框（{found.get('text')}）")
                self.settle()
            else:
                self.open_cart()
            self.pause()
        # 购物车页：继续到结算
        for _ in range(3):
            if self._looks_like_checkout():
                break
            if not self._looks_like_cart():
                break
            self.snap("cart")
            if not self.find_and_click("checkout", "去结算", maxLen=40, exclude=[r"alisverise devam", r"continue shopping"]):
                break
            self.settle()
            self.pause()
        self.snap("checkout")
        if not self._looks_like_checkout():
            raise FlowError("failed", "没有进入结算页（没有找到付款方式）")

    def _looks_like_cart(self) -> bool:
        if re.search(r"(sepet|cart|basket)", self.page.url, re.I):
            return True
        return bool(self.find("checkout", maxLen=40, patterns=[r"^odemeye gec\b", r"^alisverisi tamamla\b"]))

    def _looks_like_checkout(self) -> bool:
        return bool(self.find("pay_balance", maxLen=80, selector=OPTION_SELECTOR, exclude=PAYMENT_EXCLUDE)
                    or self.find("pay_card", maxLen=80, selector=OPTION_SELECTOR, exclude=PAYMENT_EXCLUDE))

    def select_payment(self, mode: str) -> Tuple[str, Optional[Dict[str, Any]]]:
        """
        选择付款方式。
        :return: (选项文字, 付款按钮)。如果「付款方式」本身就是付款按钮（如「Bakiye ile Öde」按钮），
                 不点击，作为付款按钮返回，交给后面的演练/付款逻辑处理。
        """
        key = "pay_balance" if mode == "balance" else "pay_card"
        what = "余额支付" if mode == "balance" else "银行卡支付"
        found = self.find(key, maxLen=80, selector=OPTION_SELECTOR, exclude=PAYMENT_EXCLUDE)
        if not found:
            self.snap("payment-options")
            raise FlowError("failed", f"结算页没有找到「{what}」选项")
        text = found.get("text") or ""
        if found.get("tag") in ("button", "input", "a") and re.search(
                r"(\bode\b|odeme yap|satin al|tamamla|onayla|\bpay\b|complete)", text):
            self.note(f"「{text}」本身就是付款按钮")
            return text, found
        self.click(found, what=f"{what}（{text}）")
        self.pause(0.8, 1.6)
        self.settle(5)
        return text, None

    def tick_agreements(self) -> int:
        """勾选「已阅读并同意销售协议」等必选复选框"""
        script = "() => {" + _JS_FOLD + r"""
            let n = 0;
            for (const box of document.querySelectorAll('input[type=checkbox]')) {
                if (box.checked || box.disabled) continue;
                const lab = (box.id && document.querySelector('label[for="' + CSS.escape(box.id) + '"]')) || box.closest('label') || box.parentElement;
                const t = fold(lab ? lab.innerText : '');
                if (/(sozlesme|okudum|onayliyorum|kabul (ediyorum|ettim)|i agree|accept|terms|kosul)/.test(t) && !/(kaydet|save|hatirla|remember|bulten|newsletter|kampanya|sms ile|e-?posta ile)/.test(t)) {
                    box.click(); if (!box.checked) { box.checked = true; box.dispatchEvent(new Event('change', {bubbles: true})); }
                    n++;
                }
            }
            return n;
        }"""
        count = 0
        for frame in self._frames():
            try:
                count += int(frame.evaluate(script) or 0)
            except Exception:  # noqa
                continue
        if count:
            self.note(f"已勾选 {count} 个协议复选框")
        return count

    def find_pay_button(self, prefer: Any = None) -> Tuple[Any, Optional[Dict[str, Any]]]:
        frames = self._frames()
        if prefer is not None and prefer in frames:
            frames = [prefer] + [f for f in frames if f is not prefer]
        for frame in frames:
            found = self.find("pay_submit", frame=frame, maxLen=40, selector=BUTTON_SELECTOR,
                              exclude=[r"bakiye yukle", r"kupon", r"uygula"])
            if found and not found.get("disabled"):
                return frame, found
        return None, None

    # ------------------------------------------------------------ 银行卡
    def card_form_visible(self) -> bool:
        return any(self._find_input("number", frame) for frame in self._frames())

    def saved_card_option(self, last4: str = "") -> Tuple[Any, Optional[Dict[str, Any]]]:
        patterns = []
        if last4:
            patterns.append(rf"(\*|x|•|\.){{2,}}[ -]?{re.escape(last4)}\b")
            patterns.append(rf"\b{re.escape(last4)}\b.*(kart|card)")
        else:
            patterns.append(r"(\*|x|•){4}[ -]?\d{4}\b")
            patterns.append(r"kayitli kart")
        for frame in self._frames():
            found = self.find("saved_card", frame=frame, patterns=patterns, maxLen=120,
                              selector="label,input[type=radio],[role=radio],li,div[class*=card i],div[class*=kart i],button,a")
            if found:
                return frame, found
        return None, None

    def fill_card(self, card: CardInfo) -> str:
        """选择已保存的卡，或填写卡信息；返回使用方式"""
        if card.use_saved:
            frame, found = self.saved_card_option(card.saved_last4)
            if found:
                self.click(found, frame=frame, what=f"已保存的卡（{found.get('text')[:40]}）")
                self.card_frame = frame
                self.pause()
                # 已保存卡可能还要求 CVV
                if card.cvv:
                    for f in self._frames():
                        cvv = self._find_input("cvv", f)
                        if cvv:
                            self._fill(cvv, card.cvv, "CVV", frame=f, secret=True, digits=True)
                            break
                return "saved"
            if not card.has_manual:
                raise FlowError("failed", "没有找到已保存的卡，插件里也没有填写完整的卡信息")
            self.note("没有找到已保存的卡，改为填写卡信息")
        if not card.has_manual:
            raise FlowError("failed", "插件里没有填写完整的卡信息（卡号、有效期、CVV）")
        target = None
        for frame in self._frames():
            if self._find_input("number", frame):
                target = frame
                break
        if target is None:
            raise FlowError("failed", "没有找到卡号输入框")
        self.card_frame = target
        number = re.sub(r"\D", "", card.number)
        month, year = parse_expiry(card.expiry)
        self._fill(self._find_input("number", target), number, "卡号", frame=target, secret=True, digits=True)
        holder = self._find_input("holder", target)
        if holder and card.holder:
            self._fill(holder, card.holder, "持卡人", frame=target)
        expiry = self._find_input("expiry", target)
        if expiry:
            self._fill_expiry(expiry, month, year, target)
        else:
            month_el = self._find_input("month", target)
            year_el = self._find_input("year", target)
            if not month_el or not year_el:
                raise FlowError("failed", "没有找到有效期输入框")
            self._fill_part(month_el, [month, str(int(month))], target, "有效期月")
            self._fill_part(year_el, [year, year[2:]], target, "有效期年")
        cvv = self._find_input("cvv", target)
        if not cvv:
            raise FlowError("failed", "没有找到 CVV 输入框")
        self._fill(cvv, card.cvv, "CVV", frame=target, secret=True, digits=True)
        return "manual"

    def _frames(self) -> List[Any]:
        try:
            return [self.page.main_frame] + [f for f in self.page.frames if f is not self.page.main_frame]
        except Exception:  # noqa
            return [self.page]

    def _find_input(self, kind: str, frame: Any = None) -> Optional[Dict[str, Any]]:
        target = frame or self.page
        override = ",".join(self.override_selectors(f"{kind}_input" if kind in ("email", "password", "quantity")
                                                    else f"card_{kind}")) or None
        try:
            found = target.evaluate(JS_FIND_INPUT, [kind, override])
        except Exception:  # noqa
            return None
        return found or None

    def _fill(self, found: Optional[Dict[str, Any]], value: str, what: str, frame: Any = None,
              secret: bool = False, digits: bool = False):
        if not found:
            raise FlowError("failed", f"没有找到「{what}」输入框")
        target = frame or self.page
        locator = target.locator(f'[data-mpx="{found["id"]}"]').first
        if secret:
            self.card_masks.append(locator)
        try:
            locator.click(timeout=5000)
        except Exception:  # noqa
            pass
        ok = False
        try:
            locator.fill(value, timeout=8000)
            current = locator.input_value(timeout=3000)
            ok = (re.sub(r"\D", "", current) == re.sub(r"\D", "", value)) if digits else (current.strip() == value.strip())
        except Exception:  # noqa
            ok = False
        if not ok:
            # 带格式化脚本的输入框：清空后逐字输入
            try:
                locator.fill("", timeout=3000)
                locator.press_sequentially(value, delay=random.randint(40, 90))
                current = locator.input_value(timeout=3000)
                ok = (re.sub(r"\D", "", current) == re.sub(r"\D", "", value)) if digits else bool(current)
            except Exception as err:
                raise FlowError("failed", f"填写「{what}」失败：{str(err)[:100]}")
        if not ok:
            raise FlowError("failed", f"填写「{what}」后校验不一致")
        self.note(f"已填写{what}" + ("" if secret else f"：{value}"))

    def _fill_expiry(self, found: Dict[str, Any], month: str, year: str, frame: Any):
        locator = frame.locator(f'[data-mpx="{found["id"]}"]').first
        self.card_masks.append(locator)
        maxlength = int(found.get("maxlength") or -1)
        candidates = [f"{month}/{year[2:]}", f"{month}{year[2:]}"] if maxlength in (-1, 5, 7) or maxlength > 5 \
            else [f"{month}{year[2:]}", f"{month}/{year[2:]}"]
        for value in candidates:
            try:
                locator.fill("", timeout=3000)
                locator.press_sequentially(value, delay=random.randint(40, 90))
                current = re.sub(r"\D", "", locator.input_value(timeout=3000))
                if current in (f"{month}{year[2:]}", f"{month}{year}"):
                    self.note("已填写有效期")
                    return
            except Exception:  # noqa
                continue
        raise FlowError("failed", "填写有效期失败")

    def _fill_part(self, found: Dict[str, Any], values: List[str], frame: Any, what: str):
        locator = frame.locator(f'[data-mpx="{found["id"]}"]').first
        if found.get("tag") == "select":
            options = locator.evaluate("el => Array.from(el.options).map(o => [o.value, o.text])")
            for value in values:
                for opt_value, opt_text in options:
                    if opt_value.strip() == value or opt_text.strip() == value:
                        locator.select_option(value=opt_value)
                        self.note(f"已选择{what}")
                        return
            raise FlowError("failed", f"{what}下拉框里没有 {values[0]}")
        self._fill(found, values[0] if int(found.get("maxlength") or -1) != 2 else values[0][-2:], what, frame=frame,
                   digits=True)

    # ------------------------------------------------------------ 提交后：结果 / 3DS
    def _verify_frame_state(self, ignore: Optional[Dict[str, Any]] = None) -> Optional[Tuple[Any, Dict[str, Any]]]:
        """
        在所有 frame 里找验证页面（3DS / 短信确认）
        :param ignore: 点击付款前就已存在的 {frame 地址: 输入框标记}，避免把结算页上的输入框当成验证码框
        """
        otp_sel = ",".join(self.override_selectors("otp_input")) or None
        best = None
        for frame in self._frames():
            try:
                state = frame.evaluate(JS_VERIFY_STATE, [otp_sel])
            except Exception:  # noqa
                continue
            if not isinstance(state, dict):
                continue
            if ignore and state.get("otp") and ignore.get(frame.url) == state["otp"].get("id"):
                state["otp"] = None
            off_site = not self.on_site(frame.url or "")
            if frame is self.card_frame and not state.get("otp"):
                # 银行卡表单所在的网关页本身会提到 3D Secure，不算验证页
                off_site = off_site and not self.card_form_in(frame)
            relevant = state.get("otp") or (off_site and (state.get("verifyText") or state.get("appText")))
            if not relevant:
                continue
            rank = 0 if state.get("otp") else 1
            if best is None or rank < best[0]:
                best = (rank, frame, state)
        if not best:
            return None
        return best[1], best[2]

    def card_form_in(self, frame: Any) -> bool:
        return bool(self._find_input("number", frame))

    def _outcome_hits(self, frame: Any) -> Tuple[List[Tuple[str, str]], str, List[str]]:
        """返回 frame 里命中的成功/失败文字、地址、兑换码"""
        url = frame.url or ""
        data = self.page_text(frame)
        text = fold(data.get("text"))
        hits: List[Tuple[str, str]] = []
        for pattern in FAIL_TEXT:
            matched = re.search(pattern, text)
            if matched:
                hits.append((OUT_FAILED, matched.group(0)))
        for pattern in SUCCESS_TEXT:
            matched = re.search(pattern, text)
            if matched:
                hits.append((OUT_SUCCESS, matched.group(0)))
        return hits, url, extract_codes(data.get("text"), self.overrides.get("code_regex"))

    def outcome_baseline(self) -> Dict[str, Any]:
        """点击付款前页面上就有的文字/兑换码/输入框，之后不再当作结果"""
        hits, _, codes = self._outcome_hits(self.page.main_frame)
        otp = {}
        for frame in self._frames():
            try:
                state = frame.evaluate(JS_VERIFY_STATE, [None])
                if isinstance(state, dict) and state.get("otp"):
                    otp[frame.url] = state["otp"].get("id")
            except Exception:  # noqa
                continue
        return {"url": self.page.url, "hits": set(hits), "codes": set(codes), "otp": otp}

    def _outcome_state(self, baseline: Optional[Dict[str, Any]] = None) -> Tuple[Optional[str], str]:
        """检查是否已出现成功/失败页面"""
        baseline = baseline or {"url": "", "hits": set(), "codes": set()}
        for frame in self._frames():
            url = frame.url or ""
            if frame is not self.page.main_frame and not self.on_site(url):
                continue
            hits, url, codes = self._outcome_hits(frame)
            same_page = frame is self.page.main_frame and url == baseline.get("url")
            if same_page:
                hits = [hit for hit in hits if hit not in baseline.get("hits", set())]
                codes = [code for code in codes if code not in baseline.get("codes", set())]
            for kind, detail in hits:
                if kind == OUT_FAILED:
                    return OUT_FAILED, detail
            if self.on_site(url) and not same_page and re.search(FAIL_URL, url, re.I):
                return OUT_FAILED, f"跳转到失败页：{url[:100]}"
            for kind, detail in hits:
                if kind == OUT_SUCCESS:
                    return OUT_SUCCESS, detail
            if self.on_site(url) and not same_page and re.search(SUCCESS_URL, url, re.I):
                return OUT_SUCCESS, f"跳转到成功页：{url[:100]}"
            if codes and frame is self.page.main_frame:
                return OUT_SUCCESS, "页面上出现了兑换码"
        return None, ""

    def submit_and_wait(self, frame: Any, button: Dict[str, Any], interaction: Interaction,
                        verify_timeout: int, settle_timeout: int = 90) -> Tuple[str, str, str]:
        """
        点击最终付款按钮，并等待结果；途中出现 3DS/短信验证时转给用户。
        :return: (结果, 说明, 验证方式)
        """
        start_url = self.page.url
        baseline = self.outcome_baseline()
        if not self.click(button, frame=frame, what=f"付款（{button.get('text')}）"):
            raise FlowError("failed", "点击付款按钮失败")
        verification = ""
        verify_started = None
        verify_notified_kind = ""
        attempts = 0
        last_submit = 0.0
        last_feedback = 0.0
        no_input_hint = 0.0
        pending_code = ""
        deadline = time.time() + settle_timeout
        while True:
            now = time.time()
            if verify_started is None and now > deadline:
                self.snap("timeout")
                return OUT_UNKNOWN, "提交后长时间没有出现结果页", verification
            if verify_started is not None and now - verify_started > verify_timeout:
                self.snap("verify-timeout")
                return OUT_TIMEOUT, f"{verify_timeout} 秒内没有完成验证", verification
            self.adopt_new_page()
            command = interaction.poll()
            if command == "cancel" or interaction.cancelled():
                self.snap("cancelled")
                return OUT_CANCELLED, "已按你的指令取消", verification
            if command and command != "ok":
                # 验证码可能比验证页先到：先存着，等出现输入框再填
                pending_code = command

            outcome, detail = self._outcome_state(baseline)
            if outcome:
                self.snap(f"result-{outcome}")
                return outcome, detail, verification

            verify = self._verify_frame_state(ignore=baseline.get("otp"))
            if verify:
                vframe, vstate = verify
                kind = "otp" if vstate.get("otp") else ("app" if vstate.get("appText") else "unknown")
                if verify_started is None:
                    verify_started = time.time()
                    deadline = verify_started + verify_timeout + settle_timeout
                if kind != verify_notified_kind:
                    verify_notified_kind = kind
                    verification = verification or kind
                    shot = self.snap(f"verify-{kind}")
                    self.note(f"需要验证（{kind}）：{vstate.get('snippet', '')[:120]}")
                    interaction.verification_needed(kind, vstate.get("snippet", ""), attempts, shot)
                if pending_code:
                    if vstate.get("otp"):
                        attempts += 1
                        self._submit_otp(vframe, vstate["otp"], pending_code)
                        pending_code = ""
                        last_submit = time.time()
                        self.snap(f"otp-submitted-{attempts}", html=False)
                        if attempts >= 5:
                            self.note("验证码已提交 5 次")
                    elif now - no_input_hint > 10:
                        no_input_hint = now
                        pending_code = ""
                        interaction.verification_feedback("当前验证页没有验证码输入框，请在银行 App 中确认")
                elif command == "ok":
                    self.note("用户表示已在 App 中确认，继续等待页面跳转")
                    found = self.find("otp_submit", frame=vframe, maxLen=30,
                                      selector="button,input[type=submit],input[type=button],a,[role=button]")
                    if found and kind != "otp":
                        self.click(found, frame=vframe, what="继续")
                if (vstate.get("errorText") and last_submit and time.time() - last_submit > 4
                        and last_submit > last_feedback):
                    last_feedback = time.time()
                    interaction.verification_feedback("验证页提示验证码错误或已过期，请重新发送")
            elif verify_started is None and self.on_site() and self.page.url == start_url:
                # 余额支付可能弹出确认框
                dialog = None
                try:
                    dialog = self.page.evaluate(JS_DIALOG_OPEN)
                except Exception:  # noqa
                    dialog = None
                if dialog:
                    found = self.find("confirm_dialog", maxLen=20,
                                      scope='[role=dialog],[role=alertdialog],.modal.show,.modal.in,.swal2-popup,.sweet-alert,.jconfirm-box,.popup.active,.mfp-content,.fancybox-content')
                    if found:
                        self.snap("confirm-dialog")
                        self.click(found, what=f"确认对话框（{found.get('text')}）")
            try:
                self.page.wait_for_timeout(1000)
            except Exception:  # noqa
                time.sleep(1)

    def _submit_otp(self, frame: Any, otp: Dict[str, Any], code: str):
        locator = frame.locator(f'[data-mpx="{otp["id"]}"]').first
        try:
            locator.fill("", timeout=3000)
        except Exception:  # noqa
            pass
        try:
            locator.press_sequentially(code, delay=random.randint(60, 120))
        except Exception:  # noqa
            locator.fill(code)
        self.note("已填入验证码")
        self.pause(0.4, 0.9)
        found = self.find("otp_submit", frame=frame, maxLen=30,
                          selector="button,input[type=submit],input[type=button],a,[role=button]")
        if found:
            self.click(found, frame=frame, what=f"提交验证码（{found.get('text')}）")
        else:
            locator.press("Enter")
            self.note("按回车提交验证码")

    def _relay_codes(self, interaction: Interaction, timeout: int, purpose: str) -> bool:
        """登录等非付款场景的验证码转发：返回是否已提交"""
        start = time.time()
        notified = False
        while time.time() - start < timeout:
            verify = self._verify_frame_state()
            if not verify:
                return notified
            frame, state = verify
            if not notified:
                shot = self.snap(f"verify-{purpose}")
                interaction.verification_needed("otp", f"{purpose}：{state.get('snippet', '')}", 0, shot)
                notified = True
            command = interaction.poll()
            if command == "cancel":
                return False
            if command and command != "ok" and state.get("otp"):
                self._submit_otp(frame, state["otp"], command)
                self.settle()
                return True
            time.sleep(1)
        return False

    # ------------------------------------------------------------ 兑换码
    def scan_codes(self) -> List[str]:
        codes: List[str] = []
        custom = self.overrides.get("code_regex")
        for frame in self._frames():
            if frame is not self.page.main_frame and not self.on_site(frame.url or ""):
                continue
            text = self.page_text(frame).get("text") or ""
            try:
                text += "\n" + (frame.evaluate(JS_INPUT_VALUES) or "")
            except Exception:  # noqa
                pass
            for code in extract_codes(text, custom):
                if code not in codes:
                    codes.append(code)
        return codes

    def reveal_codes(self) -> int:
        clicked = 0
        for _ in range(6):
            found = self.find("reveal_code", maxLen=30, skipMarked=True)
            if not found:
                break
            try:
                self.page.locator(f'[data-mpx="{found["id"]}"]').first.evaluate(
                    "el => el.setAttribute('data-mpx-skip', '1')")
            except Exception:  # noqa
                pass
            if not self.click(found, what=f"显示兑换码（{found.get('text')}）"):
                break
            clicked += 1
            self.pause(1.0, 1.8)
            self.settle(5)
        return clicked

    def collect_codes(self, known: Iterable[str], quantity: int = 1) -> Tuple[List[str], str]:
        """在结果页/订单页找新的兑换码"""
        known_set = set(known or [])

        def fresh(items: List[str]) -> List[str]:
            return [c for c in items if c not in known_set]

        codes = fresh(self.scan_codes())
        if not codes and self.reveal_codes():
            codes = fresh(self.scan_codes())
            self.snap("codes-revealed")
        if codes:
            return codes, self.page.url
        # 去订单页：打开最新一笔订单
        self.note("结果页没有兑换码，去订单页查找")
        orders_url = self.overrides.get("orders_url")
        opened = False
        if orders_url:
            self.goto(orders_url, "订单页")
            opened = True
        elif self.find_and_click("orders_link", "我的订单", maxLen=30):
            self.settle()
            opened = True
        else:
            self.goto("/", "首页")
            if self.find_and_click("orders_link", "我的订单", maxLen=30):
                self.settle()
                opened = True
        if not opened or not self._page_ok():
            for path in ORDERS_PATHS:
                self.goto(path, "订单页")
                if self._page_ok() and re.search(r"siparis|order", fold(self.page_text().get("text"))):
                    opened = True
                    break
        self.snap("orders")
        codes: List[str] = []
        # 先打开最新一笔订单的详情（列表页可能显示历史订单的兑换码，不能直接拿列表里的）
        found = self.find("order_detail", maxLen=40)
        if found and self.click(found, what=f"最新订单（{found.get('text')}）"):
            self.settle()
            self.pause()
            codes = fresh(self.scan_codes())
            if not codes:
                self.reveal_codes()
                codes = fresh(self.scan_codes())
            self.snap("order-detail")
        else:
            self.reveal_codes()
            codes = fresh(self.scan_codes())
            self.snap("orders-revealed")
        # 订单页可能列出很多历史兑换码：只取最前面的 quantity 个新码
        return codes[:max(1, quantity)], self.page.url

    def _page_ok(self) -> bool:
        data = self.page_text()
        title = fold(data.get("title"))
        return not re.search(r"(404|bulunamadi|not found|hata)", title)


# ---------------------------------------------------------------- 整体流程
def run_purchase(shop: OyunforShop, request: PurchaseRequest, interaction: Interaction,
                 login: Optional[Tuple[str, str]] = None, clear_cart: bool = True) -> PurchaseResult:
    """执行一次购买（或演练）"""
    result = PurchaseResult(status="failed", message="")
    paid = False
    try:
        shop.goto("/", "首页")
        logged = shop.is_logged_in()
        shop.snap("home")
        if logged is False:
            if login and login[0] and login[1]:
                shop.note("未登录，尝试用账号密码登录")
                ok, reason = shop.login(login[0], login[1], interaction=interaction)
                if not ok:
                    raise FlowError("login_required", reason)
                shop.note(reason)
            elif logged is False:
                raise FlowError("login_required", "Cookie 已失效（页面显示未登录），请重新复制 Cookie")
        elif logged is None:
            shop.note("无法确认登录状态，继续")
        result.balance = shop.read_balance()
        if result.balance is not None:
            shop.note(f"账户余额：{result.balance:.2f} TL")
        if clear_cart:
            shop.clear_cart()

        product = shop.resolve_product(request.amount, request.product_url)
        result.product = product
        shop.snap("product")
        if product.in_stock is False:
            raise FlowError("out_of_stock", "商品缺货")
        quantity = max(1, int(request.quantity or 1))
        if product.price is not None and request.max_total and product.price * quantity > request.max_total + 0.01:
            raise FlowError("limit", f"商品价格 {product.price:.2f} TL × {quantity} 超过单笔上限 {request.max_total:.2f} TL")
        if (request.pay_mode == "balance" and result.balance is not None and product.price is not None
                and result.balance + 0.01 < product.price * quantity):
            raise FlowError("insufficient", f"余额 {result.balance:.2f} TL 不足以支付 {product.price * quantity:.2f} TL")

        shop.start_checkout(quantity)
        option, option_button = shop.select_payment(request.pay_mode)
        shop.note(f"付款方式：{option}")
        shop.tick_agreements()

        total = shop.read_total()
        result.total = total
        if total is not None:
            shop.note(f"结算页应付金额：{total:.2f} TL")
            if request.max_total and total > request.max_total + 0.01:
                raise FlowError("limit", f"应付 {total:.2f} TL 超过单笔上限 {request.max_total:.2f} TL")
            if product.price is not None and total > product.price * quantity * 1.15 + 5:
                raise FlowError("failed", f"应付 {total:.2f} TL 明显高于商品价格 × 数量，购物车里可能有其他商品，已停止")
        else:
            shop.note("没有识别出结算页的应付金额")
            if request.max_total and product.price is None:
                raise FlowError("failed", "商品价格和应付金额都没有识别出来，无法校验单笔上限，已停止。"
                                          "可在「选择器覆盖」里填写 price 或 total")

        pay_frame, pay_button = None, None
        if request.pay_mode == "card":
            last4 = request.card.saved_last4
            if not shop.card_form_visible() and not shop.saved_card_option(last4)[1]:
                # 结算页上没有卡表单：先进入支付网关。此时还没提交任何卡信息，不会扣款。
                frame, button = (shop.page.main_frame, option_button) if option_button else shop.find_pay_button()
                if not button:
                    shop.snap("no-pay-button")
                    raise FlowError("failed", "结算页没有找到进入支付的按钮")
                shop.snap("before-gateway")
                shop.click(button, frame=frame, what=f"进入支付页（{button.get('text')}）")
                shop.settle(15)
                for _ in range(20):
                    if shop.card_form_visible() or shop.saved_card_option(last4)[1]:
                        break
                    if shop._verify_frame_state():
                        raise FlowError("unknown", "进入支付页后直接出现了银行验证（可能自动使用了已保存的卡），"
                                                   "插件已停止，请不要在银行 App 中确认这笔交易", paid=True)
                    shop.pause(0.8, 1.2)
                shop.tick_agreements()
            shop.snap("gateway")
            if not shop.card_form_visible() and not shop.saved_card_option(last4)[1]:
                raise FlowError("failed", "没有找到银行卡表单或已保存的卡")
            if request.dry_run:
                raise FlowError("dry_run", "演练模式：已到达银行卡页面，未填写卡信息、未付款")
            how = shop.fill_card(request.card)
            shop.note("使用已保存的卡" if how == "saved" else "卡信息已填写")
            shop.tick_agreements()
            pay_frame, pay_button = shop.find_pay_button(prefer=shop.card_frame)
        elif option_button:
            pay_frame, pay_button = shop.page.main_frame, option_button
        else:
            pay_frame, pay_button = shop.find_pay_button()

        if not pay_button:
            shop.snap("no-pay-button")
            raise FlowError("failed", "没有找到最终的付款按钮")
        shop.note(f"找到付款按钮：{pay_button.get('text')}")
        if request.dry_run:
            shop.snap("dry-run")
            raise FlowError("dry_run", f"演练模式：已到达付款前一步（付款按钮「{pay_button.get('text')}」），未付款")
        if request.pay_mode != "card":
            shop.snap("before-pay")
        paid = True
        outcome, detail, verification = shop.submit_and_wait(pay_frame, pay_button, interaction,
                                                             request.verify_timeout)
        result.verification = verification
        if outcome == OUT_SUCCESS:
            shop.note(f"付款成功：{detail}")
            codes, order_url = shop.collect_codes(request.known_codes, quantity)
            result.codes = codes
            result.order_url = order_url
            if codes:
                result.status, result.message = "success", "购买成功"
            else:
                result.status, result.message = "paid_no_code", "付款成功，但没有自动找到兑换码，请到 Oyunfor 订单页或邮箱查看"
        elif outcome == OUT_FAILED:
            result.status, result.message = "failed", f"付款失败：{detail}"
            paid = False
        elif outcome == OUT_CANCELLED:
            result.status, result.message = "cancelled", "已取消（没有完成验证的交易不会扣款，如有疑问请到订单页核对）"
            paid = False
        elif outcome == OUT_TIMEOUT:
            result.status, result.message = "timeout", f"{detail}，已放弃这笔交易（之后不要再在银行 App 里确认它）"
            paid = False
        else:
            result.status, result.message = "unknown", f"{detail}，结果未知，请到 Oyunfor 订单页核对，不要重复购买"
    except FlowError as err:
        result.status = err.status
        result.message = err.message
        paid = paid or err.paid
        if err.status not in ("dry_run",):
            shop.snap(f"stop-{err.status}")
    except Exception as err:  # noqa
        result.status = "unknown" if paid else "error"
        result.message = (f"付款后出现异常：{type(err).__name__}: {str(err)[:160]}，请到订单页核对，不要重复购买" if paid
                          else f"{type(err).__name__}: {str(err)[:200]}")
        shop.snap("exception")
    result.paid = paid and result.status in ("success", "paid_no_code", "unknown")
    result.steps = list(shop.recorder.steps)
    return result


def check_account(shop: OyunforShop, login: Optional[Tuple[str, str]] = None,
                  interaction: Optional[Interaction] = None) -> Dict[str, Any]:
    """只检查登录状态和余额"""
    shop.goto("/", "首页")
    logged = shop.is_logged_in()
    message = ""
    if logged is False and login and login[0] and login[1]:
        ok, message = shop.login(login[0], login[1], interaction=interaction)
        logged = True if ok else logged
    shop.snap("account")
    return {"logged_in": logged, "balance": shop.read_balance() if logged is not False else None, "message": message}


def load_overrides(raw: Optional[str]) -> Tuple[Dict[str, Any], str]:
    """解析用户填写的选择器覆盖 JSON"""
    text = (raw or "").strip()
    if not text:
        return {}, ""
    try:
        data = json.loads(text)
    except Exception as err:  # noqa
        return {}, f"选择器覆盖不是合法 JSON：{err}"
    if not isinstance(data, dict):
        return {}, "选择器覆盖必须是 JSON 对象"
    return data, ""
