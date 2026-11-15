"""
宿主集成测试：按 MoviePilot V2 真实启动流程（lifespan）拉起后端，经宿主 HTTP 接口
读取表单/详情页、保存配置触发「立即购买一次」、网页提交验证码、调用插件 API，
并检查远程命令与按钮回调是否注册到宿主。

需要单独一个进程运行：pytest tests/oyunforgiftcard/test_host_integration.py
"""
import io
import json
import time
import zipfile

import pytest

from .conftest import CARD, URL, Site, browser_env, make_config, reset_plugin_data, wait_idle, wait_message

PID = "OyunforGiftCard"


@pytest.fixture(scope="module")
def host():
    from app.db.systemconfig_oper import SystemConfigOper
    from app.schemas.types import SystemConfigKey

    oper = SystemConfigOper()
    installed = oper.get(SystemConfigKey.UserInstalledPlugins) or []
    if PID not in installed:
        installed.append(PID)
        oper.set(SystemConfigKey.UserInstalledPlugins, installed)
    oper.set(f"plugin.{PID}", {})

    from fastapi.testclient import TestClient
    from app.factory import app
    from app.core.security import create_access_token
    from app.db.user_oper import UserOper

    mp = pytest.MonkeyPatch()
    browser_env(mp)
    from app.core.config import settings
    mp.setattr(settings, "API_TOKEN", "test-api-token-0123456789")
    with TestClient(app) as client:
        user = UserOper().get_by_name(name="admin")
        assert user, "宿主应已创建超级管理员"
        token = create_access_token(userid=user.id, username=user.name, super_user=True, level=2)
        client.headers.update({"Authorization": f"Bearer {token}"})

        from app.core.plugin import PluginManager
        instance = PluginManager().running_plugins.get(PID)
        assert instance is not None, "宿主没有加载插件"
        type(instance)._base_url = URL
        type(instance)._pause_scale = 0
        reset_plugin_data(instance)
        instance.messages = []
        original = instance.post_message

        def collect(**kwargs):
            instance.messages.append(kwargs)

        instance.post_message = collect
        Site.reset()
        yield client, instance
        instance.post_message = original
    mp.undo()


def test_host_loaded_plugin(host):
    client, instance = host
    resp = client.get("/api/v1/plugin/installed")
    assert resp.status_code == 200 and PID in resp.json()
    assert instance.plugin_version == "1.1.0"
    assert instance.get_state() is False


