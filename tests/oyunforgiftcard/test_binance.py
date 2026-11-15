"""
币安支付（Binance Pay）流程：插件下单 → 把付款链接/二维码推送给用户 → 用户在币安 App 付款 → 推送兑换码。
"""
import time

from app.core.event import Event
from app.schemas.types import EventType

from .conftest import make_config, run, wait_idle, wait_message


def _binance(oyun, **extra):
    oyun.init_plugin(make_config(pay_mode="binance", **extra))


def _start_and_get_prompt(oyun):
    ok, message = oyun.start_purchase()
    assert ok, message
    prompt = wait_message(oyun, "请用币安付款")
    assert prompt, oyun.messages
    return prompt


def test_binance_dry_run_does_not_create_payment(oyun, site):
    _binance(oyun, dry_run=True)
    record = run(oyun)
    assert record["code"] == "dry_run", record
    assert "binance pay" in record["message"].lower() or "付款前一步" in record["message"]
    assert not site.posts("/odeme")


def test_binance_onsite_qr_and_link_then_auto_redirect(oyun, site):
    _binance(oyun)
    prompt = _start_and_get_prompt(oyun)
    text = prompt["text"]
    assert "6,25 USDT" in text
    assert "https://app.binance.com/payment/secpay?linkToken=LT1001abc" in text
    # 页脚的币安首页链接不能当成付款链接
    assert "binance.com/en" not in text
    assert prompt["buttons"][0][0]["url"].startswith("https://app.binance.com/payment/secpay")
    assert prompt["buttons"][1][0]["callback_data"] == "[PLUGIN]OyunforGiftCard|paid"
    job = oyun._job
    assert job.waiting == "binance"
    assert prompt["image"] == f"http://127.0.0.1:3001/api/v1/plugin/OyunforGiftCard/qr?token={job.qr_token}"
    # 二维码是 220px 的那张图，不是 24px 的币安 Logo
    from PIL import Image
    with Image.open(job.qr_path) as image:
        assert image.size[0] >= 200 and image.size[1] >= 200
    # 二维码接口：令牌正确才给图
    assert oyun.api_qr(job.qr_token).path == str(job.qr_path)
    assert oyun.api_qr("wrong").status_code == 404
    page = str(oyun.get_page())
    assert "等待你用币安付款" in page and "data:image/png;base64," in page
    # 用户在币安 App 里付款 → Oyunfor 页面自动跳到成功页
    site.binance_pay(1001)
    assert wait_idle(oyun, 90)
    record = oyun.get_data("history")[-1]
    assert record["code"] == "success", record
    assert record["codes"] == ["X01001QZ1ABCDEFG"]
    assert record["crypto"] == "6,25 USDT"
    assert record["verification"] == "binance"
    final = oyun.messages[-1]
    assert "购买成功" in final["title"] and "X01001QZ1ABCDEFG" in final["text"] and "6,25 USDT" in final["text"]
    assert oyun.api_qr(job.qr_token).status_code == 404  # 任务结束后令牌失效


def test_binance_page_not_redirecting_user_says_paid(oyun, site):
    site.set(binance_autoredirect="0")
    _binance(oyun)
    _start_and_get_prompt(oyun)
    site.binance_pay(1001)
    start = len(oyun.messages)
    oyun.handle_button(Event(EventType.MessageAction, {"plugin_id": "OyunforGiftCard", "text": "paid",
                                                       "userid": "u1", "channel": None}))
    assert wait_message(oyun, "去订单页确认付款", start=start)
    assert wait_idle(oyun, 90)
    record = oyun.get_data("history")[-1]
    assert record["code"] == "success", record
    assert record["codes"] == ["X01001QZ1ABCDEFG"]


def test_binance_hosted_checkout(oyun, site):
    site.set(binance_mode="hosted")
    _binance(oyun)
    prompt = _start_and_get_prompt(oyun)
    assert f"{site.url.replace('127.0.0.1', 'localhost')}/binance/checkout/1001" in prompt["text"]
    assert "6.25 USDT" in prompt["text"]
    # bnc:// 深链接不放进按钮
    assert all(not b.get("url", "").startswith("bnc://") for row in prompt["buttons"] for b in row)
    site.binance_pay(1001)
    assert wait_idle(oyun, 90)
    record = oyun.get_data("history")[-1]
    assert record["code"] == "success", record
    assert record["codes"] == ["X01001QZ1ABCDEFG"]


def test_binance_option_is_a_button(oyun, site):
    site.set(checkout_style="buttons")
    _binance(oyun, dry_run=True)
    assert run(oyun)["code"] == "dry_run"
    assert not site.posts("/odeme")
    _binance(oyun)
    _start_and_get_prompt(oyun)
    assert site.posts("/odeme")[-1]["data"]["method"] == "binance"
    site.binance_pay(1001)
    assert wait_idle(oyun, 90)
    assert oyun.get_data("history")[-1]["code"] == "success"


def test_binance_timeout_without_payment(oyun, site):
    _binance(oyun)
    oyun._pay_timeout = 0.1  # 6 秒
    record = run(oyun)
    assert record["code"] == "timeout", record
    assert "不要再扫" in record["message"]
    assert site.state()["orders"]["1001"]["status"] == "Ödeme Bekleniyor"
    # 没付款，不计入每日次数
    assert oyun.today_spent_count() == 0


def test_binance_user_said_paid_but_not_delivered(oyun, site):
    _binance(oyun)
    oyun._pay_timeout = 0.2  # 12 秒
    _start_and_get_prompt(oyun)
    assert oyun.submit_code("ok")[0]
    assert wait_idle(oyun, 90)
    record = oyun.get_data("history")[-1]
    assert record["code"] == "unknown", record
    assert "订单页" in record["message"]
    assert oyun.today_spent_count() == 1


def test_binance_cancel(oyun, site):
    _binance(oyun)
    _start_and_get_prompt(oyun)
    oyun.handle_command(Event(EventType.PluginAction, {"action": "oyunforgiftcard_cancel", "channel": None,
                                                       "user": "u1"}))
    assert wait_idle(oyun, 60)
    record = oyun.get_data("history")[-1]
    assert record["code"] == "cancelled", record
    assert "不要再扫" in record["message"]
    time.sleep(0.1)


def test_binance_qr_selector_override(oyun, site):
    """自动识别不到时可以用 binance_qr 指定二维码元素"""
    _binance(oyun, selectors='{"binance_qr": ".binance-pay-box img.qr-code"}')
    _start_and_get_prompt(oyun)
    assert oyun._job.qr_path and oyun._job.qr_path.exists()
    oyun.cancel_job()
    assert wait_idle(oyun, 60)
