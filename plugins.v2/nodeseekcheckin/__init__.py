"""
NodeSeek 签到（过盾版）

面向 MoviePilot V2 的 NodeSeek 每日签到插件。针对 Cloudflare 设计了三条通道，按顺序自动降级：

1. 直连通道：curl_cffi 模拟 Chrome TLS/HTTP2 指纹直接调用签到接口，最快、最轻；
2. 浏览器通道：使用 MoviePilot 自带的仿真浏览器（CloakBrowser / Playwright）打开签到页，
   通过 Cloudflare 质询后在页面内发起签到请求，并把 cf_clearance / UA 缓存给下次直连复用；
3. FlareSolverr 通道（可选）：由 FlareSolverr 过盾取得 cf_clearance 后再直连签到。

签到接口：POST /api/attendance?random=true|false，需携带 x-csrf-challenge: simple-token。
"""
import hashlib
import json
import os
import random
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import pytz
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.schemas.types import EventType

try:
    from curl_cffi import requests as curl_requests

    HAS_CURL_CFFI = True
except Exception:  # noqa
    curl_requests = None
    HAS_CURL_CFFI = False

# 原始插件 ID（分身的类名是它加后缀）
BASE_PLUGIN_ID = "NodeSeek" + "Checkin"

# NodeSeek 以北京时间 0 点为签到日界
NS_TZ = pytz.timezone("Asia/Shanghai")

# 结果状态
ST_SUCCESS = "success"  # 本次签到成功
ST_ALREADY = "already"  # 今天已经签到过
ST_INVALID = "invalid_cookie"  # Cookie 失效
ST_BLOCKED = "cf_blocked"  # 被 Cloudflare 拦截
ST_FAILED = "failed"  # 接口返回了明确的失败
ST_ERROR = "error"  # 网络/环境异常

OK_STATUSES = (ST_SUCCESS, ST_ALREADY)

CHANNEL_NAMES = {
    "direct": "直连",
    "browser": "浏览器过盾",
    "flaresolverr": "FlareSolverr",
}

# 质询/封禁页的标题关键字（Cloudflare 会按语言本地化标题）
CHALLENGE_TITLES = (
    "just a moment",
    "请稍候",
    "attention required",
    "access denied",
    "checking your browser",
)

# 兜底 UA：宿主没有提供 NORMAL_USER_AGENT 且未安装 curl_cffi 时使用
FALLBACK_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/138.0.0.0 Safari/537.36")


@dataclass
class SignResult:
    """一次签到尝试的结果"""
    status: str
    message: str = ""
    gain: Optional[int] = None
    current: Optional[int] = None
    rank: Optional[int] = None
    total: Optional[int] = None
    channel: str = ""
    http_status: Optional[int] = None
    trace: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in OK_STATUSES


def parse_cookie(cookie_str: Optional[str]) -> Dict[str, str]:
    """把浏览器里复制的 Cookie 字符串解析为字典，容忍 'Cookie:' 前缀与换行"""
    text = (cookie_str or "").strip()
    if text.lower().startswith("cookie:"):
        text = text[7:]
    result: Dict[str, str] = {}
    for part in re.split(r"[;\r\n]+", text):
        if "=" not in part:
            continue
        name, value = part.split("=", 1)
        name, value = name.strip(), value.strip()
        if name:
            result[name] = value
    return result


def cookie_to_str(cookies: Dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in cookies.items() if v is not None)


def looks_like_challenge(status_code: Optional[int], text: Optional[str],
                         headers: Optional[Dict[str, str]] = None) -> bool:
    """
    判断响应是否为 Cloudflare 质询/拦截页。
    注意：NodeSeek 的正常页面也会带 /cdn-cgi/challenge-platform/ 脚本和 Turnstile 组件，
    不能只凭这些字样判断，否则会把正常页面误判成被拦截。
    """
    headers = headers or {}
    if "challenge" in str(headers.get("cf-mitigated", "")).lower():
        return True
    low = (text or "")[:8000].lower()
    if not low:
        return False
    matched = re.search(r"<title[^>]*>(.*?)</title>", low, re.S)
    title = matched.group(1).strip() if matched else ""
    if any(keyword in title for keyword in CHALLENGE_TITLES):
        return True
    if "_cf_chl_opt" in low:
        return True
    is_html = "<html" in low or "<!doctype html" in low
    if status_code in (403, 429, 503) and is_html \
            and any(marker in low for marker in ("cf-chl", "challenge-platform", "cloudflare")):
        return True
    return False


def classify_response(status_code: Optional[int], text: Optional[str],
                      headers: Optional[Dict[str, str]] = None) -> SignResult:
    """把签到接口的响应归类"""
    body = (text or "").replace("\x00", "").strip()
    data = None
    if body[:1] in ("{", "["):
        try:
            data = json.loads(body)
        except Exception:  # noqa
            data = None

    if isinstance(data, dict):
        message = str(data.get("message") or "")
        upper = message.upper()
        if data.get("success") is True:
            gain = data.get("gain")
            if gain is None and isinstance(data.get("record"), dict):
                gain = data["record"].get("gain")
            return SignResult(ST_SUCCESS, message or "签到成功", gain=_to_int(gain),
                              current=_to_int(data.get("current")), http_status=status_code)
        if "已完成签到" in message or "已签到" in message or "重复" in message:
            return SignResult(ST_ALREADY, message, http_status=status_code)
        if "USER NOT FOUND" in upper or "未登录" in message or "请先登录" in message \
                or "UNAUTHORIZED" in upper or data.get("status") == 404:
            return SignResult(ST_INVALID, "Cookie 已失效，请重新获取", http_status=status_code)
        if "鸡腿" in message:
            return SignResult(ST_SUCCESS, message, gain=_to_int(data.get("gain")),
                              current=_to_int(data.get("current")), http_status=status_code)
        if "HIGH RISK" in upper:
            return SignResult(ST_FAILED, f"接口拒绝（{message}），请求被判定为高风险", http_status=status_code)
        return SignResult(ST_FAILED, message or f"接口返回未知内容（HTTP {status_code}）", http_status=status_code)

    if looks_like_challenge(status_code, body, headers):
        return SignResult(ST_BLOCKED, f"被 Cloudflare 拦截（HTTP {status_code}）", http_status=status_code)
    if status_code in (403, 429, 503):
        return SignResult(ST_BLOCKED, f"请求被拒绝（HTTP {status_code}），疑似 Cloudflare/WAF", http_status=status_code)
    snippet = re.sub(r"\s+", " ", body[:80])
    return SignResult(ST_FAILED, f"接口返回非 JSON（HTTP {status_code}）：{snippet}", http_status=status_code)


def _to_int(value: Any) -> Optional[int]:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def pick_impersonate(user_agent: Optional[str] = None) -> str:
    """为 curl_cffi 选择 Chrome 指纹：有 UA 时选不高于其主版本的最近版本，否则用最新版本"""
    versions: List[int] = []
    try:
        from curl_cffi.requests import BrowserType

        for item in BrowserType:
            matched = re.fullmatch(r"chrome(\d+)", str(item.value))
            if matched:
                versions.append(int(matched.group(1)))
    except Exception:  # noqa
        pass
    if not versions:
        return "chrome"
    versions.sort()
    major = None
    if user_agent:
        matched = re.search(r"Chrome/(\d+)", user_agent)
        if matched:
            major = int(matched.group(1))
    if major is None:
        return f"chrome{versions[-1]}"
    lower = [v for v in versions if v <= major]
    return f"chrome{lower[-1] if lower else versions[0]}"