def test_form_endpoint(host):
    client, _ = host
    resp = client.get(f"/api/v1/plugin/form/{PID}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["render_mode"] == "vuetify"
    assert data["conf"][0]["component"] == "VForm"
    assert data["model"]["dry_run"] is True
    components = set()

    def walk(node):
        if isinstance(node, dict):
            components.add(node.get("component"))
            for child in node.get("content") or []:
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(data["conf"])
    assert components <= {"VForm", "VRow", "VCol", "VSwitch", "VTextField", "VTextarea", "VSelect",
                          "VCronField", "VAlert"}


def test_save_config_runs_dry_run_once(host):
    client, instance = host
    Site.reset()
    resp = client.put(f"/api/v1/plugin/{PID}", json=make_config(onlyonce=True, dry_run=True))
    assert resp.status_code == 200 and resp.json()["success"] is True
    stored = client.get(f"/api/v1/plugin/{PID}").json()
    assert stored["onlyonce"] is False and stored["enabled"] is True
    assert wait_idle(instance, 120)
    history = instance.get_data("history")
    assert history and history[-1]["code"] == "dry_run", history
    page = client.get(f"/api/v1/plugin/page/{PID}").json()["page"]
    assert "演练完成" in json.dumps(page, ensure_ascii=False)


def test_commands_registered(host):
    from app.command import Command

    commands = Command().get_commands()
    for name in ("/oyunfor_buy", "/oyunfor_code", "/oyunfor_paid", "/oyunfor_cancel", "/oyunfor_status"):
        assert name in commands, list(commands.keys())


def test_status_command_through_event_bus(host):
    client, instance = host
    from app.core.event import eventmanager
    from app.schemas.types import EventType

    assert client.put(f"/api/v1/plugin/{PID}", json=make_config()).json()["success"] is True
    start = len(instance.messages)
    eventmanager.send_event(EventType.PluginAction, {"action": "oyunforgiftcard_status", "channel": None,
                                                     "user": "7"})
    assert wait_message(instance, "【Oyunfor 账户】", timeout=90, start=start), instance.messages[start:]
    assert wait_idle(instance, 60)
    account = instance.get_data("account")
    assert account["logged_in"] is True and account["balance"] == 1500.0


def test_card_purchase_with_code_from_web_form(host):
    """3DS 等待中：在网页插件设置里填验证码并保存（走宿主保存配置接口）"""
    client, instance = host
    Site.reset()
    config = make_config(pay_mode="card", **CARD)
    resp = client.put(f"/api/v1/plugin/{PID}", json=config)
    assert resp.json()["success"] is True
    start = len(instance.messages)
    ok, message = instance.start_purchase()
    assert ok, message
    assert wait_message(instance, "需要你验证付款", start=start), instance.messages[start:]
    resp = client.put(f"/api/v1/plugin/{PID}", json=dict(config, verify_code="123456"))
    assert resp.json()["success"] is True
    # 验证码字段保存后被清空，任务继续
    assert client.get(f"/api/v1/plugin/{PID}").json()["verify_code"] == ""
    assert wait_idle(instance, 90)
    record = instance.get_data("history")[-1]
    assert record["code"] == "success", record
    assert record["codes"] == ["X01001QZ1ABCDEFG"]


def test_card_purchase_with_code_from_api_and_button(host):
    client, instance = host
    Site.reset()
    from app.core.config import settings
    from app.core.event import eventmanager
    from app.schemas.types import EventType

    assert client.put(f"/api/v1/plugin/{PID}", json=make_config(pay_mode="card", **CARD)).json()["success"]
    # 模拟站点重置后订单号会重复，清掉插件记住的旧兑换码
    instance.del_data("known_codes")
    start = len(instance.messages)
    ok, message = instance.start_purchase()
    assert ok, message
    assert wait_message(instance, "需要你验证付款", start=start)
    resp = client.get(f"/api/v1/plugin/{PID}/code", params={"code": "123456", "apikey": settings.API_TOKEN})
    assert resp.status_code == 200 and resp.json()["success"] is True, resp.text
    assert wait_idle(instance, 90)
    assert instance.get_data("history")[-1]["code"] == "success"

    # 按钮回调经由宿主事件总线送达
    Site.reset()
    Site.set(acs_mode="app")
    start = len(instance.messages)
    ok, _ = instance.start_purchase()
    assert ok
    assert wait_message(instance, "需要你验证付款", start=start)
    eventmanager.send_event(EventType.MessageAction, {"plugin_id": PID, "text": "cancel", "userid": "7",
                                                      "channel": None})
    assert wait_idle(instance, 60)
    assert instance.get_data("history")[-1]["code"] == "cancelled"


def test_binance_qr_endpoint_is_anonymous_with_token(host):
    """MoviePilot 取二维码图片时不带登录信息：接口要匿名可用，但必须带对一次性令牌"""
    client, instance = host
    Site.reset()
    assert client.put(f"/api/v1/plugin/{PID}", json=make_config(pay_mode="binance")).json()["success"]
    instance.del_data("known_codes")
    start = len(instance.messages)
    ok, message = instance.start_purchase()
    assert ok, message
    prompt = wait_message(instance, "请用币安付款", start=start)
    assert prompt, instance.messages[start:]
    token = instance._job.qr_token
    path = f"/api/v1/plugin/{PID}/qr"
    resp = client.get(path, params={"token": token}, headers={"Authorization": "Bearer invalid"})
    assert resp.status_code == 200 and resp.headers["content-type"] == "image/png"
    assert resp.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert client.get(path, params={"token": "x" * 32}).status_code == 404
    Site.binance_pay(1001)
    assert wait_idle(instance, 90)
    assert instance.get_data("history")[-1]["code"] == "success"
    assert client.get(path, params={"token": token}).status_code == 404


def test_debug_zip_endpoint(host):
    client, instance = host
    from app.core.config import settings

    resp = client.get(f"/api/v1/plugin/{PID}/debug", params={"apikey": settings.API_TOKEN})
    assert resp.status_code == 200, resp.text
    archive = zipfile.ZipFile(io.BytesIO(resp.content))
    names = archive.namelist()
    assert any(name.endswith("steps.log") for name in names)
    assert any(name.endswith(".jpg") for name in names)
    resp = client.get(f"/api/v1/plugin/{PID}/debug")
    assert resp.status_code in (401, 403)


def test_disable_keeps_no_service(host):
    client, instance = host
    resp = client.put(f"/api/v1/plugin/{PID}", json=make_config(enabled=False, cron="0 9 1 * *"))
    assert resp.json()["success"] is True
    assert instance.get_state() is False and instance.get_service() == []
    resp = client.put(f"/api/v1/plugin/{PID}", json=make_config(enabled=True, cron="0 9 1 * *"))
    assert instance.get_service()[0]["id"] == PID
    from app.scheduler import Scheduler
    time.sleep(1)
    assert [job for job in Scheduler().list() if "Oyunfor" in (job.name or "")]
