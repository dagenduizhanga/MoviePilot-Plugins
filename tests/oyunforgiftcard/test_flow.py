"""
端到端流程：真实 MoviePilot V2 宿主 + 真实 Chromium + 本地模拟 Oyunfor/支付网关/银行 3DS。
"""
import json
import os
import sys
import time

import pytest

from app.core.event import Event
from app.schemas.types import EventType

from .conftest import CARD, codes_of, make_config, run, set_secrets, wait_idle, wait_message

OLD_CODE = "XOLDCODE00000001"
CATEGORY = "/apple-store/apple-store-itunes-gift-card"


def orders(site):
    return {int(k): v for k, v in site.state()["orders"].items()}


# ---------------------------------------------------------------- 余额
def test_dry_run_balance_stops_before_paying(oyun, site):
    oyun.init_plugin(make_config(dry_run=True))
    record = run(oyun)
    assert record["code"] == "dry_run", record
    assert record["dry_run"] is True
    assert "未付款" in record["message"]
    assert record["total"] == 262.5
    state = site.state()
    assert list(orders(site)) == [1000]
    assert state["balance"] == 1500.0
    assert not site.posts("/odeme")
    run_dir = oyun.get_data_path() / "runs" / record["run"]
    names = [p.name for p in run_dir.iterdir()]
    assert any("dry-run.jpg" in n for n in names), names
    assert "steps.log.enc" in names
    assert all(n.endswith(".enc") for n in names), names


def test_balance_purchase_reveals_code_on_success_page(oyun, site):
    record = run(oyun)
    assert record["code"] == "success", record
    assert codes_of(oyun, record) == ["X01001QZ1ABCDEFG"]
    assert record["total"] == 262.5
    assert site.state()["balance"] == 1237.5
    # 查重只存不可逆指纹，不存兑换码明文
    assert oyun.get_data("known_codes") is None and len(oyun.get_data("known_fp")) == 1
    assert "X01001QZ1ABCDEFG" not in json.dumps(oyun.get_data("known_fp"))
    final = oyun.messages[-1]
    assert "购买成功" in final["title"] and "X01001QZ1ABCDEFG" in final["text"]
    # 含兑换码的通知不写进 MoviePilot 的消息历史
    assert final.get("save_history") is False
    # 结算页页脚的「teşekkür ederiz」不能被当成付款成功：必须真的提交了订单
    assert len(site.posts("/odeme")) == 1


def test_codes_from_newest_order_not_old_ones(oyun, site):
    site.set(success_shows_code="0")
    record = run(oyun)
    assert record["code"] == "success", record
    assert codes_of(oyun, record) == ["X01001QZ1ABCDEFG"]
    assert OLD_CODE not in codes_of(oyun, record)


def test_add_to_cart_dialog_flow_and_quantity(oyun, site):
    site.set(show_buy_now="0")
    record = run(oyun, quantity=2)
    assert record["code"] == "success", record
    assert record["total"] == 525.0
    assert codes_of(oyun, record) == ["X01001QZ1ABCDEFG", "X01001QZ2ABCDEFG"]


def test_leftover_cart_is_cleared(oyun, site):
    site.set(cart="100")
    record = run(oyun)
    assert record["code"] == "success", record
    assert record["total"] == 262.5
    assert orders(site)[1001]["product"] == "250"


def test_payment_option_that_is_a_button(oyun, site):
    site.set(checkout_style="buttons")
    oyun.init_plugin(make_config(dry_run=True))
    record = run(oyun)
    assert record["code"] == "dry_run", record
    assert "Bakiye ile" in record["message"] or "bakiye ile" in record["message"]
    assert not site.posts("/odeme")
    oyun.init_plugin(make_config())
    record = run(oyun)
    assert record["code"] == "success", record
    assert site.posts("/odeme")[-1]["data"]["method"] == "bakiye"


# ---------------------------------------------------------------- 银行卡 + 3DS
def _card(oyun, **extra):
    """银行卡模式：卡信息走保险箱（相当于在安全录入页填写）"""
    oyun.init_plugin(make_config(pay_mode="card", **extra))
    set_secrets(oyun, **CARD)


def test_card_dry_run_reaches_gateway_without_submitting(oyun, site):
    _card(oyun, dry_run=True)
    record = run(oyun)
    assert record["code"] == "dry_run", record
    assert "银行卡页面" in record["message"]
    assert not site.posts("/paytr/pay")


