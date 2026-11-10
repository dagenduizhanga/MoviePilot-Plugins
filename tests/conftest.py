"""
测试装配：在真实的 MoviePilot V2 宿主源码里加载插件，站点换成本地模拟服务器。
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# MoviePilot V2 后端源码目录（需已按官方开发文档放好 app/helper 下的资源文件并装好依赖）
HOST = Path(os.environ.get("MP_HOST_DIR", "")).resolve()
if not (HOST / "app" / "plugins").is_dir():
    raise RuntimeError("请先设置环境变量 MP_HOST_DIR 指向 MoviePilot V2 后端源码目录")
PLUGIN_SRC = REPO / "plugins.v2" / "nodeseekcheckin"
PLUGIN_DST = HOST / "app" / "plugins" / "nodeseekcheckin"
CONFIG = Path(os.environ.get("MP_CONFIG_DIR") or tempfile.mkdtemp(prefix="mp-config-"))

os.environ["CONFIG_DIR"] = str(CONFIG)
CONFIG.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(HOST))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# 与宿主安装插件的方式一致：插件目录放到 app/plugins/<id 小写>/
PLUGIN_DST.mkdir(parents=True, exist_ok=True)
for item in PLUGIN_SRC.iterdir():
    if item.is_file():
        shutil.copy2(item, PLUGIN_DST / item.name)

import pytest  # noqa: E402
import requests  # noqa: E402

import mock_ns  # noqa: E402

MOCK_PORT = 18765
MOCK_URL = f"http://127.0.0.1:{MOCK_PORT}"


@pytest.fixture(scope="session", autouse=True)
def _host_db():
    from app.db.init import init_db, update_db

    init_db()
    update_db()
    yield


@pytest.fixture(scope="session", autouse=True)
def _mock_server():
    srv = mock_ns.start_background(MOCK_PORT)
    yield srv
    srv.shutdown()


class Mock:
    url = MOCK_URL

    @staticmethod
    def set(**kwargs):
        requests.get(f"{MOCK_URL}/__admin/set", params=kwargs, timeout=5)

    @staticmethod
    def state():
        return requests.get(f"{MOCK_URL}/__admin/log", timeout=5).json()

    @staticmethod
    def reset():
        requests.get(f"{MOCK_URL}/__admin/reset", timeout=5)

    @classmethod
    def business_log(cls):
        return [item for item in cls.state()["log"] if item["method"] in ("GET", "POST")]


@pytest.fixture()
def mock():
    Mock.reset()
    return Mock


@pytest.fixture()
def plugin(mock, monkeypatch):
    """全新的插件实例：清空插件数据，站点指向模拟服务器，通知改为收集"""
    from app.plugins.nodeseekcheckin import NodeSeekCheckin

    instance = NodeSeekCheckin()
    for key in ("history", "profile", "last_ok_date"):
        instance.del_data(key)
    instance._base_url = MOCK_URL
    instance._pause_scale = 0
    instance._retry_count = 0
    instance._retry_date = ""
    instance.messages = []

    def fake_post_message(**kwargs):
        instance.messages.append(kwargs)

    monkeypatch.setattr(instance, "post_message", fake_post_message)
    yield instance
    instance.stop_service()


def make_config(**overrides):
    config = {
        "enabled": True, "notify": True, "onlyonce": False,
        "cookie": "session=VALID; smac=abc; colorscheme=light",
        "cron": "17 8 * * *", "random_choice": True, "mode": "direct",
        "use_proxy": False, "proxy_url": "", "flaresolverr_url": "", "user_agent": "",
        "max_retries": 0, "retry_interval": 15, "random_delay": 0,
        "browser_timeout": 40, "headless": False, "history_days": 30,
    }
    config.update(overrides)
    return config
