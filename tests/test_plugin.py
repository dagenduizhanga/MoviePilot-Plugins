"""NodeSeekCheckin 插件测试（真实 MoviePilot V2 宿主 + 本地模拟 NodeSeek/Cloudflare）"""
import json
import os
import sys
import time

import pytest

from conftest import make_config, MOCK_URL

import mock_ns


def mod():
    import app.plugins.nodeseekcheckin as module
    return module


# --------------------------------------------------------------------------- 纯函数

def test_parse_cookie_variants():
    m = mod()
    assert m.parse_cookie("session=a; smac=b") == {"session": "a", "smac": "b"}
    assert m.parse_cookie("Cookie: session=a=b;\n smac=c ;") == {"session": "a=b", "smac": "c"}
    assert m.parse_cookie("") == {}
    assert m.parse_cookie(None) == {}
    assert m.parse_cookie("garbage") == {}


@pytest.mark.parametrize("status,body,headers,expected", [
    (200, '{"success":true,"message":"今天的签到收益是9个鸡腿","gain":9,"current":109}', {}, "success"),
    (500, '{"success":false,"message":"今天已完成签到，请勿重复操作"}', {}, "already"),
    (404, '{"success":false,"message":"USER NOT FOUND"}', {}, "invalid_cookie"),
    (404, '{"status":404,"message":"Not Found"}', {}, "invalid_cookie"),
    (403, '{"success":false,"message":"high risk action"}', {}, "failed"),
    (403, mock_ns.CHALLENGE_AUTO, {"cf-mitigated": "challenge"}, "cf_blocked"),
    (403, mock_ns.CHALLENGE_AUTO, {}, "cf_blocked"),
    (403, "<!DOCTYPE html><html><head><title>请稍候…</title></head></html>", {}, "cf_blocked"),
    (403, "error code: 1020", {}, "cf_blocked"),
    (503, "", {}, "cf_blocked"),
    (200, "<html><head><title>NodeSeek</title></head><body>登录</body></html>", {}, "failed"),
    (200, '\x00{"success":true,"gain":"7"}', {}, "success"),
])
def test_classify_response(status, body, headers, expected):
    assert mod().classify_response(status, body, headers).status == expected


def test_normal_page_with_cloudflare_scripts_is_not_challenge():
    """NodeSeek 正常页面自带 CF 的 JS 检测脚本和 Turnstile 组件，不能误判为被拦截"""
    m = mod()
    assert m.looks_like_challenge(200, mock_ns.BOARD_HTML, {}) is False
    assert m.looks_like_challenge(403, mock_ns.CHALLENGE_AUTO, {}) is True
    assert m.looks_like_challenge(200, "<html><head><title>Just a moment...</title></head></html>", {}) is True
    assert m.looks_like_challenge(200, "<html><script>window._cf_chl_opt={}</script></html>", {}) is True
    assert m.looks_like_challenge(200, "", {"cf-mitigated": "challenge"}) is True
    assert m.looks_like_challenge(200, '{"success":true}', {}) is False


def test_classify_extracts_numbers():
    result = mod().classify_response(200, '{"success":true,"gain":"7","current":"12"}', {})
    assert (result.gain, result.current) == (7, 12)


def test_pick_impersonate():
    m = mod()
    latest = m.pick_impersonate(None)
    assert latest.startswith("chrome") and int(latest[6:]) >= 131
    ua146 = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
    assert m.pick_impersonate(ua146) == "chrome146"
    ua141 = ua146.replace("146", "141")
    assert m.pick_impersonate(ua141) == "chrome136"  # 不高于 141 的最近指纹
    assert m.pick_impersonate("Mozilla/5.0 Firefox/130") == latest


# --------------------------------------------------------------------------- 直连通道