def test_card_otp_wrong_then_right(oyun, site):
    _card(oyun)
    ok, message = oyun.start_purchase()
    assert ok, message
    prompt = wait_message(oyun, "付款验证")
    assert prompt, oyun.messages
    # 第一行就说清楚：要输入验证码
    assert "需要输入验证码" in prompt["title"]
    assert "/oyunfor_code" in prompt["text"]
    assert prompt["buttons"][0][-1] == {"text": "❌ 取消购买", "callback_data": "[PLUGIN]OyunforGiftCard|cancel"}
    assert prompt.get("save_history") is False
    assert oyun._job.waiting == "otp"
    # 错误验证码：通过远程命令事件提交
    start = len(oyun.messages)
    oyun.handle_command(Event(EventType.PluginAction, {"action": "oyunforgiftcard_code", "arg_str": "111111",
                                                       "channel": None, "user": "u1"}))
    assert wait_message(oyun, "验证码错误", start=start), oyun.messages[start:]
    # 正确验证码
    ok, message = oyun.submit_code("123 456")
    assert ok, message
    assert wait_idle(oyun, 90)
    record = oyun.get_data("history")[-1]
    assert record["code"] == "success", record
    assert record["verification"] == "otp"
    assert codes_of(oyun, record) == ["X01001QZ1ABCDEFG"]
    assert site.state()["otp_attempts"] == 2
    posted = site.posts("/paytr/pay")[-1]["data"]
    assert posted["card_number"].replace(" ", "") == "4242424242424242"
    assert posted["expiry_month"] == "12" and posted["expiry_year"] == "2030" and posted["cvv"] == "123"
    # 填卡之后的截图仍然正常（遮挡卡号输入框）；运行文件全部加密，解密后的页面源码里也没有卡号
    run_dir = oyun.get_data_path() / "runs" / record["run"]
    steps = oyun._read_run_file(run_dir / "steps.log.enc").decode("utf-8")
    assert steps.count("截图失败") <= 1
    assert any("result-success.jpg" in p.name for p in run_dir.iterdir())
    for path in run_dir.iterdir():
        raw = path.read_bytes()
        assert path.name.endswith(".enc") and b"4242" not in raw, path.name
        if ".html" in path.name:
            text = oyun._read_run_file(path).decode("utf-8")
            assert "4242424242424242" not in text and "4242 4242 4242 4242" not in text, path.name
            assert "X01001QZ1ABCDEFG" not in text, path.name


def test_card_app_approval(oyun, site):
    site.set(acs_mode="app")
    _card(oyun)
    ok, _ = oyun.start_purchase()
    assert ok
    prompt = wait_message(oyun, "付款验证")
    # 银行卡一律按「需要输入验证码」提醒，同时说明只在 App 确认的会自动继续
    assert prompt and "需要输入验证码" in prompt["title"] and "App 里点确认" in prompt["text"], oyun.messages
    assert oyun._job.waiting == "otp"
    # 用户点了按钮（只是告知），然后在银行 App 里批准
    oyun.handle_button(Event(EventType.MessageAction, {"plugin_id": "OyunforGiftCard", "text": "approved",
                                                       "userid": "u1", "channel": None}))
    order_id = max(orders(site))
    site.approve(order_id)
    assert wait_idle(oyun, 90)
    record = oyun.get_data("history")[-1]
    assert record["code"] == "success", record
    assert record["verification"] == "otp"
    assert codes_of(oyun, record) == [f"X{order_id:05d}QZ1ABCDEFG"]


def test_saved_card(oyun, site):
    site.set(saved_card="1", acs_mode="none")
    oyun.init_plugin(make_config(pay_mode="card"))
    record = run(oyun)
    assert record["code"] == "success", record
    assert site.posts("/paytr/pay")[-1]["data"].get("saved") == "tok_4242"



def test_paytr_2026_page_saved_card_and_pay_button(oyun, site):
    """真实 PayTR 新页面（2026）：已保存卡是 label 包着 Radix 单选；「Ödeme Yap」是 type=button，
    class 里带 Tailwind 的「disabled:…」样式但按钮并没禁用 —— 之前被误判成禁用，报「没有找到最终的付款按钮」"""
    site.set(saved_card="1", acs_mode="none", gateway_style="paytr2026",
             cart_button="oyunfor", cart_path="/sepetim", header_cart="icon", balance="0")
    oyun.init_plugin(make_config(pay_mode="card"))
    record = run(oyun)
    assert record["code"] == "success", record
    assert site.posts("/paytr/pay")[-1]["data"].get("saved") == "tok_4242"
    assert codes_of(oyun, record) == ["X01001QZ1ABCDEFG"]

def test_card_cancel_from_button(oyun, site):
    _card(oyun)
    ok, _ = oyun.start_purchase()
    assert ok
    assert wait_message(oyun, "付款验证")
    oyun.handle_button(Event(EventType.MessageAction, {"plugin_id": "OyunforGiftCard", "text": "cancel",
                                                       "userid": "u1", "channel": None}))
    assert wait_idle(oyun, 60)
    record = oyun.get_data("history")[-1]
    assert record["code"] == "cancelled", record
    assert orders(site)[1001]["status"] == "Ödeme Bekleniyor"


def test_card_verify_timeout(oyun, site):
    _card(oyun)
    oyun._verify_timeout = 4
    record = run(oyun)
    assert record["code"] == "timeout", record
    assert orders(site)[1001]["status"] == "Ödeme Bekleniyor"


