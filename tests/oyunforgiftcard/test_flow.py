"""
端到端流程：真实 MoviePilot V2 宿主 + 真实 Chromium + 本地模拟 Oyunfor/支付网关/银行 3DS。
"""
import json
import os
import sys

import pytest

from app.core.event import Event
from app.schemas.types import EventType

from .conftest import CARD, make_config, run, wait_idle, wait_message

OLD_CODE = "XOLDCODE00000001"


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
    assert any(n.endswith("dry-run.jpg") for n in names), names
    assert "steps.log" in names


def test_balance_purchase_reveals_code_on_success_page(oyun, site):
    record = run(oyun)
    assert record["code"] == "success", record
    assert record["codes"] == ["X01001QZ1ABCDEFG"]
    assert record["total"] == 262.5
    assert site.state()["balance"] == 1237.5
    assert oyun.get_data("known_codes") == ["X01001QZ1ABCDEFG"]
    final = oyun.messages[-1]
    assert "购买成功" in final["title"] and "X01001QZ1ABCDEFG" in final["text"]
    # 结算页页脚的「teşekkür ederiz」不能被当成付款成功：必须真的提交了订单
    assert len(site.posts("/odeme")) == 1


def test_codes_from_newest_order_not_old_ones(oyun, site):
    site.set(success_shows_code="0")
    record = run(oyun)
    assert record["code"] == "success", record
    assert record["codes"] == ["X01001QZ1ABCDEFG"]
    assert OLD_CODE not in record["codes"]


def test_add_to_cart_dialog_flow_and_quantity(oyun, site):
    site.set(show_buy_now="0")
    record = run(oyun, quantity=2)
    assert record["code"] == "success", record
    assert record["total"] == 525.0
    assert record["codes"] == ["X01001QZ1ABCDEFG", "X01001QZ2ABCDEFG"]


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
def _card_config(**extra):
    config = make_config(pay_mode="card", **CARD)
    config.update(extra)
    return config


def test_card_dry_run_reaches_gateway_without_submitting(oyun, site):
    oyun.init_plugin(_card_config(dry_run=True))
    record = run(oyun)
    assert record["code"] == "dry_run", record
    assert "银行卡页面" in record["message"]
    assert not site.posts("/paytr/pay")


def test_card_otp_wrong_then_right(oyun, site):
    oyun.init_plugin(_card_config())
    ok, message = oyun.start_purchase()
    assert ok, message
    prompt = wait_message(oyun, "需要你验证付款")
    assert prompt, oyun.messages
    assert "/oyunfor_code" in prompt["text"]
    assert prompt["buttons"][0][0]["callback_data"] == "[PLUGIN]OyunforGiftCard|approved"
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
    assert record["codes"] == ["X01001QZ1ABCDEFG"]
    assert site.state()["otp_attempts"] == 2
    posted = site.posts("/paytr/pay")[-1]["data"]
    assert posted["card_number"].replace(" ", "") == "4242424242424242"
    assert posted["expiry_month"] == "12" and posted["expiry_year"] == "2030" and posted["cvv"] == "123"
    # 填卡之后的截图仍然正常（遮挡卡号输入框），保存的页面源码里不能出现卡号
    run_dir = oyun.get_data_path() / "runs" / record["run"]
    assert (run_dir / "steps.log").read_text(encoding="utf-8").count("截图失败") <= 1
    assert any(p.name.endswith("result-success.jpg") for p in run_dir.iterdir())
    for path in run_dir.glob("*.html"):
        text = path.read_text(encoding="utf-8")
        assert "4242424242424242" not in text and "4242 4242 4242 4242" not in text, path.name


def test_card_app_approval(oyun, site):
    site.set(acs_mode="app")
    oyun.init_plugin(_card_config())
    ok, _ = oyun.start_purchase()
    assert ok
    prompt = wait_message(oyun, "需要你验证付款")
    assert prompt and "银行 App" in prompt["text"], oyun.messages
    assert oyun._job.waiting == "app"
    # 用户点了按钮（只是告知），然后在银行 App 里批准
    oyun.handle_button(Event(EventType.MessageAction, {"plugin_id": "OyunforGiftCard", "text": "approved",
                                                       "userid": "u1", "channel": None}))
    order_id = max(orders(site))
    site.approve(order_id)
    assert wait_idle(oyun, 90)
    record = oyun.get_data("history")[-1]
    assert record["code"] == "success", record
    assert record["verification"] == "app"
    assert record["codes"] == [f"X{order_id:05d}QZ1ABCDEFG"]


def test_saved_card(oyun, site):
    site.set(saved_card="1", acs_mode="none")
    oyun.init_plugin(make_config(pay_mode="card", card_use_saved=True, card_last4="4242"))
    record = run(oyun)
    assert record["code"] == "success", record
    assert site.posts("/paytr/pay")[-1]["data"].get("saved") == "tok_4242"


def test_card_cancel_from_button(oyun, site):
    oyun.init_plugin(_card_config())
    ok, _ = oyun.start_purchase()
    assert ok
    assert wait_message(oyun, "需要你验证付款")
    oyun.handle_button(Event(EventType.MessageAction, {"plugin_id": "OyunforGiftCard", "text": "cancel",
                                                       "userid": "u1", "channel": None}))
    assert wait_idle(oyun, 60)
    record = oyun.get_data("history")[-1]
    assert record["code"] == "cancelled", record
    assert orders(site)[1001]["status"] == "Ödeme Bekleniyor"


def test_card_verify_timeout(oyun, site):
    oyun.init_plugin(_card_config())
    oyun._verify_timeout = 4
    record = run(oyun)
    assert record["code"] == "timeout", record
    assert orders(site)[1001]["status"] == "Ödeme Bekleniyor"


# ---------------------------------------------------------------- 登录
def test_login_with_password_and_sms_then_reuse_state(oyun, site):
    site.set(require_sms_login="1")
    oyun.init_plugin(make_config(cookie="oyf_session=EXPIRED", username="user@example.com", password="secret",
                                 dry_run=True))
    ok, _ = oyun.start_purchase()
    assert ok
    prompt = wait_message(oyun, "需要你验证付款")
    assert prompt, oyun.messages
    oyun.submit_code("654321")
    assert wait_idle(oyun, 90)
    record = oyun.get_data("history")[-1]
    assert record["code"] == "dry_run", record
    assert len(site.posts("/giris")) == 1
    assert (oyun.get_data_path() / "storage_state.json").exists()
    # 第二次：沿用保存的登录状态，不再登录
    record = run(oyun)
    assert record["code"] == "dry_run", record
    assert len(site.posts("/giris")) == 1


def test_expired_cookie_without_password(oyun, site):
    oyun.init_plugin(make_config(cookie="oyf_session=EXPIRED"))
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

    site.reset()
    record = run(oyun, amount=1000)
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
    steps = (oyun.get_data_path() / "runs" / record["run"] / "steps.log").read_text(encoding="utf-8")
    assert "CloakBrowser" in steps


def test_code_sent_before_verification_page(oyun, site):
    """验证码比验证页先到（用户手快 / 短信先到），也要等输入框出现后填进去"""
    oyun.init_plugin(_card_config())
    ok, _ = oyun.start_purchase()
    assert ok
    assert oyun.submit_code("123456")[0]
    assert wait_idle(oyun, 120)
    record = oyun.get_data("history")[-1]
    assert record["code"] == "success", record
    assert site.state()["otp_attempts"] == 1