def test_direct_success_and_request_shape(plugin, mock):
    plugin.init_plugin(make_config())
    record = plugin.sign(manual=True)
    assert record["code"] == "success"
    assert record["gain"] == 9 and record["current"] == 109
    assert record["rank"] == 12 and record["total"] == 3456
    assert record["channel"] == "直连"
    assert plugin.get_data("last_ok_date") == plugin._today()

    log = mock.business_log()
    assert [(i["method"], i["path"]) for i in log] == [
        ("GET", "/board"),
        ("POST", "/api/attendance?random=true"),
        ("GET", "/api/attendance/board?page=1"),
    ]
    post = log[1]["headers"]
    assert post["x-csrf-challenge"] == "simple-token"
    assert post["content-type"] == "application/json"
    assert post["origin"] == MOCK_URL and post["referer"] == MOCK_URL + "/board"
    assert post["sec-fetch-mode"] == "cors" and post["sec-fetch-site"] == "same-origin"
    assert "upgrade-insecure-requests" not in post and "sec-fetch-user" not in post
    assert post["content-length"] == "0"
    # UA、客户端提示版本一致（由 curl_cffi 成套提供）
    major = post["user-agent"].split("Chrome/")[1].split(".")[0]
    assert f'v="{major}"' in post["sec-ch-ua"]
    assert log[1]["cookie_names"] == ["colorscheme", "session", "smac"]

    # 通知
    assert len(plugin.messages) == 1
    message = plugin.messages[0]
    assert message["title"] == "【NodeSeek签到成功】"
    assert "获得鸡腿：9" in message["text"] and "第 12 名" in message["text"]
    # Cookie 不得出现在通知/历史里
    assert "VALID" not in json.dumps(plugin.get_data("history"), ensure_ascii=False)
    assert "VALID" not in message["text"]


def test_direct_fixed_reward(plugin, mock):
    plugin.init_plugin(make_config(random_choice=False))
    record = plugin.sign(manual=True)
    assert record["code"] == "success" and record["gain"] == 5
    assert any(i["path"] == "/api/attendance?random=false" for i in mock.business_log())


def test_already_signed(plugin, mock):
    mock.set(signed="1")
    plugin.init_plugin(make_config())
    record = plugin.sign(manual=True)
    assert record["code"] == "already"
    assert record["gain"] == 5 and record["rank"] == 12
    assert plugin.messages[0]["title"] == "【NodeSeek今日已签到】"
    assert plugin.get_data("last_ok_date") == plugin._today()


@pytest.mark.parametrize("style", ["msg", "status"])
def test_invalid_cookie_no_retry(plugin, mock, style):
    mock.set(invalid_style=style)
    plugin.init_plugin(make_config(cookie="session=EXPIRED", max_retries=3))
    record = plugin.sign(manual=True)
    assert record["code"] == "invalid_cookie"
    assert plugin.messages and plugin.messages[0]["title"] == "【NodeSeekCookie失效】"
    assert not plugin._scheduler or not plugin._scheduler.get_job("nodeseek_checkin_retry")
    assert plugin.get_data("last_ok_date") != plugin._today()


def test_no_cookie(plugin, mock):
    plugin.init_plugin(make_config(cookie=""))
    record = plugin.sign(manual=True)
    assert record["code"] == "invalid_cookie"
    assert mock.business_log() == []


def test_scheduled_run_skips_after_success(plugin, mock):
    plugin.init_plugin(make_config())
    assert plugin.sign(manual=False)["code"] == "success"
    count = len(mock.business_log())
    assert plugin.sign(manual=False) is None
    assert len(mock.business_log()) == count  # 没有再发任何请求
    # 手动触发不受限制
    assert plugin.sign(manual=True)["code"] == "already"


def test_session_rotation_is_persisted(plugin, mock):
    mock.set(rotate_session="ROTATED")
    plugin.init_plugin(make_config())
    assert plugin.sign(manual=True)["code"] == "success"
    assert plugin._load_profile()["cookies"]["session"] == "ROTATED"
    # 第二天：配置里仍是旧 Cookie，但缓存里是站点下发的新会话
    mock.set(signed="0")
    assert plugin.sign(manual=True)["code"] == "success"


def test_stale_cached_session_falls_back_to_config(plugin, mock):
    plugin.init_plugin(make_config())
    plugin._save_profile(cookies={"session": "STALE"})
    record = plugin.sign(manual=True)
    assert record["code"] == "success"
    posts = [i for i in mock.business_log() if i["method"] == "POST"]
    assert len(posts) == 2  # 第一次用缓存会话失败，第二次用配置 Cookie 成功