# ---------------------------------------------------------------- 登录
def test_login_with_password_and_sms_then_reuse_state(oyun, site):
    site.set(require_sms_login="1")
    oyun.init_plugin(make_config(dry_run=True))
    set_secrets(oyun, cookie="oyf_session=EXPIRED", username="user@example.com", password="secret")
    ok, _ = oyun.start_purchase()
    assert ok
    prompt = wait_message(oyun, "登录验证")
    assert prompt and "邮箱" in prompt["text"], oyun.messages
    oyun.submit_code("654321")
    assert wait_idle(oyun, 90)
    record = oyun.get_data("history")[-1]
    assert record["code"] == "dry_run", record
    assert len(site.posts("/giris")) == 1
    # 登录状态加密保存，不留明文
    assert (oyun.get_data_path() / "session.enc").exists()
    assert not (oyun.get_data_path() / "storage_state.json").exists()
    assert b"oyf_session" not in (oyun.get_data_path() / "session.enc").read_bytes()
    # 第二次：沿用保存的登录状态，不再登录
    record = run(oyun)
    assert record["code"] == "dry_run", record
    assert len(site.posts("/giris")) == 1


def test_expired_cookie_without_password(oyun, site):
    set_secrets(oyun, cookie="oyf_session=EXPIRED")
    record = run(oyun)
    assert record["code"] == "login_required", record
    assert "Cookie" in record["message"]


# ---------------------------------------------------------------- 限制与保护
def test_limits_and_guards(oyun, site):
    oyun.init_plugin(make_config(max_total=100))
    assert run(oyun)["code"] == "limit"

    oyun.init_plugin(make_config())
    site.set(balance="100")
    assert run(oyun)["code"] == "insufficient"

    site.reset()
    site.set(stock="0")
    assert run(oyun)["code"] == "out_of_stock"

    # 商品页链接写 1000，页面却是 2000 TL：面额对不上就停
    site.reset()
    record = run(oyun, amount=1000, product_url=f"{site.url}{CATEGORY}/1000-tl-tutarinda-app-store-itunes-hediye-karti")
    assert record["code"] == "failed" and "2000" in record["message"], record
    assert not site.posts("/odeme")


def test_daily_limit_and_busy(oyun, site):
    oyun.init_plugin(make_config(daily_limit=1))
    assert run(oyun)["code"] == "success"
    ok, message = oyun.start_purchase()
    assert not ok and "每日上限" in message
    # 演练不受每日次数限制；任务进行中拒绝新的任务
    ok, message = oyun.start_purchase(force_dry=True)
    assert ok, message
    ok2, message2 = oyun.start_purchase(force_dry=True)
    assert not ok2 and "进行中" in message2
    assert wait_idle(oyun)


def test_buy_command_arguments(oyun):
    assert oyun.parse_buy_args("") == (None, "", 0, False, "")
    assert oyun.parse_buy_args("250 2") == (250, "", 2, False, "")
    assert oyun.parse_buy_args("100 x3 dry") == (100, "", 3, True, "")
    url = "https://www.oyunfor.com/apple-store/apple-store-itunes-gift-card/250-tl-tutarinda-app-store-itunes-hediye-karti"
    assert oyun.parse_buy_args(f"{url} 1") == (None, url, 1, False, "")
    assert oyun.parse_buy_args("250 99")[4]
    assert oyun.parse_buy_args("abc")[4]


def test_buy_command_event_replies_to_sender(oyun, site):
    oyun.init_plugin(make_config(dry_run=True, notify=False))
    oyun.handle_command(Event(EventType.PluginAction, {"action": "oyunforgiftcard_buy", "arg_str": "100",
                                                       "channel": "Telegram", "user": "42"}))
    assert wait_idle(oyun, 120)
    record = oyun.get_data("history")[-1]
    assert record["code"] == "dry_run" and record["face"] == 100 and record["total"] == 105.0, record
    assert record["trigger"] == "命令"
    # 通知关闭时，命令触发的结果仍回复给发起人
    assert oyun.messages and all(m.get("userid") == "42" for m in oyun.messages)
    assert any("演练完成" in m["title"] for m in oyun.messages)


def test_page_and_form_render(oyun, site):
    form, model = oyun.get_form()
    models = set()

    def walk(node):
        if isinstance(node, dict):
            props = node.get("props") or {}
            if props.get("model"):
                models.add(props["model"])
            for child in node.get("content") or []:
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(form)
    assert models == set(model.keys()) - {"history_days"}
    assert model["dry_run"] is True and model["pay_mode"] == "balance"
    page = oyun.get_page()
    assert "暂无记录" in json.dumps(page, ensure_ascii=False)
    run(oyun)
    text = json.dumps(oyun.get_page(), ensure_ascii=False)
    assert "X01001QZ1ABCDEFG" in text and "购买成功" in text
    assert "data:image/jpeg;base64," in text


