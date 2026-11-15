"""
Oyunfor 插件测试装配：插件复制到真实的 MoviePilot V2 宿主里加载，站点指向本地模拟服务器。
（宿主环境、CONFIG_DIR、数据库初始化由上级 tests/conftest.py 负责）
"""
import os
import shutil
import sys
import time
from pathlib import Path

import pytest
import requests

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
HOST = Path(os.environ.get("MP_HOST_DIR", "")).resolve()
PLUGIN_SRC = REPO / "plugins.v2" / "oyunforgiftcard"
PLUGIN_DST = HOST / "app" / "plugins" / "oyunforgiftcard"

if PLUGIN_DST.exists():
    shutil.rmtree(PLUGIN_DST)
shutil.copytree(PLUGIN_SRC, PLUGIN_DST, ignore=shutil.ignore_patterns("__pycache__"))
sys.path.insert(0, str(HERE))

import mock_oyunfor  # noqa: E402

PORT = 18766
URL = f"http://127.0.0.1:{PORT}"
CARD = {"card_number": "4242 4242 4242 4242", "card_holder": "TEST USER", "card_expiry": "12/30", "card_cvv": "123"}


@pytest.fixture(scope="session", autouse=True)
def oyun_server():
    srv = mock_oyunfor.start_background(PORT)
    yield srv
    srv.shutdown()


class Site:
    url = URL

    @staticmethod
    def set(**kwargs):
        requests.get(f"{URL}/__admin/set", params=kwargs, timeout=5)

    @staticmethod
    def state():
        return requests.get(f"{URL}/__admin/state", timeout=5).json()

    @staticmethod
    def reset():
        requests.get(f"{URL}/__admin/reset", timeout=5)

    @staticmethod
    def binance_pay(order):
        requests.get(f"{URL}/__admin/binance_pay", params={"order": order}, timeout=5)

    @staticmethod
    def approve(order):
        requests.get(f"{URL}/__admin/approve", params={"order": order}, timeout=5)

    @classmethod
    def posts(cls, path):
        return [item for item in cls.state()["log"] if item["method"] == "POST" and item["path"] == path]


def make_config(**overrides):
    config = {
        "enabled": True, "notify": True, "onlyonce": False, "dry_run": False,
        "cookie": "oyf_session=VALID", "username": "", "password": "",
        "amount": "250", "product_url": "", "quantity": 1,
        "pay_mode": "balance", "pay_timeout": 15, "card_use_saved": False, "card_last4": "",
        "card_number": "", "card_holder": "", "card_expiry": "", "card_cvv": "",
        "max_total": 2000, "daily_limit": 0, "verify_timeout": 120,
        "verify_code": "", "cron": "", "use_proxy": False, "proxy_url": "", "nav_timeout": 30,
        "headless": False, "clear_cart": True, "show_code": True, "keep_runs": 100,
        "selectors": "", "history_days": 180,
    }
    config.update(overrides)
    return config


def browser_env(monkeypatch):
    """测试默认用 Playwright + 本机 Chromium（CloakBrowser 需要联网下载内核）"""
    monkeypatch.setitem(sys.modules, "cloakbrowser", None)
    if os.environ.get("TEST_CHROMIUM"):
        monkeypatch.setenv("OYUNFOR_BROWSER_EXECUTABLE", os.environ["TEST_CHROMIUM"])


def reset_plugin_data(instance):
    for key in ("history", "known_codes", "account", "last_screenshot", "state_hash"):
        instance.del_data(key)
    data_path = instance.get_data_path()
    (data_path / "storage_state.json").unlink(missing_ok=True)
    shutil.rmtree(data_path / "runs", ignore_errors=True)


@pytest.fixture()
def site():
    Site.reset()
    return Site


@pytest.fixture()
def oyun(site, monkeypatch):
    """全新的插件实例：清空数据、站点指向模拟服务器、通知改为收集"""
    from app.plugins.oyunforgiftcard import OyunforGiftCard

    browser_env(monkeypatch)
    instance = OyunforGiftCard()
    reset_plugin_data(instance)
    instance._base_url = URL
    instance._pause_scale = 0
    instance.messages = []

    def fake_post_message(**kwargs):
        instance.messages.append(kwargs)

    monkeypatch.setattr(instance, "post_message", fake_post_message)
    instance.init_plugin(make_config())
    yield instance
    wait_idle(instance, 60)


def wait_idle(instance, timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if instance._job is None:
            return True
        time.sleep(0.3)
    return False


def wait_message(instance, keyword, timeout=90, start=0, grace=8):
    began = time.time()
    deadline = began + timeout
    while time.time() < deadline:
        for message in instance.messages[start:]:
            if keyword in (message.get("title") or "") or keyword in (message.get("text") or ""):
                return message
        # 任务可能还没开始（事件异步分发），给一点宽限时间再判断任务是否已结束
        if instance._job is None and time.time() - began > grace:
            for message in instance.messages[start:]:
                if keyword in (message.get("title") or "") or keyword in (message.get("text") or ""):
                    return message
            return None
        time.sleep(0.3)
    return None


def run(instance, timeout=150, **kwargs):
    ok, message = instance.start_purchase(**kwargs)
    assert ok, message
    assert wait_idle(instance, timeout), "任务没有结束"
    return instance.get_data("history")[-1]
