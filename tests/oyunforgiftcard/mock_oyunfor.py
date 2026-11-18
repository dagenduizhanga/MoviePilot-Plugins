"""
本地模拟的 Oyunfor + 支付网关 + 银行 3DS 页面（仅供离线测试）。

页面结构是按土耳其电商常见写法臆造的，用来验证插件的流程、启发式定位和 3DS 转发逻辑，
不代表 oyunfor.com 的真实页面。
- 主站：http://127.0.0.1:PORT
- 支付网关 / 银行验证页：http://localhost:PORT（与主站不同源，模拟跨域 iframe）
"""
import html
import json
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

LOCK = threading.RLock()
# 列表页结构仿照真实分类页截图：卡片 = 标题 + 价格（100.00 TL）+ DETAY + SEPETE EKLE，
# 第一张是「100 - 2000 TL」自定义金额商品（Tutar giriniz. 输入框）
PRODUCTS = {
    "custom": {"slug": "100-2000-tl-app-store-card", "title": "100 - 2000 TL App Store Card",
               "listing": "100 - 2000 TL App Store Card", "price": 0.0},
    "2500": {"slug": "2500-tl-tutarinda-app-store-itunes-hediye-karti", "listing": "2500 TL App Store Card",
             "title": "2500 TL Tutarında App Store & iTunes Hediye Kartı", "price": 2600.0},
    "250": {"slug": "250-tl-tutarinda-app-store-itunes-hediye-karti", "listing": "250 TL App Store Card",
            "title": "250 TL Tutarında App Store & iTunes Hediye Kartı", "price": 262.5},
    "100": {"slug": "100-tl-tutarinda-app-store-itunes-hediye-karti", "listing": "100 TL App Store Card",
            "title": "100 TL Tutarında App Store & iTunes Hediye Kartı", "price": 105.0},
    # 与真实站点一样：链接写 1000，页面却是 2000 TL
    "1000": {"slug": "1000-tl-tutarinda-app-store-itunes-hediye-karti", "listing": "2000 TL App Store Card",
             "title": "2000 TL Tutarında App Store & iTunes Hediye Kartı", "price": 2050.0},
}
CATEGORY = "/apple-store/apple-store-itunes-gift-card"
# 真实站点每个面额都有固定的商品编号（data-id），「SEPETE EKLE」就是用它 POST /basket/addBasket
PRODUCT_IDS = {"custom": "10161", "100": "10159", "250": "10160", "2500": "19990", "1000": "11812"}
# 仿 Cloudflare 520 错误页（源服务器返回了异常响应）
CF_520_PAGE = """<!doctype html><html lang="en-US"><head><meta charset="utf-8">
<title>oyunfor.com | 520: Web server is returning an unknown error</title></head><body>
<div id="cf-wrapper"><div id="cf-error-details" class="p-0"><header class="mx-auto pt-10">
<h1><span class="inline-block">Web server is returning an unknown error</span>
<span class="code-label">Error code 520</span></h1>
<div>Visit <a href="https://www.cloudflare.com/5xx-error-landing">cloudflare.com</a> for more information.</div></header>
<div class="cf-section">Browser Working · <a href="https://www.cloudflare.com/5xx-error-landing">Cloudflare</a> Working · oyunfor.com Host Error</div>
<p>Your IP: <button type="button" class="cf-footer-ip-reveal-btn">Click to reveal</button></p></div></div></body></html>"""
GOOD_CARD = {"number": "4242424242424242", "month": "12", "year": "2030", "cvv": "123"}
OTP = "123456"
LOGIN_SMS = "654321"


def _default_state():
    return {
        "balance": 1500.0,
        "stock": True,
        "acs_mode": "otp",            # otp / app / none
        "require_sms_login": False,
        "login_code_style": "email",   # email / boxes
        "success_shows_code": True,
        "checkout_style": "radios",   # radios / buttons
        "show_buy_now": True,
        "cart_row_style": "plain",      # plain / real（真实购物车：右上角 ×、Adet 数量框带 −/+）
        "add_qty": 1,                   # 列表上点一次加入购物车加几件（模拟数量异常）
        "cart_path": "/sepet",          # 购物车地址
        "header_cart": "text",          # text / icon（只有购物车图标和数量）
        "cart_button": "odemeye_gec",   # odemeye_gec / satinal_link / satinal_submit / tamamla_submit / oyunfor
        "cf_error_path": CATEGORY,      # 这个地址接下来 cf_error_left 次返回 Cloudflare 520 错误页
        "cf_error_left": 0,
        "balance_label": "Bakiye ile Öde",
        "listing_style": "plain",      # plain / ajax / nobuttons / empty / oyunfor（真实写法：div.addToCart + data-id）
        "binance_mode": "onsite",      # onsite / hosted
        "binance_autoredirect": True,
        "binance_paid": [],
        "add_redirect_ms": 0,           # ajax 列表：加入购物车成功后过多少毫秒网站自己跳到购物车（真实站点约 1 秒；0 = 不跳）
        "gateway_delay": 0,
        "gateway_phone_otp": False,     # PayTR 2026：先要短信验证手机号才显示已保存的卡（不认识的浏览器）
        "gateway_loading_text": False,  # PayTR 2026：卡片出来之前显示「doğrulanıyor」之类的加载提示
        "paytr_otp_sent": 0,            # PayTR 手机验证码被请求了几次             # PayTR 2026 页面：已保存的卡过多少毫秒才画出来（真实约 9 秒）
        "slow_font": 0,                 # 每个页面都引用一个要下载这么多秒的网页字体（截图会等字体）
        "binance_qr_delay": 0,          # 币安收银台的二维码晚多少毫秒才出现（真实页面链接先出、二维码后画）
        "saved_card": False,
        "gateway_style": "classic",     # classic / paytr2026（真实 PayTR 新页面：Tailwind class、Radix 单选、type=button 付款按钮）
        "cart": [],
        "orders": {1000: {"id": 1000, "product": "100", "qty": 1, "total": 105.0, "status": "Tamamlandı",
                          "codes": ["XOLDCODE00000001"], "method": "bakiye"}},
        "next_order": 1001,
        "approved": [],
        "sessions": ["VALID"],
        "log": [],
        "otp_attempts": 0,
        "acs_requested": [],
    }


STATE = _default_state()


def make_codes(order_id, qty):
    return [f"X{order_id:05d}QZ{i + 1}ABCDEFG"[:16] for i in range(qty)]


def money(value):
    whole, frac = f"{value:,.2f}".split(".")
    return whole.replace(",", ".") + "," + frac


