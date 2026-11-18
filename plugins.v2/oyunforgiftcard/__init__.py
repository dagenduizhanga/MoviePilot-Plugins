"""
Oyunfor App Store 礼品卡自动购买

用浏览器在 oyunfor.com 购买 App Store & iTunes 礼品卡（土耳其区，TL 面额），取回兑换码后推送给你。
- 付款方式：账户余额（默认）、币安支付（推送付款码给你）、银行卡（3D Secure 转发给你）；
- 需要验证时推送消息，写明是「在 App 里确认」还是「回复验证码」；登录要邮箱验证码时同样转发；
- 安全：账号、密码、Cookie、卡信息、登录状态、兑换码、运行截图全部 AES-256-GCM 加密保存，
  密钥只来自环境变量 OYUNFOR_SECRET；敏感信息在一次性的「安全录入页」填写，不经过设置数据库；
- 默认开启「演练模式」：走到付款前一步就停下，不会扣款。
"""
import base64
import io
import json
import queue
import re
import secrets
import shutil
import threading
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import pytz
from apscheduler.triggers.cron import CronTrigger
from starlette.requests import Request

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas import NotificationType
from app.schemas.types import EventType

from .browser import add_cookies, launch_browser, playwright_proxy
from .secure_page import (FIELDS, SECURITY_HEADERS, mask_value, render_error, render_form, render_result,
                          validate)
from .shop import (BASE_URL, CATEGORY_PATH, CardInfo, Interaction, OyunforShop, PurchaseRequest, PurchaseResult,
                   StepRecorder, check_account, load_overrides, mask_card_text, mask_code, parse_catalog, parse_cookie,
                   run_purchase, warm_up)
from .vault import ENV_NAME, MIN_SECRET_LENGTH, Vault, VaultError, generate_secret

BASE_PLUGIN_ID = "Oyunfor" + "GiftCard"
# 默认商品链接：App Store 礼品卡分类页（按面额在里面挑），不用用户填写
DEFAULT_PRODUCT_URL = BASE_URL + CATEGORY_PATH
# 快速模式：点击之间「像人一样」的随机停顿缩短到这个比例
FAST_PAUSE_SCALE = 0.3
# 机器人里「选面额」按钮（Oyunfor App Store 分类页上的固定面额；其他 100~2000 的整数用命令买自定义面额卡）
MENU_AMOUNTS = (100, 250, 500, 1000, 1250, 1500, 1750, 2000)
# 确认购买的按钮多久后失效（秒），避免点到很久以前的旧按钮
BUY_BUTTON_TTL = 600
# 快速下单：点了面额后提前准备好的浏览器最多等多久（秒），等不到「购买」就关掉
WARM_TTL = 180
# 每天几点刷新一次各面额的商品编号（顺便刷新登录状态 / Cloudflare 验证）；设置了「检查 Cookie」时改在检查时一起刷新
CATALOG_REFRESH_HOUR, CATALOG_REFRESH_MINUTE = 4, 10
# 定时检查 Cookie（保活）：默认 cron（每天 09:00）；碰上正在买东西时隔多久再试；没能确认（网站打不开等）时隔多久再试；
# 每次最多试几次
DEFAULT_KEEPALIVE_CRON = "0 9 * * *"
KEEPALIVE_BUSY_DELAY = 10 * 60
KEEPALIVE_RETRY_DELAY = 15 * 60
KEEPALIVE_MAX_TRIES = 3

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
    "site_error": "网站出错",
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
PAY_MODES = ("balance", "binance", "card")
PAY_MODE_SHORT = {"balance": "余额", "card": "银行卡", "binance": "币安"}
# 敏感字段（只存在保险箱里）
SECRET_FIELDS = tuple(key for key, _, _ in FIELDS)
SESSION_FILE = "session.enc"
SECURE_TOKEN_TTL = 600


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
        # 是否在等用户直接回复验证码（结束时要释放）
        self.text_input = False


class _WarmSession:
    """
    快速下单的预热：用户点了面额后，提前启动浏览器、打开购物车、清空购物车。
    浏览器只能在启动它的线程里用，所以这个线程自己等「购买」请求，等到了就在这里接着买，等不到就关掉。
    """

    def __init__(self, job: "_Job"):
        self.job = job
        self.inbox: "queue.Queue[PurchaseRequest]" = queue.Queue()
        self.lock = threading.Lock()
        self.state = "starting"   # starting / used / closed
        self.cancel = threading.Event()

    def hand_over(self, request: PurchaseRequest) -> bool:
        """把购买请求交给预热好的浏览器；它已经关了就返回 False"""
        with self.lock:
            if self.state != "starting":
                return False
            self.state = "used"
            self.inbox.put(request)
            return True

    def close(self) -> bool:
        """不用了：还没被用上就标记关闭，返回 True"""
        with self.lock:
            if self.state != "starting":
                return False
            self.state = "closed"
            self.cancel.set()
            return True


class _KnownCodes:
    """已买过的兑换码：只保存不可逆指纹，用 `in` 判断"""

    def __init__(self, vault: Vault, fingerprints: Iterable[str]):
        self.vault = vault
        self.fingerprints = set(fingerprints or [])

    def __contains__(self, code: object) -> bool:
        return isinstance(code, str) and self.vault.fingerprint(code) in self.fingerprints


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
        self.plugin.send(self.job.reply, "【Oyunfor 验证】", text, force=True)
        # 还在等验证码（比如上一个不对）：让用户下一条普通消息继续当验证码
        self.plugin.await_text_input(self.job)

    def payment_needed(self, info: Dict[str, Any], qr: Optional[Path], screenshot: Optional[Path]) -> None:
        self.job.waiting = "binance"
        self.job.pay_info = {k: info.get(k) for k in ("links", "amount", "currency", "countdown", "url")}
        self.job.qr_path = qr or self.job.qr_path
        self.job.screenshot = qr or screenshot or self.job.screenshot
        self.plugin.notify_payment(self.job)

    def payment_feedback(self, text: str) -> None:
        self.plugin.send(self.job.reply, "【Oyunfor 币安付款】", text, force=True)

    def poll(self) -> Optional[str]:
        try:
            value = self.job.inbox.get_nowait()
        except queue.Empty:
            return None
        if value not in ("ok", "cancel"):
            self.plugin.send(self.job.reply, "【Oyunfor 验证】", "已收到验证码，正在提交……", force=True)
        return value

    def cancelled(self) -> bool:
        return self.job.cancel.is_set()