@pytest.mark.skipif(not os.environ.get("TEST_CHROMIUM"), reason="需要本机 Chromium 充当 CloakBrowser 内核")
def test_cloakbrowser_engine(oyun, site, monkeypatch):
    """MoviePilot v2.12+ 默认引擎：CloakBrowser（这里用本机 Chromium 代替它的定制内核）"""
    monkeypatch.delitem(sys.modules, "cloakbrowser", raising=False)
    monkeypatch.setenv("CLOAKBROWSER_BINARY_PATH", os.environ["TEST_CHROMIUM"])
    monkeypatch.setenv("CLOAKBROWSER_AUTO_UPDATE", "false")
    monkeypatch.setenv("CLOAKBROWSER_SUPPRESS_FONT_WARNING", "1")
    oyun.init_plugin(make_config(dry_run=True))
    record = run(oyun)
    assert record["code"] == "dry_run", record
    steps = oyun._read_run_file(oyun.get_data_path() / "runs" / record["run"] / "steps.log.enc").decode("utf-8")
    assert "CloakBrowser" in steps


def test_code_sent_before_verification_page(oyun, site):
    """验证码比验证页先到（用户手快 / 短信先到），也要等输入框出现后填进去"""
    _card(oyun)
    ok, _ = oyun.start_purchase()
    assert ok
    assert oyun.submit_code("123456")[0]
    assert wait_idle(oyun, 120)
    record = oyun.get_data("history")[-1]
    assert record["code"] == "success", record
    assert site.state()["otp_attempts"] == 1



# ---------------------------------------------------------------- 整页链接（分类页）里按面额挑商品
def _cart_posts(site):
    return [item["data"] for item in site.posts("/sepet/ekle")]


def test_listing_url_picks_card_by_amount(oyun, site):
    """商品链接填整页分类地址，按面额挑：不能点到第一张「100 - 2000 TL」自定义金额卡"""
    oyun.init_plugin(make_config(product_url=f"{site.url}{CATEGORY}", amount="100"))
    record = run(oyun)
    assert record["code"] == "success", record
    assert record["product"] == "100 TL App Store Card"
    assert record["total"] == 105.0
    assert [item["product"] for item in _cart_posts(site)] == ["100"]
    assert codes_of(oyun, record) == ["X01001QZ1ABCDEFG"]


def test_listing_250_not_2500(oyun, site):
    oyun.init_plugin(make_config(product_url=f"{site.url}{CATEGORY}", amount="250"))
    record = run(oyun)
    assert record["code"] == "success", record
    assert [item["product"] for item in _cart_posts(site)] == ["250"]


def test_custom_amount_card(oyun, site):
    """固定面额里没有 150：用「100 - 2000 TL」自定义金额那一项，填 150"""
    oyun.init_plugin(make_config(amount="150"))
    record = run(oyun)
    assert record["code"] == "success", record
    posted = _cart_posts(site)
    assert posted == [{"product": "custom", "from": "liste", "tutar": "150", "kurus": "00"}]
    assert record["total"] == 150.0
    assert codes_of(oyun, record) == ["X01001QZ1ABCDEFG"]


def test_amount_not_available(oyun, site):
    oyun.init_plugin(make_config(amount="5000"))
    record = run(oyun)
    assert record["code"] == "failed", record
    assert "5000" in record["message"]
    assert not _cart_posts(site)


def test_command_url_only_uses_configured_amount_on_listing(oyun, site):
    oyun.init_plugin(make_config(amount="100", dry_run=True))
    record = run(oyun, product_url=f"{site.url}{CATEGORY}")
    assert record["code"] == "dry_run", record
    assert record["product"] == "100 TL App Store Card"


def test_single_product_page_url_with_dialog_and_quantity(oyun, site):
    site.set(show_buy_now="0")
    url = f"{site.url}{CATEGORY}/250-tl-tutarinda-app-store-itunes-hediye-karti"
    oyun.init_plugin(make_config(product_url=url, amount="250"))
    record = run(oyun, quantity=2)
    assert record["code"] == "success", record
    assert record["total"] == 525.0
    assert len(codes_of(oyun, record)) == 2


def test_card_rejected_by_gateway_fails_fast(oyun, site):
    """支付页提示卡信息有误：直接判失败，不把持卡人输入框当成验证码框、也不傻等到超时"""
    _card(oyun)
    set_secrets(oyun, card_number="4000000000000002")
    record = run(oyun, timeout=90)
    assert record["code"] == "failed", record
    assert "支付页提示" in record["message"] and "hatali" in record["message"]
    assert not any("付款验证" in (m.get("title") or "") for m in oyun.messages)


# ---------------------------------------------------------------- 验证提醒的写法
def test_okx_style_app_only_confirmation(oyun, site):
    """OKX 卡：验证页上没有输入框，只能在 App 里确认。提醒仍按「需要输入验证码」，并说明 App 确认会自动继续"""
    site.set(acs_mode="okx")
    _card(oyun)
    ok, _ = oyun.start_purchase()
    assert ok
    prompt = wait_message(oyun, "付款验证")
    assert prompt and "需要输入验证码" in prompt["title"], oyun.messages
    assert "OKX" in prompt["text"] and "不用回复" in prompt["text"]
    assert prompt["buttons"][0][0]["callback_data"] == "[PLUGIN]OyunforGiftCard|approved"
    assert oyun._job.waiting == "otp"
    site.approve(max(orders(site)))
    assert wait_idle(oyun, 90)
    record = oyun.get_data("history")[-1]
    assert record["code"] == "success" and record["verification"] == "otp", record