def test_changing_cookie_drops_cache(plugin, mock):
    plugin.init_plugin(make_config())
    plugin._save_profile(cookies={"session": "X", "cf_clearance": "old"}, ua="UA/1", headers={})
    assert plugin._load_profile().get("ua") == "UA/1"
    plugin.init_plugin(make_config(cookie="session=VALID; smac=other"))
    assert plugin._load_profile() == {}


def test_missing_csrf_would_fail(plugin, mock, monkeypatch):
    """反向验证：模拟服务器确实校验 x-csrf-challenge（旧插件缺的就是它）"""
    import requests
    resp = requests.post(MOCK_URL + "/api/attendance?random=true", headers={"Cookie": "session=VALID"})
    assert resp.status_code == 403 and "high risk" in resp.text


def test_direct_blocked_then_retry_scheduled_quietly(plugin, mock):
    mock.set(cf="all")
    plugin.init_plugin(make_config(mode="direct", max_retries=2))
    record = plugin.sign(manual=False)  # 定时触发
    assert record["code"] == "cf_blocked"
    job = plugin._scheduler.get_job("nodeseek_checkin_retry")
    assert job is not None and "1/2" in job.name
    assert job.kwargs == {"manual": True, "is_retry": True}
    assert plugin.messages == []  # 还会重试，不打扰
    # 页面被质询后接口只再试一次，不反复撞
    assert [i["path"] for i in mock.business_log()] == ["/board", "/api/attendance?random=true"]

    plugin.sign(manual=True, is_retry=True)
    assert plugin.messages == []
    plugin.sign(manual=True, is_retry=True)  # 第 3 次失败：重试用尽
    assert len(plugin.messages) == 1
    final = plugin.messages[0]
    assert final["title"] == "【NodeSeek被Cloudflare拦截】"
    assert "直连" in final["text"] and "最大重试次数" in final["text"]


def test_manual_run_reports_failure_immediately(plugin, mock):
    """「立即运行一次」失败要马上告诉用户，而不是等重试用尽"""
    mock.set(cf="all")
    plugin.init_plugin(make_config(mode="direct", max_retries=3))
    plugin.sign(manual=True)
    assert len(plugin.messages) == 1
    assert "分钟后进行第 1/3 次重试" in plugin.messages[0]["text"]


def test_api_reachable_even_if_page_challenged(plugin, mock):
    """只有页面被质询、接口没被质询时，直连就能完成，不需要开浏览器"""
    mock.set(cf="page")
    plugin.init_plugin(make_config(mode="direct"))
    record = plugin.sign(manual=True)
    assert record["code"] == "success" and record["channel"] == "直连"


def test_notify_off_stays_silent(plugin, mock):
    plugin.init_plugin(make_config(notify=False))
    assert plugin.sign(manual=False)["code"] == "success"
    assert plugin.messages == []


def test_concurrent_trigger_ignored(plugin, mock):
    plugin.init_plugin(make_config())
    plugin._lock.acquire()
    try:
        assert plugin.sign(manual=True) is None
    finally:
        plugin._lock.release()
    assert mock.business_log() == []


def test_requests_fallback_without_curl_cffi(plugin, mock, monkeypatch):
    monkeypatch.setattr(mod(), "HAS_CURL_CFFI", False)
    plugin.init_plugin(make_config())
    record = plugin.sign(manual=True)
    assert record["code"] == "success"
    post = [i for i in mock.business_log() if i["method"] == "POST"][0]["headers"]
    assert post["x-csrf-challenge"] == "simple-token"
    assert "Chrome/" in post["user-agent"]


def test_history_pruned(plugin, mock):
    plugin.init_plugin(make_config(history_days=7))
    plugin.save_data("history", [
        {"date": "2020-01-01 00:00:00", "status": "签到成功", "code": "success"},
        {"date": "bad", "status": "x"},
    ])
    plugin.sign(manual=True)
    history = plugin.get_data("history")
    assert len(history) == 1 and history[0]["code"] == "success"