class _HttpTransport:
    """直连传输：curl_cffi（优先）或 requests"""

    def __init__(self, base_url: str, cookies: Dict[str, str], profile: Optional[dict] = None,
                 proxies: Optional[Dict[str, str]] = None, timeout: int = 30, verify: bool = True):
        self.base_url = base_url.rstrip("/")
        self.cookies = dict(cookies)
        self.profile = profile or {}
        self.proxies = proxies
        self.timeout = timeout
        self.verify = verify
        self.user_agent = self.profile.get("ua") or None
        self.use_curl = HAS_CURL_CFFI
        self.impersonate = pick_impersonate(self.user_agent) if self.use_curl else ""
        # 没有指定 UA 时，完全使用 curl_cffi 自带的成套浏览器头，保证 UA/客户端提示/TLS 指纹一致
        self.explicit_headers = bool(self.user_agent) or not self.use_curl
        if self.explicit_headers and not self.user_agent:
            self.user_agent = getattr(settings, "NORMAL_USER_AGENT", None) or FALLBACK_UA

    @property
    def description(self) -> str:
        if self.use_curl:
            return f"curl_cffi/{self.impersonate}" + ("（复用浏览器指纹）" if self.profile.get("ua") else "")
        return "requests（未安装 curl_cffi，过盾能力弱）"

    def _headers(self, method: str, kind: str) -> Dict[str, Optional[str]]:
        headers: Dict[str, Optional[str]] = {}
        if self.explicit_headers:
            headers["User-Agent"] = self.user_agent
            for name, value in (self.profile.get("headers") or {}).items():
                if value:
                    headers[name] = value
            headers.setdefault("Accept-Language", "zh-CN,zh;q=0.9,en;q=0.8")
        else:
            headers["Accept-Language"] = "zh-CN,zh;q=0.9,en;q=0.8"
        if kind == "api":
            headers["Accept"] = "application/json, text/plain, */*"
            headers["Referer"] = f"{self.base_url}/board"
            headers["x-csrf-challenge"] = "simple-token"
            headers["Sec-Fetch-Site"] = "same-origin"
            headers["Sec-Fetch-Mode"] = "cors"
            headers["Sec-Fetch-Dest"] = "empty"
            if method == "POST":
                headers["Content-Type"] = "application/json"
                headers["Origin"] = self.base_url
            if not self.explicit_headers:
                # 去掉 curl_cffi 为“页面导航”准备的默认头
                headers["Sec-Fetch-User"] = None
                headers["Upgrade-Insecure-Requests"] = None
                headers["Cache-Control"] = None
        elif self.explicit_headers:
            headers["Accept"] = ("text/html,application/xhtml+xml,application/xml;q=0.9,"
                                 "image/avif,image/webp,image/apng,*/*;q=0.8")
            headers["Upgrade-Insecure-Requests"] = "1"
            headers["Sec-Fetch-Site"] = "none"
            headers["Sec-Fetch-Mode"] = "navigate"
            headers["Sec-Fetch-User"] = "?1"
            headers["Sec-Fetch-Dest"] = "document"
        if self.cookies:
            headers["Cookie"] = cookie_to_str(self.cookies)
        return headers

    def request(self, method: str, path: str, kind: str = "api") -> Tuple[int, Dict[str, str], str]:
        url = f"{self.base_url}{path}"
        headers = self._headers(method, kind)
        data = b"" if method == "POST" else None
        if self.use_curl:
            resp = curl_requests.request(method, url, headers=headers, data=data,
                                         impersonate=self.impersonate,
                                         default_headers=not self.explicit_headers,
                                         proxies=self.proxies, timeout=self.timeout,
                                         verify=self.verify, allow_redirects=True)
        else:
            clean = {k: v for k, v in headers.items() if v is not None}
            resp = requests.request(method, url, headers=clean, data=data, proxies=self.proxies,
                                    timeout=self.timeout, verify=self.verify, allow_redirects=True)
        try:
            for name, value in dict(resp.cookies).items():
                if name and value is not None:
                    self.cookies[name] = value
        except Exception:  # noqa
            pass
        try:
            text = resp.text or ""
        except Exception:  # noqa
            text = ""
        resp_headers = {str(k).lower(): str(v) for k, v in resp.headers.items()}
        return resp.status_code, resp_headers, text


_PAGE_FETCH_JS = """
async ([path, method]) => {
    try {
        const init = {method: method, credentials: 'include', headers: {
            'accept': 'application/json, text/plain, */*',
            'x-csrf-challenge': 'simple-token'
        }};
        if (method === 'POST') { init.headers['content-type'] = 'application/json'; }
        const resp = await fetch(path, init);
        const text = await resp.text();
        return {
            status: resp.status,
            text: (text || '').slice(0, 20000),
            mitigated: resp.headers.get('cf-mitigated') || '',
            ctype: resp.headers.get('content-type') || ''
        };
    } catch (e) {
        return {error: String(e && e.message ? e.message : e)};
    }
}
"""

_PAGE_FINGERPRINT_JS = """
() => {
    const out = {ua: navigator.userAgent, headers: {}};
    try {
        const uad = navigator.userAgentData;
        if (uad && uad.brands && uad.brands.length) {
            out.headers['sec-ch-ua'] = uad.brands.map(b => '"' + b.brand + '";v="' + b.version + '"').join(', ');
            out.headers['sec-ch-ua-mobile'] = uad.mobile ? '?1' : '?0';
            if (uad.platform) { out.headers['sec-ch-ua-platform'] = '"' + uad.platform + '"'; }
        }
    } catch (e) {}
    try {
        const langs = (navigator.languages || []).slice(0, 4);
        if (langs.length) {
            out.headers['Accept-Language'] = langs.map((l, i) => i === 0 ? l : l + ';q=' + (1 - i / 10).toFixed(1)).join(',');
        }
    } catch (e) {}
    return out;
}
"""

_PAGE_CHALLENGE_JS = """
() => {
    if (typeof window._cf_chl_opt !== 'undefined') { return true; }
    return !!document.querySelector(
        '#challenge-form, #challenge-running, #cf-challenge-running, #challenge-stage, #cf-please-wait, ' +
        '#challenge-error-text, script[src*="/cdn-cgi/challenge-platform/"][src*="orchestrate"]'
    );
}
"""


class _PageTransport:
    """浏览器页面内传输：请求由真实浏览器发出，天然携带 cf_clearance 与浏览器指纹"""

    description = "页面内 fetch"

    def __init__(self, page: Any):
        self.page = page

    def request(self, method: str, path: str, kind: str = "api") -> Tuple[int, Dict[str, str], str]:
        result = self.page.evaluate(_PAGE_FETCH_JS, [path, method])
        if not isinstance(result, dict):
            raise RuntimeError("页面内请求没有返回结果")
        if result.get("error"):
            raise RuntimeError(f"页面内请求失败：{result.get('error')}")
        headers = {"content-type": result.get("ctype") or ""}
        if result.get("mitigated"):
            headers["cf-mitigated"] = result.get("mitigated")
        return int(result.get("status") or 0), headers, result.get("text") or ""