def test_login_code_in_six_boxes_with_retry(oyun, site):
    """Cookie 失效 → 账号密码登录 → 邮箱验证码（六个一位的框），先错一次再对"""
    site.set(require_sms_login="1", login_code_style="boxes")
    oyun.init_plugin(make_config(dry_run=True))
    set_secrets(oyun, cookie="oyf_session=EXPIRED", username="user@example.com", password="secret")
    ok, _ = oyun.start_purchase()
    assert ok
    prompt = wait_message(oyun, "登录验证")
    assert prompt, oyun.messages
    start = len(oyun.messages)
    oyun.submit_code("111111")
    assert wait_message(oyun, "验证码不对", start=start), oyun.messages[start:]
    oyun.submit_code("654321")
    assert wait_idle(oyun, 90)
    record = oyun.get_data("history")[-1]
    assert record["code"] == "dry_run", record
    # 账号不能出现在日志 / 步骤记录里
    steps = oyun._read_run_file(oyun.get_data_path() / "runs" / record["run"] / "steps.log.enc").decode()
    assert "user@example.com" not in steps


# ---------------------------------------------------------------- 没有口令 / 旧版本迁移
def test_without_secret_nothing_is_stored(oyun, site, monkeypatch):
    from app.plugins.oyunforgiftcard.vault import VaultError

    monkeypatch.delenv("OYUNFOR_SECRET", raising=False)
    oyun.init_plugin(make_config())
    ok, message = oyun.start_purchase()
    assert not ok and "OYUNFOR_SECRET" in message
    try:
        oyun.store_secrets({"card_number": "4242424242424242"})
        raise AssertionError("没有口令时不应保存")
    except VaultError:
        pass
    form, _ = oyun.get_form()
    text = str(form)
    assert "还不能保存账号信息" in text and "OYUNFOR_SECRET=" in text


def test_legacy_plaintext_is_migrated_and_wiped(oyun, site):
    """旧版本把 Cookie / 卡号明文放在配置里、登录状态和兑换码明文落盘：升级后全部加密并清除明文"""
    data_path = oyun.get_data_path()
    (data_path / "storage_state.json").write_text(json.dumps({"cookies": [], "origins": []}))
    oyun.save_data("known_codes", ["XOLDCODE00000001"])
    oyun.save_data("history", [{"date": "2026-01-01 00:00:00", "code": "success", "codes": ["XOLDCODE00000001"]}])
    legacy = make_config(cookie="oyf_session=VALID", card_number="4242 4242 4242 4242", card_cvv="123",
                         card_expiry="12/30", password="secret", username="user@example.com", verify_code="")
    oyun.update_config(legacy)
    oyun.init_plugin(legacy)
    config = oyun.get_config()
    for key in ("cookie", "card_number", "card_cvv", "card_expiry", "password", "username", "verify_code"):
        assert key not in config, key
    assert oyun.read_secrets()["card_number"] == "4242424242424242"
    assert not (data_path / "storage_state.json").exists() and (data_path / "session.enc").exists()
    assert oyun.get_data("known_codes") is None and len(oyun.get_data("known_fp")) == 1
    history = oyun.get_data("history")
    assert history[0]["codes"] == [] and codes_of(oyun, history[0]) == ["XOLDCODE00000001"]
    assert "XOLDCODE00000001" not in json.dumps(history)



def test_bank_page_needs_send_code_click_and_early_code(oyun, site):
    """银行验证页要先点「Kod Gönder」才出现输入框；验证码比输入框先到也要等着填进去"""
    site.set(acs_mode="request")
    _card(oyun)
    ok, _ = oyun.start_purchase()
    assert ok
    prompt = wait_message(oyun, "付款验证")
    assert prompt and "需要输入验证码" in prompt["title"], oyun.messages
    oyun.submit_code("123456")
    assert wait_idle(oyun, 90)
    record = oyun.get_data("history")[-1]
    assert record["code"] == "success", record
    assert len(site.posts("/acs/request")) == 1 and site.state()["otp_attempts"] == 1



# ---------------------------------------------------------------- 真实分类页的几种写法
def test_listing_rendered_by_script_with_div_buttons(oyun, site):
    """商品列表由脚本延迟渲染、「SEPETE EKLE」是 div + 图标字体，加入购物车用 fetch 不跳页"""
    site.set(listing_style="ajax")
    oyun.init_plugin(make_config(product_url=f"{site.url}{CATEGORY}", amount="250"))
    record = run(oyun)
    assert record["code"] == "success", record
    assert record["product"] == "250 TL App Store Card"
    assert [(item["product"], item["from"]) for item in _cart_posts(site)] == [("250", "ajax")]
    assert codes_of(oyun, record) == ["X01001QZ1ABCDEFG"]