# --------------------------------------------------------------------------- 浏览器通道

def _use_playwright_only(monkeypatch):
    """模拟没有 CloakBrowser 的旧版 MoviePilot：只能用 Playwright"""
    monkeypatch.setitem(sys.modules, "cloakbrowser", None)
    # 本机 Playwright 自带的浏览器版本不匹配时，可用 TEST_CHROMIUM 指定一个 Chromium 可执行文件
    if os.environ.get("TEST_CHROMIUM"):
        monkeypatch.setenv("NODESEEK_BROWSER_EXECUTABLE", os.environ["TEST_CHROMIUM"])


ENGINES = ["cloakbrowser", "playwright"]


@pytest.mark.parametrize("engine", ENGINES)
def test_auto_falls_back_to_browser_and_caches_clearance(plugin, mock, monkeypatch, engine):
    if engine == "playwright":
        _use_playwright_only(monkeypatch)
    mock.set(cf="all")
    plugin.init_plugin(make_config(mode="auto"))
    record = plugin.sign(manual=True)
    assert record["code"] == "success", record
    assert record["channel"] == "浏览器过盾"
    assert record["gain"] == 9 and record["rank"] == 12

    state = mock.state()
    assert state["clearance_issued"] == 1
    profile = plugin._load_profile()
    assert profile["ua"] and "Chrome/" in profile["ua"]
    assert profile["cookies"].get("cf_clearance") == mock_ns.clearance_for(profile["ua"])
    assert profile["cookies"]["session"] == "VALID"
    assert ("CloakBrowser" if engine == "cloakbrowser" else "Playwright") in profile["source"]

    # 页面内请求带了正确的头和 Cookie
    post = [i for i in mock.business_log() if i["method"] == "POST" and i["path"].startswith("/api/attendance")][-1]
    assert post["headers"]["x-csrf-challenge"] == "simple-token"
    assert "cf_clearance" in post["cookie_names"] and "session" in post["cookie_names"]

    # 第二天：直连复用浏览器的 cf_clearance + UA，不再触发质询、不再开浏览器
    mock.set(signed="0")
    served = mock.state()["challenge_served"]
    record = plugin.sign(manual=True)
    assert record["code"] == "success" and record["channel"] == "直连", record
    after = mock.state()
    assert after["challenge_served"] == served
    assert after["clearance_issued"] == 1
    direct_post = [i for i in mock.business_log() if i["method"] == "POST"][-1]["headers"]
    assert direct_post["user-agent"] == profile["ua"]


def test_browser_handles_api_only_challenge(plugin, mock):
    mock.set(cf="api")
    plugin.init_plugin(make_config(mode="browser"))
    record = plugin.sign(manual=True)
    assert record["code"] == "success", record
    assert mock.state()["clearance_issued"] == 1


def test_browser_clicks_interactive_turnstile(plugin, mock):
    mock.set(cf="interactive")
    plugin.init_plugin(make_config(mode="browser"))
    record = plugin.sign(manual=True)
    assert record["code"] == "success", record


def test_browser_invalid_cookie(plugin, mock):
    mock.set(cf="all")
    plugin.init_plugin(make_config(mode="browser", cookie="session=EXPIRED"))
    record = plugin.sign(manual=True)
    assert record["code"] == "invalid_cookie"


def test_browser_unsolvable_then_final_report(plugin, mock):
    mock.set(cf="unsolvable")
    plugin.init_plugin(make_config(mode="auto", browser_timeout=20, flaresolverr_url=MOCK_URL))
    started = time.time()
    record = plugin.sign(manual=True)
    assert record["code"] == "cf_blocked"
    assert time.time() - started < 90
    text = plugin.messages[0]["text"]
    assert "直连" in text and "浏览器过盾" in text and "FlareSolverr" in text
    assert (plugin.get_data_path() / "last_challenge.png").exists()


def test_browser_headed_falls_back_to_headless_without_display(plugin, mock, monkeypatch):
    monkeypatch.delenv("DISPLAY", raising=False)
    mock.set(cf="all")
    plugin.init_plugin(make_config(mode="browser"))
    record = plugin.sign(manual=True)
    assert record["code"] == "success", record
    assert "无头" in plugin._load_profile()["source"]