class NodeSeekCheckin(_PluginBase):
    # 插件名称
    plugin_name = "NodeSeek签到（过盾版）"
    # 插件描述
    plugin_desc = "NodeSeek 每日签到：直连失败时自动用内置浏览器通过 Cloudflare 质询后再签到，支持 FlareSolverr。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/dagenduizhanga/MoviePilot-Plugins/main/icons/nodeseekcheckin.png"
    # 插件版本
    plugin_version = "1.0.0"
    # 插件作者
    plugin_author = "dagenduizhanga"
    # 作者主页
    author_url = "https://github.com/dagenduizhanga"
    # 插件配置项ID前缀
    plugin_config_prefix = "nodeseekcheckin_"
    # 加载顺序
    plugin_order = 30
    # 可使用的用户级别
    auth_level = 1

    # 站点地址（测试时可覆盖）
    _base_url = "https://www.nodeseek.com"

    # 配置
    _enabled = False
    _notify = True
    _onlyonce = False
    _cookie = ""
    _cron = "17 8 * * *"
    _random_choice = True
    _mode = "auto"
    _use_proxy = False
    _proxy_url = ""
    _flaresolverr_url = ""
    _user_agent = ""
    _max_retries = 3
    _retry_interval = 15
    _random_delay = 30
    _browser_timeout = 90
    _headless = False
    _history_days = 30

    # 运行时
    _scheduler: Optional[BackgroundScheduler] = None
    _lock: Optional[threading.Lock] = None
    _retry_count = 0
    _retry_date = ""
    # 测试时可置 0 以跳过拟人等待
    _pause_scale = 1.0

    def init_plugin(self, config: dict = None):
        self.stop_service()
        if self._lock is None:
            self._lock = threading.Lock()

        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify", True))
        self._onlyonce = bool(config.get("onlyonce"))
        self._cookie = (config.get("cookie") or "").strip()
        self._cron = (config.get("cron") or "").strip() or "17 8 * * *"
        self._random_choice = bool(config.get("random_choice", True))
        self._mode = config.get("mode") if config.get("mode") in ("auto", "direct", "browser", "flaresolverr") \
            else "auto"
        self._use_proxy = bool(config.get("use_proxy"))
        self._proxy_url = (config.get("proxy_url") or "").strip()
        self._flaresolverr_url = (config.get("flaresolverr_url") or "").strip()
        self._user_agent = (config.get("user_agent") or "").strip()
        self._max_retries = self._int(config.get("max_retries"), 3, 0, 10)
        self._retry_interval = self._int(config.get("retry_interval"), 15, 1, 720)
        self._random_delay = self._int(config.get("random_delay"), 30, 0, 3600)
        self._browser_timeout = self._int(config.get("browser_timeout"), 90, 20, 600)
        self._headless = bool(config.get("headless"))
        self._history_days = self._int(config.get("history_days"), 30, 1, 365)

        if self._onlyonce:
            # 立即运行一次：3 秒后在独立线程执行，随后复位开关
            self._onlyonce = False
            self.__save_config()
            if self._cookie:
                logger.info("NodeSeek 签到：收到「立即运行一次」，3 秒后执行")
                self.__ensure_scheduler().add_job(
                    func=self.sign, kwargs={"manual": True}, trigger="date",
                    run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                    name="NodeSeek 签到（立即运行）")
            else:
                logger.warning("NodeSeek 签到：未填写 Cookie，无法立即运行")

    # ------------------------------------------------------------------ 配置辅助

    @staticmethod
    def _int(value: Any, default: int, low: int, high: int) -> int:
        try:
            number = int(float(value))
        except (TypeError, ValueError):
            return default
        return max(low, min(high, number))

    def __save_config(self):
        self.update_config({
            "enabled": self._enabled,
            "notify": self._notify,
            "onlyonce": False,
            "cookie": self._cookie,
            "cron": self._cron,
            "random_choice": self._random_choice,
            "mode": self._mode,
            "use_proxy": self._use_proxy,
            "proxy_url": self._proxy_url,
            "flaresolverr_url": self._flaresolverr_url,
            "user_agent": self._user_agent,
            "max_retries": self._max_retries,
            "retry_interval": self._retry_interval,
            "random_delay": self._random_delay,
            "browser_timeout": self._browser_timeout,
            "headless": self._headless,
            "history_days": self._history_days,
        })

    def __ensure_scheduler(self) -> BackgroundScheduler:
        if not self._scheduler:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
        if not self._scheduler.running:
            self._scheduler.start()
        return self._scheduler

    def _pause(self, low: float, high: float):
        seconds = random.uniform(low, high) * float(self._pause_scale)
        if seconds > 0:
            time.sleep(seconds)

    @staticmethod
    def _today() -> str:
        return datetime.now(NS_TZ).strftime("%Y-%m-%d")

    @staticmethod
    def _now_str() -> str:
        return datetime.now(pytz.timezone(settings.TZ)).strftime("%Y-%m-%d %H:%M:%S")

    def _cookie_hash(self) -> str:
        normalized = cookie_to_str(parse_cookie(self._cookie))
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]

    # ------------------------------------------------------------------ 代理

    def _proxy_address(self) -> Optional[str]:
        """返回要使用的代理地址；未启用代理时返回 None"""
        if not self._use_proxy:
            return None
        if self._proxy_url:
            return self._proxy_url
        proxy = getattr(settings, "PROXY", None)
        if isinstance(proxy, dict):
            return proxy.get("https") or proxy.get("http")
        if isinstance(proxy, str):
            return proxy
        return None

    def _requests_proxies(self) -> Optional[Dict[str, str]]:
        address = self._proxy_address()
        return {"http": address, "https": address} if address else None

    def _playwright_proxy(self) -> Optional[Dict[str, str]]:
        address = self._proxy_address()
        if not address:
            return None
        parsed = urlparse(address if "://" in address else f"http://{address}")
        server = f"{parsed.scheme}://{parsed.hostname}" + (f":{parsed.port}" if parsed.port else "")
        proxy = {"server": server}
        if parsed.username:
            proxy["username"] = parsed.username
        if parsed.password:
            proxy["password"] = parsed.password
        return proxy

    # ------------------------------------------------------------------ 浏览器指纹/过盾缓存

    def _load_profile(self) -> dict:
        """读取与当前 Cookie 配套的过盾缓存（cf_clearance、UA、站点下发的新 Cookie）"""
        profile = self.get_data("profile") or {}
        if not isinstance(profile, dict) or profile.get("cookie_hash") != self._cookie_hash():
            return {}
        return profile

    def _save_profile(self, cookies: Dict[str, str], ua: Optional[str] = None,
                      headers: Optional[Dict[str, str]] = None, source: str = ""):
        profile = self._load_profile()
        profile.update({
            "cookie_hash": self._cookie_hash(),
            "cookies": {k: v for k, v in (cookies or {}).items() if v},
            "saved_at": self._now_str(),
        })
        if ua:
            profile["ua"] = ua
            profile["headers"] = headers or {}
            profile["source"] = source
        self.save_data("profile", profile)

    def _clear_profile(self):
        self.save_data("profile", {})

    def _effective_cookies(self, profile: Optional[dict] = None) -> Dict[str, str]:
        cookies = parse_cookie(self._cookie)
        profile = self._load_profile() if profile is None else profile
        for name, value in (profile.get("cookies") or {}).items():
            if value:
                cookies[name] = value
        return cookies

    # ------------------------------------------------------------------ 签到主流程

    def run_scheduled(self):
        """定时服务入口"""
        self.sign(manual=False)

    def sign(self, manual: bool = False, reply: Optional[dict] = None,
             is_retry: bool = False) -> Optional[dict]:
        """
        执行签到
        :param manual: 是否跳过随机延迟与“今日已签”短路（手动触发、失败重试时为 True）
        :param reply: 远程命令触发时的回复目标
        :param is_retry: 是否为失败后的自动重试
        """
        if self._lock is None:
            self._lock = threading.Lock()
        if not self._lock.acquire(blocking=False):
            logger.info("NodeSeek 签到：已有任务在运行，忽略本次触发")
            return None
        # 用户亲手触发的（立即运行一次/远程命令）每次都通知；定时与自动重试只通知最终结果
        verbose = bool(reply) or (manual and not is_retry)
        try:
            return self.__sign(manual=manual, reply=reply, verbose=verbose)
        except Exception as err:
            logger.error(f"NodeSeek 签到：执行异常 - {err}", exc_info=True)
            result = SignResult(ST_ERROR, f"插件执行异常：{err}")
            return self.__finish(result, reply, verbose=verbose)
        finally:
            self._lock.release()

    def __sign(self, manual: bool, reply: Optional[dict], verbose: bool = False) -> Optional[dict]:
        today = self._today()
        if not parse_cookie(self._cookie):
            logger.error("NodeSeek 签到：未配置 Cookie")
            return self.__finish(SignResult(ST_INVALID, "未配置 Cookie，请在插件设置中填写"), reply,
                                 retry=False, verbose=verbose)

        if not manual:
            if self.get_data("last_ok_date") == today:
                logger.info("NodeSeek 签到：今天已签到成功，跳过本次定时任务")
                return None
            if self._random_delay > 0:
                delay = random.uniform(0, self._random_delay) * float(self._pause_scale)
                logger.info(f"NodeSeek 签到：随机等待 {delay:.0f} 秒后开始")
                time.sleep(delay)

        logger.info(f"NodeSeek 签到：开始（模式={self._mode}，试试手气={'是' if self._random_choice else '否'}，"
                    f"代理={'开' if self._proxy_address() else '关'}，curl_cffi={'可用' if HAS_CURL_CFFI else '不可用'}）")

        trace: List[str] = []
        result: Optional[SignResult] = None
        for channel in self.__channels():
            name = CHANNEL_NAMES[channel]
            try:
                current = getattr(self, f"_channel_{channel}")()
            except Exception as err:
                logger.error(f"NodeSeek 签到：{name}通道异常 - {err}", exc_info=True)
                current = SignResult(ST_ERROR, f"{type(err).__name__}: {err}")
            current.channel = name
            icon = "✅" if current.ok else "❌"
            trace.append(f"{icon} {name}：{current.message}")
            logger.info(f"NodeSeek 签到：{name}通道结果 [{current.status}] {current.message}")
            result = current
            if current.status in (ST_SUCCESS, ST_ALREADY, ST_INVALID):
                break
        if result is None:
            result = SignResult(ST_ERROR, "没有可用的签到通道")
        result.trace = trace
        return self.__finish(result, reply, verbose=verbose)

    def __channels(self) -> List[str]:
        if self._mode in ("direct", "browser", "flaresolverr"):
            return [self._mode]
        channels = ["direct", "browser"]
        if self.__flaresolverr_endpoint():
            host_mode = str(getattr(settings, "BROWSER_EMULATION", "") or "").lower()
            if host_mode == "flaresolverr" and not self._flaresolverr_url:
                # 宿主已选择 FlareSolverr 作为仿真方式，优先于内置浏览器
                channels = ["direct", "flaresolverr", "browser"]
            else:
                channels.append("flaresolverr")
        return channels

    def __sign_with(self, transport: Any, warmup: bool) -> SignResult:
        """用给定传输完成：预热 -> 签到 -> 查询签到记录"""
        if warmup:
            # 先像浏览器一样访问签到页（顺带拿到 __cf_bm 等 Cookie）
            status, headers, text = transport.request("GET", "/board", kind="page")
            if looks_like_challenge(status, text, headers) or status in (403, 429, 503):
                # 页面被质询不代表接口也被质询，接口仍然试一次，由接口响应决定是否降级
                logger.info(f"NodeSeek 签到：签到页被 Cloudflare 质询（HTTP {status}），继续尝试直接请求接口")
            else:
                self._pause(1.5, 3.5)

        random_flag = "true" if self._random_choice else "false"
        status, headers, text = transport.request("POST", f"/api/attendance?random={random_flag}", kind="api")
        result = classify_response(status, text, headers)
        if result.status in (ST_BLOCKED, ST_INVALID):
            return result

        # 查询签到记录：补全鸡腿与排名；接口返回含糊时用记录确认是否已签到
        try:
            self._pause(0.8, 1.6)
            record = self.__fetch_record(transport)
        except Exception as err:
            logger.warning(f"NodeSeek 签到：查询签到记录失败 - {err}")
            record = {}
        if record.get("today"):
            if result.gain is None:
                result.gain = record.get("gain")
            result.rank = record.get("rank")
            result.total = record.get("total")
            if result.status == ST_FAILED:
                result.status = ST_ALREADY
                result.message = "今日已签到（由签到记录确认）"
        return result

    @staticmethod
    def __fetch_record(transport: Any) -> dict:
        status, headers, text = transport.request("GET", "/api/attendance/board?page=1", kind="api")
        try:
            data = json.loads(text)
        except Exception:  # noqa
            return {}
        if not isinstance(data, dict):
            return {}
        record = data.get("record")
        if not isinstance(record, dict) or not record.get("created_at"):
            return {}
        try:
            created = datetime.fromisoformat(str(record["created_at"]).replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = pytz.utc.localize(created)
            is_today = created.astimezone(NS_TZ).date() == datetime.now(NS_TZ).date()
        except Exception:  # noqa
            is_today = False
        return {
            "today": is_today,
            "gain": _to_int(record.get("gain")),
            "rank": _to_int(data.get("order")),
            "total": _to_int(data.get("total")),
        }

    # ------------------------------------------------------------------ 通道一：直连

    def _channel_direct(self) -> SignResult:
        profile = self._load_profile()
        if self._user_agent:
            # 用户手动指定 UA（通常与 Cookie 里的 cf_clearance 配套）
            profile = dict(profile, ua=self._user_agent, headers={})
        result = self.__direct_once(profile)
        if result.status == ST_INVALID and (profile.get("cookies") or {}):
            # 缓存里的会话可能已过期，丢弃缓存后只用配置的 Cookie 再试一次
            logger.info("NodeSeek 签到：缓存会话失效，清除缓存后使用配置的 Cookie 重试")
            self._clear_profile()
            fresh = {"ua": self._user_agent, "headers": {}} if self._user_agent else {}
            result = self.__direct_once(fresh)
        return result

    def __direct_once(self, profile: dict) -> SignResult:
        transport = _HttpTransport(base_url=self._base_url,
                                   cookies=self._effective_cookies(profile),
                                   profile=profile,
                                   proxies=self._requests_proxies())
        logger.info(f"NodeSeek 签到：直连通道使用 {transport.description}")
        result = self.__sign_with(transport, warmup=True)
        if result.ok:
            self._save_profile(cookies=transport.cookies)
        return result

    # ------------------------------------------------------------------ 通道二：浏览器过盾

    def _channel_browser(self) -> SignResult:
        box: Dict[str, Any] = {}

        def runner():
            try:
                box["result"] = self.__browser_sign()
            except Exception as err:
                logger.error(f"NodeSeek 签到：浏览器通道异常 - {err}", exc_info=True)
                box["result"] = SignResult(ST_ERROR, f"浏览器通道异常：{type(err).__name__}: {str(err)[:200]}")

        # Playwright 同步接口不能运行在带事件循环的线程里，这里固定放到独立线程
        thread = threading.Thread(target=runner, name="nodeseek-browser", daemon=True)
        thread.start()
        thread.join(timeout=self._browser_timeout * 3 + 240)
        if thread.is_alive():
            return SignResult(ST_ERROR, "浏览器通道超时未返回")
        return box.get("result") or SignResult(ST_ERROR, "浏览器通道没有返回结果")

    def __launch_browser(self) -> Tuple[Any, Any, str]:
        """启动浏览器上下文，返回 (context, 关闭函数, 引擎名)"""
        proxy = self._playwright_proxy()
        modes = [bool(self._headless)]
        if not self._headless:
            modes.append(True)  # 有头模式起不来（没有显示设备）时退回无头
        errors: List[str] = []
        for headless in modes:
            label = "无头" if headless else "有头"
            # 1. CloakBrowser（MoviePilot v2.12+ 内置的反检测 Chromium）
            try:
                from cloakbrowser import launch_context
            except Exception:  # noqa
                launch_context = None
            if launch_context:
                for extra in ({"humanize": True}, {}):
                    try:
                        context = launch_context(headless=headless, proxy=proxy, **extra)
                        return context, context.close, f"CloakBrowser/{label}"
                    except TypeError:
                        continue
                    except Exception as err:
                        errors.append(f"CloakBrowser/{label}: {str(err)[:160]}")
                        break
            # 2. Playwright
            try:
                from playwright.sync_api import sync_playwright
            except Exception as err:
                errors.append(f"Playwright 不可用: {err}")
                continue
            playwright = None
            try:
                playwright = sync_playwright().start()
                browser_type = str(getattr(settings, "PLAYWRIGHT_BROWSER_TYPE", "") or "chromium")
                launcher = getattr(playwright, browser_type, playwright.chromium)
                kwargs: Dict[str, Any] = {"headless": headless}
                if proxy:
                    kwargs["proxy"] = proxy
                if browser_type == "chromium":
                    kwargs["args"] = ["--disable-blink-features=AutomationControlled", "--no-sandbox",
                                      "--disable-dev-shm-usage", "--window-size=1366,768"]
                executable = os.environ.get("NODESEEK_BROWSER_EXECUTABLE")
                if executable:
                    kwargs["executable_path"] = executable
                browser = launcher.launch(**kwargs)
                context = browser.new_context(viewport={"width": 1366, "height": 768}, locale="zh-CN")

                def closer(_context=context, _browser=browser, _playwright=playwright):
                    for action in (_context.close, _browser.close, _playwright.stop):
                        try:
                            action()
                        except Exception:  # noqa
                            pass

                return context, closer, f"Playwright-{browser_type}/{label}"
            except Exception as err:
                errors.append(f"Playwright/{label}: {str(err)[:160]}")
                if playwright:
                    try:
                        playwright.stop()
                    except Exception:  # noqa
                        pass
        raise RuntimeError("无法启动浏览器 - " + "；".join(errors))

    def __cookie_domain(self) -> str:
        host = urlparse(self._base_url).hostname or ""
        return ".nodeseek.com" if host.endswith("nodeseek.com") else host

    def __browser_sign(self) -> SignResult:
        context, closer, engine = self.__launch_browser()
        logger.info(f"NodeSeek 签到：浏览器通道已启动 {engine}")
        try:
            domain = self.__cookie_domain()
            secure = self._base_url.startswith("https://")
            cookies = []
            for name, value in self._effective_cookies().items():
                if not name or not value:
                    continue
                if name.startswith("__Host-"):
                    cookies.append({"name": name, "value": value, "url": f"{self._base_url}/"})
                else:
                    cookies.append({"name": name, "value": value, "domain": domain, "path": "/",
                                    "secure": secure})
            try:
                context.add_cookies(cookies)
            except Exception as err:
                logger.warning(f"NodeSeek 签到：批量写入 Cookie 失败，改为逐个写入 - {err}")
                for item in cookies:
                    try:
                        context.add_cookies([item])
                    except Exception:  # noqa
                        logger.warning(f"NodeSeek 签到：Cookie {item['name']} 写入失败，已跳过")

            page = context.new_page()
            page.set_default_timeout(30000)
            board_url = f"{self._base_url}/board"
            try:
                page.goto(board_url, wait_until="domcontentloaded", timeout=60000)
            except Exception as err:
                logger.warning(f"NodeSeek 签到：打开签到页时出现异常（继续等待质询）- {str(err)[:200]}")

            passed, reason = self.__wait_challenge(page, self._browser_timeout)
            if not passed:
                self.__save_screenshot(page)
                return SignResult(ST_BLOCKED, reason)
            if not str(page.url or "").startswith(self._base_url):
                self.__save_screenshot(page)
                return SignResult(ST_ERROR, f"签到页未能打开（当前地址：{str(page.url)[:80]}）")
            page.wait_for_timeout(int(random.uniform(1500, 3000) * float(self._pause_scale)) + 50)

            transport = _PageTransport(page)
            result = self.__sign_with(transport, warmup=False)
            if result.status == ST_BLOCKED:
                # 页面过了质询但接口仍被拦：直接打开接口地址让浏览器解一次接口路径的质询，再回签到页重试
                logger.info("NodeSeek 签到：接口请求被单独质询，尝试对接口路径过盾后重试")
                try:
                    page.goto(f"{self._base_url}/api/attendance/board?page=1",
                              wait_until="domcontentloaded", timeout=60000)
                except Exception as err:
                    logger.warning(f"NodeSeek 签到：打开接口地址异常 - {str(err)[:200]}")
                passed, reason = self.__wait_challenge(page, self._browser_timeout)
                if passed:
                    try:
                        page.goto(board_url, wait_until="domcontentloaded", timeout=60000)
                    except Exception as err:
                        logger.warning(f"NodeSeek 签到：返回签到页异常 - {str(err)[:200]}")
                    passed, reason = self.__wait_challenge(page, self._browser_timeout)
                if not passed:
                    self.__save_screenshot(page)
                    return SignResult(ST_BLOCKED, f"接口路径质询未通过：{reason}")
                page.wait_for_timeout(int(random.uniform(1000, 2000) * float(self._pause_scale)) + 50)
                result = self.__sign_with(transport, warmup=False)
                if result.status == ST_BLOCKED:
                    self.__save_screenshot(page)

            # 无论签到结果如何，只要过了质询就把 cf_clearance / UA / 新 Cookie 留给下次直连
            try:
                self.__harvest(context, page, engine)
            except Exception as err:
                logger.warning(f"NodeSeek 签到：保存浏览器过盾信息失败 - {err}")
            return result
        finally:
            try:
                closer()
            except Exception as err:  # noqa
                logger.debug(f"NodeSeek 签到：关闭浏览器异常 - {err}")

    def __harvest(self, context: Any, page: Any, engine: str):
        domain = self.__cookie_domain().lstrip(".")
        cookies: Dict[str, str] = {}
        for item in context.cookies():
            if domain and domain in str(item.get("domain") or ""):
                cookies[item.get("name")] = item.get("value")
        fingerprint = page.evaluate(_PAGE_FINGERPRINT_JS) or {}
        merged = self._effective_cookies()
        merged.update({k: v for k, v in cookies.items() if k and v})
        self._save_profile(cookies=merged, ua=fingerprint.get("ua"),
                           headers=fingerprint.get("headers") or {}, source=engine)
        logger.info(f"NodeSeek 签到：已缓存浏览器过盾信息（Cookie：{', '.join(sorted(cookies.keys())) or '无'}）")

    def __save_screenshot(self, page: Any):
        try:
            path = self.get_data_path() / "last_challenge.png"
            page.screenshot(path=str(path))
            logger.info(f"NodeSeek 签到：质询未通过，页面截图已保存到 {path}")
        except Exception as err:
            logger.debug(f"NodeSeek 签到：保存截图失败 - {err}")

    @staticmethod
    def __challenge_state(page: Any) -> Tuple[Optional[bool], str]:
        """
        返回 (是否仍在质询, 页面标题)。None 表示页面正在跳转暂时无法判断。
        """
        try:
            title = page.title() or ""
        except Exception:  # noqa
            return None, ""
        low = title.lower()
        if "attention required" in low or "access denied" in low or "you have been blocked" in low:
            return True, title
        if "just a moment" in low or "请稍候" in title or "checking your browser" in low:
            return True, title
        try:
            return bool(page.evaluate(_PAGE_CHALLENGE_JS)), title
        except Exception:  # noqa
            return None, title

    def __wait_challenge(self, page: Any, timeout: int) -> Tuple[bool, str]:
        """等待 Cloudflare 质询通过；需要点击 Turnstile 时尝试点击"""
        start = time.time()
        deadline = start + timeout
        seen = False
        clear_hits = 0
        last_click = 0.0
        reloaded = False
        title = ""
        while time.time() < deadline:
            state, title = self.__challenge_state(page)
            now = time.time()
            if state is False:
                clear_hits += 1
                # 连续两次确认，避开质询通过后页面跳转的瞬间
                if clear_hits >= 2 or not seen:
                    if seen:
                        logger.info(f"NodeSeek 签到：已通过 Cloudflare 质询（耗时 {now - start:.0f} 秒）")
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=15000)
                    except Exception:  # noqa
                        pass
                    return True, ""
            elif state:
                clear_hits = 0
                if not seen:
                    logger.info(f"NodeSeek 签到：遇到 Cloudflare 质询（{title}），等待通过，最长 {timeout} 秒")
                seen = True
                low = title.lower()
                if "attention required" in low or "access denied" in low or "you have been blocked" in low:
                    return False, f"出口 IP 被 Cloudflare 直接封禁（{title}），请更换出口 IP 或代理"
                elapsed = now - start
                if elapsed > 5 and now - last_click > 7:
                    if self.__click_turnstile(page):
                        last_click = now
                if not reloaded and elapsed > timeout * 0.6:
                    reloaded = True
                    logger.info("NodeSeek 签到：质询长时间未通过，刷新页面再试一次")
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=45000)
                    except Exception:  # noqa
                        pass
            try:
                page.wait_for_timeout(1000)
            except Exception:  # noqa
                time.sleep(1)
        return False, f"{timeout} 秒内未能通过 Cloudflare 质询（页面标题：{title or '未知'}）"

    @staticmethod
    def __click_turnstile(page: Any) -> bool:
        """尽力点击 Turnstile 复选框（位于跨域 iframe 内，按 iframe 位置点击其左侧复选框区域）"""
        try:
            for frame in page.frames:
                url = frame.url or ""
                if "challenges.cloudflare.com" not in url:
                    continue
                try:
                    box = frame.frame_element().bounding_box()
                except Exception:  # noqa
                    box = None
                if box and box.get("width", 0) > 40 and box.get("height", 0) > 20:
                    x = box["x"] + min(30.0, box["width"] * 0.12) + random.uniform(-3, 3)
                    y = box["y"] + box["height"] / 2 + random.uniform(-3, 3)
                    page.mouse.move(x - random.uniform(40, 90), y - random.uniform(20, 60))
                    page.wait_for_timeout(random.randint(120, 320))
                    page.mouse.click(x, y, delay=random.randint(60, 140))
                    logger.info("NodeSeek 签到：已尝试点击 Turnstile 验证框")
                    return True
        except Exception as err:
            logger.debug(f"NodeSeek 签到：点击 Turnstile 失败 - {err}")
        return False

    # ------------------------------------------------------------------ 通道三：FlareSolverr

    def __flaresolverr_endpoint(self) -> str:
        url = self._flaresolverr_url or str(getattr(settings, "FLARESOLVERR_URL", "") or "")
        url = url.strip().rstrip("/")
        if not url:
            return ""
        return url if url.endswith("/v1") else f"{url}/v1"

    def _channel_flaresolverr(self) -> SignResult:
        endpoint = self.__flaresolverr_endpoint()
        if not endpoint:
            return SignResult(ST_ERROR, "未配置 FlareSolverr 地址")
        base_cookies = self._effective_cookies()
        payload: Dict[str, Any] = {
            "cmd": "request.get",
            "url": f"{self._base_url}/board",
            "maxTimeout": int(self._browser_timeout) * 1000,
            "cookies": [{"name": k, "value": v} for k, v in base_cookies.items() if k and v],
        }
        address = self._proxy_address()
        if address:
            payload["proxy"] = {"url": address}
        session = requests.Session()
        session.trust_env = False  # FlareSolverr 一般在内网，不走环境变量里的代理
        try:
            resp = session.post(endpoint, json=payload, timeout=self._browser_timeout + 30)
            data = resp.json()
        except Exception as err:
            return SignResult(ST_ERROR, f"调用 FlareSolverr 失败：{str(err)[:160]}")
        finally:
            session.close()
        if not isinstance(data, dict) or data.get("status") != "ok":
            message = data.get("message") if isinstance(data, dict) else "无效响应"
            return SignResult(ST_BLOCKED, f"FlareSolverr 未能过盾：{str(message)[:160]}")
        solution = data.get("solution") or {}
        ua = solution.get("userAgent") or ""
        cookies = dict(base_cookies)
        for item in solution.get("cookies") or []:
            if isinstance(item, dict) and item.get("name") and item.get("value"):
                cookies[item["name"]] = item["value"]
        if looks_like_challenge(solution.get("status"), solution.get("response"), {}):
            return SignResult(ST_BLOCKED, "FlareSolverr 返回的页面仍是 Cloudflare 质询")
        profile = {"ua": ua, "headers": {}, "cookies": cookies}
        transport = _HttpTransport(base_url=self._base_url, cookies=cookies, profile=profile,
                                   proxies=self._requests_proxies())
        logger.info(f"NodeSeek 签到：FlareSolverr 过盾完成，使用 {transport.description} 发起签到")
        self._pause(1.0, 2.5)
        result = self.__sign_with(transport, warmup=False)
        if result.ok:
            self._save_profile(cookies=transport.cookies, ua=ua, headers={}, source="FlareSolverr")
        return result

    # ------------------------------------------------------------------ 结果处理

    def __finish(self, result: SignResult, reply: Optional[dict] = None, retry: bool = True,
                 verbose: bool = False) -> dict:
        today = self._today()
        if self._retry_date != today:
            self._retry_date = today
            self._retry_count = 0

        status_text = {
            ST_SUCCESS: "签到成功",
            ST_ALREADY: "今日已签到",
            ST_INVALID: "Cookie失效",
            ST_BLOCKED: "被Cloudflare拦截",
            ST_FAILED: "签到失败",
            ST_ERROR: "执行出错",
        }.get(result.status, result.status)

        record = {
            "date": self._now_str(),
            "status": status_text,
            "code": result.status,
            "channel": result.channel,
            "message": result.message,
            "gain": result.gain,
            "current": result.current,
            "rank": result.rank,
            "total": result.total,
        }
        self.__append_history(record)

        retry_note = ""
        if result.ok:
            self.save_data("last_ok_date", today)
            self._retry_count = 0
        elif result.status == ST_INVALID or not retry:
            pass
        elif self._max_retries > 0 and self._retry_count < self._max_retries:
            self._retry_count += 1
            minutes = self._retry_interval + random.randint(0, max(1, self._retry_interval // 3))
            run_date = datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(minutes=minutes)
            try:
                scheduler = self.__ensure_scheduler()
                if scheduler.get_job("nodeseek_checkin_retry"):
                    scheduler.remove_job("nodeseek_checkin_retry")
                scheduler.add_job(func=self.sign, kwargs={"manual": True, "is_retry": True},
                                  trigger="date", run_date=run_date,
                                  id="nodeseek_checkin_retry",
                                  name=f"NodeSeek 签到重试 {self._retry_count}/{self._max_retries}")
                retry_note = f"将在 {minutes} 分钟后进行第 {self._retry_count}/{self._max_retries} 次重试"
                logger.info(f"NodeSeek 签到：{retry_note}")
            except Exception as err:
                logger.error(f"NodeSeek 签到：安排重试失败 - {err}")
        elif self._max_retries > 0:
            retry_note = f"已达到最大重试次数（{self._max_retries}），今日不再重试"
            logger.warning(f"NodeSeek 签到：{retry_note}")

        # 自动重试还没用完时不打扰，只在最终结果出来时通知；用户亲手触发的每次都通知
        final = result.ok or result.status == ST_INVALID or not retry_note.startswith("将在")
        if reply or (self._notify and (final or verbose)):
            self.__notify(result, status_text, retry_note, reply)
        return record

    def __notify(self, result: SignResult, status_text: str, retry_note: str, reply: Optional[dict]):
        lines: List[str] = []
        if result.ok:
            if result.gain is not None:
                gain_line = f"🍗 获得鸡腿：{result.gain}"
                if result.current is not None:
                    gain_line += f"（当前共 {result.current}）"
                lines.append(gain_line)
            if result.rank:
                rank_line = f"🏅 今日排名：第 {result.rank} 名"
                if result.total:
                    rank_line += f" / {result.total} 人"
                lines.append(rank_line)
            if result.message:
                lines.append(f"💬 {result.message}")
            if result.channel:
                lines.append(f"🛣 通道：{result.channel}")
        else:
            if result.status == ST_INVALID:
                lines.append(f"🔑 {result.message}")
            if result.trace:
                lines.extend(result.trace)
            elif result.status != ST_INVALID:
                lines.append(f"❌ {result.message}")
            if retry_note:
                lines.append(f"🔁 {retry_note}")
        lines.append(f"⏱ {self._now_str()}")
        try:
            self.post_message(
                channel=(reply or {}).get("channel"),
                userid=(reply or {}).get("userid"),
                mtype=NotificationType.SiteMessage,
                title=f"【NodeSeek{status_text}】",
                text="\n".join(lines),
            )
        except Exception as err:
            logger.error(f"NodeSeek 签到：发送通知失败 - {err}")

    def __append_history(self, record: dict):
        history = self.get_data("history") or []
        if not isinstance(history, list):
            history = []
        history.append(record)
        cutoff = datetime.now() - timedelta(days=self._history_days)
        kept = []
        for item in history:
            try:
                if datetime.strptime(item.get("date", ""), "%Y-%m-%d %H:%M:%S") >= cutoff:
                    kept.append(item)
            except (TypeError, ValueError):
                continue
        self.save_data("history", kept[-200:])

    # ------------------------------------------------------------------ 宿主接口

    def get_state(self) -> bool:
        return bool(self._enabled)

    def __action_name(self) -> str:
        return f"{self.__class__.__name__.lower()}_sign"

    def get_command(self) -> List[Dict[str, Any]]:
        # 插件分身（多账号）时类名会带后缀，命令随之区分，避免多个分身抢同一条命令
        plugin_id = self.__class__.__name__
        suffix = plugin_id[len(BASE_PLUGIN_ID):].lower() if plugin_id.startswith(BASE_PLUGIN_ID) else ""
        return [{
            "cmd": "/nodeseek_sign" + (f"_{suffix}" if suffix else ""),
            "event": EventType.PluginAction,
            "desc": f"{self.plugin_name}",
            "category": "站点",
            "data": {"action": self.__action_name()},
        }]

    @eventmanager.register(EventType.PluginAction)
    def handle_command(self, event: Event = None):
        if not event:
            return
        event_data = event.event_data or {}
        if event_data.get("action") != self.__action_name():
            return
        reply = {"channel": event_data.get("channel"), "userid": event_data.get("user")}
        self.post_message(channel=reply["channel"], userid=reply["userid"],
                          mtype=NotificationType.SiteMessage, title="开始执行 NodeSeek 签到 ...")
        if self.sign(manual=True, reply=reply) is None:
            self.post_message(channel=reply["channel"], userid=reply["userid"],
                              mtype=NotificationType.SiteMessage, title="NodeSeek 签到任务正在运行中，请稍后查看结果")

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled or not self._cron:
            return []
        try:
            trigger = CronTrigger.from_crontab(self._cron)
        except Exception as err:
            logger.error(f"NodeSeek 签到：签到周期 {self._cron} 无效 - {err}")
            return []
        return [{
            "id": self.__class__.__name__,
            "name": self.plugin_name,
            "trigger": trigger,
            "func": self.run_scheduled,
            "kwargs": {},
        }]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        def col(md: int, component: str, **props) -> dict:
            return {"component": "VCol", "props": {"cols": 12, "md": md},
                    "content": [{"component": component, "props": props}]}

        host_fs = str(getattr(settings, "FLARESOLVERR_URL", "") or "")
        env_text = (
            f"运行环境：curl_cffi {'已安装' if HAS_CURL_CFFI else '未安装（直连通道过盾能力弱，请重装插件依赖）'}；"
            f"系统 FlareSolverr {'已配置' if host_fs else '未配置'}。"
            "通道顺序：直连 → 浏览器过盾 → FlareSolverr（配置了才启用），前一个被 Cloudflare 拦截才会用下一个。"
        )
        form = [{
            "component": "VForm",
            "content": [
                {"component": "VRow", "content": [
                    col(3, "VSwitch", model="enabled", label="启用插件"),
                    col(3, "VSwitch", model="notify", label="发送通知"),
                    col(3, "VSwitch", model="random_choice", label="试试手气", hint="关闭则领取固定 5 个鸡腿",
                        **{"persistent-hint": True}),
                    col(3, "VSwitch", model="onlyonce", label="立即运行一次"),
                ]},
                {"component": "VRow", "content": [
                    col(12, "VTextarea", model="cookie", label="NodeSeek Cookie", rows=3,
                        placeholder="session=xxx; smac=xxx; ...",
                        hint="浏览器登录 nodeseek.com 后，F12 → 网络 → 任意请求 → 请求头中的 Cookie 整段复制",
                        **{"persistent-hint": True}),
                ]},
                {"component": "VRow", "content": [
                    col(4, "VCronField", model="cron", label="签到周期", placeholder="17 8 * * *"),
                    col(4, "VSelect", model="mode", label="签到通道", items=[
                        {"title": "自动（推荐）", "value": "auto"},
                        {"title": "仅直连", "value": "direct"},
                        {"title": "仅浏览器过盾", "value": "browser"},
                        {"title": "仅 FlareSolverr", "value": "flaresolverr"},
                    ]),
                    col(4, "VTextField", model="random_delay", label="随机延迟上限（秒）", type="number",
                        hint="定时触发后随机等待 0~N 秒再签到", **{"persistent-hint": True}),
                ]},
                {"component": "VRow", "content": [
                    col(4, "VTextField", model="max_retries", label="失败重试次数", type="number"),
                    col(4, "VTextField", model="retry_interval", label="重试间隔（分钟）", type="number"),
                    col(4, "VTextField", model="history_days", label="历史保留天数", type="number"),
                ]},
                {"component": "VRow", "content": [
                    col(4, "VSwitch", model="use_proxy", label="使用代理",
                        hint="三条通道都会走代理；Cloudflare 对机房 IP 更严格，家宽直连通常更容易通过",
                        **{"persistent-hint": True}),
                    col(8, "VTextField", model="proxy_url", label="自定义代理地址（可选）",
                        placeholder="留空使用 MoviePilot 系统代理，如 http://user:pass@host:port"),
                ]},
                {"component": "VRow", "content": [
                    col(4, "VTextField", model="browser_timeout", label="过盾等待上限（秒）", type="number"),
                    col(4, "VSwitch", model="headless", label="浏览器无头模式",
                        hint="默认有头（虚拟显示）更容易过盾，起不来会自动退回无头", **{"persistent-hint": True}),
                    col(4, "VTextField", model="flaresolverr_url", label="FlareSolverr 地址（可选）",
                        placeholder="留空使用系统设置，如 http://192.168.1.2:8191"),
                ]},
                {"component": "VRow", "content": [
                    col(12, "VTextField", model="user_agent", label="自定义 User-Agent（可选，一般留空）",
                        hint="仅当 Cookie 里带有 cf_clearance 时填写获取该 Cookie 的浏览器 UA，两者必须配套",
                        **{"persistent-hint": True}),
                ]},
                {"component": "VRow", "content": [
                    col(12, "VAlert", type="info", variant="tonal", text=env_text),
                ]},
                {"component": "VRow", "content": [
                    col(12, "VAlert", type="warning", variant="tonal",
                        text="提示：Cookie 只在本机使用，不会上传到任何第三方。过盾失败时的页面截图保存在插件数据目录的 "
                             "last_challenge.png。也可在消息渠道发送 /nodeseek_sign 手动触发签到。"),
                ]},
            ],
        }]
        model = {
            "enabled": False,
            "notify": True,
            "onlyonce": False,
            "cookie": "",
            "cron": "17 8 * * *",
            "random_choice": True,
            "mode": "auto",
            "use_proxy": False,
            "proxy_url": "",
            "flaresolverr_url": "",
            "user_agent": "",
            "max_retries": 3,
            "retry_interval": 15,
            "random_delay": 30,
            "browser_timeout": 90,
            "headless": False,
            "history_days": 30,
        }
        return form, model

    def get_page(self) -> List[dict]:
        history = self.get_data("history") or []
        if not isinstance(history, list) or not history:
            return [{
                "component": "VAlert",
                "props": {"type": "info", "variant": "tonal", "text": "暂无签到记录，保存配置后可点击「立即运行一次」测试。"},
            }]
        history = sorted(history, key=lambda item: item.get("date", ""), reverse=True)
        latest = history[0]
        ok_days = len({item.get("date", "")[:10] for item in history if item.get("code") in OK_STATUSES})
        total_gain = sum(item.get("gain") or 0 for item in history if item.get("code") == ST_SUCCESS)
        profile = self._load_profile()

        summary = f"最近一次：{latest.get('date')} {latest.get('status')}"
        if latest.get("channel"):
            summary += f"（{latest.get('channel')}）"
        summary += f"；记录内成功签到 {ok_days} 天，累计获得 {total_gain} 个鸡腿。"
        if profile.get("ua"):
            summary += f" 已缓存浏览器过盾信息（{profile.get('source') or '浏览器'}，{profile.get('saved_at')}）。"

        color_map = {ST_SUCCESS: "success", ST_ALREADY: "info", ST_INVALID: "error",
                     ST_BLOCKED: "warning", ST_FAILED: "error", ST_ERROR: "error"}
        rows = []
        for item in history[:60]:
            gain = item.get("gain")
            rank = item.get("rank")
            rows.append({
                "component": "tr",
                "content": [
                    {"component": "td", "props": {"class": "text-no-wrap"}, "text": item.get("date", "")},
                    {"component": "td", "content": [{
                        "component": "VChip",
                        "props": {"color": color_map.get(item.get("code"), "default"), "size": "small",
                                  "variant": "tonal"},
                        "text": item.get("status", ""),
                    }]},
                    {"component": "td", "text": item.get("channel") or "-"},
                    {"component": "td", "text": "-" if gain is None else str(gain)},
                    {"component": "td", "text": f"{rank}" if rank else "-"},
                    {"component": "td", "text": item.get("message") or ""},
                ],
            })
        return [
            {"component": "VAlert",
             "props": {"type": "success" if latest.get("code") in OK_STATUSES else "warning",
                       "variant": "tonal", "class": "mb-3", "text": summary}},
            {"component": "VTable", "props": {"hover": True, "density": "compact"}, "content": [
                {"component": "thead", "content": [{"component": "tr", "content": [
                    {"component": "th", "text": "时间"},
                    {"component": "th", "text": "结果"},
                    {"component": "th", "text": "通道"},
                    {"component": "th", "text": "鸡腿"},
                    {"component": "th", "text": "排名"},
                    {"component": "th", "text": "说明"},
                ]}]},
                {"component": "tbody", "content": rows},
            ]},
        ]

    def stop_service(self):
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown(wait=False)
                self._scheduler = None
        except Exception as err:
            logger.error(f"NodeSeek 签到：停止服务失败 - {err}")
