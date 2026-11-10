"""
宿主集成测试：按 MoviePilot V2 真实的启动流程（lifespan）拉起后端，
通过宿主 HTTP 接口读取配置表单/详情页、保存配置触发「立即运行一次」，
并检查定时服务与远程命令是否注册成功。

单独在一个进程里运行：pytest test_host_integration.py
"""
import json
import time

import pytest

from conftest import make_config, MOCK_URL, Mock

PID = "NodeSeekCheckin"


@pytest.fixture(scope="module")
def host():
    from app.db.systemconfig_oper import SystemConfigOper
    from app.schemas.types import SystemConfigKey

    oper = SystemConfigOper()
    installed = oper.get(SystemConfigKey.UserInstalledPlugins) or []
    if PID not in installed:
        installed.append(PID)
        oper.set(SystemConfigKey.UserInstalledPlugins, installed)
    # 干净的初始配置：未启用
    oper.set(f"plugin.{PID}", {})

    from fastapi.testclient import TestClient
    from app.factory import app
    from app.core.security import create_access_token
    from app.db.user_oper import UserOper

    with TestClient(app) as client:
        user = UserOper().get_by_name(name="admin")
        assert user, "宿主应已创建超级管理员"
        token = create_access_token(userid=user.id, username=user.name, super_user=True, level=2)
        client.headers.update({"Authorization": f"Bearer {token}"})

        from app.core.plugin import PluginManager
        instance = PluginManager().running_plugins.get(PID)
        assert instance is not None, "宿主没有加载插件"
        # 站点指向本地模拟服务器
        type(instance)._base_url = MOCK_URL
        type(instance)._pause_scale = 0
        for key in ("history", "profile", "last_ok_date"):
            instance.del_data(key)
        Mock.reset()
        yield client, instance


def test_host_loaded_plugin(host):
    client, instance = host
    resp = client.get("/api/v1/plugin/installed")
    assert resp.status_code == 200 and PID in resp.json()
    assert instance.plugin_version == "1.0.0"
    assert instance.get_state() is False


def test_form_endpoint(host):
    client, _ = host
    resp = client.get(f"/api/v1/plugin/form/{PID}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["render_mode"] == "vuetify"
    assert data["conf"][0]["component"] == "VForm"
    model = data["model"]
    for key in ("enabled", "cookie", "cron", "mode", "random_choice", "max_retries", "use_proxy"):
        assert key in model

    # 表单里每个 model 字段都要有默认值，且组件名都是宿主前端支持的
    models, components = set(), set()

    def walk(node):
        if isinstance(node, dict):
            components.add(node.get("component"))
            props = node.get("props") or {}
            if props.get("model"):
                models.add(props["model"])
            for child in node.get("content") or []:
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(data["conf"])
    assert models == set(model.keys())
    assert components <= {"VForm", "VRow", "VCol", "VSwitch", "VTextField", "VTextarea", "VSelect",
                          "VCronField", "VAlert"}


def test_page_endpoint_empty(host):
    client, _ = host
    resp = client.get(f"/api/v1/plugin/page/{PID}")
    assert resp.status_code == 200
    page = resp.json()["page"]
    assert page[0]["component"] == "VAlert"


def test_save_config_runs_once_and_registers_service(host):
    client, instance = host
    config = make_config(onlyonce=True, mode="auto", max_retries=0)
    resp = client.put(f"/api/v1/plugin/{PID}", json=config)
    assert resp.status_code == 200 and resp.json()["success"] is True

    # 「立即运行一次」开关保存后自动复位
    stored = client.get(f"/api/v1/plugin/{PID}").json()
    assert stored["onlyonce"] is False and stored["enabled"] is True
    assert stored["cookie"] == config["cookie"]

    # 3 秒后在后台线程执行
    deadline = time.time() + 40
    history = []
    while time.time() < deadline:
        history = instance.get_data("history") or []
        if history:
            break
        time.sleep(0.5)
    assert history and history[-1]["code"] == "success", history

    # 定时服务已注册到宿主调度器
    from app.scheduler import Scheduler
    jobs = [job for job in Scheduler().list() if "NodeSeek" in (job.name or "")]
    assert jobs, [job.name for job in Scheduler().list()]
    assert instance.get_service()[0]["id"] == PID

    # 详情页展示历史
    page = client.get(f"/api/v1/plugin/page/{PID}").json()["page"]
    assert page[1]["component"] == "VTable"
    text = json.dumps(page, ensure_ascii=False)
    assert "签到成功" in text and "直连" in text
    assert "VALID" not in text


def test_remote_command_registered_and_works(host):
    client, instance = host
    from app.command import Command
    from app.core.event import eventmanager
    from app.schemas.types import EventType

    commands = Command().get_commands()
    assert "/nodeseek_sign" in commands, list(commands.keys())

    before = len(instance.get_data("history") or [])
    Mock.set(signed="0")
    eventmanager.send_event(EventType.PluginAction, {"action": "nodeseekcheckin_sign", "channel": None, "user": None})
    deadline = time.time() + 30
    while time.time() < deadline:
        if len(instance.get_data("history") or []) > before:
            break
        time.sleep(0.5)
    history = instance.get_data("history")
    assert len(history) == before + 1 and history[-1]["code"] == "success"


def test_browser_channel_inside_running_host(host):
    """宿主事件循环运行期间，由「立即运行一次」触发浏览器过盾（验证线程模型在真实宿主里可用）"""
    client, instance = host
    Mock.reset()
    Mock.set(cf="all")
    instance.del_data("profile")
    before = len(instance.get_data("history") or [])
    resp = client.put(f"/api/v1/plugin/{PID}", json=make_config(onlyonce=True, mode="auto", max_retries=0))
    assert resp.json()["success"] is True
    deadline = time.time() + 90
    while time.time() < deadline:
        if len(instance.get_data("history") or []) > before:
            break
        time.sleep(0.5)
    history = instance.get_data("history")
    assert len(history) == before + 1, history
    assert history[-1]["code"] == "success" and history[-1]["channel"] == "浏览器过盾", history[-1]
    assert Mock.state()["clearance_issued"] == 1


def test_disable_removes_service(host):
    client, instance = host
    resp = client.put(f"/api/v1/plugin/{PID}", json=make_config(enabled=False))
    assert resp.json()["success"] is True
    assert instance.get_state() is False
    assert instance.get_service() == []