def test_script_listing_custom_amount(oyun, site):
    site.set(listing_style="ajax")
    oyun.init_plugin(make_config(amount="150"))
    record = run(oyun)
    assert record["code"] == "success", record
    assert _cart_posts(site)[0]["tutar"] == "150" and record["total"] == 150.0


def test_listing_without_add_buttons_falls_back_to_detail_page(oyun, site):
    """列表上认不出「Sepete Ekle」：打开对应商品的 DETAY 详情页再买"""
    site.set(listing_style="nobuttons")
    oyun.init_plugin(make_config(product_url=f"{site.url}{CATEGORY}", amount="250"))
    record = run(oyun)
    assert record["code"] == "success", record
    assert orders(site)[1001]["product"] == "250" and record["total"] == 262.5


def test_failure_message_lists_what_was_seen(oyun, site):
    """完全认不出列表时不能乱买：停下，并在失败消息里写明页面上看到了什么，方便对照真实页面调整"""
    site.set(listing_style="empty")
    oyun.init_plugin(make_config(product_url=f"{site.url}{CATEGORY}", amount="250"))
    record = run(oyun)
    assert record["code"] == "failed", record
    assert "看到的按钮" in record["message"], record
    assert not _cart_posts(site)


# ---------------------------------------------------------------- 购物车 → 结算的几种写法
def test_icon_only_cart_link_and_other_cart_path(oyun, site):
    """页头只有购物车图标（没有文字），购物车地址也不是 /sepet"""
    site.set(header_cart="icon", cart_path="/sepetim")
    record = run(oyun)
    assert record["code"] == "success", record
    assert codes_of(oyun, record) == ["X01001QZ1ABCDEFG"]


def test_dry_run_never_submits_risky_cart_button(oyun, site):
    """购物车的下一步是写着「SATIN AL」的提交按钮：演练不点（可能直接下单），实买才点"""
    site.set(cart_button="satinal_submit")
    oyun.init_plugin(make_config(dry_run=True))
    record = run(oyun)
    assert record["code"] == "dry_run", record
    assert "演练停在购物车" in record["message"] and "SATIN AL" in record["message"]
    assert not site.posts("/sepet/onay") and not site.posts("/odeme")
    oyun.init_plugin(make_config())
    record = run(oyun)
    assert record["code"] == "success", record
    assert len(site.posts("/sepet/onay")) == 1


def test_dry_run_follows_risky_named_plain_link(oyun, site):
    """「SATIN AL」只是个普通链接：演练直接打开链接（GET 不会下单），继续走到付款前一步"""
    site.set(cart_button="satinal_link")
    oyun.init_plugin(make_config(dry_run=True))
    record = run(oyun)
    assert record["code"] == "dry_run", record
    assert "付款前一步" in record["message"] and record["total"] == 262.5
    assert not site.posts("/odeme")


def test_balance_option_named_oyunfor_bakiyesi(oyun, site):
    site.set(balance_label="Oyunfor Bakiyesi")
    record = run(oyun)
    assert record["code"] == "success", record
    assert site.posts("/odeme")[-1]["data"]["method"] == "bakiye"



def test_real_like_cart_page_with_alisverisi_tamamla(oyun, site):
    """页头有「Yemeksepeti」活动链接（地址里含 sepet，不能当成购物车），购物车只有图标；
    「Alışverişi Tamamla」在 Oyunfor 上会直接用余额下单 —— 演练绝不能点，实买才点"""
    site.set(header_cart="icon", cart_button="tamamla_submit", cart="100")
    oyun.init_plugin(make_config(dry_run=True, amount="100"))
    record = run(oyun)
    assert record["code"] == "dry_run", record
    assert "演练停在购物车" in record["message"] and "Alışverişi Tamamla" in record["message"], record
    assert not site.posts("/sepet/onay") and not site.posts("/odeme")
    # 访问过的页面里不能有活动页
    assert not [item for item in site.state()["log"] if item["path"] == "/mocanetwork/yemeksepeti"]
    oyun.init_plugin(make_config(amount="100"))
    record = run(oyun)
    assert record["code"] == "success", record



def test_oyunfor_cart_enough_balance_order_button_not_clicked_in_dry_run(oyun, site):
    """真实购物车、余额够：按钮是「Alışverişi Tamamla」(.siparisTamamla)，点了直接用余额下单。
    演练停在它前面；实买时它就是付款按钮"""
    site.set(cart_button="oyunfor", cart_path="/sepetim", header_cart="icon")
    oyun.init_plugin(make_config(dry_run=True))
    record = run(oyun)
    assert record["code"] == "dry_run", record
    assert "付款前一步" in record["message"] and "Alışverişi Tamamla" in record["message"], record
    assert record["total"] == 262.5
    assert not site.posts("/orders/siparisTamamla") and site.state()["balance"] == 1500.0
    oyun.init_plugin(make_config())
    record = run(oyun)
    assert record["code"] == "success", record
    assert len(site.posts("/orders/siparisTamamla")) == 1
    assert codes_of(oyun, record) == ["X01001QZ1ABCDEFG"]


