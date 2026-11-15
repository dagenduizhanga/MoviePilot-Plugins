"""
Oyunfor App Store 礼品卡自动购买

用浏览器在 oyunfor.com 购买 App Store & iTunes 礼品卡（土耳其区，TL 面额），取回兑换码后推送给你。
- 付款方式：账户余额（推荐）或银行卡；
- 银行卡需要 3D Secure 时，插件停在银行验证页，把验证请求推送给你：
  回复 /oyunfor_code 验证码，或在银行 App 里确认后点「已在银行App确认」；
- 默认开启「演练模式」：走到付款前一步就停下，不会扣款。
"""
import base64
import hashlib
import queue
import re
import secrets
import shutil
import threading
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytz
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.schemas.types import EventType

from .browser import add_cookies, launch_browser, playwright_proxy
from .shop import (BASE_URL, CardInfo, Interaction, OyunforShop, PurchaseRequest, PurchaseResult,
                   StepRecorder, check_account, load_overrides, mask_code, parse_cookie, parse_expiry, run_purchase)

BASE_PLUGIN_ID = "Oyunfor" + "GiftCard"

STATUS_TEXT = {
    "success": "购买成功",
    "paid_no_code": "已付款·未取到码",
    "dry_run": "演练完成",
    "failed": "失败",
    "cancelled": "已取消",
    "timeout": "验证超时",
    "unknown": "结果未知",
    "error": "出错",
    "blocked": "被拦截",
    "login_required": "需要登录",
    "out_of_stock": "缺货",
    "limit": "超出限额",
    "insufficient": "余额不足",
    "busy": "忙",
}
STATUS_COLOR = {
    "success": "success", "paid_no_code": "warning", "dry_run": "info", "unknown": "warning",
    "cancelled": "default", "timeout": "default",
}
# 计入每日次数的状态（可能已经扣款）
SPENT_STATUSES = ("success", "paid_no_code", "unknown")
VERIFY_KIND_TEXT = {
    "otp": "银行要求输入验证码（短信/OTP）",
    "app": "银行要求在手机 App 中确认（Digital Token / 推送）",
    "unknown": "出现了银行验证页面",
    "binance": "等待你用币安付款",
}
PAY_MODES = ("binance", "balance", "card")
PAY_MODE_SHORT = {"balance": "余额", "card": "银行卡", "binance": "币安"}


class _Job:
    """正在进行的任务"""

    def __init__(self, kind: str, trigger: str, reply: Optional[dict]):
        self.id = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.kind = kind
        self.trigger = trigger
        self.reply = reply or {}
        self.started = time.time()
        self.stage = "准备中"
        self.waiting = ""
        self.screenshot: Optional[Path] = None
        self.cancel = threading.Event()
        self.inbox: "queue.Queue[str]" = queue.Queue()
        # 币安付款信息与二维码（二维码通过一次性令牌对外提供，给消息渠道取图）
        self.pay_info: Dict[str, Any] = {}
        self.qr_path: Optional[Path] = None
        self.qr_token = secrets.token_hex(16)


class _PluginInteraction(Interaction):
    """把流程中的验证请求转成消息，把用户的回复转回流程"""

    def __init__(self, plugin: "OyunforGiftCard", job: _Job):
        self.plugin = plugin
        self.job = job

    def verification_needed(self, kind: str, detail: str, attempt: int, screenshot: Optional[Path]) -> None:
        self.job.waiting = kind
        self.job.screenshot = screenshot or self.job.screenshot
        self.plugin.notify_verification(self.job, kind, detail)

    def verification_feedback(self, text: str) -> None:
        self.plugin.send(self.job.reply, "【Oyunfor 验证】", text)

    def payment_needed(self, info: Dict[str, Any], qr: Optional[Path], screenshot: Optional[Path]) -> None:
        self.job.waiting = "binance"
        self.job.pay_info = {k: info.get(k) for k in ("links", "amount", "currency", "countdown", "url")}
        self.job.qr_path = qr or self.job.qr_path
        self.job.screenshot = qr or screenshot or self.job.screenshot
        self.plugin.notify_payment(self.job)

    def payment_feedback(self, text: str) -> None:
        self.plugin.send(self.job.reply, "【Oyunfor 币安付款】", text)

    def poll(self) -> Optional[str]:
        try:
            value = self.job.inbox.get_nowait()
        except queue.Empty:
            return None
        if value not in ("ok", "cancel"):
            self.plugin.send(self.job.reply, "【Oyunfor 验证】", "已收到验证码，正在提交……")
        return value

    def cancelled(self) -> bool:
        return self.job.cancel.is_set()