# --------------------------------------------------------------------------- FlareSolverr 通道

def test_flaresolverr_channel(plugin, mock):
    mock.set(cf="all")
    plugin.init_plugin(make_config(mode="flaresolverr", flaresolverr_url=MOCK_URL + "/"))
    record = plugin.sign(manual=True)
    assert record["code"] == "success", record
    assert record["channel"] == "FlareSolverr"
    state = mock.state()
    fs = [i for i in state["log"] if i["method"] == "FS"][0]
    assert fs["cmd"] == "request.get" and fs["path"] == MOCK_URL + "/board"
    assert fs["cookie_names"] == ["colorscheme", "session", "smac"]
    post = [i for i in mock.business_log() if i["method"] == "POST"][-1]["headers"]
    assert post["user-agent"] == mock_ns.FS_UA
    assert plugin._load_profile()["source"] == "FlareSolverr"


def test_flaresolverr_not_configured(plugin, mock, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "FLARESOLVERR_URL", None)
    plugin.init_plugin(make_config(mode="flaresolverr"))
    record = plugin.sign(manual=True)
    assert record["code"] == "error" and "FlareSolverr" in record["message"]


def test_channel_order(plugin, mock, monkeypatch):
    from app.core.config import settings
    channels = plugin._NodeSeekCheckin__channels
    monkeypatch.setattr(settings, "FLARESOLVERR_URL", None)
    plugin.init_plugin(make_config(mode="auto"))
    assert channels() == ["direct", "browser"]
    plugin.init_plugin(make_config(mode="auto", flaresolverr_url="http://fs:8191"))
    assert channels() == ["direct", "browser", "flaresolverr"]
    monkeypatch.setattr(settings, "FLARESOLVERR_URL", "http://fs:8191")
    monkeypatch.setattr(settings, "BROWSER_EMULATION", "flaresolverr")
    plugin.init_plugin(make_config(mode="auto"))
    assert channels() == ["direct", "flaresolverr", "browser"]
    plugin.init_plugin(make_config(mode="browser"))
    assert channels() == ["browser"]


# --------------------------------------------------------------------------- 代理

def test_proxy_resolution(plugin, mock, monkeypatch):
    from app.core.config import settings
    plugin.init_plugin(make_config(use_proxy=False, proxy_url="http://u:p@10.0.0.1:7890"))
    assert plugin._requests_proxies() is None and plugin._playwright_proxy() is None
    plugin.init_plugin(make_config(use_proxy=True, proxy_url="http://u:p@10.0.0.1:7890"))
    assert plugin._requests_proxies() == {"http": "http://u:p@10.0.0.1:7890", "https": "http://u:p@10.0.0.1:7890"}
    assert plugin._playwright_proxy() == {"server": "http://10.0.0.1:7890", "username": "u", "password": "p"}
    monkeypatch.setattr(settings, "PROXY_HOST", "socks5://192.168.1.1:1080")
    plugin.init_plugin(make_config(use_proxy=True))
    assert plugin._requests_proxies()["https"] == "socks5://192.168.1.1:1080"
    assert plugin._playwright_proxy() == {"server": "socks5://192.168.1.1:1080"}


# --------------------------------------------------------------------------- 分身（多账号）

def test_clone_gets_its_own_command_and_service(plugin, mock):
    base = type(plugin)
    clone_cls = type("NodeSeekCheckinAlt", (base,), {})
    clone = clone_cls()
    clone.init_plugin(make_config())
    try:
        original = plugin.get_command()[0]
        cloned = clone.get_command()[0]
        assert original["cmd"] == "/nodeseek_sign" and original["data"]["action"] == "nodeseekcheckin_sign"
        assert cloned["cmd"] == "/nodeseek_sign_alt" and cloned["data"]["action"] == "nodeseekcheckinalt_sign"
        plugin.init_plugin(make_config())
        assert plugin.get_service()[0]["id"] == "NodeSeekCheckin"
        assert clone.get_service()[0]["id"] == "NodeSeekCheckinAlt"
    finally:
        clone.stop_service()