def test_oyunfor_cart_enough_balance_but_card_mode_stops(oyun, site):
    """余额够时网站只给「Alışverişi Tamamla」（用余额下单），选了银行卡就停下并说明原因"""
    site.set(cart_button="oyunfor", cart_path="/sepetim", header_cart="icon")
    _card(oyun, dry_run=True)
    record = run(oyun)
    assert record["code"] == "failed", record
    assert "余额已经够付" in record["message"]
    assert not site.posts("/orders/siparisTamamla") and not site.posts("/payment/paymentSender")


def test_oyunfor_cart_short_balance_goes_to_topup_page(oyun, site):
    """真实购物车、余额不够：按钮是「Ödeme Yap」，只跳到 /profil/odemeyap/<差额> 补差额页（不下单）；
    选「Kredi Kartı」→ 付款渠道 →「Ödeme Yap」在新标签页打开 PayTR，演练停在卡表单前"""
    site.set(cart_button="oyunfor", cart_path="/sepetim", header_cart="icon", balance="0")
    _card(oyun, dry_run=True)
    record = run(oyun)
    assert record["code"] == "dry_run", record
    assert "银行卡页面" in record["message"], record
    assert record["total"] == 262.5
    assert any(item["path"] == "/profil/odemeyap/262/50" for item in site.state()["log"])
    assert len(site.posts("/payment/paymentSender")) == 1
    assert site.posts("/payment/paymentSender")[0]["data"]["method"] == "kart"
    assert not site.posts("/paytr/pay") and not site.posts("/orders/siparisTamamla")


def test_real_cart_row_close_icon_clears_leftovers(oyun, site):
    """真实购物车：删除只有右上角一个「×」（没有文字和提示）—— 演练留下的商品也要能清掉"""
    site.set(cart_row_style="real", header_cart="icon", cart_button="tamamla_submit", cart="100,250")
    oyun.init_plugin(make_config(amount="100"))
    record = run(oyun)
    assert record["code"] == "success", record
    assert record["total"] == 105.0
    assert len(site.posts("/sepet/sil")) == 2
    assert codes_of(oyun, record) == ["X01001QZ1ABCDEFG"]


def test_cart_quantity_is_aligned(oyun, site):
    """加入购物车后数量变成了 2（比如网站合并了上次的同款），插件用「−」改回 1 再结算"""
    site.set(cart_row_style="real", cart_button="tamamla_submit", add_qty="2")
    oyun.init_plugin(make_config(amount="100"))
    record = run(oyun)
    assert record["code"] == "success", record
    assert record["total"] == 105.0
    assert [item["data"].get("op") for item in site.posts("/sepet/adet")] == ["minus"]


# ---------------------------------------------------------------- 网站出错
def test_cloudflare_520_is_retried(oyun, site):
    """Oyunfor 偶尔返回 Cloudflare 520（源服务器出错）：等一会儿重试，恢复后照常购买"""
    site.set(cf_error_left="2")
    record = run(oyun)
    assert record["code"] == "success", record
    assert codes_of(oyun, record) == ["X01001QZ1ABCDEFG"]


def test_cloudflare_520_keeps_failing(oyun, site):
    """一直 520：重试几次后停下，说明是网站的问题；不会把错误页标题当成商品名，也不会加购物车"""
    site.set(cf_error_left="9")
    oyun.init_plugin(make_config(dry_run=True))
    record = run(oyun)
    assert record["code"] == "site_error", record
    assert "520" in record["message"] and "稍后再试" in record["message"], record
    assert "Web server" not in (record.get("product") or "")
    assert not site.posts("/sepet/ekle")


# ---------------------------------------------------------------- 机器人选面额
def _button(event_text, **extra):
    data = {"plugin_id": "OyunforGiftCard", "text": event_text, "channel": "Telegram", "userid": "42"}
    data.update(extra)
    return Event(EventType.MessageAction, data)


def _callbacks(message):
    return [b.get("callback_data", "").split("|", 1)[-1] for row in message.get("buttons") or [] for b in row]


def test_bot_buy_without_args_shows_amount_menu(oyun, site):
    """/oyunfor_buy 不带参数：不直接买，发面额按钮（超过单笔上限的面额不显示）"""
    oyun.init_plugin(make_config(max_total=1000))
    oyun.handle_command(Event(EventType.PluginAction, {"action": "oyunforgiftcard_buy", "arg_str": "",
                                                       "channel": "Telegram", "user": "42"}))
    assert oyun._job is None
    menu = oyun.messages[-1]
    assert "选择面额" in menu["title"] and menu["userid"] == "42"
    assert _callbacks(menu) == ["pick:100:1", "pick:250:1", "pick:500:1", "pick:1000:1", "nobuy"]


