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
PRODUCTS = {
    "2500": {"slug": "2500-tl-tutarinda-app-store-itunes-hediye-karti",
             "title": "2500 TL Tutarında App Store & iTunes Hediye Kartı", "price": 2600.0},
    "250": {"slug": "250-tl-tutarinda-app-store-itunes-hediye-karti",
            "title": "250 TL Tutarında App Store & iTunes Hediye Kartı", "price": 262.5},
    "100": {"slug": "100-tl-tutarinda-app-store-itunes-hediye-karti",
            "title": "100 TL Tutarında App Store & iTunes Hediye Kartı", "price": 105.0},
    # 与真实站点一样：链接写 1000，页面却是 2000 TL
    "1000": {"slug": "1000-tl-tutarinda-app-store-itunes-hediye-karti",
             "title": "2000 TL Tutarında App Store & iTunes Hediye Kartı", "price": 2050.0},
}
CATEGORY = "/apple-store/apple-store-itunes-gift-card"
GOOD_CARD = {"number": "4242424242424242", "month": "12", "year": "2030", "cvv": "123"}
OTP = "123456"
LOGIN_SMS = "654321"


def _default_state():
    return {
        "balance": 1500.0,
        "stock": True,
        "acs_mode": "otp",            # otp / app / none
        "require_sms_login": False,
        "success_shows_code": True,
        "checkout_style": "radios",   # radios / buttons
        "show_buy_now": True,
        "saved_card": False,
        "cart": [],
        "orders": {1000: {"id": 1000, "product": "100", "qty": 1, "total": 105.0, "status": "Tamamlandı",
                          "codes": ["XOLDCODE00000001"], "method": "bakiye"}},
        "next_order": 1001,
        "approved": [],
        "sessions": ["VALID"],
        "log": [],
        "otp_attempts": 0,
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
        if logged:
            nav = (f'<a href="/hesabim">Hesabım</a> <a href="/hesabim/siparislerim">Siparişlerim</a> '
                   f'<span class="user-balance">Bakiye: {money(balance)} ₺</span> <a href="/bakiye-yukle">Bakiye Yükle</a> '
                   f'<a href="/cikis">Çıkış</a>')
        else:
            nav = '<a href="/giris">Giriş Yap</a> <a href="/kayit">Üye Ol</a>'
        return f"""<!doctype html><html lang="tr"><head><meta charset="utf-8"><title>{html.escape(title)} - Oyunfor</title>
<style>body{{font-family:sans-serif;margin:0}} header,main,footer{{padding:12px}} .hidden{{display:none}}
[role=dialog]{{position:fixed;top:30%;left:30%;background:#fff;border:2px solid #333;padding:20px}}</style></head>
<body><header><a href="/">Oyunfor</a> <a href="{CATEGORY}">App Store</a> {nav} <a href="/sepet">Sepetim ({cart_n})</a></header>
<main>{body}</main>
<footer>Oyunfor'u tercih ettiğiniz için teşekkür ederiz. <a href="/iletisim">İletişim</a></footer></body></html>"""

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
        if path == "/":
            return self.send_html(self.page("Ana Sayfa", "<h1>Türkiye'nin epin sitesi</h1>"))
        if path == "/giris":
            return self.send_html(self.page("Giriş Yap", """<h1>Üye Girişi</h1>
<form method="post" action="/giris"><label>E-posta</label><input type="email" name="email" placeholder="E-posta adresiniz">
<label>Şifre</label><input type="password" name="password" placeholder="Şifreniz">
<button type="submit" class="btn">Giriş Yap</button></form>"""))
        if path == "/cikis":
            return self.redirect("/", {"Set-Cookie": "oyf_session=; Path=/; Max-Age=0"})
        if path == CATEGORY:
            items = "".join(f'<div class="product-card"><a href="{CATEGORY}/{p["slug"]}">{html.escape(p["title"])}</a>'
                            f'<span class="price">{money(p["price"])} TL</span></div>' for p in PRODUCTS.values())
            return self.send_html(self.page("Apple Store", f"<h1>App Store Hediye Kartları</h1>{items}"))
        if path.startswith(CATEGORY + "/"):
            slug = path.rsplit("/", 1)[-1]
            product = next((k for k, p in PRODUCTS.items() if p["slug"] == slug), None)
            if not product:
                return self.send_html(self.page("404", "<h1>Sayfa bulunamadı</h1>"), status=404)
            return self.send_html(self.product_page(product, added=query.get("added") == "1"))
        if path == "/sepet":
            return self.send_html(self.cart_page())
        if path == "/odeme":
            if not self.session():
                return self.redirect("/giris")
            with LOCK:
                if not STATE["cart"]:
                    return self.redirect("/sepet")
            return self.send_html(self.checkout_page(query.get("hata", "")))
        if path == "/odeme/kart":
            order = int(query.get("order", "0"))
            return self.send_html(self.page("Kart ile Ödeme", f"""<h1>Kart ile Ödeme</h1>
<p>Sipariş #{order}</p><iframe id="paytr" src="{self.gateway}/paytr/form?order={order}" style="width:700px;height:520px;border:1px solid #ccc"></iframe>"""))
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
        if path == "/hesabim/siparislerim":
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
                if sms:
                    return self.send_html(self.page("Doğrulama", """<h1>SMS Doğrulama</h1>
<p>Telefonunuza gönderilen doğrulama kodunu girin.</p>
<form method="post" action="/giris/dogrula"><input type="text" name="sms_kodu" maxlength="6" placeholder="Doğrulama kodu">
<button type="submit">Doğrula</button></form>""", logged=""))
                return self.login_ok()
            return self.send_html(self.page("Giriş Yap", "<h1>Üye Girişi</h1><p>E-posta veya şifre hatalı.</p>"))
        if path == "/giris/dogrula":
            if data.get("sms_kodu") == LOGIN_SMS:
                return self.login_ok()
            return self.send_html(self.page("Doğrulama", "<p>Kod hatalı</p>", logged=""))
        if path in ("/sepet/ekle", "/sepet/hemen-al"):
            product = data.get("product")
            qty = max(1, int(data.get("adet") or 1))
            with LOCK:
                if not STATE["stock"]:
                    return self.redirect(f"{CATEGORY}/{PRODUCTS[product]['slug']}")
                STATE["cart"].append({"product": product, "qty": qty})
            if path == "/sepet/hemen-al":
                return self.redirect("/odeme")
            return self.redirect(f"{CATEGORY}/{PRODUCTS[product]['slug']}?added=1")
        if path == "/sepet/sil":
            idx = int(data.get("index") or 0)
            with LOCK:
                if 0 <= idx < len(STATE["cart"]):
                    STATE["cart"].pop(idx)
            return self.redirect("/sepet")
        if path == "/odeme":
            return self.checkout_submit(data)
        if path == "/paytr/pay":
            return self.gateway_submit(data)
        if path == "/acs/submit":
            return self.acs_submit(data)
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
            dialog = """<div role="dialog" class="modal show"><p>Ürün sepetinize eklendi.</p>
<a href="/" class="btn">Alışverişe Devam Et</a> <a href="/sepet" class="btn">Sepete Git</a></div>"""
        body = f"""<h1>{html.escape(p['title'])}</h1>
<div class="product-price"><del class="old-price">{money(p['price'] + 10)} TL</del> <span class="price">{money(p['price'])} TL</span></div>
<p>Türkiye bölgesinden kullanılabilir.</p>{buttons}{dialog}"""
        return self.page(p["title"], body)

    def cart_total(self):
        return sum(PRODUCTS[i["product"]]["price"] * i["qty"] for i in STATE["cart"])

    def cart_page(self):
        with LOCK:
            cart = list(STATE["cart"])
            total = self.cart_total()
        if not cart:
            return self.page("Sepetim", "<h1>Sepetim</h1><p>Sepetinizde ürün bulunmamaktadır.</p>")
        rows = "".join(f"""<tr><td>{html.escape(PRODUCTS[i['product']]['title'])}</td><td>{i['qty']}</td>
<td>{money(PRODUCTS[i['product']]['price'] * i['qty'])} TL</td>
<td><form method="post" action="/sepet/sil"><input type="hidden" name="index" value="{n}"><button type="submit" title="Sil" class="btn-remove">✕</button></form></td></tr>"""
                       for n, i in enumerate(cart))
        return self.page("Sepetim", f"""<h1>Sepetim</h1><table>{rows}</table>
<div class="cart-total">Toplam: {money(total)} TL</div><a href="/" class="btn">Alışverişe Devam Et</a>
<a href="/odeme" class="btn btn-primary">Ödemeye Geç</a>""")

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
<button type="submit" name="method" value="kart" class="pay">Kredi Kartı ile Öde</button>"""
        else:
            methods = f"""<div class="payment-methods">
<label><input type="radio" name="method" value="bakiye"> Bakiye ile Öde (Bakiyeniz: {money(balance)} TL)</label>
<label><input type="radio" name="method" value="kart"> Kredi Kartı / Banka Kartı</label></div>
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
                return self.redirect("/sepet")
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
        STATE["next_order"] -= 1
        return self.send_html(self.checkout_page("Lütfen ödeme yöntemi seçin."))

    def gateway_form(self, order, error=""):
        with LOCK:
            saved = STATE["saved_card"]
            total = STATE["orders"].get(order, {}).get("total", 0)
        months = "".join(f'<option value="{m:02d}">{m:02d}</option>' for m in range(1, 13))
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

    def acs_page(self, order, error=""):
        with LOCK:
            mode = STATE["acs_mode"]
            total = STATE["orders"].get(order, {}).get("total", 0)
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
                    elif key in ("stock", "require_sms_login", "success_shows_code", "saved_card", "show_buy_now"):
                        STATE[key] = value in ("1", "true", "yes")
                    elif key == "cart":
                        STATE["cart"] = [{"product": p, "qty": 1} for p in value.split(",") if p]
                    else:
                        STATE[key] = value
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