class Handler(BaseHTTPRequestHandler):
    server_version = "MockOyunfor/1.0"

    def log_message(self, *args):  # 安静
        pass

    # ------------------------------------------------------------ 工具
    @property
    def port(self):
        return self.server.server_address[1]

    @property
    def main(self):
        return f"http://127.0.0.1:{self.port}"

    @property
    def gateway(self):
        return f"http://localhost:{self.port}"

    def session(self):
        cookie = SimpleCookie(self.headers.get("Cookie") or "")
        token = cookie["oyf_session"].value if "oyf_session" in cookie else ""
        with LOCK:
            return token if token in STATE["sessions"] else ""

    def form(self):
        length = int(self.headers.get("Content-Length") or 0)
        data = self.rfile.read(length).decode("utf-8") if length else ""
        return {k: v[-1] for k, v in parse_qs(data, keep_blank_values=True).items()}

    def send_html(self, body, status=200, headers=None):
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def send_json(self, data, status=200):
        raw = json.dumps(data, ensure_ascii=False, default=list).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def redirect(self, location, headers=None):
        self.send_response(302)
        self.send_header("Location", location)
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def page(self, title, body, logged=None):
        logged = self.session() if logged is None else logged
        with LOCK:
            balance = STATE["balance"]
            cart_n = len(STATE["cart"])
            slow_font = STATE["slow_font"]
        font_css = ("@font-face{font-family:SlowFont;src:url(/static/slow.woff2) format('woff2')} "
                    "body{font-family:SlowFont,sans-serif !important} ") if slow_font else ""
        if logged:
            nav = (f'<a href="/hesabim">Hesabım</a> <a href="/hesabim/siparislerim">Siparişlerim</a> '
                   f'<span class="user-balance">Bakiye: {money(balance)} ₺</span> <a href="/bakiye-yukle">Bakiye Yükle</a> '
                   f'<a href="/cikis">Çıkış</a>')
        else:
            nav = '<a href="/giris">Giriş Yap</a> <a href="/kayit">Üye Ol</a>'
        return f"""<!doctype html><html lang="tr"><head><meta charset="utf-8"><title>{html.escape(title)} - Oyunfor</title>
<style>{font_css}body{{font-family:sans-serif;margin:0}} header,main,footer{{padding:12px}} .hidden{{display:none}}
[role=dialog]{{position:fixed;top:30%;left:30%;background:#fff;border:2px solid #333;padding:20px}}</style></head>
<body><header><a href="/">Oyunfor</a> <a href="{CATEGORY}">App Store</a> <a href="/mocanetwork/yemeksepeti">Yemeksepeti x Oyunfor</a> {nav} {self.header_cart(cart_n)}</header>
<main>{body}</main>
<footer>Oyunfor'u tercih ettiğiniz için teşekkür ederiz. <a href="/iletisim">İletişim</a>
<a href="https://www.binance.com/en">Binance</a></footer></body></html>"""

    def log(self, item):
        with LOCK:
            STATE["log"].append(item)

    # ------------------------------------------------------------ 路由
    def do_GET(self):
        url = urlparse(self.path)
        query = {k: v[-1] for k, v in parse_qs(url.query).items()}
        path = url.path.rstrip("/") or "/"
        if path.startswith("/__admin"):
            return self.admin(path, query)
        self.log({"method": "GET", "path": path})
        with LOCK:
            cf_error = STATE["cf_error_left"] > 0 and path == STATE["cf_error_path"]
            if cf_error:
                STATE["cf_error_left"] -= 1
        if cf_error:
            return self.send_html(CF_520_PAGE, status=520)
        if path == "/":
            return self.send_html(self.page("Ana Sayfa", "<h1>Türkiye'nin epin sitesi</h1>"))
        if path == "/mocanetwork/yemeksepeti":
            # 地址里含「sepet」但不是购物车（真实站点上的活动页）
            return self.send_html(self.page("Yemeksepeti x Oyunfor Kampanyası",
                                            "<h1>Yemeksepeti x Oyunfor Kampanyası</h1><p>Kampanya detayları</p>"))
        if path == "/giris":
            return self.send_html(self.page("Giriş Yap", """<h1>Üye Girişi</h1>
<form method="post" action="/giris"><label>E-posta</label><input type="email" name="email" placeholder="E-posta adresiniz">
<label>Şifre</label><input type="password" name="password" placeholder="Şifreniz">
<button type="submit" class="btn">Giriş Yap</button></form>"""))
        if path == "/cikis":
            return self.redirect("/", {"Set-Cookie": "oyf_session=; Path=/; Max-Age=0"})
        if path == CATEGORY:
            return self.send_html(self.category_page(added=query.get("added") == "1", error=query.get("hata", "")))
        if path.startswith(CATEGORY + "/"):
            slug = path.rsplit("/", 1)[-1]
            product = next((k for k, p in PRODUCTS.items() if p["slug"] == slug), None)
            if not product:
                return self.send_html(self.page("404", "<h1>Sayfa bulunamadı</h1>"), status=404)
            return self.send_html(self.product_page(product, added=query.get("added") == "1"))
        with LOCK:
            cart_path = STATE["cart_path"]
        if path == cart_path:
            return self.send_html(self.cart_page())
        if path == "/odeme":
            if not self.session():
                return self.redirect("/giris")
            with LOCK:
                if not STATE["cart"]:
                    return self.redirect(STATE["cart_path"])
            return self.send_html(self.checkout_page(query.get("hata", "")))
        if path == "/odeme/kart":
            order = int(query.get("order", "0"))
            return self.send_html(self.page("Kart ile Ödeme", f"""<h1>Kart ile Ödeme</h1>
<p>Sipariş #{order}</p><iframe id="paytr" src="{self.gateway}/paytr/form?order={order}" style="width:700px;height:520px;border:1px solid #ccc"></iframe>"""))
        if path == "/static/slow.woff2":
            with LOCK:
                seconds = STATE["slow_font"]
            time.sleep(seconds)
            return self.send_html("", status=404)
        if path == "/static/apple-card.svg":
            return self.send_svg('<svg xmlns="http://www.w3.org/2000/svg" width="110" height="72"><rect width="110" height="72" rx="6" fill="#1e90ff"/></svg>')
        if path == "/static/binance-logo.svg":
            return self.send_svg('<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24"><rect width="24" height="24" fill="#f0b90b"/></svg>')
        if path == "/odeme/binance/qr.svg":
            return self.send_svg(self.qr_svg(query.get("order", "0")))
        if path == "/odeme/binance":
            return self.send_html(self.binance_onsite(int(query.get("order", "0"))))
        if path == "/odeme/binance/status" or path.startswith("/binance/status"):
            order = int(query.get("order", "0"))
            with LOCK:
                paid = order in STATE["binance_paid"]
            return self.send_json({"paid": paid})
        if path.startswith("/binance/checkout/"):
            return self.send_html(self.binance_hosted(int(path.rsplit("/", 1)[-1])))
        if path == "/paytr/form":
            return self.send_html(self.gateway_form(int(query.get("order", "0"))))
        if path == "/acs":
            return self.send_html(self.acs_page(int(query.get("order", "0"))))
        if path == "/acs/status":
            order = int(query.get("order", "0"))
            with LOCK:
                approved = order in STATE["approved"]
            return self.send_json({"approved": approved})
        if path == "/acs/done":
            order = int(query.get("order", "0"))
            with LOCK:
                if order in STATE["approved"]:
                    self.mark_paid(order)
            return self.send_html(self.top_redirect(f"{self.main}/siparis/basarili?order={order}"))
        if path == "/acs/cancel":
            order = int(query.get("order", "0"))
            return self.send_html(self.top_redirect(f"{self.main}/odeme/hata?order={order}"))
        if path == "/siparis/basarili":
            return self.send_html(self.success_page(int(query.get("order", "0"))))
        if path == "/odeme/hata":
            return self.send_html(self.page("Ödeme", "<h1>Ödeme başarısız</h1><p>İşleminiz başarısız oldu.</p>"))
        if path.startswith("/profil/odemeyap/"):
            parts = path.split("/")
            return self.send_html(self.topup_page(f"{int(parts[-2])}.{parts[-1]}"))
        if path in ("/hesabim/siparislerim", "/profil/aldiklarim"):
            if not self.session():
                return self.redirect("/giris")
            return self.send_html(self.orders_page())
        if path.startswith("/hesabim/siparis/"):
            return self.send_html(self.order_detail(int(path.rsplit("/", 1)[-1])))
        if path == "/hesabim":
            return self.send_html(self.page("Hesabım", '<h1>Hesabım</h1><a href="/hesabim/siparislerim">Siparişlerim</a>'))
        return self.send_html(self.page("404", "<h1>Sayfa bulunamadı</h1>"), status=404)

    def do_POST(self):
        url = urlparse(self.path)
        path = url.path.rstrip("/")
        data = self.form()
        self.log({"method": "POST", "path": path, "data": {k: v for k, v in data.items() if k not in ("password",)}})
        if path == "/giris":
            if data.get("email") == "user@example.com" and data.get("password") == "secret":
                with LOCK:
                    sms = STATE["require_sms_login"]
                    style = STATE["login_code_style"]
                if sms:
                    return self.send_html(self.login_code_page(style))
                return self.login_ok()
            return self.send_html(self.page("Giriş Yap", "<h1>Üye Girişi</h1><p>E-posta veya şifre hatalı.</p>"))
        if path == "/giris/dogrula":
            code = data.get("email_kodu") or "".join(data.get(f"k{i}", "") for i in range(6))
            if code == LOGIN_SMS:
                return self.login_ok()
            with LOCK:
                style = STATE["login_code_style"]
            return self.send_html(self.login_code_page(style, error="Doğrulama kodu hatalı."))
        if path in ("/sepet/ekle", "/sepet/hemen-al"):
            product = data.get("product")
            qty = max(1, int(data.get("adet") or 1))
            if data.get("from") in ("liste", "ajax"):
                with LOCK:
                    qty = int(STATE["add_qty"])
            item = {"product": product, "qty": qty}
            if product == "custom":
                try:
                    amount = float(f"{int(data.get('tutar') or 0)}.{int(data.get('kurus') or 0):02d}")
                except ValueError:
                    amount = 0
                if not 100 <= amount <= 2000:
                    return self.redirect(f"{CATEGORY}?hata=tutar")
                item["price"] = amount
            with LOCK:
                if not STATE["stock"]:
                    return self.redirect(f"{CATEGORY}/{PRODUCTS[product]['slug']}")
                STATE["cart"].append(item)
            if path == "/sepet/hemen-al":
                return self.redirect("/odeme")
            if data.get("from") == "ajax":
                return self.send_json({"ok": True})
            if data.get("from") == "liste":
                return self.redirect(f"{CATEGORY}?added=1")
            return self.redirect(f"{CATEGORY}/{PRODUCTS[product]['slug']}?added=1")
        if path == "/basket/addBasket":
            # 和真实站点一样：表单格式、不要 csrf；出错返回 {msg}，成功返回购物车里的商品列表
            pid = data.get("productID") or ""
            key = next((k for k, v in PRODUCT_IDS.items() if v == pid), None)
            with LOCK:
                stock = STATE["stock"]
                if not key:
                    return self.send_json({"msg": "Ürün bulunamadı"})
                if not stock:
                    return self.send_json({"msg": "Ürün stokta bulunmamaktadır"})
                qty = max(1, int(data.get("counts") or 1))
                item = {"product": key, "qty": qty}
                if key == "custom":
                    try:
                        item["price"] = float(data.get("tutar") or 0)
                    except ValueError:
                        item["price"] = 0.0
                STATE["cart"].append(item)
                items = [{"productID": PRODUCT_IDS[i["product"]], "productName": PRODUCTS[i["product"]]["listing"],
                          "countP": i["qty"]} for i in STATE["cart"]]
            return self.send_json(items)
        if path == "/sepet/adet":
            idx = int(data.get("index") or 0)
            with LOCK:
                if 0 <= idx < len(STATE["cart"]):
                    item = STATE["cart"][idx]
                    if data.get("op") == "minus":
                        item["qty"] = max(1, item["qty"] - 1)
                    elif data.get("op") == "plus":
                        item["qty"] += 1
                    elif (data.get("adet") or "").isdigit():
                        item["qty"] = max(1, int(data["adet"]))
            return self.redirect(STATE["cart_path"])
        if path == "/sepet/onay":
            return self.redirect("/odeme")
        if path == "/orders/siparisTamamla":
            # 真实站点：余额够就直接用余额下单
            with LOCK:
                cart, total = list(STATE["cart"]), self.cart_total()
                ok = bool(cart) and STATE["balance"] + 0.001 >= total
                if ok:
                    order_id = STATE["next_order"]
                    STATE["next_order"] += 1
                    qty = sum(i["qty"] for i in cart)
                    STATE["balance"] -= total
                    STATE["orders"][order_id] = {"id": order_id, "product": cart[0]["product"], "qty": qty,
                                                 "total": total, "status": "Tamamlandı",
                                                 "codes": make_codes(order_id, qty), "method": "bakiye"}
                    STATE["cart"] = []
            if not ok:
                return self.send_json({"status": 0, "msg": "Bakiyeniz yetersiz."})
            return self.send_json({"status": 1, "msg": "Siparişiniz başarıyla oluşturuldu."})
        if path == "/payment/paymentSender":
            # 补差额页的「Ödeme Yap」：建一笔待付订单，返回网关地址（真实站点在新标签页打开 PayTR）
            method = data.get("method")
            with LOCK:
                cart, total = list(STATE["cart"]), self.cart_total()
                if not cart or method not in ("kart", "binance"):
                    return self.send_json({"status": 0, "msg": "Ödeme yöntemi seçiniz."})
                order_id = STATE["next_order"]
                STATE["next_order"] += 1
                STATE["orders"][order_id] = {"id": order_id, "product": cart[0]["product"],
                                             "qty": sum(i["qty"] for i in cart), "total": total,
                                             "status": "Ödeme Bekleniyor", "codes": [], "method": method}
            target = f"/odeme/kart?order={order_id}" if method == "kart" else f"/odeme/binance?order={order_id}"
            return self.send_json({"status": 1, "paytrTokens": self.main + target})
        if path == "/sepet/sil":
            idx = int(data.get("index") or 0)
            with LOCK:
                if 0 <= idx < len(STATE["cart"]):
                    STATE["cart"].pop(idx)
            return self.redirect(STATE["cart_path"])
        if path == "/odeme":
            return self.checkout_submit(data)
        if path == "/paytr/otp/send":
            with LOCK:
                STATE["paytr_otp_sent"] += 1
            return self.send_json({"ok": True})
        if path == "/paytr/pay":
            return self.gateway_submit(data)
        if path == "/acs/submit":
            return self.acs_submit(data)
        if path == "/acs/request":
            order = int(data.get("order") or 0)
            with LOCK:
                STATE["acs_requested"].append(order)
            return self.redirect(f"{self.gateway}/acs?order={order}")
        return self.send_html("not found", status=404)

    # ------------------------------------------------------------ 页面
    def login_ok(self):
        token = f"S{int(time.time() * 1000)}"
        with LOCK:
            STATE["sessions"].append(token)
        return self.redirect("/", {"Set-Cookie": f"oyf_session={token}; Path=/"})

    def product_page(self, key, added=False):
        p = PRODUCTS[key]
        with LOCK:
            stock = STATE["stock"]
            buy_now = STATE["show_buy_now"]
        if stock:
            buttons = f"""<form method="post" action="/sepet/ekle" id="f1"><input type="hidden" name="product" value="{key}">
<label>Adet</label><input type="number" name="adet" value="1" min="1" class="qty">
<button type="submit" class="btn-cart">Sepete Ekle</button>
{'<button type="submit" formaction="/sepet/hemen-al" class="btn-buy">Hemen Al</button>' if buy_now else ''}</form>"""
        else:
            buttons = '<div class="stock-out">Stokta Yok</div><button disabled>Sepete Ekle</button>'
        dialog = ""
        if added:
            dialog = f"""<div role="dialog" class="modal show"><p>Ürün sepetinize eklendi.</p>
<a href="/" class="btn">Alışverişe Devam Et</a> <a href="{STATE['cart_path']}" class="btn">Sepete Git</a></div>"""
        body = f"""<h1>{html.escape(p['title'])}</h1>
<div class="product-price"><del class="old-price">{money(p['price'] + 10)} TL</del> <span class="price">{money(p['price'])} TL</span></div>
<p>Türkiye bölgesinden kullanılabilir.</p>{buttons}{dialog}"""
        return self.page(p["title"], body)

    @staticmethod
    def item_price(item):
        return item.get("price") or PRODUCTS[item["product"]]["price"]

    def cart_total(self):
        return sum(self.item_price(i) * i["qty"] for i in STATE["cart"])

    def category_page(self, added=False, error=""):
        with LOCK:
            stock = STATE["stock"]
            style = STATE["listing_style"]
        if style == "ajax":
            return self.category_page_ajax()
        if style == "oyunfor":
            return self.category_page_oyunfor()
        cards = []
        for key, p in PRODUCTS.items():
            detail = f'<a href="{CATEGORY}/{p["slug"]}" class="btn btn-outline">ℹ DETAY</a>'
            disabled = "" if stock else " disabled"
            if key == "custom":
                body = ('<div class="amount"><input type="text" name="tutar" placeholder="Tutar giriniz."> . '
                        '<input type="text" name="kurus" placeholder="TL" maxlength="2" style="width:50px"></div>')
            else:
                body = f'<div class="product-price">{p["price"]:.2f} TL</div>'
            if style in ("nobuttons", "empty"):
                cards.append(f'<div class="product-row"><div class="product-name">{html.escape(p["listing"])}</div>'
                             f'<div class="product-price">{p["price"]:.2f} TL</div>'
                             f'{detail if style == "nobuttons" else "<span class=badge>Yeni</span>"}</div>')
                continue
            cards.append(f"""<div class="product-row"><img src="/static/apple-card.svg" width="110" height="72" alt="">
<div class="product-name">{html.escape(p["listing"])}</div>
<form method="post" action="/sepet/ekle"><input type="hidden" name="product" value="{key}"><input type="hidden" name="from" value="liste">
{body}{detail} <button type="submit" class="btn btn-success"{disabled}>🛒 SEPETE EKLE</button></form></div>""")
        toast = '<div class="toast">Ürün sepete eklendi.</div>' if added else ""
        err = '<div class="alert">Lütfen 100 - 2000 TL arasında bir tutar giriniz.</div>' if error else ""
        return self.page("Apple Store", f"<h1>Apple Store</h1>{toast}{err}<div class='product-list'>{''.join(cards)}</div>")

    def category_page_oyunfor(self):
        """仿真实分类页：每张卡片的「SEPETE EKLE」是 div.button.addToCart，带 data-id / data-type / data-price / data-name；
        点了用表单格式 POST /basket/addBasket（不带 csrf），成功后跳到购物车"""
        with LOCK:
            cart_path, stock = STATE["cart_path"], STATE["stock"]
        cards = []
        for key, p in PRODUCTS.items():
            if not stock:
                cards.append(f"""<div class="product-item"><h3 class="name">{html.escape(p["listing"])}</h3>
<div class="price">{p["price"]:.2f} TL</div><span class="badge">Stokta Yok</span></div>""")
                continue
            inputs = ('<input class="itunesInput itunesTL desktop" placeholder="Tutar giriniz."> . '
                      '<input class="itunesInput itunesKURUS desktop" maxlength="2">') if key == "custom" else ""
            price = 250.0 if key == "custom" else p["price"]
            cards.append(f"""<div class="product-item"><h3 class="name">{html.escape(p["listing"])}</h3>
<div class="price">{price:.2f} TL</div>{inputs}<a class="button" href="{CATEGORY}/{p["slug"]}">DETAY</a>
<div class="button addToCart desktop" data-id="{PRODUCT_IDS[key]}" data-type="1" data-price="{price:.2f}" data-options="[]"
 data-name="{html.escape(p["listing"])}"><i class="fas fa-cart-plus"></i> SEPETE EKLE</div></div>""")
        script = """<script>
document.querySelectorAll('.addToCart').forEach(b => b.addEventListener('click', () => {
  let tutar = 0;
  if (b.dataset.id === '10161') tutar = (document.querySelector('.itunesTL').value || '0') + '.' + (document.querySelector('.itunesKURUS').value || '0');
  const body = new URLSearchParams({productID: b.dataset.id, productType: b.dataset.type, secenekler: '', counts: '1', tutar: String(tutar), optionsc: '', sideProductId: '0'});
  fetch('/basket/addBasket', {method: 'POST', headers: {'Content-type': 'application/x-www-form-urlencoded'}, body: body.toString()})
    .then(r => r.json()).then(r => { if (r.msg || r.limitmsg) alert(r.msg || r.limitmsg); else location.href = CART_PATH; });
}));
</script>""".replace("CART_PATH", json.dumps(cart_path))
        return self.page("App Store Hediye Kartı Satın Al", f"<h1>App Store Card</h1><div class='list'>{''.join(cards)}</div>{script}")

    def category_page_ajax(self):
        """商品列表由脚本在页面加载后渲染；加入购物车是 div 做的按钮（带图标字体字符），用 fetch 提交"""
        items = [{"key": key, "name": p["listing"], "price": p["price"], "slug": p["slug"]} for key, p in PRODUCTS.items()]
        data = json.dumps(items, ensure_ascii=False)
        with LOCK:
            redirect_ms, cart_path = int(STATE["add_redirect_ms"]), STATE["cart_path"]
        script = """
<script>
const ITEMS = %s;
function addToCart(key, el) {
  const card = el.closest('.product-item');
  const body = new URLSearchParams({product: key, from: 'ajax'});
  const t = card.querySelector('input[name=tutar]'); if (t) { body.set('tutar', t.value); body.set('kurus', card.querySelector('input[name=kurus]').value || '0'); }
  fetch('/sepet/ekle', {method: 'POST', body}).then(r => r.json()).then(() => {
    const toast = document.getElementById('toast'); toast.textContent = 'Ürün sepete eklendi'; toast.style.display = 'block';
    if (REDIRECT_MS > 0) setTimeout(() => { window.location.href = CART_PATH; }, REDIRECT_MS);
  });
}
setTimeout(() => {
  const box = document.getElementById('list');
  box.innerHTML = ITEMS.map(it => `<div class="product-item"><div class="name">${it.name}</div>` +
    (it.key === 'custom' ? '<div><input type="text" name="tutar" placeholder="Tutar giriniz."> . <input type="text" name="kurus" placeholder="TL" maxlength="2"></div>'
                         : `<div class="price">${it.price.toFixed(2)} TL</div>`) +
    `<div class="actions"><a class="btn btn-default" href="/apple-store/apple-store-itunes-gift-card/${it.slug}"><i class="fa"></i> DETAY</a>` +
    `<div class="btn btn-success" onclick="addToCart('${it.key}', this)"><i class="fa fa-cart">\uf07a</i> <span>SEPETE EKLE</span></div></div></div>`).join('');
}, 1200);
</script>""" % data
        script = script.replace("<script>", f"<script>\nconst REDIRECT_MS = {redirect_ms}; const CART_PATH = {json.dumps(cart_path)};", 1)
        return self.page("Apple Store", f"<h1>App Store Card</h1><div id='toast' style='display:none'></div><div id='list'>Yükleniyor...</div>{script}")

    def cart_page(self):
        with LOCK:
            cart = list(STATE["cart"])
            total = self.cart_total()
        if not cart:
            return self.page("Sepetim", "<h1>Sepetim</h1><p>Sepetinizde ürün bulunmamaktadır.</p>")
        with LOCK:
            row_style = STATE["cart_row_style"]
        if row_style == "real":
            rows = "".join(f"""<div class="cart-item" style="position:relative;border:1px solid #ddd;padding:12px;margin:8px 0">
<form method="post" action="/sepet/sil" style="position:absolute;right:8px;top:4px"><input type="hidden" name="index" value="{n}">
<button type="submit" class="close" style="border:0;background:none;font-size:20px">×</button></form>
<div class="name">{html.escape(PRODUCTS[i['product']].get('listing') or PRODUCTS[i['product']]['title'])}</div>
<div class="unit"><small>Adet Fiyatı</small> {self.item_price(i):.2f} TL</div>
<form method="post" action="/sepet/adet" class="qty"><small>Adet</small><input type="hidden" name="index" value="{n}">
<button type="submit" name="op" value="minus" class="qty-btn">−</button><input type="text" name="adet" value="{i['qty']}" class="qty-input" style="width:40px">
<button type="submit" name="op" value="plus" class="qty-btn">+</button></form></div>"""
                           for n, i in enumerate(cart))
        else:
            rows = "".join(f"""<tr><td>{html.escape(PRODUCTS[i['product']]['title'])}</td><td>{i['qty']}</td>
<td>{money(self.item_price(i) * i['qty'])} TL</td>
<td><form method="post" action="/sepet/sil"><input type="hidden" name="index" value="{n}"><button type="submit" title="Sil" class="btn-remove">✕</button></form></td></tr>"""
                           for n, i in enumerate(cart))
        return self.page("Sepetim", f"""<h1>Sepetim</h1><table>{rows}</table>
<div class="summary"><h3>Sipariş Özeti</h3><div class="cart-total">Tutar: {total:.2f} TL</div></div><a href="/" class="btn">Alışverişe Devam Et</a>
{self.cart_button()}""")

    def cart_button(self):
        with LOCK:
            kind = STATE["cart_button"]
            balance, total = STATE["balance"], self.cart_total()
        if kind == "satinal_link":
            return '<a href="/odeme" class="btn btn-primary">SATIN AL</a>'
        if kind == "oyunfor":
            # 和真实站点一样：同一个按钮按余额切换。余额够 →「Alışverişi Tamamla」直接用余额下单；
            # 不够 →「Ödeme Yap」只跳到补差额页 /profil/odemeyap/<TL>/<kuruş>
            if balance + 0.001 >= total:
                return """<div class="alisverisTamamlaRealDiv"><button type="button" class="btn checkCart siparisTamamla alisVerisiTamamla"><i class="far fa-credit-card"></i>&nbsp;Alışverişi Tamamla</button></div>
<script>document.querySelector('.siparisTamamla').addEventListener('click', function () {
  this.disabled = true;
  fetch('/orders/siparisTamamla', {method: 'POST', headers: {'Content-Type': 'application/x-www-form-urlencoded'}, body: 'csrf=x'})
    .then(r => r.json()).then(a => { if (a.status == 1) setTimeout(() => { location.href = '/profil/aldiklarim?islem=ok'; }, 300); else location.reload(); });
});</script>"""
            lira, kurus = f"{total - balance:.2f}".split(".")
            return f"""<div class="alisverisTamamlaRealDiv"><button type="button" class="btn checkCart odmbttn alisVerisiTamamlaOdemeYap"><i class="far fa-credit-card"></i>&nbsp;Ödeme Yap</button></div>
<script>document.querySelector('.alisVerisiTamamlaOdemeYap').addEventListener('click', function () {{
  setTimeout(() => {{ location.href = '/profil/odemeyap/{lira}/{kurus}'; }}, 300);
}});</script>"""
        if kind == "tamamla_submit":
            # 文字是「Alışverişi Tamamla」、但没有 Oyunfor class 的普通提交按钮（演练不能点）
            return ('<form method="post" action="/sepet/onay"><button type="submit" class="btn btn-success">'
                    '<i class="fa fa-credit-card"></i> Alışverişi Tamamla</button></form>')
        if kind == "satinal_submit":
            return '<form method="post" action="/sepet/onay"><button type="submit" class="btn btn-primary">SATIN AL</button></form>'
        return '<a href="/odeme" class="btn btn-primary">Ödemeye Geç</a>'

    def topup_page(self, amount):
        """仿真实补差额付款页：付款方式方块 → 付款渠道 → 「Ödeme Yap」（同步请求后在新标签页打开网关）"""
        tiles = "".join(f"""<div class="col-md-25 bankaGetir col-50" data-id="{bank_id}" data-durum="ilk">
<div class="banksItem"><div class="bankImg"></div><div class="bankTitle">{title}</div></div></div>"""
                        for bank_id, title in ((2, "Kredi Kartı"), (27, "BinancePay"), (1, "ATM-HAVALE-EFT")))
        script = """<script>
const methods = {'2': 'kart', '27': 'binance', '1': 'havale'};
let method = '';
document.querySelectorAll('.bankaGetir').forEach(tile => tile.addEventListener('click', () => {
  method = methods[tile.dataset.id];
  document.getElementById('providers').innerHTML =
    '<div class="listItem listItem2" data-bankid="45"><div class="bankProvider"><img alt="banka logosu" src="/static/apple-card.svg"></div></div>';
  document.querySelector('.paymentSender').style.display = 'none';
  document.querySelector('.listItem').addEventListener('click', () => {
    document.querySelector('.paymentSender').style.display = '';
  });
}));
document.querySelector('.paymentSender').addEventListener('click', () => {
  const xhr = new XMLHttpRequest();
  xhr.open('POST', '/payment/paymentSender', false);
  xhr.setRequestHeader('Content-Type', 'application/x-www-form-urlencoded');
  xhr.send('method=' + method);
  const a = JSON.parse(xhr.responseText);
  if (a.paytrTokens) window.open(a.paytrTokens, '_blank');
});
</script>"""
        return self.page("Ödeme Yap", f"""<div class="bakiyeYukle-content"><ul class="tabClass"><li class="tab-active">TÜM ÖDEME SEÇENEKLERİ</li></ul>
<div class="row">{tiles}</div><div id="providers"></div>
<div class="payout"><div class="payoutcls">Ödenecek Tutar: {amount} TRY</div>
<div class="payoutDivBtn"><button type="button" class="btn cartButton paymentSender" style="display:none">Ödeme Yap</button></div></div>
<p>Siparişi tamamlamak için sol tarafta bulunan ödeme yöntemlerinden birini seçiniz.</p></div>{script}""")

    @staticmethod
    def header_cart(count):
        with LOCK:
            cart_path, kind = STATE["cart_path"], STATE["header_cart"]
        if kind == "icon":
            return f'<a href="{cart_path}" class="header-cart"><i class="fa fa-shopping-cart"></i><span class="badge">{count}</span></a>'
        return f'<a href="{cart_path}">Sepetim ({count})</a>'

    def checkout_page(self, error=""):
        with LOCK:
            total = self.cart_total()
            balance = STATE["balance"]
            style = STATE["checkout_style"]
            lines = "".join(f"<li>{html.escape(PRODUCTS[i['product']]['title'])} x {i['qty']}</li>" for i in STATE["cart"])
        err = f'<div class="alert">{html.escape(error)}</div>' if error else ""
        agree = """<label><input type="checkbox" name="agree" value="on"> Mesafeli Satış Sözleşmesi'ni okudum, onaylıyorum.</label>
<label><input type="checkbox" name="kampanya" value="on"> Kampanyalardan e-posta ile haberdar olmak istiyorum</label>"""
        coupon = '<div><input type="text" name="kupon" placeholder="Kupon Kodu"><button type="button">Uygula</button></div>'
        if style == "buttons":
            methods = f"""<p>Bakiyeniz: {money(balance)} TL</p>
<button type="submit" name="method" value="bakiye" class="pay">Bakiye ile Öde</button>
<button type="submit" name="method" value="kart" class="pay">Kredi Kartı ile Öde</button>
<button type="submit" name="method" value="binance" class="pay">Binance Pay ile Öde</button>"""
        else:
            methods = f"""<div class="payment-methods">
<label><input type="radio" name="method" value="bakiye"> {html.escape(STATE["balance_label"])} (Bakiyeniz: {money(balance)} TL)</label>
<label><input type="radio" name="method" value="kart"> Kredi Kartı / Banka Kartı</label>
<label><input type="radio" name="method" value="binance"> <img src="/static/binance-logo.svg" class="pay-logo" width="24" height="24" alt="Binance"> Binance Pay (USDT)</label></div>
<button type="submit" class="btn-pay">Ödemeyi Tamamla</button>"""
        return self.page("Ödeme", f"""<h1>Ödeme</h1>{err}<ul>{lines}</ul>
<div class="summary"><div>Ara Toplam: {money(total)} TL</div><div class="grand">Genel Toplam: {money(total)} TL</div></div>
<form method="post" action="/odeme">{coupon}{agree}{methods}</form>""")

    def checkout_submit(self, data):
        if not self.session():
            return self.redirect("/giris")
        if data.get("agree") != "on":
            return self.send_html(self.checkout_page("Lütfen sözleşmeyi onaylayın."))
        with LOCK:
            total = self.cart_total()
            cart = list(STATE["cart"])
            if not cart:
                return self.redirect(STATE["cart_path"])
            order_id = STATE["next_order"]
            STATE["next_order"] += 1
            qty = sum(i["qty"] for i in cart)
            order = {"id": order_id, "product": cart[0]["product"], "qty": qty, "total": total,
                     "status": "Ödeme Bekleniyor", "codes": [], "method": data.get("method")}
            if data.get("method") == "bakiye":
                if STATE["balance"] + 0.001 < total:
                    STATE["next_order"] -= 1
                    return self.send_html(self.checkout_page("Bakiyeniz yetersiz."))
                STATE["balance"] -= total
                order["status"] = "Tamamlandı"
                order["codes"] = make_codes(order_id, qty)
                STATE["orders"][order_id] = order
                STATE["cart"] = []
                return self.redirect(f"/siparis/basarili?order={order_id}")
            if data.get("method") == "kart":
                STATE["orders"][order_id] = order
                return self.redirect(f"/odeme/kart?order={order_id}")
            if data.get("method") == "binance":
                STATE["orders"][order_id] = order
                if STATE["binance_mode"] == "hosted":
                    return self.redirect(f"{self.gateway}/binance/checkout/{order_id}")
                return self.redirect(f"/odeme/binance?order={order_id}")
        STATE["next_order"] -= 1
        return self.send_html(self.checkout_page("Lütfen ödeme yöntemi seçin."))

    def gateway_form(self, order, error=""):
        with LOCK:
            saved = STATE["saved_card"]
            total = STATE["orders"].get(order, {}).get("total", 0)
            style = STATE["gateway_style"]
        if style == "paytr2026" and saved:
            with LOCK:
                delay = int(STATE["gateway_delay"])
                phone_otp, loading = STATE["gateway_phone_otp"], STATE["gateway_loading_text"]
            return self.paytr2026_form(order, total, error, delay, phone_otp, loading)
        months ="".join(f'<option value="{m:02d}">{m:02d}</option>' for m in range(1, 13))
        years = "".join(f'<option value="{y}">{y}</option>' for y in range(2026, 2036))
        saved_html = ""
        if saved:
            saved_html = """<div class="saved-cards"><p>Kayıtlı Kartlarım</p>
<label><input type="radio" name="saved" value="tok_4242"> **** **** **** 4242</label>
<label><input type="radio" name="saved" value="tok_1881"> **** **** **** 1881</label></div><p>veya yeni kart:</p>"""
        err = f'<p class="err">{html.escape(error)}</p>' if error else ""
        return f"""<!doctype html><html><head><meta charset="utf-8"><title>PayTR Güvenli Ödeme</title></head><body>
<h3>Güvenli Ödeme — {money(total)} TL</h3>{err}
<form method="post" action="/paytr/pay"><input type="hidden" name="order" value="{order}">{saved_html}
<label for="cc_owner">Kart Üzerindeki İsim</label><input id="cc_owner" name="cc_owner" type="text">
<label for="card_number">Kart Numarası</label><input id="card_number" name="card_number" type="text" maxlength="19" placeholder="0000 0000 0000 0000" inputmode="numeric">
<label>Son Kullanma Tarihi</label><select name="expiry_month" id="expiry_month"><option value="">Ay</option>{months}</select>
<select name="expiry_year" id="expiry_year"><option value="">Yıl</option>{years}</select>
<label for="cvv">CVV</label><input id="cvv" name="cvv" type="text" maxlength="3" placeholder="CVV">
<p>Ödemeniz 3D Secure ile güvence altındadır.</p>
<button type="submit">Ödeme Yap</button></form>
<script>
document.getElementById('card_number').addEventListener('input', e => {{
  const d = e.target.value.replace(/\\D/g, '').slice(0, 16);
  e.target.value = d.replace(/(\\d{{4}})(?=\\d)/g, '$1 ');
}});
</script></body></html>"""

    @staticmethod
    def paytr2026_form(order, total, error="", delay=0, phone_otp=False, loading=False):
        """仿 2026 年 PayTR「Güvenli Ödeme Sayfası」：已保存卡是 label 包着 Radix 单选 button[role=radio]；
        付款按钮是 type=button，class 里带 Tailwind 的「disabled:…」样式（按钮本身并没有禁用）"""
        radio_cls = ("aspect-square h-6 w-6 border-2 rounded-full focus:outline-none "
                     "data-[state=checked]:border-pay-blue-100 disabled:cursor-not-allowed disabled:opacity-50")
        pay_cls = ("px-12 inline-flex items-center justify-center gap-2 whitespace-nowrap transition-colors "
                   "disabled:text-pay-gray-60 disabled:bg-pay-gray-20 disabled:cursor-not-allowed "
                   "dark:disabled:bg-pay-dark-60 bg-pay-blue-100 text-white py-4 w-full")
        cards = "".join(f"""<label class="flex items-center justify-start gap-4 cursor-pointer">
<button type="button" role="radio" aria-checked="false" data-state="unchecked" value="{token}" id="card_{token}" class="{radio_cls}"></button>
<span>TEST USER</span> <span>**** **** **** {last4}</span> <span>Son Kullanma Tarihi 10/31</span></label>"""
                        for token, last4 in (("tok_4242", "4242"), ("tok_1881", "1881")))
        err = f'<p class="err">{html.escape(error)}</p>' if error else ""
        return f"""<!doctype html><html><head><meta charset="utf-8"><title>Güvenli Ödeme Sayfası - PayTR</title></head><body>
<button type="button" class="flex items-center justify-between bg-white">TÜRKÇE</button>
<div>Kart ile Ödeme</div><div>Diğer Ödeme Yöntemleri</div><div>Kayıtlı Kartlarım</div><div>Yeni Kartla Ödeme</div>{err}
<form method="post" action="/paytr/pay" id="pay-form"><input type="hidden" name="order" value="{order}"><input type="hidden" name="saved" value="">
<div id="loading">{"Ödeme bilgileriniz doğrulanıyor, lütfen bekleyin..." if loading else ""}</div>
<div id="phone-otp" style="display:none"><p>Kayıtlı kartlarınızı görüntülemek için cep telefonunuza (+1 *** *** 0147) gönderilecek doğrulama kodunu girin.</p>
<button type="button" id="send-otp">Kod Gönder</button>
<div id="otp-box" style="display:none"><input type="text" name="dogrulama_kodu" maxlength="6" placeholder="Doğrulama Kodu" inputmode="numeric">
<button type="button" id="check-otp">Doğrula</button><p id="otp-err" style="display:none">Doğrulama kodu hatalı.</p></div></div>
<div id="cards"></div><template id="cards-tpl">{cards}</template><div>Ödemeniz Gereken Tutar {money(total * 1.025)} TL</div>
<button type="button" class="{pay_cls}" id="pay">Ödeme Yap</button></form>
<script>
// 真实 PayTR 页面是脚本渲染的：已保存的卡要过一会儿才出现；不认识的浏览器要先验证手机号
function showCards() {{
  document.getElementById('loading').textContent = '';
  document.getElementById('phone-otp').style.display = 'none';
  document.getElementById('cards').innerHTML = document.getElementById('cards-tpl').innerHTML;
  document.querySelectorAll('#cards [role=radio]').forEach(r => r.addEventListener('click', () => {{
    document.querySelectorAll('#cards [role=radio]').forEach(x => {{ x.setAttribute('aria-checked', 'false'); x.dataset.state = 'unchecked'; }});
    r.setAttribute('aria-checked', 'true'); r.dataset.state = 'checked';
    document.querySelector('[name=saved]').value = r.value;
  }}));
}}
document.getElementById('send-otp').addEventListener('click', () => {{
  fetch('/paytr/otp/send', {{method: 'POST'}}).then(() => {{ document.getElementById('otp-box').style.display = ''; }});
}});
document.getElementById('check-otp').addEventListener('click', () => {{
  const code = document.querySelector('[name=dogrulama_kodu]').value;
  if (code === '246810') showCards(); else document.getElementById('otp-err').style.display = '';
}});
setTimeout(() => {{
  if ({'true' if phone_otp else 'false'}) {{ document.getElementById('loading').textContent = ''; document.getElementById('phone-otp').style.display = ''; }}
  else showCards();
}}, {delay});
document.getElementById('pay').addEventListener('click', () => {{
  if (!document.querySelector('[name=saved]').value) {{ alert('Lütfen bir kart seçin'); return; }}
  document.getElementById('pay-form').submit();
}});
</script></body></html>"""

    def gateway_submit(self, data):
        order = int(data.get("order") or 0)
        number = (data.get("card_number") or "").replace(" ", "")
        ok_saved = data.get("saved") == "tok_4242"
        ok_new = (number == GOOD_CARD["number"] and data.get("expiry_month") == GOOD_CARD["month"]
                  and data.get("expiry_year") == GOOD_CARD["year"] and data.get("cvv") == GOOD_CARD["cvv"])
        if not (ok_saved or ok_new):
            return self.send_html(self.gateway_form(order, "Kart bilgileri hatalı."))
        with LOCK:
            mode = STATE["acs_mode"]
        if mode == "none":
            with LOCK:
                self.mark_paid(order)
            return self.send_html(self.top_redirect(f"{self.main}/siparis/basarili?order={order}"))
        return self.redirect(f"{self.gateway}/acs?order={order}")

    def login_code_page(self, style, error=""):
        """登录后的邮箱验证码页（仿常见写法：一个框 / 六个一位的框）"""
        err = f'<p class="alert">{html.escape(error)}</p>' if error else ""
        if style == "boxes":
            boxes = "".join(f'<input type="text" name="k{i}" maxlength="1" inputmode="numeric" style="width:32px">'
                            for i in range(6))
            field = f'<div class="otp-boxes">{boxes}</div>'
        else:
            field = '<input type="text" name="email_kodu" maxlength="6" placeholder="E-posta doğrulama kodu">'
        return self.page("Doğrulama", f"""<h1>E-posta Doğrulama</h1>{err}
<p>E-posta adresinize gönderilen 6 haneli doğrulama kodunu girin.</p>
<form method="post" action="/giris/dogrula">{field}<button type="submit">Doğrula</button></form>""", logged="")

    def acs_page(self, order, error=""):
        with LOCK:
            mode = STATE["acs_mode"]
            total = STATE["orders"].get(order, {}).get("total", 0)
        if mode == "request":
            with LOCK:
                requested = order in STATE["acs_requested"]
            if not requested:
                # 先点「Kod Gönder」才出现验证码输入框
                return f"""<!doctype html><html><head><meta charset="utf-8"><title>3D Secure</title></head><body>
<h3>3D Secure Doğrulama</h3><p>OYUNFOR · TRY {total:.2f}</p>
<p>Doğrulama kodunu almak için aşağıdaki butona tıklayın.</p>
<form method="post" action="/acs/request"><input type="hidden" name="order" value="{order}">
<button type="submit">Kod Gönder</button></form></body></html>"""
            mode = "otp"
        if mode == "okx":
            # 仿 OKX 卡：只有「在 App 里确认」，没有任何输入框
            return f"""<!doctype html><html><head><meta charset="utf-8"><title>Secure payment</title></head><body>
<h3>Confirm your payment</h3><p>OYUNFOR · TRY {total:.2f}</p>
<p>Open the OKX app and confirm this payment. Waiting for your confirmation…</p>
<script>
setInterval(async () => {{
  const r = await fetch('/acs/status?order={order}'); const j = await r.json();
  if (j.approved) {{ location.href = '/acs/done?order={order}'; }}
}}, 800);
</script></body></html>"""
        if mode == "app":
            return f"""<!doctype html><html><head><meta charset="utf-8"><title>DBS Card Authentication</title></head><body>
<h3>Authenticate your transaction</h3><p>Merchant: OYUNFOR  Amount: TRY {total:.2f}</p>
<p>We have sent a notification to your digibank app. Approve this transaction with your Digital Token to continue.</p>
<p>This page will update automatically.</p><a href="/acs/cancel?order={order}">Cancel</a>
<script>
setInterval(async () => {{
  const r = await fetch('/acs/status?order={order}'); const j = await r.json();
  if (j.approved) {{ location.href = '/acs/done?order={order}'; }}
}}, 800);
</script></body></html>"""
        err = f'<p style="color:red">{html.escape(error)}</p>' if error else ""
        return f"""<!doctype html><html><head><meta charset="utf-8"><title>3D Secure</title></head><body>
<h3>Verify your purchase</h3><p>Merchant: OYUNFOR  Amount: TRY {total:.2f}</p>
<p>Enter the One-Time Password (OTP) sent to your mobile number ending 1234.</p>{err}
<form method="post" action="/acs/submit"><input type="hidden" name="order" value="{order}">
<input type="text" name="otp" maxlength="6" autocomplete="one-time-code" inputmode="numeric">
<button type="submit">Submit</button></form><a href="/acs/cancel?order={order}">Cancel</a></body></html>"""

    def acs_submit(self, data):
        order = int(data.get("order") or 0)
        with LOCK:
            STATE["otp_attempts"] += 1
        if data.get("otp") != OTP:
            return self.send_html(self.acs_page(order, "Incorrect OTP. Please try again."))
        with LOCK:
            self.mark_paid(order)
        return self.send_html(self.top_redirect(f"{self.main}/siparis/basarili?order={order}"))

    def send_svg(self, body):
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "image/svg+xml")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    @staticmethod
    def qr_svg(seed):
        cells = []
        n = 25
        for y in range(n):
            for x in range(n):
                if (x * 7 + y * 13 + int(seed or 0)) % 3 == 0 or (x < 7 and y < 7) or (x > 17 and y < 7) or (x < 7 and y > 17):
                    cells.append(f'<rect x="{x * 8}" y="{y * 8}" width="8" height="8"/>')
        return f'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200"><rect width="200" height="200" fill="#fff"/><g fill="#000">{"".join(cells)}</g></svg>'

    @staticmethod
    def usdt(order):
        total = STATE["orders"].get(order, {}).get("total", 0)
        return f"{total / 42:.2f}".replace(".", ",")

    def binance_onsite(self, order):
        with LOCK:
            amount = self.usdt(order)
            auto = STATE["binance_autoredirect"]
        poll = f"""<script>
setInterval(async () => {{ const r = await fetch('/odeme/binance/status?order={order}'); const j = await r.json();
  if (j.paid) location.href = '/siparis/basarili?order={order}'; }}, 800);
</script>""" if auto else ""
        return self.page("Binance Pay", f"""<h1>Binance Pay ile Ödeme</h1><p>Sipariş #{order}</p>
<div class="binance-pay-box"><img class="qr-code" src="/odeme/binance/qr.svg?order={order}" width="220" height="220" alt="Binance Pay QR">
<p>Binance uygulamanızla QR kodu okutun.</p><p class="amount">Ödenecek tutar: {amount} USDT</p>
<p>Kalan süre: <span id="t">14:59</span></p>
<a href="https://app.binance.com/payment/secpay?linkToken=LT{order}abc" class="btn">Binance uygulamasında aç</a></div>{poll}""")

    def binance_hosted(self, order):
        with LOCK:
            amount = self.usdt(order).replace(",", ".")
            paid = order in STATE["binance_paid"]
            qr_delay = int(STATE["binance_qr_delay"])
        if paid:
            return f"""<!doctype html><html><head><meta charset="utf-8"><title>Binance Pay</title></head><body>
<h2>Payment Successful</h2><p>Redirecting to merchant...</p>
<script>setTimeout(() => location.href = {json.dumps(self.main + '/siparis/basarili?order=' + str(order))}, 1500);</script></body></html>"""
        return f"""<!doctype html><html><head><meta charset="utf-8"><title>Binance Pay Checkout</title></head><body>
<h2>Binance Pay</h2><p>Merchant: OYUNFOR</p><p>{amount} USDT</p><p>Expires in 59:30</p>
<div id="qr-box"></div>
<p>Scan with the Binance app to pay</p>
<a href="bnc://app.binance.com/payment/secpay?linkToken=HT{order}">Pay with Binance App</a>
<script>
// 真实币安收银台：链接先出来，二维码过一会儿才画出来（binance_qr_delay 毫秒）
setTimeout(() => {{
  document.getElementById('qr-box').innerHTML = '<canvas class="qrcode" width="240" height="240" id="qr"></canvas>';
  const c = document.getElementById('qr').getContext('2d'); c.fillStyle = '#fff'; c.fillRect(0, 0, 240, 240); c.fillStyle = '#000';
  for (let y = 0; y < 24; y++) for (let x = 0; x < 24; x++) if ((x * 5 + y * 11) % 3 === 0) c.fillRect(x * 10, y * 10, 10, 10);
}}, {qr_delay});
setInterval(async () => {{ const r = await fetch('/binance/status?order={order}'); const j = await r.json(); if (j.paid) location.reload(); }}, 800);
</script></body></html>"""

    @staticmethod
    def top_redirect(url):
        return f"<!doctype html><html><body><p>Yönlendiriliyorsunuz...</p><script>window.top.location.href = {json.dumps(url)};</script></body></html>"

    @staticmethod
    def mark_paid(order_id):
        order = STATE["orders"].get(order_id)
        if order and order["status"] != "Tamamlandı":
            order["status"] = "Tamamlandı"
            order["codes"] = make_codes(order_id, order["qty"])
            STATE["cart"] = []

    def success_page(self, order_id):
        with LOCK:
            order = dict(STATE["orders"].get(order_id) or {})
            show = STATE["success_shows_code"]
        if not order or order.get("status") != "Tamamlandı":
            return self.page("Sipariş", "<h1>Sipariş bulunamadı</h1>")
        codes = ""
        if show:
            spans = "".join(f'<span class="code hidden">{c}</span> ' for c in order["codes"])
            codes = f"""<div>{spans}<button type="button" onclick="document.querySelectorAll('.code').forEach(e => e.classList.remove('hidden')); this.remove();">Kodu Göster</button></div>"""
        else:
            codes = "<p>Kodlarınızı Siparişlerim sayfasından görüntüleyebilirsiniz.</p>"
        return self.page("Sipariş", f"<h1>Siparişiniz başarıyla alındı</h1><p>Sipariş No: #{order_id}</p>{codes}")

    def orders_page(self):
        with LOCK:
            orders = sorted(STATE["orders"].values(), key=lambda o: -o["id"])
        rows = "".join(
            f"<tr><td>#{o['id']}</td><td>{html.escape(PRODUCTS[o['product']]['title'])}</td><td>{money(o['total'])} TL</td>"
            f"<td>{o['status']}</td><td><a href='/hesabim/siparis/{o['id']}'>Detay</a></td>"
            # 旧订单在列表里直接显示兑换码：插件不能把它当成新码
            f"<td>{' '.join(o['codes']) if o['id'] == 1000 else ''}</td></tr>" for o in orders)
        return self.page("Siparişlerim", f"<h1>Siparişlerim</h1><table>{rows}</table>")

    def order_detail(self, order_id):
        with LOCK:
            order = dict(STATE["orders"].get(order_id) or {})
        if not order:
            return self.page("Sipariş", "<h1>Sipariş bulunamadı</h1>")
        spans = "".join(f'<span class="code hidden">{c}</span> ' for c in order["codes"])
        return self.page("Sipariş Detayı", f"""<h1>Sipariş #{order_id}</h1><p>{html.escape(PRODUCTS[order['product']]['title'])}</p>
<p>Durum: {order['status']}</p><div>{spans}<button type="button" onclick="document.querySelectorAll('.code').forEach(e => e.classList.remove('hidden'))">Kodu Göster</button></div>""")

    # ------------------------------------------------------------ 管理
    def admin(self, path, query):
        global STATE
        with LOCK:
            if path == "/__admin/reset":
                STATE = _default_state()
                return self.send_json({"ok": True})
            if path == "/__admin/set":
                for key, value in query.items():
                    if key == "balance":
                        STATE[key] = float(value)
                    elif key in ("gateway_phone_otp", "gateway_loading_text"):
                        STATE[key] = value in ("1", "true", "yes")
                    elif key in ("stock", "require_sms_login", "success_shows_code", "saved_card", "show_buy_now",
                                 "binance_autoredirect"):
                        STATE[key] = value in ("1", "true", "yes")
                    elif key == "cart":
                        STATE["cart"] = [{"product": p, "qty": 1} for p in value.split(",") if p]
                    elif key == "sessions":
                        # 有效的会话（逗号分隔）；设成空 = 所有 Cookie 都失效（模拟 Cookie 过期）
                        STATE[key] = [v for v in value.split(",") if v]
                    elif key in ("cf_error_left", "binance_qr_delay", "add_redirect_ms", "gateway_delay", "slow_font"):
                        STATE[key] = int(value)
                    else:
                        STATE[key] = value
                return self.send_json({"ok": True})
            if path == "/__admin/binance_pay":
                order = int(query.get("order", "0"))
                STATE["binance_paid"].append(order)
                self.mark_paid(order)
                return self.send_json({"ok": True})
            if path == "/__admin/approve":
                STATE["approved"].append(int(query.get("order", "0")))
                return self.send_json({"ok": True})
            if path == "/__admin/state":
                return self.send_json(STATE)
        return self.send_json({"error": "unknown"}, status=404)


def start_background(port):
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__":
    import sys

    srv = start_background(int(sys.argv[1]) if len(sys.argv) > 1 else 18766)
    print("mock oyunfor running")
    threading.Event().wait()
