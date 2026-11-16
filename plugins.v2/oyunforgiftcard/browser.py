"""
浏览器启动：优先 MoviePilot 自带的 CloakBrowser（反检测 Chromium），其次 Playwright。
"""
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

# 测试或自定义部署时可指定 Chromium 可执行文件
EXECUTABLE_ENV = "OYUNFOR_BROWSER_EXECUTABLE"

# Playwright 截图前默认会等页面的网页字体全部下载完：网络慢时字体迟迟下不完，截图就一直等到超时（每次白等十几秒）。
# 这个环境变量让它不等字体直接截（要在浏览器驱动启动前设置；对截图内容没有别的影响）
NO_FONT_WAIT_ENV = "PW_TEST_SCREENSHOT_NO_FONTS_READY"
os.environ.setdefault(NO_FONT_WAIT_ENV, "1")


def playwright_proxy(address: Optional[str]) -> Optional[Dict[str, str]]:
    """把 http://user:pass@host:port 转换为 Playwright 的代理配置"""
    if not address:
        return None
    parsed = urlparse(address if "://" in address else f"http://{address}")
    if not parsed.hostname:
        return None
    server = f"{parsed.scheme}://{parsed.hostname}" + (f":{parsed.port}" if parsed.port else "")
    proxy = {"server": server}
    if parsed.username:
        proxy["username"] = parsed.username
    if parsed.password:
        proxy["password"] = parsed.password
    return proxy


def launch_browser(headless: bool = False, proxy: Optional[Dict[str, str]] = None,
                   storage_state: Optional[Any] = None, locale: str = "tr-TR",
                   humanize: bool = False, log: Callable[[str], None] = print,
                   browser_type: str = "chromium") -> Tuple[Any, Callable[[], None], str]:
    """
    启动浏览器上下文
    :return: (context, 关闭函数, 引擎说明)
    """
    os.environ.setdefault(NO_FONT_WAIT_ENV, "1")
    context_kwargs: Dict[str, Any] = {"viewport": {"width": 1366, "height": 900}}
    if isinstance(storage_state, dict):
        # 解密后的登录状态直接以内存对象传入，不落明文文件
        context_kwargs["storage_state"] = storage_state
    elif storage_state and Path(storage_state).exists():
        context_kwargs["storage_state"] = str(storage_state)
    if locale:
        context_kwargs["locale"] = locale
    modes = [bool(headless)]
    if not headless:
        modes.append(True)  # 没有显示设备时退回无头
    errors: List[str] = []
    for mode in modes:
        label = "无头" if mode else "有头"
        # 1. CloakBrowser
        try:
            from cloakbrowser import launch_context
        except Exception:  # noqa
            launch_context = None
        if launch_context:
            for extra in ({"humanize": humanize} if humanize else {}, {}):
                try:
                    context = launch_context(headless=mode, proxy=proxy, **extra, **context_kwargs)
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
            launcher = getattr(playwright, browser_type or "chromium", playwright.chromium)
            kwargs: Dict[str, Any] = {"headless": mode}
            if proxy:
                kwargs["proxy"] = proxy
            if launcher is playwright.chromium:
                kwargs["args"] = ["--disable-blink-features=AutomationControlled", "--no-sandbox",
                                  "--disable-dev-shm-usage", "--window-size=1366,900"]
            executable = os.environ.get(EXECUTABLE_ENV)
            if executable:
                kwargs["executable_path"] = executable
            browser = launcher.launch(**kwargs)
            context = browser.new_context(**context_kwargs)

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


def add_cookies(context: Any, cookies: Dict[str, str], base_url: str, log: Callable[[str], None] = print) -> int:
    """把 Cookie 写入浏览器上下文（域名取站点主域）"""
    host = urlparse(base_url).hostname or ""
    parts = host.split(".")
    domain = "." + ".".join(parts[-2:]) if len(parts) >= 2 and not host.replace(".", "").isdigit() else host
    secure = base_url.startswith("https://")
    items = []
    for name, value in cookies.items():
        if not name or value is None:
            continue
        if name.startswith("__Host-"):
            items.append({"name": name, "value": value, "url": base_url.rstrip("/") + "/"})
        else:
            items.append({"name": name, "value": value, "domain": domain, "path": "/", "secure": secure})
    added = 0
    try:
        context.add_cookies(items)
        added = len(items)
    except Exception as err:
        log(f"批量写入 Cookie 失败，改为逐个写入 - {err}")
        for item in items:
            try:
                context.add_cookies([item])
                added += 1
            except Exception:  # noqa
                log(f"Cookie {item['name']} 写入失败，已跳过")
    return added