class OyunforGiftCard(_PluginBase):
    # 插件名称
    plugin_name = "Oyunfor 礼品卡购买"
    # 插件描述
    plugin_desc = "在 oyunfor.com 自动购买 App Store 礼品卡（TL）：把币安付款码推给你，付款后推送兑换码；也支持余额、银行卡（3DS 转发）。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/dagenduizhanga/MoviePilot-Plugins/main/icons/oyunforgiftcard.png"
    # 插件版本
    plugin_version = "1.1.0"
    # 插件作者
    plugin_author = "dagenduizhanga"
    # 作者主页
    author_url = "https://github.com/dagenduizhanga"
    # 插件配置项ID前缀
    plugin_config_prefix = "oyunforgiftcard_"
    # 加载顺序
    plugin_order = 40
    # 可使用的用户级别
    auth_level = 1

    # 站点地址（测试时可覆盖）
    _base_url = BASE_URL
    # 测试时可置 0 跳过拟人等待
    _pause_scale = 1.0

    # 配置
    _enabled = False
    _notify = True
    _onlyonce = False
    _dry_run = True
    _cookie = ""
    _username = ""
    _password = ""
    _amount = "250"
    _product_url = ""
    _quantity = 1
    _pay_mode = "binance"
    _pay_timeout = 15
    _card_use_saved = True
    _card_last4 = ""
    _card_number = ""
    _card_holder = ""
    _card_expiry = ""
    _card_cvv = ""
    _max_total = 2000.0
    _daily_limit = 3
    _verify_timeout = 300
    _cron = ""
    _use_proxy = False
    _proxy_url = ""
    _headless = False
    _nav_timeout = 60
    _clear_cart = True
    _show_code = True
    _selectors = ""
    _keep_runs = 10
    _history_days = 180

    # 运行时（类属性只作默认值，实例上首次使用时创建）
    _lock: Optional[threading.Lock] = None
    _job: Optional[_Job] = None

    def init_plugin(self, config: dict = None):
        if self._lock is None:
            self._lock = threading.Lock()
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify", True))
        self._onlyonce = bool(config.get("onlyonce"))
        self._dry_run = bool(config.get("dry_run", True))
        self._cookie = (config.get("cookie") or "").strip()
        self._username = (config.get("username") or "").strip()
        self._password = config.get("password") or ""
        self._amount = str(config.get("amount") or "").strip()
        self._product_url = (config.get("product_url") or "").strip()
        self._quantity = self._int(config.get("quantity"), 1, 1, 10)
        self._pay_mode = config.get("pay_mode") if config.get("pay_mode") in PAY_MODES else "binance"
        self._pay_timeout = self._int(config.get("pay_timeout"), 15, 3, 60)
        self._card_use_saved = bool(config.get("card_use_saved", True))
        self._card_last4 = re.sub(r"\D", "", str(config.get("card_last4") or ""))[-4:]
        self._card_number = re.sub(r"[^\d ]", "", str(config.get("card_number") or "")).strip()
        self._card_holder = (config.get("card_holder") or "").strip()
        self._card_expiry = (config.get("card_expiry") or "").strip()
        self._card_cvv = re.sub(r"\D", "", str(config.get("card_cvv") or ""))
        self._max_total = self._float(config.get("max_total"), 2000.0, 0, 1000000)
        self._daily_limit = self._int(config.get("daily_limit"), 3, 0, 100)
        self._verify_timeout = self._int(config.get("verify_timeout"), 300, 60, 900)
        self._cron = (config.get("cron") or "").strip()
        self._use_proxy = bool(config.get("use_proxy"))
        self._proxy_url = (config.get("proxy_url") or "").strip()
        self._headless = bool(config.get("headless"))
        self._nav_timeout = self._int(config.get("nav_timeout"), 60, 20, 300)
        self._clear_cart = bool(config.get("clear_cart", True))
        self._show_code = bool(config.get("show_code", True))
        self._selectors = config.get("selectors") or ""
        self._keep_runs = self._int(config.get("keep_runs"), 10, 1, 100)
        self._history_days = self._int(config.get("history_days"), 180, 7, 3650)

        _, error = load_overrides(self._selectors)
        if error:
            logger.warning(f"Oyunfor：{error}")
        if self._card_expiry and not parse_expiry(self._card_expiry):
            logger.warning("Oyunfor：卡有效期格式不对，应为 MM/YY")

        # 网页上填写的验证码：交给正在等待的任务，然后清空该字段
        verify_code = str(config.get("verify_code") or "").strip()
        need_save = False
        if verify_code:
            ok, message = self.submit_code(verify_code)
            logger.info(f"Oyunfor：网页提交验证码 - {message}")
            need_save = True
        if self._onlyonce:
            self._onlyonce = False
            need_save = True
            ok, message = self.start_purchase(trigger="手动")
            logger.info(f"Oyunfor：立即购买一次 - {message}")
        if need_save:
            self.__save_config()

    # ------------------------------------------------------------------ 配置辅助
    @staticmethod
    def _int(value: Any, default: int, low: int, high: int) -> int:
        try:
            number = int(float(value))
        except (TypeError, ValueError):
            return default
        return max(low, min(high, number))

    @staticmethod
    def _float(value: Any, default: float, low: float, high: float) -> float:
        try:
            number = float(str(value).replace(",", "."))
        except (TypeError, ValueError):
            return default
        return max(low, min(high, number))

    def __config_dict(self) -> dict:
        return {
            "enabled": self._enabled, "notify": self._notify, "onlyonce": False, "dry_run": self._dry_run,
            "cookie": self._cookie, "username": self._username, "password": self._password,
            "amount": self._amount, "product_url": self._product_url, "quantity": self._quantity,
            "pay_mode": self._pay_mode, "pay_timeout": self._pay_timeout, "card_use_saved": self._card_use_saved, "card_last4": self._card_last4,
            "card_number": self._card_number, "card_holder": self._card_holder, "card_expiry": self._card_expiry,
            "card_cvv": self._card_cvv, "max_total": self._max_total, "daily_limit": self._daily_limit,
            "verify_timeout": self._verify_timeout, "cron": self._cron, "use_proxy": self._use_proxy,
            "proxy_url": self._proxy_url, "headless": self._headless, "nav_timeout": self._nav_timeout,
            "clear_cart": self._clear_cart, "show_code": self._show_code, "selectors": self._selectors,
            "keep_runs": self._keep_runs, "history_days": self._history_days, "verify_code": "",
        }

    def __save_config(self):
        self.update_config(self.__config_dict())

    @staticmethod
    def _now() -> datetime:
        return datetime.now(pytz.timezone(settings.TZ))

    def _now_str(self) -> str:
        return self._now().strftime("%Y-%m-%d %H:%M:%S")

    def _cookie_hash(self) -> str:
        normalized = ";".join(f"{k}={v}" for k, v in sorted(parse_cookie(self._cookie).items()))
        return hashlib.sha256(f"{normalized}|{self._username}".encode("utf-8")).hexdigest()[:16]

    def _proxy(self) -> Optional[Dict[str, str]]:
        if not self._use_proxy:
            return None
        if self._proxy_url:
            return playwright_proxy(self._proxy_url)
        proxy = getattr(settings, "PROXY", None)
        if isinstance(proxy, dict):
            return playwright_proxy(proxy.get("https") or proxy.get("http"))
        if isinstance(proxy, str):
            return playwright_proxy(proxy)
        return None

    def _card(self) -> CardInfo:
        return CardInfo(number=self._card_number, holder=self._card_holder, expiry=self._card_expiry,
                        cvv=self._card_cvv, saved_last4=self._card_last4, use_saved=self._card_use_saved)

    # ------------------------------------------------------------------ 消息
    def send(self, reply: Optional[dict], title: str, text: str = "", buttons: Optional[list] = None,
             force: bool = False, image: Optional[str] = None):
        """发送消息：有回复目标时只回给触发者，否则按通知开关广播"""
        reply = reply or {}
        if not force and not reply.get("channel") and not reply.get("userid") and not self._notify:
            return
        kwargs: Dict[str, Any] = {}
        if buttons:
            kwargs["buttons"] = buttons
        if image:
            kwargs["image"] = image
        try:
            self.post_message(channel=reply.get("channel"), userid=reply.get("userid"),
                              mtype=NotificationType.Plugin, title=title, text=text, **kwargs)
        except Exception as err:
            logger.error(f"Oyunfor：发送消息失败 - {err}")

    @staticmethod
    def _has_target(reply: Optional[dict]) -> bool:
        return bool(reply and (reply.get("channel") or reply.get("userid")))

    def _cmd(self, name: str) -> str:
        plugin_id = self.__class__.__name__
        suffix = plugin_id[len(BASE_PLUGIN_ID):].lower() if plugin_id.startswith(BASE_PLUGIN_ID) else ""
        return f"/oyunfor_{name}" + (f"_{suffix}" if suffix else "")

    def notify_verification(self, job: _Job, kind: str, detail: str):
        lines = [VERIFY_KIND_TEXT.get(kind, VERIFY_KIND_TEXT["unknown"])]
        if kind == "otp":
            lines.append(f"👉 收到验证码后回复：{self._cmd('code')} 123456")
        elif kind == "app":
            lines.append("👉 请打开银行 App 确认这笔交易，确认后页面会自动继续（也可以点下面的按钮）")
            lines.append(f"如果银行改发了短信验证码，回复：{self._cmd('code')} 123456")
        else:
            lines.append(f"👉 有验证码就回复 {self._cmd('code')} 123456；如果是在 App 中确认，确认后点下面的按钮")
        lines.append(f"不想买了：{self._cmd('cancel')}")
        lines.append(f"⏳ {self._verify_timeout // 60} 分钟内有效；也可以在插件设置的「验证码」框里填写后保存")
        if detail:
            lines.append(f"📄 页面：{detail[:120]}")
        plugin_id = self.__class__.__name__
        buttons = [[
            {"text": "✅ 已在银行App确认", "callback_data": f"[PLUGIN]{plugin_id}|approved"},
            {"text": "❌ 取消购买", "callback_data": f"[PLUGIN]{plugin_id}|cancel"},
        ]]
        self.send(job.reply, "【Oyunfor 需要你验证付款】", "\n".join(lines), buttons=buttons, force=True)

    def qr_image_url(self, job: _Job) -> Optional[str]:
        """给消息渠道取二维码图片用的地址（MoviePilot 自己去取，再以图片发给 Telegram 等渠道）"""
        if not job.qr_path:
            return None
        port = getattr(settings, "PORT", 3001) or 3001
        return f"http://127.0.0.1:{port}/api/v1/plugin/{self.__class__.__name__}/qr?token={job.qr_token}"

    def notify_payment(self, job: _Job):
        info = job.pay_info or {}
        plugin_id = self.__class__.__name__
        lines: List[str] = []
        if info.get("amount"):
            lines.append(f"💰 应付：{info['amount']} {info.get('currency') or ''}".rstrip())
        if info.get("countdown"):
            lines.append(f"⏳ 页面倒计时：{info['countdown']}")
        links = [link for link in info.get("links") or [] if link.startswith("http")]
        if links:
            lines.append(f"🔗 付款链接（手机上点开会跳到币安 App）：{links[0]}")
        if job.qr_path:
            lines.append("📷 或者在币安 App 里用「扫一扫」扫这张二维码（也可以长按保存，从相册识别）")
        if not links and not job.qr_path:
            lines.append("⚠️ 没抓到二维码和付款链接，请打开插件详情页看截图")
        lines.append(f"✅ 付好后不用回复，插件会自动取码；几分钟没动静就点「我已付款」或发 {self._cmd('paid')}")
        lines.append(f"❌ 不买了：{self._cmd('cancel')}（不付款的话，付款单到期会自动关闭）")
        lines.append(f"⏱ 最多等 {self._pay_timeout} 分钟")
        buttons = [[
            {"text": "✅ 我已付款", "callback_data": f"[PLUGIN]{plugin_id}|paid"},
            {"text": "❌ 取消购买", "callback_data": f"[PLUGIN]{plugin_id}|cancel"},
        ]]
        if links:
            buttons.insert(0, [{"text": "🔗 打开币安付款", "url": links[0]}])
        self.send(job.reply, "【Oyunfor 请用币安付款】", "\n".join(lines), buttons=buttons,
                  image=self.qr_image_url(job), force=True)

    # ------------------------------------------------------------------ 任务控制
    def busy(self) -> bool:
        return self._job is not None

    def submit_code(self, raw: str) -> Tuple[bool, str]:
        """接收用户的验证码 / 确认"""
        job = self._job
        if not job:
            return False, "当前没有进行中的任务"
        value = (raw or "").strip()
        if value.lower() in ("", "ok", "done", "yes", "approved", "已确认", "确认"):
            job.inbox.put("ok")
            if job.waiting == "binance":
                return True, "好的，正在去订单页确认付款，查到兑换码后马上推送给你"
            return True, "好的，继续等待页面跳转"
        digits = re.sub(r"[\s-]", "", value)
        if not re.fullmatch(r"[0-9A-Za-z]{3,12}", digits):
            return False, "验证码格式不对（应为 3~12 位数字或字母）"
        job.inbox.put(digits)
        return True, "验证码已提交" if job.waiting else "验证码已收下，出现验证页后会自动填入"

    def cancel_job(self) -> Tuple[bool, str]:
        job = self._job
        if not job:
            return False, "当前没有进行中的任务"
        job.cancel.set()
        job.inbox.put("cancel")
        return True, "已发出取消指令"

    def today_spent_count(self) -> int:
        today = self._now().strftime("%Y-%m-%d")
        return sum(1 for item in self._history() if str(item.get("date", "")).startswith(today)
                   and item.get("code") in SPENT_STATUSES and not item.get("dry_run"))

    def parse_buy_args(self, arg_str: str) -> Tuple[Optional[int], str, int, bool, str]:
        """解析 /oyunfor_buy 的参数：[面额|链接] [数量] [dry]"""
        amount: Optional[int] = None
        url = ""
        quantity = 0
        force_dry = False
        for token in (arg_str or "").split():
            low = token.lower()
            if low.startswith("http://") or low.startswith("https://"):
                url = token
            elif low in ("dry", "演练", "test"):
                force_dry = True
            elif re.fullmatch(r"\d+", token):
                if amount is None and not url:
                    amount = int(token)
                elif not quantity:
                    quantity = int(token)
                else:
                    return None, "", 0, False, f"多余的参数：{token}"
            elif re.fullmatch(r"[x×*]\d+", low):
                quantity = int(low[1:])
            else:
                return None, "", 0, False, f"看不懂的参数：{token}"
        if quantity and not 1 <= quantity <= 10:
            return None, "", 0, False, "数量应在 1~10 之间"
        return amount, url, quantity, force_dry, ""

    def start_purchase(self, amount: Optional[int] = None, product_url: str = "", quantity: int = 0,
                       reply: Optional[dict] = None, trigger: str = "手动", force_dry: bool = False) -> Tuple[bool, str]:
        if self._lock is None:
            self._lock = threading.Lock()
        if not self._cookie and not (self._username and self._password):
            return False, "请先在插件设置中填写 Oyunfor Cookie（或账号密码）"
        if not amount and not product_url:
            product_url = self._product_url
            if not product_url:
                try:
                    amount = int(re.sub(r"\D", "", self._amount))
                except ValueError:
                    return False, "请先在插件设置中填写面额或商品链接"
        dry_run = self._dry_run or force_dry
        if self._pay_mode == "card" and not dry_run and not self._card_use_saved and not self._card().has_manual:
            return False, "银行卡模式需要填写完整的卡信息，或开启「使用网站已保存的卡」"
        if not dry_run and self._daily_limit and self.today_spent_count() >= self._daily_limit:
            return False, f"今天已经买了 {self._daily_limit} 次，达到每日上限"
        if not self._lock.acquire(blocking=False):
            return False, "已有任务在进行中，请稍后再试"
        job = _Job("purchase", trigger, reply)
        self._job = job
        request = PurchaseRequest(amount=amount, product_url=product_url, quantity=quantity or self._quantity,
                                  pay_mode=self._pay_mode, dry_run=dry_run, max_total=self._max_total,
                                  card=self._card(), verify_timeout=self._verify_timeout,
                                  pay_timeout=self._pay_timeout * 60,
                                  known_codes=list(self.get_data("known_codes") or []))
        thread = threading.Thread(target=self._purchase_worker, args=(job, request), daemon=True,
                                  name=f"oyunfor-{job.id}")
        thread.start()
        target = product_url or f"{amount} TL"
        what = "演练" if dry_run else "购买"
        return True, f"开始{what}：{target} × {request.quantity}（{PAY_MODE_SHORT[self._pay_mode]}）"

    def start_check(self, reply: Optional[dict] = None) -> Tuple[bool, str]:
        if self._lock is None:
            self._lock = threading.Lock()
        if not self._lock.acquire(blocking=False):
            return False, "已有任务在进行中，请稍后再试"
        job = _Job("check", "手动", reply)
        self._job = job
        threading.Thread(target=self._check_worker, args=(job,), daemon=True, name=f"oyunfor-check-{job.id}").start()
        return True, "正在检查 Oyunfor 登录状态和余额……"

    # ------------------------------------------------------------------ 浏览器会话
    def _run_dir(self, job: _Job) -> Path:
        path = self.get_data_path() / "runs" / job.id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _state_file(self) -> Path:
        return self.get_data_path() / "storage_state.json"

    def _open_session(self, job: _Job, recorder: StepRecorder) -> Tuple[Any, Any, Any, OyunforShop]:
        state_file = self._state_file()
        use_state = state_file.exists() and self.get_data("state_hash") == self._cookie_hash()
        if state_file.exists() and not use_state:
            state_file.unlink(missing_ok=True)
        context, closer, engine = launch_browser(
            headless=self._headless, proxy=self._proxy(), storage_state=state_file if use_state else None,
            humanize=bool(getattr(settings, "CLOAKBROWSER_HUMANIZE", False)) and self._pause_scale > 0,
            log=recorder.note, browser_type=str(getattr(settings, "PLAYWRIGHT_BROWSER_TYPE", "") or "chromium"))
        recorder.note(f"浏览器已启动：{engine}" + ("（沿用上次的登录状态）" if use_state else ""))
        if not use_state:
            cookies = parse_cookie(self._cookie)
            if cookies:
                added = add_cookies(context, cookies, self._base_url, log=recorder.note)
                recorder.note(f"已写入 {added} 个 Cookie")
        page = context.new_page()
        page.set_default_timeout(30000)
        overrides, _ = load_overrides(self._selectors)
        shop = OyunforShop(context, page, base_url=self._base_url, log=recorder.note, recorder=recorder,
                           overrides=overrides, nav_timeout=self._nav_timeout, pause_scale=self._pause_scale)
        return context, closer, page, shop

    def _save_state(self, context: Any, keep: bool):
        state_file = self._state_file()
        try:
            if keep:
                context.storage_state(path=str(state_file))
                self.save_data("state_hash", self._cookie_hash())
            else:
                state_file.unlink(missing_ok=True)
                self.del_data("state_hash")
        except Exception as err:
            logger.warning(f"Oyunfor：保存登录状态失败 - {err}")

    def _make_recorder(self, job: _Job) -> StepRecorder:
        def log(text: str):
            job.stage = text[:120]
            logger.info(f"Oyunfor：{text}")

        return StepRecorder(self._run_dir(job), log)

    def _purchase_worker(self, job: _Job, request: PurchaseRequest):
        result: Optional[PurchaseResult] = None
        recorder = self._make_recorder(job)
        try:
            what = "演练" if request.dry_run else "购买"
            recorder.note(f"开始{what}（{job.trigger}）：{request.product_url or str(request.amount) + ' TL'} × "
                          f"{request.quantity}，付款方式 {request.pay_mode}")
            if self._has_target(job.reply):
                self.send(job.reply, f"【Oyunfor 开始{what}】",
                          f"{request.product_url or str(request.amount) + ' TL'} × {request.quantity}")
            # Playwright 同步接口不能运行在带事件循环的线程里：这里是独立线程
            context = closer = None
            try:
                context, closer, page, shop = self._open_session(job, recorder)
                login = (self._username, self._password) if self._username and self._password else None
                result = run_purchase(shop, request, _PluginInteraction(self, job), login=login,
                                      clear_cart=self._clear_cart)
                self._save_state(context, keep=result.status != "login_required")
            finally:
                if closer:
                    try:
                        closer()
                    except Exception:  # noqa
                        pass
        except Exception as err:
            logger.error(f"Oyunfor：任务异常 - {err}", exc_info=True)
            result = PurchaseResult(status="error", message=f"{type(err).__name__}: {str(err)[:200]}",
                                    steps=list(recorder.steps))
        finally:
            try:
                self._finish_purchase(job, request, result, recorder)
            finally:
                self._job = None
                self._cleanup_runs()
                if self._lock:
                    self._lock.release()

    def _check_worker(self, job: _Job):
        recorder = self._make_recorder(job)
        info: Dict[str, Any] = {}
        try:
            context = closer = None
            try:
                context, closer, page, shop = self._open_session(job, recorder)
                login = (self._username, self._password) if self._username and self._password else None
                info = check_account(shop, login=login, interaction=_PluginInteraction(self, job))
                self._save_state(context, keep=info.get("logged_in") is not False)
            finally:
                if closer:
                    try:
                        closer()
                    except Exception:  # noqa
                        pass
        except Exception as err:
            logger.error(f"Oyunfor：检查账户异常 - {err}", exc_info=True)
            info = {"logged_in": None, "balance": None, "message": f"{type(err).__name__}: {str(err)[:200]}"}
        finally:
            info["time"] = self._now_str()
            info["run"] = job.id
            self.save_data("account", info)
            state = {True: "✅ 已登录", False: "❌ 未登录（Cookie 失效）", None: "❔ 无法确认"}[info.get("logged_in")]
            lines = [state]
            if info.get("balance") is not None:
                lines.append(f"💰 余额：{info['balance']:.2f} TL")
            if info.get("message"):
                lines.append(f"💬 {info['message']}")
            self.send(job.reply, "【Oyunfor 账户】", "\n".join(lines), force=self._has_target(job.reply))
            self._job = None
            self._cleanup_runs()
            if self._lock:
                self._lock.release()

    # ------------------------------------------------------------------ 结果
    def _finish_purchase(self, job: _Job, request: PurchaseRequest, result: Optional[PurchaseResult],
                         recorder: StepRecorder):
        result = result or PurchaseResult(status="error", message="任务没有返回结果")
        product = result.product
        record = {
            "date": self._now_str(),
            "trigger": job.trigger,
            "run": job.id,
            "product": (product.title if product else "") or (request.product_url or f"{request.amount} TL"),
            "face": (product.face_value if product else None) or request.amount,
            "price": product.price if product else None,
            "quantity": request.quantity,
            "total": result.total,
            "pay_mode": request.pay_mode,
            "dry_run": request.dry_run,
            "code": result.status,
            "status": STATUS_TEXT.get(result.status, result.status),
            "message": result.message,
            "codes": result.codes,
            "verification": result.verification,
            "crypto": f"{job.pay_info.get('amount')} {job.pay_info.get('currency') or ''}".strip()
            if job.pay_info.get("amount") else "",
            "order_url": result.order_url,
            "balance": result.balance,
        }
        history = self._history()
        history.append(record)
        cutoff = (self._now() - timedelta(days=self._history_days)).strftime("%Y-%m-%d %H:%M:%S")
        self.save_data("history", [item for item in history if str(item.get("date", "")) >= cutoff][-500:])
        if result.codes:
            known = list(self.get_data("known_codes") or [])
            known.extend(code for code in result.codes if code not in known)
            self.save_data("known_codes", known[-2000:])
        if recorder.last_screenshot:
            self.save_data("last_screenshot", str(recorder.last_screenshot))
        if result.balance is not None:
            account = self.get_data("account") or {}
            account.update({"balance": result.balance, "time": self._now_str(),
                            "logged_in": result.status != "login_required"})
            self.save_data("account", account)
        logger.info(f"Oyunfor：任务结束 [{result.status}] {result.message}")
        self._notify_result(job, request, result, record)

    def _notify_result(self, job: _Job, request: PurchaseRequest, result: PurchaseResult, record: dict):
        status_text = record["status"]
        lines: List[str] = []
        if record.get("product"):
            lines.append(f"🎁 {record['product']} × {record['quantity']}")
        if result.total is not None:
            lines.append(f"💳 应付：{result.total:.2f} TL（{PAY_MODE_SHORT.get(request.pay_mode, request.pay_mode)}）")
        if job.pay_info.get("amount"):
            lines.append(f"🪙 币安付款：{job.pay_info['amount']} {job.pay_info.get('currency') or ''}".rstrip())
        if result.codes:
            for code in result.codes:
                lines.append(f"🔑 {code if self._show_code else mask_code(code)}")
            lines.append("兑换：App Store → 账户 → 兑换充值卡或代码（需土耳其区 Apple 账户）")
        if result.message and result.status != "success":
            lines.append(f"💬 {result.message}")
        if result.balance is not None:
            lines.append(f"💰 购买前余额：{result.balance:.2f} TL")
        if result.status not in ("success", "dry_run"):
            lines.append(f"🗂 截图与页面保存在插件数据目录 runs/{job.id}")
        lines.append(f"⏱ {record['date']}")
        title = f"【Oyunfor {status_text}】"
        # 已付款/结果未知/有码的结果一定通知；其他结果按通知开关
        important = result.status in SPENT_STATUSES or bool(result.codes)
        self.send(job.reply, title, "\n".join(lines), force=important or self._has_target(job.reply))

    def _history(self) -> List[dict]:
        history = self.get_data("history") or []
        return history if isinstance(history, list) else []

    def _cleanup_runs(self):
        try:
            runs = sorted((self.get_data_path() / "runs").iterdir(), key=lambda p: p.name)
        except Exception:  # noqa
            return
        for path in runs[:-self._keep_runs]:
            shutil.rmtree(path, ignore_errors=True)

    # ------------------------------------------------------------------ 宿主接口
    def get_state(self) -> bool:
        return bool(self._enabled)

    def __action(self, name: str) -> str:
        return f"{self.__class__.__name__.lower()}_{name}"

    def get_command(self) -> List[Dict[str, Any]]:
        commands = [
            ("buy", "Oyunfor 购买礼品卡（参数：面额 数量，如 250 1）"),
            ("code", "Oyunfor 提交 3DS 验证码（参数：验证码；不带参数=已在App确认）"),
            ("paid", "Oyunfor 我已用币安付款（让插件去订单页确认）"),
            ("cancel", "Oyunfor 取消当前购买"),
            ("status", "Oyunfor 检查登录与余额"),
        ]
        return [{
            "cmd": self._cmd(name),
            "event": EventType.PluginAction,
            "desc": desc,
            "category": "Oyunfor",
            "data": {"action": self.__action(name)},
        } for name, desc in commands]

    @eventmanager.register(EventType.PluginAction)
    def handle_command(self, event: Event = None):
        if not event:
            return
        data = event.event_data or {}
        action = data.get("action") or ""
        if not action.startswith(self.__class__.__name__.lower() + "_"):
            return
        name = action[len(self.__class__.__name__) + 1:]
        reply = {"channel": data.get("channel"), "userid": data.get("user")}
        arg_str = str(data.get("arg_str") or "").strip()
        if name == "buy":
            amount, url, quantity, force_dry, error = self.parse_buy_args(arg_str)
            if error:
                self.send(reply, "【Oyunfor】", f"{error}\n用法：{self._cmd('buy')} [面额|商品链接] [数量] [dry]", force=True)
                return
            ok, message = self.start_purchase(amount=amount, product_url=url, quantity=quantity, reply=reply,
                                              trigger="命令", force_dry=force_dry)
            if not ok:
                self.send(reply, "【Oyunfor】", message, force=True)
        elif name == "code":
            ok, message = self.submit_code(arg_str)
            self.send(reply, "【Oyunfor 验证】", message, force=True)
        elif name == "paid":
            ok, message = self.submit_code("ok")
            self.send(reply, "【Oyunfor】", message, force=True)
        elif name == "cancel":
            ok, message = self.cancel_job()
            self.send(reply, "【Oyunfor】", message, force=True)
        elif name == "status":
            ok, message = self.start_check(reply=reply)
            self.send(reply, "【Oyunfor】", message, force=True)

    @eventmanager.register(EventType.MessageAction)
    def handle_button(self, event: Event = None):
        if not event:
            return
        data = event.event_data or {}
        if data.get("plugin_id") != self.__class__.__name__:
            return
        reply = {"channel": data.get("channel"), "userid": data.get("userid")}
        text = str(data.get("text") or "")
        if text in ("approved", "paid"):
            ok, message = self.submit_code("ok")
        elif text == "cancel":
            ok, message = self.cancel_job()
        else:
            return
        self.send(reply, "【Oyunfor 验证】", message, force=True)

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {"path": "/code", "endpoint": self.api_code, "methods": ["GET"], "summary": "提交 3DS 验证码",
             "description": "参数 code=验证码；code=ok 表示已在 App 中确认"},
            {"path": "/cancel", "endpoint": self.api_cancel, "methods": ["GET"], "summary": "取消当前购买"},
            {"path": "/qr", "endpoint": self.api_qr, "methods": ["GET"], "allow_anonymous": True,
             "summary": "当前币安付款二维码（一次性令牌，任务结束即失效）"},
            {"path": "/debug", "endpoint": self.api_debug, "methods": ["GET"],
             "summary": "下载最近一次运行的截图与页面（zip）", "description": "参数 run=运行编号，留空为最近一次"},
        ]

    def api_code(self, code: str = "") -> dict:
        ok, message = self.submit_code(code)
        return {"success": ok, "message": message}

    def api_qr(self, token: str = ""):
        from starlette.responses import FileResponse, JSONResponse

        job = self._job
        if (not job or not job.qr_path or not token or not secrets.compare_digest(token, job.qr_token)
                or not Path(job.qr_path).exists()):
            return JSONResponse({"success": False, "message": "没有可用的付款二维码"}, status_code=404)
        return FileResponse(str(job.qr_path), media_type="image/png")

    def api_cancel(self) -> dict:
        ok, message = self.cancel_job()
        return {"success": ok, "message": message}

    def api_debug(self, run: str = ""):
        from starlette.responses import FileResponse, JSONResponse

        runs_dir = self.get_data_path() / "runs"
        runs = sorted([p for p in runs_dir.iterdir() if p.is_dir()], key=lambda p: p.name) if runs_dir.exists() else []
        if run:
            runs = [p for p in runs if p.name == re.sub(r"[^0-9-]", "", run)]
        if not runs:
            return JSONResponse({"success": False, "message": "没有运行记录"}, status_code=404)
        target = runs[-1]
        archive = self.get_data_path() / f"debug-{target.name}.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for item in sorted(target.iterdir()):
                zf.write(item, arcname=f"{target.name}/{item.name}")
        return FileResponse(str(archive), filename=archive.name, media_type="application/zip")

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled or not self._cron:
            return []
        try:
            trigger = CronTrigger.from_crontab(self._cron)
        except Exception as err:
            logger.error(f"Oyunfor：定时购买周期 {self._cron} 无效 - {err}")
            return []
        return [{
            "id": self.__class__.__name__,
            "name": f"{self.plugin_name}（定时）",
            "trigger": trigger,
            "func": self.run_scheduled,
            "kwargs": {},
        }]

    def run_scheduled(self):
        ok, message = self.start_purchase(trigger="定时")
        logger.info(f"Oyunfor：定时购买 - {message}")
        if not ok:
            self.send(None, "【Oyunfor 定时购买未执行】", message)

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        def col(md: int, component: str, **props) -> dict:
            return {"component": "VCol", "props": {"cols": 12, "md": md},
                    "content": [{"component": component, "props": props}]}

        def hint(md: int, component: str, hint_text: str, **props) -> dict:
            props.update({"hint": hint_text, "persistent-hint": True})
            return col(md, component, **props)

        def title(text: str) -> dict:
            return {"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12}, "content": [
                {"component": "VAlert", "props": {"type": "info", "variant": "tonal", "density": "compact",
                                                  "text": text}}]}]}

        form = [{
            "component": "VForm",
            "content": [
                {"component": "VRow", "content": [
                    col(3, "VSwitch", model="enabled", label="启用插件"),
                    col(3, "VSwitch", model="notify", label="发送通知"),
                    hint(3, "VSwitch", "开启时只走到付款前一步，不扣款", model="dry_run", label="演练模式",
                         color="warning"),
                    hint(3, "VSwitch", "保存后立刻按下面的设置执行一次", model="onlyonce", label="立即购买一次"),
                ]},
                title("① 账号：推荐粘贴 Cookie。账号密码只在 Cookie 失效时尝试（有图形验证码时无法自动登录）"),
                {"component": "VRow", "content": [
                    hint(12, "VTextarea", "浏览器登录 oyunfor.com → F12 → 网络 → 任意请求 → 请求头里的 Cookie 整段复制",
                         model="cookie", label="Oyunfor Cookie", rows=3),
                ]},
                {"component": "VRow", "content": [
                    col(6, "VTextField", model="username", label="账号（邮箱，可选）"),
                    col(6, "VTextField", model="password", label="密码（可选）", type="password"),
                ]},
                title("② 买什么"),
                {"component": "VRow", "content": [
                    hint(3, "VTextField", "如 100、250、500", model="amount", label="面额（TL）"),
                    hint(3, "VTextField", "每次买几张", model="quantity", label="数量", type="number"),
                    hint(6, "VTextField", "填了就优先用链接，不按面额找", model="product_url", label="商品链接（可选）",
                         placeholder="https://www.oyunfor.com/apple-store/..."),
                ]},
                title("③ 怎么付：币安支付最省事（把付款码推给你，你在币安 App 付款）；余额要先充值；银行卡需要完成 3DS"),
                {"component": "VRow", "content": [
                    col(4, "VSelect", model="pay_mode", label="付款方式", items=[
                        {"title": "币安支付 Binance Pay（推荐）", "value": "binance"},
                        {"title": "账户余额", "value": "balance"},
                        {"title": "银行卡（3DS 转发给你）", "value": "card"},
                    ]),
                    hint(4, "VTextField", "推送付款码后最多等多久（分钟）", model="pay_timeout", label="币安付款等待",
                         type="number"),
                    col(4, "VAlert", type="info", variant="tonal", density="compact",
                        text="币安模式不需要填卡；下面的银行卡设置只在选「银行卡」时使用"),
                ]},
                {"component": "VRow", "content": [
                    hint(4, "VSwitch", "网站上有已保存的卡时直接选它", model="card_use_saved", label="使用网站已保存的卡"),
                    hint(4, "VTextField", "有多张已保存的卡时用来挑选", model="card_last4", label="已保存卡的后 4 位"),
                ]},
                {"component": "VRow", "content": [
                    hint(5, "VTextField", "只在网站没有保存卡时使用；明文保存在 MoviePilot 数据库里",
                         model="card_number", label="卡号（可选）", type="password"),
                    col(3, "VTextField", model="card_holder", label="持卡人姓名"),
                    col(2, "VTextField", model="card_expiry", label="有效期 MM/YY"),
                    col(2, "VTextField", model="card_cvv", label="CVV", type="password"),
                ]},
                title("④ 安全限制"),
                {"component": "VRow", "content": [
                    hint(4, "VTextField", "应付金额超过这个数就停下（TL）；0 = 不限制", model="max_total",
                         label="单笔金额上限（TL）", type="number"),
                    hint(4, "VTextField", "不含演练；0 = 不限制", model="daily_limit", label="每日最多下单次数",
                         type="number"),
                    hint(4, "VTextField", "等你完成 3DS 的最长时间", model="verify_timeout", label="验证等待（秒）",
                         type="number"),
                ]},
                {"component": "VRow", "content": [
                    hint(6, "VTextField", "正在等待 3DS 时，可以把验证码填在这里再点保存（也可用 /oyunfor_code）",
                         model="verify_code", label="验证码（网页提交）"),
                    hint(6, "VCronField", "留空不定时；定时触发时也会走 3DS 转发", model="cron", label="定时购买（可选）"),
                ]},
                title("⑤ 网络与浏览器"),
                {"component": "VRow", "content": [
                    hint(3, "VSwitch", "留空地址则用系统代理", model="use_proxy", label="使用代理"),
                    col(5, "VTextField", model="proxy_url", label="代理地址（可选）",
                        placeholder="http://user:pass@host:port"),
                    hint(4, "VTextField", "打开页面、等 Cloudflare 的最长时间", model="nav_timeout",
                         label="页面超时（秒）", type="number"),
                ]},
                {"component": "VRow", "content": [
                    hint(3, "VSwitch", "默认有头（虚拟显示）更容易过 Cloudflare", model="headless", label="无头浏览器"),
                    hint(3, "VSwitch", "避免上次演练留下的商品混进订单", model="clear_cart", label="下单前清空购物车"),
                    hint(3, "VSwitch", "关闭则只显示首尾 4 位", model="show_code", label="通知显示完整兑换码"),
                    hint(3, "VTextField", "截图与页面保留最近几次", model="keep_runs", label="保留运行记录",
                         type="number"),
                ]},
                {"component": "VRow", "content": [
                    hint(12, "VTextarea", "页面结构与预期不同时使用，键名见插件说明。例："
                                          '{"buy_now": "#btnHemenAl", "pay_balance": "label[for=bakiye]"}',
                         model="selectors", label="选择器覆盖（JSON，高级）", rows=3),
                ]},
                {"component": "VRow", "content": [
                    col(12, "VAlert", type="warning", variant="tonal",
                        text="首次使用请保持「演练模式」，跑通后再关闭。演练只走到付款前一步；银行卡模式的演练会进入支付页"
                             "但不填卡、不提交。每次运行的截图和页面保存在插件数据目录 runs/ 下，也可以通过 "
                             "/api/v1/plugin/" + self.__class__.__name__ + "/debug?apikey=你的APIKEY 下载最近一次的压缩包。"),
                ]},
            ],
        }]
        model = {
            "enabled": False, "notify": True, "onlyonce": False, "dry_run": True,
            "cookie": "", "username": "", "password": "",
            "amount": "250", "product_url": "", "quantity": 1,
            "pay_mode": "binance", "pay_timeout": 15, "card_use_saved": True, "card_last4": "",
            "card_number": "", "card_holder": "", "card_expiry": "", "card_cvv": "",
            "max_total": 2000, "daily_limit": 3, "verify_timeout": 300,
            "verify_code": "", "cron": "",
            "use_proxy": False, "proxy_url": "", "nav_timeout": 60,
            "headless": False, "clear_cart": True, "show_code": True, "keep_runs": 10,
            "selectors": "", "history_days": 180,
        }
        return form, model

    def get_page(self) -> List[dict]:
        blocks: List[dict] = []
        job = self._job
        if job:
            if job.waiting == "binance":
                info = job.pay_info
                text = (f"金额 {info.get('amount') or '?'} {info.get('currency') or ''}，用币安 App 扫下面的二维码"
                        + (f"，或打开付款链接：{(info.get('links') or [''])[0]}" if info.get("links") else "")
                        + f"。付好后会自动取码；没动静就发 {self._cmd('paid')}。")
                blocks.append({"component": "VAlert", "props": {"type": "warning", "variant": "tonal", "class": "mb-3",
                                                                "title": "等待你用币安付款", "text": text}})
            elif job.waiting:
                text = (f"{VERIFY_KIND_TEXT.get(job.waiting, '等待验证')}。发送 {self._cmd('code')} 验证码，"
                        f"或在插件设置的「验证码」框填写后保存；在 App 中确认的话页面会自动继续。")
                blocks.append({"component": "VAlert", "props": {"type": "warning", "variant": "tonal", "class": "mb-3",
                                                                "title": "等待你完成付款验证", "text": text}})
            blocks.append({"component": "VAlert", "props": {
                "type": "info", "variant": "tonal", "class": "mb-3",
                "text": f"任务进行中（{job.trigger}，已运行 {int(time.time() - job.started)} 秒）：{job.stage}"}})
        account = self.get_data("account") or {}
        if account:
            state = {True: "已登录", False: "未登录（Cookie 失效）"}.get(account.get("logged_in"), "未确认")
            balance = account.get("balance")
            text = f"账户：{state}" + (f"；余额 {balance:.2f} TL" if isinstance(balance, (int, float)) else "") + \
                   f"（{account.get('time', '')}）"
            blocks.append({"component": "VAlert", "props": {"type": "success" if account.get("logged_in") else "warning",
                                                            "variant": "tonal", "class": "mb-3", "text": text}})
        if job and job.waiting == "binance" and job.qr_path and Path(job.qr_path).exists():
            qr_data = base64.b64encode(Path(job.qr_path).read_bytes()).decode("ascii")
            blocks.append({"component": "VImg", "props": {"src": f"data:image/png;base64,{qr_data}",
                                                          "max-width": 320, "class": "mb-3"}})
        shot = self.get_data("last_screenshot")
        if job and job.screenshot:
            shot = str(job.screenshot)
        if shot and Path(shot).exists() and Path(shot).stat().st_size < 2_500_000:
            data = base64.b64encode(Path(shot).read_bytes()).decode("ascii")
            blocks.append({"component": "VExpansionPanels", "props": {"class": "mb-3"}, "content": [{
                "component": "VExpansionPanel", "props": {"title": f"最近一次截图：{Path(shot).parent.name}/{Path(shot).name}"},
                "content": [{"component": "VExpansionPanelText", "content": [
                    {"component": "VImg", "props": {
                        "src": f"data:image/{'png' if str(shot).endswith('.png') else 'jpeg'};base64,{data}",
                        "max-height": 900}}]}]}]})
        history = sorted(self._history(), key=lambda item: item.get("date", ""), reverse=True)
        if not history:
            blocks.append({"component": "VAlert", "props": {
                "type": "info", "variant": "tonal",
                "text": f"暂无记录。先保持「演练模式」，打开「立即购买一次」保存，或在消息渠道发送 {self._cmd('buy')}。"}})
            return blocks
        rows = []
        for item in history[:80]:
            codes = item.get("codes") or []
            total = item.get("total")
            rows.append({"component": "tr", "content": [
                {"component": "td", "props": {"class": "text-no-wrap"}, "text": item.get("date", "")},
                {"component": "td", "text": item.get("trigger", "")},
                {"component": "td", "text": f"{item.get('product', '')} × {item.get('quantity', 1)}"},
                {"component": "td", "text": "-" if total is None else f"{total:.2f}"},
                {"component": "td", "text": PAY_MODE_SHORT.get(item.get("pay_mode"), item.get("pay_mode") or "")
                                            + ("·演练" if item.get("dry_run") else "")},
                {"component": "td", "content": [{"component": "VChip", "props": {
                    "color": STATUS_COLOR.get(item.get("code"), "error"), "size": "small", "variant": "tonal"},
                    "text": item.get("status", "")}]},
                {"component": "td", "props": {"style": "font-family: monospace"}, "text": " ".join(codes) or "-"},
                {"component": "td", "text": item.get("message") or ""},
            ]})
        blocks.append({"component": "VTable", "props": {"hover": True, "density": "compact"}, "content": [
            {"component": "thead", "content": [{"component": "tr", "content": [
                {"component": "th", "text": text} for text in
                ("时间", "触发", "商品", "应付(TL)", "方式", "结果", "兑换码", "说明")]}]},
            {"component": "tbody", "content": rows},
        ]})
        return blocks

    def stop_service(self):
        # 正在等待 3DS 的任务不在这里中断：保存配置（例如网页提交验证码）也会调用到这里
        pass