def test_bot_pick_confirm_then_dry_run(oyun, site):
    """点面额 → 确认消息（可改数量）→ 点「演练」才开始；演练模式下主按钮也是演练"""
    oyun.init_plugin(make_config(dry_run=True, notify=False))
    oyun.handle_button(_button("pick:100:2"))
    assert oyun._job is None
    confirm = oyun.messages[-1]
    assert "确认购买" in confirm["title"] and "100 TL App Store 卡 × 2" in confirm["text"]
    assert "演练模式" in confirm["text"]
    go = [c for c in _callbacks(confirm) if c.startswith("dry:100:2:")]
    assert go and not [c for c in _callbacks(confirm) if c.startswith("buy:")]
    oyun.handle_button(_button("pick:100:1"))
    go = [c for c in _callbacks(oyun.messages[-1]) if c.startswith("dry:100:1:")][0]
    oyun.handle_button(_button(go))
    assert wait_idle(oyun, 120)
    record = oyun.get_data("history")[-1]
    assert record["code"] == "dry_run" and record["face"] == 100 and record["trigger"] == "按钮", record
    assert all(m.get("userid") == "42" for m in oyun.messages)


def test_bot_expired_or_bad_buy_button_does_not_buy(oyun, site):
    oyun.handle_button(_button("buy:100:1:1"))          # 1970 年的时间戳 = 过期
    assert oyun._job is None and "过期" in oyun.messages[-1]["text"]
    assert "选择面额" in oyun.messages[-1]["title"]
    oyun.handle_button(_button("buy:100:1"))            # 没有时间戳
    assert oyun._job is None
    oyun.handle_button(_button("buy:5000:1:ffffffffff"))  # 面额超范围
    assert oyun._job is None and "100~2000" in oyun.messages[-1]["text"]
    assert not site.posts("/sepet/ekle")


def test_verification_message_hides_card(oyun):
    """验证提醒里不再写「如 OKX」，页面摘要里的卡号片段打码"""
    from app.plugins.oyunforgiftcard import _Job
    job = _Job("purchase", "命令", {"channel": "Telegram", "userid": "42"})
    oyun.notify_verification(job, "otp", "mastercard identity check paytr try 102.50 533171*****1307 verify")
    text = oyun.messages[-1]["text"]
    assert "OKX" not in text and "533171" not in text and "1307" not in text
    assert "102.50" in text


def test_bot_buttons_update_the_same_message(oyun, site):
    """点按钮时 MoviePilot 会带上原消息 ID：菜单 → 确认 → 开始 都在同一条消息上原地更新，结果消息才新发"""
    oyun.init_plugin(make_config(dry_run=True, notify=False))
    origin = {"source": "tg1", "original_message_id": 555, "original_chat_id": 42}

    def edited(message):
        return (message.get("original_message_id") == 555 and message.get("original_chat_id") == "42"
                and bool(message.get("buttons")))

    oyun.handle_button(_button("pick:100:1", **origin))
    confirm = oyun.messages[-1]
    assert "确认购买" in confirm["title"] and edited(confirm)
    oyun.handle_button(_button("menu", **origin))
    assert "选择面额" in oyun.messages[-1]["title"] and edited(oyun.messages[-1])
    oyun.handle_button(_button("nobuy", **origin))
    assert "不买了" in oyun.messages[-1]["text"] and edited(oyun.messages[-1])
    go = [c for c in _callbacks(confirm) if c.startswith("dry:100:1:")][0]
    oyun.handle_button(_button(go, **origin))
    assert wait_idle(oyun, 120)
    start = [m for m in oyun.messages if "开始" in m["title"]][-1]
    assert edited(start) and _callbacks(start) == ["cancel"]
    later = oyun.messages[oyun.messages.index(start) + 1:]
    assert later and all("original_message_id" not in m for m in later)
    assert oyun.get_data("history")[-1]["code"] == "dry_run"


def test_slow_paytr_page_in_fast_mode(oyun, site):
    """PayTR 页面过几秒才把已保存的卡画出来：等它（按真实时间），不能因为停顿被缩短就判「没有找到已保存的卡」"""
    site.set(saved_card="1", acs_mode="none", gateway_style="paytr2026", gateway_delay="4000",
             cart_button="oyunfor", cart_path="/sepetim", header_cart="icon", balance="0")
    oyun.init_plugin(make_config(pay_mode="card"))
    record = run(oyun)
    assert record["code"] == "success", record
    assert site.posts("/paytr/pay")[-1]["data"].get("saved") == "tok_4242"


def test_site_redirects_to_cart_after_add_and_slow_fonts(oyun, site):
    """加入购物车后网站自己 1 秒后跳到购物车（不抢着跳，不报 ERR_ABORTED）；网页字体下不完时截图不卡住"""
    site.set(listing_style="ajax", add_redirect_ms="1000", cart_path="/sepetim", header_cart="icon",
             cart_button="oyunfor", slow_font="20")
    began = time.time()
    record = run(oyun)
    assert record["code"] == "success", record
    run_dir = oyun.get_data_path() / "runs" / record["run"]
    steps = oyun._read_run_file(run_dir / "steps.log.enc").decode("utf-8")
    assert "截图失败" not in steps and "ERR_ABORTED" not in steps, steps
    assert time.time() - began < 100