class OyunforGiftCard(_PluginBase):
    # 插件名称
    plugin_name = "Oyunfor 礼品卡购买"
    # 插件描述
    plugin_desc = "在 oyunfor.com 自动购买 App Store 礼品卡（TL）：余额 / 币安 / 银行卡付款，验证推送给你；敏感信息全部加密。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/dagenduizhanga/MoviePilot-Plugins/main/icons/oyunforgiftcard.png"
    # 插件版本
    plugin_version = "1.2.3"
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
    _fast_mode = True
    _quick_mode = True
    _warm: Optional[_WarmSession] = None

    # 配置（不含任何敏感信息）
    _enabled = False
    _notify = True
    _onlyonce = False
    _dry_run = True
    _amount = "250"
    _product_url = ""
    _quantity = 1
    _pay_mode = "balance"
    _pay_timeout = 15
    _max_total = 2000.0
    _daily_limit = 3
    _verify_timeout = 300
    _cron = ""
    _keepalive_cron = DEFAULT_KEEPALIVE_CRON
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
    _vault: Optional[Vault] = None
    _secure_tokens: Optional[Dict[str, float]] = None

    def init_plugin(self, config: dict = None):
        if self._lock is None:
            self._lock = threading.Lock()
        if self._secure_tokens is None:
            self._secure_tokens = {}
        self._vault = Vault(self.get_data_path(), aad=self.__class__.__name__)
        config = config or {}
        self._enabled = bool(config.get("enabled"))
        self._notify = bool(config.get("notify", True))
        self._onlyonce = bool(config.get("onlyonce"))
        self._dry_run = bool(config.get("dry_run", True))
        self._amount = str(config.get("amount") or "").strip()
        self._product_url = (config.get("product_url") or "").strip()
        self._quantity = self._int(config.get("quantity"), 1, 1, 10)
        self._pay_mode = config.get("pay_mode") if config.get("pay_mode") in PAY_MODES else "balance"
        self._pay_timeout = self._int(config.get("pay_timeout"), 15, 3, 60)
        self._max_total = self._float(config.get("max_total"), 2000.0, 0, 1000000)
        self._daily_limit = self._int(config.get("daily_limit"), 3, 0, 100)
        self._verify_timeout = self._int(config.get("verify_timeout"), 300, 60, 900)
        self._cron = (config.get("cron") or "").strip()
        self._keepalive_cron = self._keepalive_cron_from(config)
        self._use_proxy = bool(config.get("use_proxy"))
        self._proxy_url = (config.get("proxy_url") or "").strip()
        self._headless = bool(config.get("headless"))
        self._nav_timeout = self._int(config.get("nav_timeout"), 60, 20, 300)
        self._clear_cart = bool(config.get("clear_cart", True))
        self._fast_mode = bool(config.get("fast_mode", True))
        self._quick_mode = bool(config.get("quick_mode", True))
        self._show_code = bool(config.get("show_code", True))
        self._selectors = config.get("selectors") or ""
        self._keep_runs = self._int(config.get("keep_runs"), 10, 1, 100)
        self._history_days = self._int(config.get("history_days"), 180, 7, 3650)

        _, error = load_overrides(self._selectors)
        if error:
            logger.warning(f"Oyunfor：{error}")

        need_save = self._migrate(config)
        if "keepalive_time" in config and "keepalive_cron" not in config:
            # 1.2.2 存的「每天检查 Cookie」时间已换算成 cron：写回设置，设置页上显示的就是实际用的周期
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
            "amount": self._amount, "product_url": self._product_url, "quantity": self._quantity,
            "pay_mode": self._pay_mode, "pay_timeout": self._pay_timeout, "max_total": self._max_total,
            "daily_limit": self._daily_limit, "verify_timeout": self._verify_timeout, "cron": self._cron,
            "keepalive_cron": self._keepalive_cron,
            "use_proxy": self._use_proxy, "proxy_url": self._proxy_url, "headless": self._headless,
            "nav_timeout": self._nav_timeout, "clear_cart": self._clear_cart, "fast_mode": self._fast_mode,
            "quick_mode": self._quick_mode,
            "show_code": self._show_code,
            "selectors": self._selectors, "keep_runs": self._keep_runs, "history_days": self._history_days,
        }

    def __save_config(self):
        self.update_config(self.__config_dict())

    @staticmethod
    def _now() -> datetime:
        return datetime.now(pytz.timezone(settings.TZ))

    def _now_str(self) -> str:
        return self._now().strftime("%Y-%m-%d %H:%M:%S")

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

    # ------------------------------------------------------------------ 保险箱
    def _secret_meta(self) -> Dict[str, Dict[str, str]]:
        meta = self.get_data("vault_meta") or {}
        return meta if isinstance(meta, dict) else {}

    def read_secrets(self) -> Dict[str, str]:
        """解密敏感信息（只在任务开始时调用，用完即丢）"""
        blob = self.get_data("vault")
        if not blob:
            return {}
        data = self._vault.decrypt(blob, purpose="secrets")
        return data if isinstance(data, dict) else {}

    def store_secrets(self, updates: Dict[str, Optional[str]]) -> Dict[str, str]:
        """合并保存敏感信息（值为 None 表示删除），返回打码后的状态"""
        if not self._vault.ready:
            raise VaultError(self._vault.status_text())
        meta = self._secret_meta()
        try:
            current = self.read_secrets()
        except VaultError:
            # 口令换过：旧数据解不开，只能整体覆盖，旧的「已保存」提示也一并作废
            current, meta = {}, {}
        for key, value in updates.items():
            if key not in SECRET_FIELDS:
                continue
            if value is None:
                current.pop(key, None)
                meta.pop(key, None)
            else:
                current[key] = value
                meta[key] = {"hint": mask_value(key, value), "updated": self._now_str()}
        self.save_data("vault", self._vault.encrypt(current, purpose="secrets"))
        self.save_data("vault_meta", meta)
        if {"cookie", "username"} & set(updates):
            # 换了账号 / Cookie：旧的浏览器登录状态作废
            (self.get_data_path() / SESSION_FILE).unlink(missing_ok=True)
        return {key: value.get("hint", "") for key, value in meta.items()}

    def _migrate(self, config: dict) -> bool:
        """把旧版本留在配置里的明文挪进保险箱；清掉各种明文残留。返回配置是否需要重写"""
        need_save = False
        legacy = {key: str(config.get(key) or "").strip() for key in SECRET_FIELDS if str(config.get(key) or "").strip()}
        if legacy or any(key in config for key in SECRET_FIELDS + ("verify_code", "card_last4", "card_use_saved")):
            need_save = True
        if legacy:
            if self._vault.ready:
                accepted = {}
                for key, value in legacy.items():
                    clean, error = validate(key, value)
                    if not error:
                        accepted[key] = clean
                self.store_secrets(accepted)
                logger.info(f"Oyunfor：已把旧版本配置里的 {len(accepted)} 项敏感信息加密保存，并从配置中删除明文")
            else:
                logger.warning(f"Oyunfor：旧版本配置里的敏感信息已删除（没有设置 {ENV_NAME}，不能明文保存），"
                               f"请设置环境变量后在「安全录入」重新填写")
        data_path = self.get_data_path()
        # 旧版本的明文登录状态
        legacy_state = data_path / "storage_state.json"
        if legacy_state.exists():
            try:
                if self._vault.ready:
                    self._vault.write_file(data_path / SESSION_FILE, legacy_state.read_bytes(), purpose="session")
            finally:
                legacy_state.unlink(missing_ok=True)
        self.del_data("state_hash")
        # 旧版本的明文诊断文件和压缩包
        for path in list(data_path.glob("debug-*.zip")):
            path.unlink(missing_ok=True)
        runs = data_path / "runs"
        if runs.exists():
            for path in runs.rglob("*"):
                if path.is_file() and not path.name.endswith(".enc"):
                    path.unlink(missing_ok=True)
        # 旧版本的明文兑换码
        if self._vault.ready:
            known = self.get_data("known_codes")
            if known:
                fingerprints = set(self.get_data("known_fp") or [])
                fingerprints.update(self._vault.fingerprint(code) for code in known if isinstance(code, str))
                self.save_data("known_fp", sorted(fingerprints))
                self.del_data("known_codes")
            history = self._history()
            changed = False
            for item in history:
                if item.get("codes"):
                    item["codes_enc"] = self._vault.encrypt(item["codes"], purpose="codes")
                    item["codes_masked"] = [mask_code(code) for code in item["codes"]]
                    item["codes"] = []
                    changed = True
            if changed:
                self.save_data("history", history)
        return need_save

    def _issue_secure_token(self) -> str:
        now = time.time()
        tokens = {k: v for k, v in (self._secure_tokens or {}).items() if v > now}
        token = secrets.token_urlsafe(32)
        tokens[token] = now + SECURE_TOKEN_TTL
        # 最多保留 5 个有效链接
        self._secure_tokens = dict(sorted(tokens.items(), key=lambda item: item[1])[-5:])
        return token

    def _check_secure_token(self, token: str, consume: bool) -> bool:
        now = time.time()
        tokens = self._secure_tokens or {}
        for known, expires in list(tokens.items()):
            if expires <= now:
                tokens.pop(known, None)
                continue
            if token and secrets.compare_digest(token, known):
                if consume:
                    tokens.pop(known, None)
                return True
        return False

    def _read_run_file(self, path: Optional[Path]) -> Optional[bytes]:
        if not path:
            return None
        path = Path(path)
        if not path.exists():
            return None
        if path.name.endswith(".enc"):
            try:
                return self._vault.read_file(path, purpose="run")
            except VaultError:
                return None
        return path.read_bytes()

    # ------------------------------------------------------------------ 消息
    def send(self, reply: Optional[dict], title: str, text: str = "", buttons: Optional[list] = None,
             force: bool = False, image: Optional[str] = None, save_history: bool = True,
             edit: Optional[dict] = None):
        """
        发送消息：有回复目标时只回给触发者，否则按通知开关广播。
        :param edit: 点按钮触发时传入原消息（message_id / chat_id / source）：新消息带按钮时，
                     MoviePilot 会直接把原消息改成新内容，而不是再发一条
        """
        reply = reply or {}
        if not force and not reply.get("channel") and not reply.get("userid") and not self._notify:
            return
        kwargs: Dict[str, Any] = {}
        if buttons:
            kwargs["buttons"] = buttons
            if edit and edit.get("message_id") and edit.get("chat_id"):
                kwargs["original_message_id"] = edit["message_id"]
                kwargs["original_chat_id"] = str(edit["chat_id"])
                if edit.get("source"):
                    kwargs["source"] = edit["source"]
        if image:
            kwargs["image"] = image
        if not save_history:
            kwargs["save_history"] = False
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
        return "/oyunfor" + (f"_{name}" if name else "") + (f"_{suffix}" if suffix else "")

    def notify_verification(self, job: _Job, kind: str, detail: str):
        """
        验证提醒。能接管用户下一条普通消息时（命令或按钮触发的任务），提示「直接回复验证码」，不用记命令；
        否则（定时任务等）提示用命令回复。
        """
        plugin_id = self.__class__.__name__
        minutes = max(1, self._verify_timeout // 60)
        plain = self.await_text_input(job)
        how = "直接把验证码发给我就行（例如 123456），不用加命令" if plain else f"回复：{self._cmd('code')} 123456"
        cancel = {"text": "❌ 取消购买", "callback_data": f"[PLUGIN]{plugin_id}|cancel"}
        resend = {"text": "🔁 重新发送验证码", "callback_data": f"[PLUGIN]{plugin_id}|resend"}
        if kind == "login":
            title = "【Oyunfor 登录验证】需要验证码"
            lines = ["Cookie 失效了，插件正在用账号密码登录，Oyunfor 发了一个验证码（通常在你的邮箱里）。",
                     f"👉 收到后{how}"]
            buttons = [[cancel]]
        elif kind == "gateway":
            title = "【Oyunfor 支付页验证】需要验证码（还没付款）"
            lines = ["PayTR 要先验证一下才显示你保存的银行卡，这一步还没有付款。",
                     "验证码一般发到你第一次保存卡时填的手机号。",
                     f"👉 收到后{how}",
                     "没收到就点「重新发送验证码」。"]
            buttons = [[resend, cancel]]
        else:
            title = "【Oyunfor 付款验证】需要输入验证码"
            lines = ["银行会把验证码发到你的手机（短信 / App 里显示）。",
                     f"👉 收到后{how}",
                     "如果银行只要你在 App 里点确认，确认后页面会自动继续，不用回复。"]
            buttons = [[{"text": "✅ 已在 App 确认", "callback_data": f"[PLUGIN]{plugin_id}|approved"}],
                       [resend, cancel]]
        lines.append(f"不想买了：点「取消购买」{'或回复「取消」' if plain else ''}；⏳ {minutes} 分钟内有效")
        detail = mask_card_text(detail)
        if detail:
            lines.append(f"📄 页面：{detail[:260 if kind == 'gateway' else 120]}")
        self.send(job.reply, title, "\n".join(lines), buttons=buttons, force=True, save_history=False)

    # ------------------------------------------------------------------ 直接回复验证码（不用命令）
    @staticmethod
    def _input_manager():
        """MoviePilot 的「插件接管用户下一条消息」功能（新版才有）"""
        try:
            from app.helper.interaction import plugin_input_interaction_manager
            return plugin_input_interaction_manager
        except Exception:  # noqa
            return None

    def await_text_input(self, job: Optional[_Job]) -> bool:
        """
        让触发任务的用户下一条普通消息（不是命令）直接交给插件当验证码。
        :return: 是否可以「直接回复」（有回复目标）；MoviePilot 旧版没有这个功能时，靠 UserMessage 事件兜底
        """
        reply = (job.reply if job else None) or {}
        if not reply.get("userid"):
            return False
        manager = self._input_manager()
        if manager:
            try:
                manager.create_or_replace(
                    user_id=reply["userid"], plugin_id=self.__class__.__name__, channel=reply.get("channel"),
                    source=reply.get("source"), username=None, prompt_id=f"oyunfor-code-{job.id}",
                    timeout_seconds=max(60, self._verify_timeout), payload={"job": job.id})
            except Exception as err:  # noqa
                logger.debug(f"Oyunfor：注册直接回复失败 - {err}")
        job.text_input = True
        return True

    def release_text_input(self, job: Optional[_Job]):
        """任务结束：不再接管用户的消息"""
        reply = (job.reply if job else None) or {}
        if not job or not getattr(job, "text_input", False) or not reply.get("userid"):
            return
        job.text_input = False
        manager = self._input_manager()
        if not manager:
            return
        try:
            pending = manager.get_by_user(reply["userid"], reply.get("channel"), reply.get("source"))
            if pending and pending.plugin_id == self.__class__.__name__:
                manager.pop_by_user(reply["userid"], reply.get("channel"), reply.get("source"))
        except Exception:  # noqa
            pass

    def handle_text_code(self, reply: dict, text: str):
        """用户直接回复的一条普通消息：当验证码交给正在等待的任务"""
        job = self._job
        value = (text or "").strip()
        if value in ("取消", "退出", "cancel", "q", "quit", "exit"):
            ok, message = self.cancel_job()
            self.send(reply, "【Oyunfor】", message, force=True, save_history=False)
            return
        if not job or not job.waiting or job.waiting == "binance":
            self.send(reply, "【Oyunfor】", "现在没有在等验证码", force=True, save_history=False)
            return
        ok, message = self.submit_code(value)
        if not ok:
            # 格式不对：提示一下，继续等下一条
            self.send(reply, "【Oyunfor 验证】", f"{message}，请直接发验证码数字", force=True, save_history=False)
            self.await_text_input(job)

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
                  image=self.qr_image_url(job), force=True, save_history=False)

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
        if not self._vault or not self._vault.ready:
            return False, f"请先设置环境变量 {ENV_NAME}（见插件设置页），否则插件不保存、也不使用任何账号信息"
        meta = self._secret_meta()
        if "cookie" not in meta and not ("username" in meta and "password" in meta):
            return False, "请先在插件设置页打开「安全录入」，填写 Oyunfor Cookie（或账号密码）"
        try:
            config_amount: Optional[int] = int(re.sub(r"\D", "", self._amount)) or None
        except ValueError:
            config_amount = None
        if not amount and not product_url:
            # 用设置：链接可以是分类页（按面额在里面挑）或商品页（面额要对得上）
            product_url = self._product_url
            amount = config_amount
            if not product_url and not amount:
                return False, "请先在插件设置中填写面额或商品链接"
        dry_run = self._dry_run or force_dry
        if not dry_run and self._daily_limit and self.today_spent_count() >= self._daily_limit:
            return False, f"今天已经买了 {self._daily_limit} 次，达到每日上限"
        # 默认的 App Store 分类页和不填一样；快速下单：有这个面额保存好的商品编号就直接加入购物车
        if product_url and product_url.rstrip("/") == DEFAULT_PRODUCT_URL:
            product_url = ""
        quick_item = (self._catalog_table().get(amount) if self._quick_mode and amount and not product_url
                      else None)
        request = PurchaseRequest(amount=amount, product_url=product_url, quantity=quantity or self._quantity,
                                  pay_mode=self._pay_mode, dry_run=dry_run, max_total=self._max_total,
                                  verify_timeout=self._verify_timeout, pay_timeout=self._pay_timeout * 60,
                                  pick_amount=config_amount, quick_item=quick_item,
                                  known_codes=_KnownCodes(self._vault, self.get_data("known_fp") or []))
        warm = self._warm
        if warm is not None:
            # 点面额时已经在预热：把请求交给那个浏览器（同一个线程里接着买）
            warm.job.reply = dict(reply or {})
            warm.job.trigger = trigger
            if warm.hand_over(request):
                self._job = warm.job
                return True, self._start_message(amount, product_url, request, dry_run)
            # 预热的浏览器正在关：等它把锁放掉
            acquired = self._lock.acquire(timeout=15)
        else:
            acquired = self._lock.acquire(blocking=False)
        if not acquired:
            return False, "已有任务在进行中，请稍后再试"
        job = _Job("purchase", trigger, reply)
        self._job = job
        thread = threading.Thread(target=self._purchase_worker, args=(job, request), daemon=True,
                                  name=f"oyunfor-{job.id}")
        thread.start()
        return True, self._start_message(amount, product_url, request, dry_run)

    def _start_message(self, amount: Optional[int], product_url: str, request: PurchaseRequest,
                       dry_run: bool) -> str:
        shown_url = "" if product_url.rstrip("/") == DEFAULT_PRODUCT_URL else product_url
        target = (f"{amount} TL（{shown_url}）" if amount and shown_url else shown_url or f"{amount} TL")
        what = "演练" if dry_run else "购买"
        return f"开始{what}：{target} × {request.quantity}（{PAY_MODE_SHORT[self._pay_mode]}）"

    # ------------------------------------------------------------------ 快速下单：商品编号与预热
    def _catalog_table(self) -> Dict[int, Dict[str, Any]]:
        """保存好的各面额商品编号：{面额: {"id", "type", "price", "title"}}"""
        return parse_catalog((self.get_data("catalog") or {}).get("items") or [])

    def _save_catalog(self, items: List[Dict[str, Any]]) -> Dict[int, Dict[str, Any]]:
        table = parse_catalog(items)
        if table:
            keep = [item for item in items if str(item.get("id")) in {v["id"] for v in table.values()}]
            self.save_data("catalog", {"time": self._now_str(), "items": keep})
        return table

    def refresh_catalog(self):
        """每天凌晨：打开一次礼品卡分类页，刷新各面额的商品编号，顺便刷新登录状态 / Cloudflare 验证"""
        if self._lock is None:
            self._lock = threading.Lock()
        if not self._vault or not self._vault.ready:
            return
        meta = self._secret_meta()
        if "cookie" not in meta and not ("username" in meta and "password" in meta):
            return
        if not self._lock.acquire(blocking=False):
            logger.info("Oyunfor：正在执行其他任务，跳过这次商品编号刷新")
            return
        threading.Thread(target=self._catalog_worker, daemon=True, name="oyunfor-catalog").start()

    def _catalog_worker(self):
        recorder = StepRecorder(None, lambda text: logger.info(f"Oyunfor：{text}"))
        creds: Dict[str, str] = {}
        closer = None
        try:
            creds = self.read_secrets()
            context, closer, shop = self._open_session(recorder, creds)
            shop.goto(CATEGORY_PATH, "礼品卡分类页")
            logged = shop.is_logged_in()
            shop.wait_products()
            table = self._save_catalog(shop.read_catalog())
            if table:
                recorder.note("商品编号已刷新：" + "、".join(f"{face} TL" for face in sorted(table)))
            else:
                recorder.note("分类页上没读到商品编号，保留上次保存的")
            if logged is not False:
                self._save_state(context, keep=True)
        except Exception as err:  # noqa
            logger.warning(f"Oyunfor：刷新商品编号失败 - {type(err).__name__}: {str(err)[:160]}")
        finally:
            creds = {}
            if closer:
                try:
                    closer()
                except Exception:  # noqa
                    pass
            self._lock.release()

    def start_warmup(self, reply: Optional[dict]):
        """机器人里点了面额：提前启动浏览器、打开购物车并清空，等用户点「购买」"""
        if not self._quick_mode or self._warm is not None or self._job is not None:
            return
        if not self._vault or not self._vault.ready:
            return
        meta = self._secret_meta()
        if "cookie" not in meta and not ("username" in meta and "password" in meta):
            return
        if self._lock is None:
            self._lock = threading.Lock()
        if not self._lock.acquire(blocking=False):
            return
        warm = _WarmSession(_Job("purchase", "按钮", reply))
        self._warm = warm
        threading.Thread(target=self._warm_worker, args=(warm,), daemon=True,
                         name=f"oyunfor-warm-{warm.job.id}").start()

    def stop_warmup(self):
        warm = self._warm
        if warm is not None:
            warm.close()

    def _warm_worker(self, warm: _WarmSession):
        job = warm.job
        recorder = self._make_recorder(job)
        creds: Dict[str, str] = {}
        context = closer = shop = None
        prepared = None
        try:
            recorder.note("快速下单：你选了面额，提前启动浏览器、打开购物车")
            creds = self.read_secrets()
            context, closer, shop = self._open_session(recorder, creds)
            if not warm.cancel.is_set():
                prepared = warm_up(shop, clear_cart=self._clear_cart)
        except Exception as err:  # noqa
            recorder.note(f"预热没成功（不影响购买，购买时会重新打开）：{type(err).__name__}: {str(err)[:120]}")
            prepared = None
        request = None
        deadline = time.time() + WARM_TTL
        while request is None and time.time() < deadline and not warm.cancel.is_set():
            try:
                request = warm.inbox.get(timeout=0.5)
            except queue.Empty:
                continue
        if request is None:
            with warm.lock:
                if warm.state == "used":
                    # 正好在关的时候点了「购买」（请求在锁里放进来的）：用上
                    request = warm.inbox.get_nowait()
                else:
                    warm.state = "closed"
        if request is None:
            if closer:
                try:
                    closer()
                except Exception:  # noqa
                    pass
            creds = {}
            self._warm = None
            shutil.rmtree(self.get_data_path() / "runs" / job.id, ignore_errors=True)
            logger.info("Oyunfor：预热的浏览器没用上，已关闭")
            self._lock.release()
            return
        self._warm = None
        session = {"recorder": recorder, "creds": creds, "prepared": prepared,
                   "context": context, "closer": closer, "shop": shop}
        self._purchase_worker(job, request, session=session)

    def start_check(self, reply: Optional[dict] = None) -> Tuple[bool, str]:
        if self._lock is None:
            self._lock = threading.Lock()
        if not self._vault or not self._vault.ready:
            return False, f"请先设置环境变量 {ENV_NAME}"
        if self._warm is not None:
            # 还没用上的预热浏览器：关掉再查
            self.stop_warmup()
            acquired = self._lock.acquire(timeout=15)
        else:
            acquired = self._lock.acquire(blocking=False)
        if not acquired:
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

    def _open_session(self, recorder: StepRecorder, creds: Dict[str, str]) -> Tuple[Any, Any, OyunforShop]:
        state = None
        session = self.get_data_path() / SESSION_FILE
        if session.exists():
            try:
                state = json.loads(self._vault.read_file(session, purpose="session").decode("utf-8"))
            except (VaultError, ValueError) as err:
                recorder.note(f"上次的登录状态无法解密，已丢弃：{err}")
                session.unlink(missing_ok=True)
        context, closer, engine = launch_browser(
            headless=self._headless, proxy=self._proxy(), storage_state=state,
            humanize=bool(getattr(settings, "CLOAKBROWSER_HUMANIZE", False)) and self._pause_scale > 0,
            log=recorder.note, browser_type=str(getattr(settings, "PLAYWRIGHT_BROWSER_TYPE", "") or "chromium"))
        recorder.note(f"浏览器已启动：{engine}" + ("（沿用上次的登录状态）" if state else ""))
        if not state:
            cookies = parse_cookie(creds.get("cookie"))
            if cookies:
                added = add_cookies(context, cookies, self._base_url, log=recorder.note)
                recorder.note(f"已写入 {added} 个 Cookie")
        page = context.new_page()
        page.set_default_timeout(30000)
        overrides, _ = load_overrides(self._selectors)
        shop = OyunforShop(context, page, base_url=self._base_url, log=recorder.note, recorder=recorder,
                           overrides=overrides, nav_timeout=self._nav_timeout,
                           pause_scale=self._pause_scale * (FAST_PAUSE_SCALE if self._fast_mode else 1.0))
        return context, closer, shop

    def _save_state(self, context: Any, keep: bool):
        session = self.get_data_path() / SESSION_FILE
        try:
            if keep:
                state = context.storage_state()
                self._vault.write_file(session, json.dumps(state).encode("utf-8"), purpose="session")
            else:
                session.unlink(missing_ok=True)
        except Exception as err:
            logger.warning(f"Oyunfor：保存登录状态失败 - {type(err).__name__}")

    def _make_recorder(self, job: _Job) -> StepRecorder:
        def log(text: str):
            job.stage = text[:120]
            logger.info(f"Oyunfor：{text}")

        return StepRecorder(self._run_dir(job), log, vault=self._vault)

    @staticmethod
    def _login_of(creds: Dict[str, str]) -> Optional[Tuple[str, str]]:
        if creds.get("username") and creds.get("password"):
            return creds["username"], creds["password"]
        return None

    def _purchase_worker(self, job: _Job, request: PurchaseRequest, session: Optional[Dict[str, Any]] = None):
        """
        :param session: 快速下单预热好的浏览器（同一个线程里启动的）：直接用，不再启动、不再清空购物车
        """
        result: Optional[PurchaseResult] = None
        session = session or {}
        recorder = session.get("recorder") or self._make_recorder(job)
        try:
            what = "演练" if request.dry_run else "购买"
            target = (f"{request.amount} TL" if request.amount and (
                not request.product_url or request.product_url.rstrip("/") == DEFAULT_PRODUCT_URL)
                      else request.product_url or f"{request.amount} TL")
            recorder.note(f"开始{what}（{job.trigger}）：{target} × {request.quantity}，付款方式 {request.pay_mode}")
            if self._has_target(job.reply):
                # 按钮触发：把确认消息原地改成「开始」，只用这一次，之后的验证 / 结果消息照常新发
                edit = job.reply.pop("edit", None)
                cancel = [[{"text": "❌ 取消购买", "callback_data": f"[PLUGIN]{self.__class__.__name__}|cancel"}]]
                self.send(job.reply, f"【Oyunfor 开始{what}】", f"{target} × {request.quantity}",
                          buttons=cancel if edit else None, edit=edit)
            # 敏感信息只在这里解密，任务结束即丢弃
            creds = session.get("creds") or self.read_secrets()
            request.card = CardInfo(number=creds.get("card_number", ""), holder=creds.get("card_holder", ""),
                                    expiry=creds.get("card_expiry", ""), cvv=creds.get("card_cvv", ""),
                                    use_saved=True)
            context = closer = None
            try:
                # Playwright 同步接口不能运行在带事件循环的线程里：这里是独立线程
                if session.get("shop") is not None:
                    context, closer, shop = session["context"], session["closer"], session["shop"]
                else:
                    context, closer, shop = self._open_session(recorder, creds)
                result = run_purchase(shop, request, _PluginInteraction(self, job), login=self._login_of(creds),
                                      clear_cart=self._clear_cart, prepared=session.get("prepared"))
                if shop.catalog_seen:
                    # 打开过分类页：顺手把各面额的商品编号存下来，下次直接加入购物车
                    self._save_catalog(shop.catalog_seen)
                self._save_state(context, keep=result.status != "login_required")
            finally:
                creds = {}
                session.clear()
                request.card = CardInfo()
                if closer:
                    try:
                        closer()
                    except Exception:  # noqa
                        pass
        except Exception as err:
            logger.error(f"Oyunfor：任务异常 - {type(err).__name__}: {str(err)[:200]}")
            result = PurchaseResult(status="error", message=f"{type(err).__name__}: {str(err)[:200]}",
                                    steps=list(recorder.steps))
        finally:
            self.release_text_input(job)
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
            creds = self.read_secrets()
            context = closer = None
            try:
                context, closer, shop = self._open_session(recorder, creds)
                info = check_account(shop, login=self._login_of(creds), interaction=_PluginInteraction(self, job))
                self._save_state(context, keep=info.get("logged_in") is not False)
            finally:
                creds = {}
                if closer:
                    try:
                        closer()
                    except Exception:  # noqa
                        pass
        except Exception as err:
            logger.error(f"Oyunfor：检查账户异常 - {type(err).__name__}: {str(err)[:200]}")
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
            self.release_text_input(job)
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
            "pay_mode": result.paid_by or request.pay_mode,
            "dry_run": request.dry_run,
            "code": result.status,
            "status": STATUS_TEXT.get(result.status, result.status),
            "message": result.message,
            # 兑换码只加密保存
            "codes": [],
            "codes_enc": self._vault.encrypt(result.codes, purpose="codes") if result.codes else None,
            "codes_masked": [mask_code(code) for code in result.codes],
            "verification": result.verification,
            "crypto": f"{job.pay_info.get('amount')} {job.pay_info.get('currency') or ''}".strip()
            if job.pay_info.get("amount") else "",
            "balance": result.balance,
        }
        history = self._history()
        history.append(record)
        cutoff = (self._now() - timedelta(days=self._history_days)).strftime("%Y-%m-%d %H:%M:%S")
        self.save_data("history", [item for item in history if str(item.get("date", "")) >= cutoff][-500:])
        if result.codes:
            fingerprints = list(self.get_data("known_fp") or [])
            fingerprints.extend(self._vault.fingerprint(code) for code in result.codes)
            self.save_data("known_fp", sorted(set(fingerprints))[-5000:])
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
            paid_by = result.paid_by or request.pay_mode
            if paid_by == "balance" and request.pay_mode in ("card", "binance"):
                # 设置的是银行卡 / 币安，但余额够付，网站直接用余额下单了
                how = "余额够付，直接用余额支付，" + ("没有刷卡" if request.pay_mode == "card" else "没有用币安")
            else:
                how = PAY_MODE_SHORT.get(paid_by, paid_by)
            lines.append(f"💳 应付：{result.total:.2f} TL（{how}）")
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
            lines.append("🗂 截图在插件详情页（加密保存）")
        lines.append(f"⏱ {record['date']}")
        title = f"【Oyunfor {status_text}】"
        # 已付款/结果未知/有码的结果一定通知；其他结果按通知开关。含兑换码的消息不写入 MoviePilot 消息历史
        important = result.status in SPENT_STATUSES or bool(result.codes)
        self.send(job.reply, title, "\n".join(lines), force=important or self._has_target(job.reply),
                  save_history=not result.codes)

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
            ("", "Oyunfor 买礼品卡：发 /oyunfor 弹出面额按钮；/oyunfor 250 直接买 1 张 250 TL"),
            ("buy", "Oyunfor 购买礼品卡（同 /oyunfor；参数：面额 数量，如 250 2）"),
            ("code", "Oyunfor 提交验证码（参数：验证码；不带参数=已在App确认）"),
            ("paid", "Oyunfor 我已用币安付款（让插件去订单页确认）"),
            ("cancel", "Oyunfor 取消当前购买"),
            ("status", "Oyunfor 检查登录与余额"),
        ]
        return [{
            "cmd": self._cmd(name),
            "event": EventType.PluginAction,
            "desc": desc,
            "category": "Oyunfor",
            # /oyunfor 是 /oyunfor_buy 的简写，走同一个动作
            "data": {"action": self.__action(name or "buy")},
            # 机器人菜单里只显示 /oyunfor；其他命令不显示，但照样可以用
            "show": not name,
        } for name, desc in commands]

    # ------------------------------------------------------------------ 机器人选面额
    def _menu_amounts(self) -> List[int]:
        return [a for a in MENU_AMOUNTS if not self._max_total or a <= self._max_total + 0.01]

    def send_amount_menu(self, reply: Optional[dict], edit: Optional[dict] = None, notice: str = ""):
        """/oyunfor 不带参数：发面额按钮（edit：直接改原消息）"""
        plugin_id = self.__class__.__name__
        amounts = self._menu_amounts()
        rows = [[{"text": f"{a} TL", "callback_data": f"[PLUGIN]{plugin_id}|pick:{a}:1"} for a in amounts[i:i + 4]]
                for i in range(0, len(amounts), 4)]
        rows.append([{"text": "❌ 不买了", "callback_data": f"[PLUGIN]{plugin_id}|nobuy"}])
        lines = [f"付款方式：{PAY_MODE_SHORT[self._pay_mode]}" + (f"；单笔上限 {self._max_total:g} TL" if self._max_total else ""),
                 "点面额后会再确认一次，确认了才开始买。",
                 f"也可以直接发：{self._cmd('')} 250（买 1 张）、{self._cmd('')} 375 2（任意 100~2000 的面额 × 2 张）"]
        if self._dry_run:
            lines.append("⚠️ 现在开着「演练模式」：只会走到付款前一步，不扣款")
        if notice:
            lines.insert(0, notice)
        self.send(reply, "【Oyunfor 选择面额】", "\n".join(lines), buttons=rows, force=True, save_history=False,
                  edit=edit)

    def send_buy_confirm(self, reply: Optional[dict], amount: int, quantity: int, edit: Optional[dict] = None):
        """选了面额：再确认一次（可改数量、只演练）"""
        plugin_id = self.__class__.__name__
        stamp = format(int(time.time()), "x")
        go = "dry" if self._dry_run else "buy"
        rows = [
            [{"text": f"{'✓ ' if q == quantity else ''}{q} 张", "callback_data": f"[PLUGIN]{plugin_id}|pick:{amount}:{q}"}
             for q in (1, 2, 3)],
            [{"text": f"✅ 购买 {amount} TL × {quantity}" if go == "buy" else f"🧪 演练 {amount} TL × {quantity}",
              "callback_data": f"[PLUGIN]{plugin_id}|{go}:{amount}:{quantity}:{stamp}"}],
            [{"text": "🧪 只演练", "callback_data": f"[PLUGIN]{plugin_id}|dry:{amount}:{quantity}:{stamp}"},
             {"text": "↩️ 换面额", "callback_data": f"[PLUGIN]{plugin_id}|menu"},
             {"text": "❌ 不买了", "callback_data": f"[PLUGIN]{plugin_id}|nobuy"}],
        ]
        if go == "dry":
            rows[2].pop(0)
        lines = [f"🎁 {amount} TL App Store 卡 × {quantity}（共 {amount * quantity} TL）",
                 f"付款方式：{PAY_MODE_SHORT[self._pay_mode]}" + ("（PayTR 另收约 2.5% 手续费）" if self._pay_mode == "card" else ""),
                 f"按钮 {BUY_BUTTON_TTL // 60} 分钟内有效。"]
        if self._max_total and amount * quantity > self._max_total + 0.01:
            lines.append(f"⚠️ 超过单笔上限 {self._max_total:g} TL，会被拦下，请减少数量")
        if self._dry_run:
            lines.append("⚠️ 现在开着「演练模式」：只会走到付款前一步，不扣款。要真买请先在插件设置里关掉")
        self.send(reply, "【Oyunfor 确认购买】", "\n".join(lines), buttons=rows, force=True, save_history=False,
                  edit=edit)

    def handle_buy_button(self, reply: dict, text: str, edit: Optional[dict] = None) -> bool:
        """
        处理选面额 / 确认购买按钮；不是这类按钮返回 False。
        :param edit: 被点的那条消息：菜单 → 确认 → 开始 都在这一条消息上原地更新，不再一条条新发
        """
        plugin_id = self.__class__.__name__
        if text == "menu":
            self.send_amount_menu(reply, edit=edit)
            return True
        if text == "nobuy":
            self.stop_warmup()
            again = [[{"text": "🔄 重新选面额", "callback_data": f"[PLUGIN]{plugin_id}|menu"}]]
            self.send(reply, "【Oyunfor】", "好的，不买了", buttons=again, force=True, save_history=False, edit=edit)
            return True
        match = re.fullmatch(r"(pick|buy|dry):(\d{2,5}):(\d{1,2})(?::([0-9a-f]+))?", text)
        if not match:
            return False
        action, amount, quantity, stamp = match.group(1), int(match.group(2)), int(match.group(3)), match.group(4)
        if not 1 <= quantity <= 10 or not 100 <= amount <= 2000:
            self.send(reply, "【Oyunfor】", "面额应在 100~2000 TL，数量 1~10", force=True)
            return True
        if action == "pick":
            self.send_buy_confirm(reply, amount, quantity, edit=edit)
            # 快速下单：用户多半要买了，趁他确认的这几秒提前打开浏览器、清空购物车
            self.start_warmup(reply)
            return True
        if not stamp or time.time() - int(stamp, 16) > BUY_BUTTON_TTL:
            self.send_amount_menu(reply, edit=edit, notice="⌛ 刚才的按钮已过期，请重新选择面额")
            return True
        # 「开始购买」也改在这条消息上（带一个取消按钮），确认按钮随之消失，不会被重复点
        start_reply = dict(reply, edit=edit) if edit else reply
        ok, message = self.start_purchase(amount=amount, quantity=quantity, reply=start_reply, trigger="按钮",
                                          force_dry=action == "dry")
        if not ok:
            again = [[{"text": "🔄 重新选面额", "callback_data": f"[PLUGIN]{plugin_id}|menu"}]]
            self.send(reply, "【Oyunfor】", message, buttons=again, force=True, edit=edit)
        return True

    @eventmanager.register(EventType.PluginAction)
    def handle_command(self, event: Event = None):
        if not event:
            return
        data = event.event_data or {}
        action = data.get("action") or ""
        if not action.startswith(self.__class__.__name__.lower() + "_"):
            return
        name = action[len(self.__class__.__name__) + 1:]
        reply = {"channel": data.get("channel"), "userid": data.get("user"), "source": data.get("source")}
        arg_str = str(data.get("arg_str") or "").strip()
        if name == "buy":
            if not arg_str:
                # 不带参数：发面额按钮，选好、确认后再买
                self.send_amount_menu(reply)
                return
            amount, url, quantity, force_dry, error = self.parse_buy_args(arg_str)
            if error:
                self.send(reply, "【Oyunfor】", f"{error}\n用法：{self._cmd('')} [面额] [数量] [dry]，如 {self._cmd('')} 250 1；只发 {self._cmd('')} 会弹出面额按钮", force=True)
                return
            ok, message = self.start_purchase(amount=amount, product_url=url, quantity=quantity, reply=reply,
                                              trigger="命令", force_dry=force_dry)
            if not ok:
                self.send(reply, "【Oyunfor】", message, force=True)
        elif name == "code":
            ok, message = self.submit_code(arg_str)
            self.send(reply, "【Oyunfor 验证】", message, force=True, save_history=False)
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
        reply = {"channel": data.get("channel"), "userid": data.get("userid"), "source": data.get("source")}
        text = str(data.get("text") or "")
        if text.startswith("plugin_input|"):
            # 用户直接回复的普通消息（MoviePilot 转给插件）
            self.handle_text_code(reply, str(data.get("input_text") or ""))
            return
        if text.startswith("plugin_input_cancel|"):
            ok, message = self.cancel_job()
            self.send(reply, "【Oyunfor】", message, force=True, save_history=False)
            return
        if text.startswith("plugin_input_expired|"):
            return
        if text in ("approved", "paid"):
            ok, message = self.submit_code("ok")
        elif text == "resend":
            job = self._job
            if not job or not job.waiting:
                ok, message = False, "现在没有在等验证码"
            else:
                job.inbox.put("resend")
                ok, message = True, "好的，正在重新请求验证码，收到后直接发给我"
        elif text == "cancel":
            ok, message = self.cancel_job()
        else:
            edit = {"message_id": data.get("original_message_id"), "chat_id": data.get("original_chat_id"),
                    "source": data.get("source")}
            self.handle_buy_button(reply, text, edit=edit if edit["message_id"] and edit["chat_id"] else None)
            return
        self.send(reply, "【Oyunfor 验证】", message, force=True)

    @eventmanager.register(EventType.UserMessage)
    def handle_user_message(self, event: Event = None):
        """
        兜底：MoviePilot 旧版没有「插件接管下一条消息」时，普通消息会走 UserMessage 事件。
        只在正在等验证码、而且是发起任务的那个人发来像验证码的内容时才处理。
        """
        if not event or self._input_manager():
            return
        job = self._job
        if not job or not job.waiting or job.waiting == "binance" or not getattr(job, "text_input", False):
            return
        data = event.event_data or {}
        reply = job.reply or {}
        if str(data.get("userid")) != str(reply.get("userid")):
            return
        text = str(data.get("text") or "").strip()
        if re.fullmatch(r"[0-9A-Za-z]{3,12}", re.sub(r"[\s-]", "", text)) or text in ("取消", "退出"):
            self.handle_text_code(reply, text)

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {"path": "/secure", "endpoint": self.api_secure_form, "methods": ["GET"], "allow_anonymous": True,
             "summary": "安全录入页（一次性链接，从插件设置页打开）"},
            {"path": "/secure", "endpoint": self.api_secure_submit, "methods": ["POST"], "allow_anonymous": True,
             "summary": "提交安全录入（一次性令牌）"},
            {"path": "/code", "endpoint": self.api_code, "methods": ["GET"], "summary": "提交验证码",
             "description": "参数 code=验证码；code=ok 表示已在 App 中确认"},
            {"path": "/cancel", "endpoint": self.api_cancel, "methods": ["GET"], "summary": "取消当前购买"},
            {"path": "/qr", "endpoint": self.api_qr, "methods": ["GET"], "allow_anonymous": True,
             "summary": "当前币安付款二维码（一次性令牌，任务结束即失效）"},
            {"path": "/debug", "endpoint": self.api_debug, "methods": ["GET"],
             "summary": "下载最近一次运行的截图与页面（zip，解密后打包）", "description": "参数 run=运行编号，留空为最近一次"},
        ]

    def _html(self, content: str, status: int = 200):
        from starlette.responses import HTMLResponse

        return HTMLResponse(content, status_code=status, headers=SECURITY_HEADERS)

    def _saved_hints(self) -> Dict[str, str]:
        return {key: value.get("hint", "") for key, value in self._secret_meta().items()}

    def api_secure_form(self, request: Request, t: str = ""):
        if not self._vault or not self._vault.ready:
            return self._html(render_error(self._vault.status_text() if self._vault else "插件未初始化"), 403)
        if not self._check_secure_token(t, consume=False):
            return self._html(render_error("链接已失效或不正确（10 分钟内有效，只能提交一次）"), 403)
        return self._html(render_form(t, self._saved_hints(), [], request.url.scheme != "https"))

    async def api_secure_submit(self, request: Request):
        form = await request.form()
        token = str(form.get("t") or "")
        if not self._vault or not self._vault.ready:
            return self._html(render_error(self._vault.status_text() if self._vault else "插件未初始化"), 403)
        if not self._check_secure_token(token, consume=False):
            return self._html(render_error("链接已失效或不正确（10 分钟内有效，只能提交一次）"), 403)
        updates: Dict[str, Optional[str]] = {}
        errors: List[Tuple[str, str]] = []
        for key, label, _ in FIELDS:
            if form.get(f"clear_{key}"):
                updates[key] = None
                continue
            raw = form.get(key)
            if raw is None or str(raw).strip() == "":
                continue
            value, error = validate(key, str(raw))
            if error:
                errors.append(("err", f"{label}：{error}"))
            else:
                updates[key] = value
        if errors:
            # 有错误：什么都不保存，令牌保留，让用户改了再提交
            return self._html(render_form(token, self._saved_hints(), errors, request.url.scheme != "https"), 400)
        self._check_secure_token(token, consume=True)
        if not updates:
            return self._html(render_result(self._saved_hints(), [("warn", "没有填写任何内容，保持不变")]))
        try:
            hints = self.store_secrets(updates)
        except VaultError as err:
            return self._html(render_error(str(err)), 500)
        changed = "、".join(dict((k, label) for k, label, _ in FIELDS)[key] for key in updates)
        logger.info(f"Oyunfor：安全录入已更新 {len(updates)} 项（内容不记录）")
        return self._html(render_result(hints, [("ok", f"已加密保存：{changed}")]))

    def api_code(self, code: str = "") -> dict:
        ok, message = self.submit_code(code)
        return {"success": ok, "message": message}

    def api_qr(self, token: str = ""):
        from starlette.responses import JSONResponse, Response

        job = self._job
        if not job or not job.qr_path or not token or not secrets.compare_digest(token, job.qr_token):
            return JSONResponse({"success": False, "message": "没有可用的付款二维码"}, status_code=404)
        data = self._read_run_file(job.qr_path)
        if not data:
            return JSONResponse({"success": False, "message": "没有可用的付款二维码"}, status_code=404)
        return Response(data, media_type="image/png", headers={"Cache-Control": "no-store"})

    def api_cancel(self) -> dict:
        ok, message = self.cancel_job()
        return {"success": ok, "message": message}

    def api_debug(self, run: str = ""):
        from starlette.responses import JSONResponse, Response

        runs_dir = self.get_data_path() / "runs"
        runs = sorted([p for p in runs_dir.iterdir() if p.is_dir()], key=lambda p: p.name) if runs_dir.exists() else []
        if run:
            runs = [p for p in runs if p.name == re.sub(r"[^0-9-]", "", run)]
        if not runs:
            return JSONResponse({"success": False, "message": "没有运行记录"}, status_code=404)
        target = runs[-1]
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for item in sorted(target.iterdir()):
                data = self._read_run_file(item)
                if data is None:
                    continue
                name = item.name[:-4] if item.name.endswith(".enc") else item.name
                zf.writestr(f"{target.name}/{name}", data)
        return Response(buffer.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="oyunfor-{target.name}.zip"',
                                 "Cache-Control": "no-store"})

    def get_service(self) -> List[Dict[str, Any]]:
        if not self._enabled:
            return []
        services: List[Dict[str, Any]] = []
        if self._cron:
            try:
                services.append({
                    "id": self.__class__.__name__,
                    "name": f"{self.plugin_name}（定时）",
                    "trigger": CronTrigger.from_crontab(self._cron),
                    "func": self.run_scheduled,
                    "kwargs": {},
                })
            except Exception as err:
                logger.error(f"Oyunfor：定时购买周期 {self._cron} 无效 - {err}")
        keep = self._keepalive_trigger()
        if self._keepalive_cron and not keep:
            logger.error(f"Oyunfor：检查 Cookie 的周期「{self._keepalive_cron}」无效，请填 cron 表达式，如 0 9 * * *")
        if keep:
            # 定时检查 Cookie（顺便刷新登录状态和快速下单的商品编号）
            services.append({
                "id": f"{self.__class__.__name__}Keepalive",
                "name": f"{self.plugin_name}（检查 Cookie）",
                "trigger": keep,
                "func": self.run_keepalive,
                "kwargs": {},
            })
        elif self._quick_mode:
            # 快速下单：每天凌晨刷新一次各面额的商品编号
            services.append({
                "id": f"{self.__class__.__name__}Catalog",
                "name": f"{self.plugin_name}（刷新商品编号）",
                "trigger": CronTrigger(hour=CATALOG_REFRESH_HOUR, minute=CATALOG_REFRESH_MINUTE),
                "func": self.refresh_catalog,
                "kwargs": {},
            })
        return services

    def run_scheduled(self):
        ok, message = self.start_purchase(trigger="定时")
        logger.info(f"Oyunfor：定时购买 - {message}")
        if not ok:
            self.send(None, "【Oyunfor 定时购买未执行】", message)

    # ------------------------------------------------------------------ 定时检查 Cookie（保活）
    @classmethod
    def _keepalive_cron_from(cls, config: dict) -> str:
        """
        「检查 Cookie」的 cron。1.2.2 存的是时间（keepalive_time，如 09:00）：自动换算成 cron（分 时 * * *），不用重新设置
        """
        if "keepalive_cron" in config:
            return str(config.get("keepalive_cron") or "").strip()
        if "keepalive_time" in config:
            old = str(config.get("keepalive_time") or "").strip()
            if not old:
                return ""
            hour_minute = cls._parse_time(old)
            return f"{hour_minute[1]} {hour_minute[0]} * * *" if hour_minute else DEFAULT_KEEPALIVE_CRON
        return DEFAULT_KEEPALIVE_CRON

    def _keepalive_trigger(self) -> Optional[CronTrigger]:
        """检查 Cookie 的触发器：cron 表达式；顺手兼容直接写的时间（如 09:00）；留空 / 无效返回 None"""
        text = self._keepalive_cron
        if not text:
            return None
        try:
            return CronTrigger.from_crontab(text)
        except Exception:  # noqa
            hour_minute = self._parse_time(text)
            return CronTrigger(hour=hour_minute[0], minute=hour_minute[1]) if hour_minute else None

    @staticmethod
    def _parse_time(text: str) -> Optional[Tuple[int, int]]:
        """「09:00」「9:30」「21」「9点」「9点半」「21点15分」→ (时, 分)；看不懂返回 None"""
        raw = (text or "").strip().replace("：", ":").replace(" ", "")
        match = re.fullmatch(r"(\d{1,2})(?:(?::|点|时)(\d{1,2}|半)?分?)?", raw)
        if not match:
            return None
        hour = int(match.group(1))
        minute = 30 if match.group(2) == "半" else int(match.group(2) or 0)
        return (hour, minute) if 0 <= hour <= 23 and 0 <= minute <= 59 else None

    def run_keepalive(self, attempt: int = 1):
        """
        定时：用插件保存的登录状态打开 Oyunfor，看 Cookie 是否还有效（顺便刷新登录状态和快速下单的商品编号）。
        失效（自动重新登录也不行）才发消息；有效不打扰；网站打不开等没能确认时过一会儿再试，不发消息。
        """
        if not self._enabled:
            return
        if self._lock is None:
            self._lock = threading.Lock()
        if not self._vault or not self._vault.ready:
            logger.info("Oyunfor：还没设置加密口令，跳过 Cookie 检查")
            return
        meta = self._secret_meta()
        if "cookie" not in meta and not ("username" in meta and "password" in meta):
            logger.info("Oyunfor：还没录入 Cookie，跳过 Cookie 检查")
            return
        if not self._lock.acquire(blocking=False):
            # 正在买东西 / 预热：过一会儿再查
            self._retry_keepalive(attempt, KEEPALIVE_BUSY_DELAY, "正在执行其他任务")
            return
        job = _Job("check", "检查 Cookie", None)
        self._job = job
        threading.Thread(target=self._keepalive_worker, args=(job, attempt), daemon=True,
                         name=f"oyunfor-keepalive-{job.id}").start()

    def _retry_keepalive(self, attempt: int, delay: int, reason: str):
        if attempt >= KEEPALIVE_MAX_TRIES:
            logger.info(f"Oyunfor：{reason}，这次的 Cookie 检查先不做了")
            return
        logger.info(f"Oyunfor：{reason}，{max(1, delay // 60)} 分钟后再检查 Cookie")
        timer = threading.Timer(delay, self.run_keepalive, kwargs={"attempt": attempt + 1})
        timer.daemon = True
        timer.start()

    def _keepalive_worker(self, job: _Job, attempt: int):
        recorder = StepRecorder(None, lambda text: logger.info(f"Oyunfor：{text}"))
        creds: Dict[str, str] = {}
        closer = None
        info: Dict[str, Any] = {"logged_in": None, "balance": None, "message": ""}
        catalog: Dict[int, Dict[str, Any]] = {}
        try:
            creds = self.read_secrets()
            context, closer, shop = self._open_session(recorder, creds)
            # 没登录时有账号密码会自动重新登录（要邮箱验证码时会发消息问你）
            info = check_account(shop, login=self._login_of(creds), interaction=_PluginInteraction(self, job))
            if info.get("logged_in") is not False:
                if self._quick_mode:
                    try:
                        shop.goto(CATEGORY_PATH, "礼品卡分类页")
                        shop.wait_products()
                        catalog = self._save_catalog(shop.read_catalog())
                    except Exception as err:  # noqa
                        recorder.note(f"刷新商品编号没成功（不影响检查）：{type(err).__name__}")
                self._save_state(context, keep=True)
            else:
                # 登录状态已失效：丢掉保存的状态，下次用安全录入里的 Cookie 重新开始
                self._save_state(context, keep=False)
        except Exception as err:  # noqa
            info = {"logged_in": None, "balance": None, "message": f"{type(err).__name__}: {str(err)[:160]}"}
        finally:
            creds = {}
            if closer:
                try:
                    closer()
                except Exception:  # noqa
                    pass
            self._job = None
            self._lock.release()
        logged = info.get("logged_in")
        if logged is not None:
            self.save_data("account", {"logged_in": logged, "balance": info.get("balance"),
                                       "message": info.get("message") or "", "time": self._now_str(), "run": ""})
        if logged is True:
            balance = info.get("balance")
            logger.info("Oyunfor：Cookie 检查通过（已登录" + (f"，余额 {balance:.2f} TL" if isinstance(balance, (int, float))
                                                       else "") + "）" + (f"，商品编号已刷新 {len(catalog)} 个" if catalog else ""))
        elif logged is False:
            self.notify_cookie_invalid(info)
        else:
            reason = info.get("message") or "页面上没认出登录状态"
            self._retry_keepalive(attempt, KEEPALIVE_RETRY_DELAY, f"这次没能确认 Cookie 是否有效（{reason}）")

    def notify_cookie_invalid(self, info: Dict[str, Any]):
        """Cookie 失效：发消息提醒（不受「发送通知」开关影响）"""
        meta = self._secret_meta()
        lines = ["定时检查 Cookie 发现：插件登录 Oyunfor 的状态已经失效（页面显示未登录），现在去买会失败。"]
        if "username" in meta and "password" in meta:
            lines.append(f"用保存的账号密码自动重新登录也没成功：{info.get('message') or '原因未知'}")
        lines.append("👉 请重新复制 Oyunfor 的 Cookie：插件设置 → 安全录入 → Cookie。")
        if not ("username" in meta and "password" in meta):
            lines.append("也可以在安全录入里填上 Oyunfor 账号和密码，以后 Cookie 失效时插件会自动重新登录。")
        logger.warning("Oyunfor：Cookie 已失效，已发消息提醒")
        self.send(None, "【Oyunfor Cookie 已失效】", "\n".join(lines), force=True)

    # ------------------------------------------------------------------ 设置页
    def _security_block(self) -> List[dict]:
        """设置页顶部：加密状态 + 安全录入入口"""
        vault = self._vault or Vault(self.get_data_path(), aad=self.__class__.__name__)
        if not vault.ready:
            suggestion = generate_secret()
            text = (f"{vault.status_text()}。\n请在 MoviePilot 的 docker 环境变量里加一行（下面是随机生成的，可直接用）：\n"
                    f"{ENV_NAME}={suggestion}\n然后重启容器，再回到这里打开「安全录入」。"
                    f"这个口令至少 {MIN_SECRET_LENGTH} 位，只放在环境变量里，不要写进 /config 目录；换口令后需要重新录入。")
            return [{"component": "VRow", "content": [{"component": "VCol", "props": {"cols": 12}, "content": [
                {"component": "VAlert", "props": {"type": "error", "variant": "tonal", "title": "还不能保存账号信息",
                                                  "text": text, "style": "white-space: pre-line"}}]}]}]
        token = self._issue_secure_token()
        url = f"/api/v1/plugin/{self.__class__.__name__}/secure?t={token}"
        hints = self._saved_hints()
        labels = dict((key, label) for key, label, _ in FIELDS)
        saved = "；".join(f"{labels[key]} {hint}" for key, hint in hints.items() if hint) or "还没有保存任何内容"
        return [{"component": "VRow", "content": [
            {"component": "VCol", "props": {"cols": 12, "md": 8}, "content": [
                {"component": "VAlert", "props": {
                    "type": "success", "variant": "tonal", "density": "compact",
                    "text": f"🔐 {vault.status_text()}。已保存：{saved}"}}]},
            {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                {"component": "VBtn", "props": {"href": url, "target": "_blank", "color": "primary", "block": True,
                                                "variant": "tonal", "prepend-icon": "mdi-lock"},
                 "text": "安全录入账号 / Cookie / 银行卡"}]},
        ]}]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        def col(md: int, component: str, **props) -> dict:
            return {"component": "VCol", "props": {"cols": 12, "md": md},
                    "content": [{"component": component, "props": props}]}

        def hint(md: int, component: str, hint_text: str, **props) -> dict:
            props.update({"hint": hint_text, "persistent-hint": True})
            return col(md, component, **props)

        advanced = [
            {"component": "VRow", "content": [
                hint(12, "VTextField", "默认就是 App Store 分类页（按面额在里面挑），一般不用改；留空也一样",
                     model="product_url", label="商品链接", placeholder=DEFAULT_PRODUCT_URL),
            ]},
            {"component": "VRow", "content": [
                hint(4, "VTextField", "应付超过就停下；0 = 不限", model="max_total", label="单笔上限（TL）", type="number"),
                hint(4, "VTextField", "演练不计；0 = 不限", model="daily_limit", label="每日最多下单", type="number"),
                hint(4, "VCronField", "留空不定时", model="cron", label="定时购买"),
            ]},
            {"component": "VRow", "content": [
                hint(4, "VCronField", "默认每天 09:00；Cookie 失效时发消息提醒你，有效就不打扰；留空不检查",
                     model="keepalive_cron", label="检查 Cookie"),
            ]},
            {"component": "VRow", "content": [
                hint(4, "VTextField", "等你在 App 确认 / 回复验证码", model="verify_timeout", label="验证等待（秒）",
                     type="number"),
                hint(4, "VTextField", "币安付款码推送后等多久", model="pay_timeout", label="币安付款等待（分钟）",
                     type="number"),
                hint(4, "VTextField", "打开页面、等 Cloudflare", model="nav_timeout", label="页面超时（秒）", type="number"),
            ]},
            {"component": "VRow", "content": [
                hint(3, "VSwitch", "留空地址用系统代理", model="use_proxy", label="使用代理"),
                col(9, "VTextField", model="proxy_url", label="代理地址", placeholder="http://user:pass@host:port"),
            ]},
            {"component": "VRow", "content": [
                hint(3, "VSwitch", "默认有头，更容易过 Cloudflare", model="headless", label="无头浏览器"),
                hint(3, "VSwitch", "避免演练留下的商品混进订单", model="clear_cart", label="下单前清空购物车"),
                hint(3, "VSwitch", "关闭则只显示首尾 4 位", model="show_code", label="通知显示完整兑换码"),
                hint(3, "VTextField", "加密保存的截图保留几次", model="keep_runs", label="保留运行记录", type="number"),
            ]},
            {"component": "VRow", "content": [
                hint(3, "VSwitch", "点击之间的停顿缩短到约 30%；遇到网站拦截再关掉", model="fast_mode", label="快速模式"),
                hint(9, "VSwitch", "用保存的商品编号直接加入购物车（不打开商品列表）；在机器人里点面额时就提前打开浏览器、"
                                   "清空购物车；检查 Cookie 时顺便刷新商品编号（不检查时凌晨 "
                                   f"{CATALOG_REFRESH_HOUR:02d}:{CATALOG_REFRESH_MINUTE:02d} 刷新）。对不上时自动改回老办法",
                     model="quick_mode", label="快速下单"),
            ]},
            {"component": "VRow", "content": [
                hint(12, "VTextarea", "页面结构与预期不同时使用，键名见插件说明", model="selectors",
                     label="选择器覆盖（JSON）", rows=2),
            ]},
        ]
        form = [{
            "component": "VForm",
            "content": self._security_block() + [
                {"component": "VRow", "content": [
                    col(3, "VSwitch", model="enabled", label="启用插件"),
                    hint(3, "VSwitch", "只走到付款前一步，不扣款", model="dry_run", label="演练模式", color="warning"),
                    hint(3, "VSwitch", "保存后马上执行一次", model="onlyonce", label="立即购买一次"),
                    col(3, "VSwitch", model="notify", label="发送通知"),
                ]},
                {"component": "VRow", "content": [
                    hint(4, "VTextField", "如 100、250、500；没有时用「100 - 2000 TL」自定义金额", model="amount",
                         label="面额（TL）"),
                    col(4, "VTextField", model="quantity", label="数量", type="number"),
                    col(4, "VSelect", model="pay_mode", label="付款方式", items=[
                        {"title": "账户余额", "value": "balance"},
                        {"title": "币安支付（推送付款码给你）", "value": "binance"},
                        {"title": "银行卡（App 确认 / 验证码转发给你）", "value": "card"},
                    ]),
                ]},
                {"component": "VRow", "props": {"show": "{{pay_mode == 'card'}}"}, "content": [
                    {"component": "VCol", "props": {"cols": 12}, "content": [
                        {"component": "VAlert", "props": {
                            "type": "info", "variant": "tonal", "density": "compact",
                            "text": "银行卡信息在上面的「安全录入」里填写（加密保存）。需要验证时会推送消息告诉你是"
                                    "「在 App 里确认」还是「回复验证码」。"}}]},
                ]},
                {"component": "VExpansionPanels", "props": {"variant": "accordion", "class": "mt-2"}, "content": [
                    {"component": "VExpansionPanel", "props": {"title": "高级设置（一般不用改）"}, "content": [
                        {"component": "VExpansionPanelText", "content": advanced}]}]},
            ],
        }]
        model = {
            "enabled": False, "dry_run": True, "onlyonce": False, "notify": True,
            "amount": "250", "quantity": 1, "pay_mode": "balance",
            "product_url": DEFAULT_PRODUCT_URL, "max_total": 2000, "daily_limit": 3, "cron": "",
            "keepalive_cron": DEFAULT_KEEPALIVE_CRON,
            "verify_timeout": 300, "pay_timeout": 15, "nav_timeout": 60,
            "use_proxy": False, "proxy_url": "",
            "headless": False, "clear_cart": True, "fast_mode": True, "quick_mode": True, "show_code": True,
            "keep_runs": 10,
            "selectors": "", "history_days": 180,
        }
        return form, model

    def _image_block(self, path: Optional[Path], title: str, open_panel: bool = False) -> List[dict]:
        data = self._read_run_file(path)
        if not data or len(data) > 2_500_000:
            return []
        mime = "image/png" if ".png" in Path(path).name else "image/jpeg"
        image = {"component": "VImg", "props": {"src": f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}",
                                                "max-height": 900}}
        if open_panel:
            return [{"component": "VCard", "props": {"class": "mb-3", "max-width": 360}, "content": [image]}]
        return [{"component": "VExpansionPanels", "props": {"class": "mb-3"}, "content": [{
            "component": "VExpansionPanel", "props": {"title": title},
            "content": [{"component": "VExpansionPanelText", "content": [image]}]}]}]

    def get_page(self) -> List[dict]:
        blocks: List[dict] = []
        if not self._vault or not self._vault.ready:
            blocks.append({"component": "VAlert", "props": {
                "type": "error", "variant": "tonal", "class": "mb-3",
                "text": f"没有可用的加密口令（{ENV_NAME}），插件不能保存或使用账号信息。请打开插件设置查看说明。"}})
        job = self._job
        if job:
            if job.waiting == "binance":
                info = job.pay_info
                text = (f"金额 {info.get('amount') or '?'} {info.get('currency') or ''}，用币安 App 扫下面的二维码"
                        + (f"，或打开付款链接：{(info.get('links') or [''])[0]}" if info.get("links") else "")
                        + f"。付好后会自动取码；没动静就发 {self._cmd('paid')}。")
                blocks.append({"component": "VAlert", "props": {"type": "warning", "variant": "tonal", "class": "mb-3",
                                                                "title": "等待你用币安付款", "text": text}})
                blocks += self._image_block(job.qr_path, "付款二维码", open_panel=True)
            elif job.waiting:
                text = (f"登录需要验证码（通常在邮箱里），发送 {self._cmd('code')} 验证码。" if job.waiting == "login"
                        else f"付款需要验证码：发送 {self._cmd('code')} 验证码。只在 App 里确认的，确认后页面会自动继续。")
                blocks.append({"component": "VAlert", "props": {"type": "warning", "variant": "tonal", "class": "mb-3",
                                                                "title": "等待你完成验证", "text": text}})
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
        shot = Path(job.screenshot) if job and job.screenshot else (
            Path(self.get_data("last_screenshot")) if self.get_data("last_screenshot") else None)
        if shot:
            blocks += self._image_block(shot, f"最近一次截图：{shot.parent.name}/{shot.name.replace('.enc', '')}")
        history = sorted(self._history(), key=lambda item: item.get("date", ""), reverse=True)
        if not history:
            blocks.append({"component": "VAlert", "props": {
                "type": "info", "variant": "tonal",
                "text": f"暂无记录。先保持「演练模式」，打开「立即购买一次」保存，或在消息渠道发送 {self._cmd('')}（弹出面额按钮）。"}})
            return blocks
        rows = []
        for item in history[:80]:
            codes = item.get("codes") or []
            if item.get("codes_enc") and self._vault and self._vault.ready:
                try:
                    codes = self._vault.decrypt(item["codes_enc"], purpose="codes") or []
                except VaultError:
                    codes = item.get("codes_masked") or ["（无法解密）"]
            elif item.get("codes_masked"):
                codes = item["codes_masked"]
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
        # 正在等待验证的任务不在这里中断：保存配置也会调用到这里；只关掉还没用上的预热浏览器
        self.stop_warmup()
