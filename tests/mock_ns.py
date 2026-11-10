"""
本地模拟服务器：NodeSeek 签到接口 + Cloudflare 质询 + FlareSolverr。

仅用于离线测试插件逻辑，行为按公开的社区脚本与接口返回整理：
- POST /api/attendance?random=true|false   需要 x-csrf-challenge: simple-token
- GET  /api/attendance/board?page=1
- Cloudflare 质询：403 + cf-mitigated: challenge + <title>Just a moment...</title>，
  浏览器执行页面 JS 后拿到与 UA 绑定的 cf_clearance，再放行。

控制接口（测试用）：
- GET /__admin/set?cf=off|all|api|interactive|unsolvable&signed=0|1&session=...&already_status=500
- GET /__admin/log     返回收到的业务请求（含请求头）
- GET /__admin/reset
- POST /v1             模拟 FlareSolverr
"""
import hashlib
import hmac
import json
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

SECRET = b"mock-cf-secret"
FS_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"

STATE = {}
LOCK = threading.Lock()


def reset_state():
    with LOCK:
        STATE.clear()
        STATE.update({
            "cf": "off",              # off / all / api / interactive / unsolvable
            "signed": False,          # 今天是否已签到
            "session": "VALID",       # 有效的 session cookie 值
            "already_status": 500,    # 已签到时的 HTTP 状态码
            "invalid_style": "msg",   # msg: USER NOT FOUND / status: {"status":404}
            "gain_fixed": 5,
            "gain_random": 9,
            "current": 100,
            "require_csrf": True,
            "rotate_session": "",     # 非空则在 /board 响应里下发新的 session
            "log": [],
            "challenge_served": 0,
            "clearance_issued": 0,
        })


reset_state()


def clearance_for(ua: str) -> str:
    return hmac.new(SECRET, (ua or "").encode(), hashlib.sha256).hexdigest()[:40]


def parse_cookie(header: str) -> dict:
    out = {}
    for part in (header or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.strip()] = v.strip()
    return out


CHALLENGE_AUTO = """<!DOCTYPE html><html lang="en-US"><head><title>Just a moment...</title>
<meta http-equiv="Content-Type" content="text/html; charset=UTF-8"></head>
<body><div class="main-wrapper"><div id="challenge-running">Verifying you are human. This may take a few seconds.</div>
<div class="ray_id">Ray ID: mock</div></div>
<script>
setTimeout(async function () {
  await fetch('/cdn-cgi/challenge-platform/verify', {method: 'POST', credentials: 'include',
    headers: {'content-type': 'application/json'},
    body: JSON.stringify({wd: navigator.webdriver === true, langs: navigator.languages.length})});
  location.reload();
}, 2500);
</script></body></html>"""

# 交互式：Turnstile 放在 closed shadow root 的 iframe 里，必须点击复选框
CHALLENGE_INTERACTIVE = """<!DOCTYPE html><html lang="en-US"><head><title>Just a moment...</title></head>
<body><div class="main-wrapper"><h1>www.nodeseek.com</h1>
<p id="challenge-running">Verify you are human by completing the action below.</p>
<div id="turnstile-host" style="width:300px;height:65px;margin:40px"></div>
<div class="ray_id">Ray ID: mock</div></div>
<script>
const host = document.getElementById('turnstile-host');
const root = host.attachShadow({mode: 'closed'});
const f = document.createElement('iframe');
f.src = '/challenges.cloudflare.com/turnstile/widget';
f.style = 'width:300px;height:65px;border:0';
root.appendChild(f);
window.addEventListener('message', function (e) { if (e.data === 'cf-ok') location.reload(); });
</script></body></html>"""

TURNSTILE_WIDGET = """<!DOCTYPE html><html><body style="margin:0">
<div style="width:300px;height:65px;border:1px solid #ccc;display:flex;align-items:center">
<input type="checkbox" id="cb" style="width:24px;height:24px;margin-left:16px"> <span>Verify you are human</span></div>
<script>
document.getElementById('cb').addEventListener('click', async function (ev) {
  if (!ev.isTrusted) return;
  await fetch('/cdn-cgi/challenge-platform/verify', {method: 'POST', credentials: 'include',
    headers: {'content-type': 'application/json'}, body: '{}'});
  parent.postMessage('cf-ok', '*');
});
</script></body></html>"""

CHALLENGE_UNSOLVABLE = """<!DOCTYPE html><html lang="en-US"><head><title>Just a moment...</title></head>
<body><div id="challenge-running">Verifying you are human.</div><div class="ray_id">Ray ID: mock</div></body></html>"""

BOARD_HTML = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8"><title>签到 - NodeSeek</title></head>
<body><div id="nsk-body">签到页面</div><div class="cf-turnstile" hidden></div>
<script src="/cdn-cgi/challenge-platform/scripts/jsd/main.js" defer></script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静默
        pass

    # ---- 工具 ----
    def _send(self, status, body, ctype="application/json; charset=utf-8", headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Server", "cloudflare")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _cookies(self):
        return parse_cookie(self.headers.get("Cookie", ""))

    def _ua(self):
        return self.headers.get("User-Agent", "")

    def _has_clearance(self):
        return self._cookies().get("cf_clearance") == clearance_for(self._ua())

    def _record(self, body=b""):
        with LOCK:
            STATE["log"].append({
                "method": self.command,
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "cookie_names": sorted(self._cookies().keys()),
                "body": body.decode("utf-8", "replace")[:300],
            })

    def _challenge(self):
        """按当前模式返回质询页；不需要质询时返回 False"""
        path = urlparse(self.path).path
        cf = STATE["cf"]
        if cf == "off" or self._has_clearance():
            return False
        if cf == "api" and not path.startswith("/api/"):
            return False
        if cf == "page" and path.startswith("/api/"):
            return False
        with LOCK:
            STATE["challenge_served"] += 1
        html = {"interactive": CHALLENGE_INTERACTIVE,
                "unsolvable": CHALLENGE_UNSOLVABLE}.get(cf, CHALLENGE_AUTO)
        self._send(403, html, "text/html; charset=UTF-8", {"cf-mitigated": "challenge"})
        return True

    # ---- 路由 ----
    def do_GET(self):
        url = urlparse(self.path)
        path, qs = url.path, parse_qs(url.query)

        if path == "/__admin/set":
            with LOCK:
                for k, v in qs.items():
                    v = v[0]
                    if k == "signed":
                        STATE[k] = v == "1"
                    elif k in ("already_status", "current", "gain_fixed", "gain_random"):
                        STATE[k] = int(v)
                    elif k == "require_csrf":
                        STATE[k] = v == "1"
                    else:
                        STATE[k] = v
            return self._send(200, {"ok": True})
        if path == "/__admin/reset":
            reset_state()
            return self._send(200, {"ok": True})
        if path == "/__admin/log":
            with LOCK:
                snap = {k: v for k, v in STATE.items()}
            return self._send(200, snap)

        if path == "/challenges.cloudflare.com/turnstile/widget":
            return self._send(200, TURNSTILE_WIDGET, "text/html; charset=UTF-8")
        if path == "/favicon.ico":
            return self._send(404, "", "text/plain")
        if path == "/cdn-cgi/challenge-platform/scripts/jsd/main.js":
            return self._send(200, "/* cloudflare js detections */", "application/javascript")

        self._record()
        if self._challenge():
            return

        if path in ("/", "/board"):
            headers = {}
            if STATE["rotate_session"]:
                headers["Set-Cookie"] = "session=%s; Path=/; HttpOnly" % STATE["rotate_session"]
                with LOCK:
                    STATE["session"] = STATE["rotate_session"]
                    STATE["rotate_session"] = ""
            return self._send(200, BOARD_HTML, "text/html; charset=UTF-8", headers)

        if path == "/api/attendance/board":
            if self._cookies().get("session") != STATE["session"]:
                return self._send(200, {"success": True, "list": [], "total": 3456})
            data = {"success": True, "total": 3456,
                    "list": [{"member_id": 9, "member_name": "top", "gain": 20, "rank": 1}]}
            if STATE["signed"]:
                data["record"] = {
                    "member_id": 1, "gain": STATE.get("last_gain", 5), "random": True,
                    "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                }
                data["order"] = 12
            return self._send(200, data)

        return self._send(404, "<html><head><title>404</title></head><body>Cannot GET %s</body></html>" % path,
                          "text/html; charset=UTF-8")

    def do_POST(self):
        url = urlparse(self.path)
        path, qs = url.path, parse_qs(url.query)
        body = self._read_body()

        # Cloudflare 质询校验：只有“真的在浏览器里跑了 JS”才会走到这里
        if path == "/cdn-cgi/challenge-platform/verify":
            token = clearance_for(self._ua())
            with LOCK:
                STATE["clearance_issued"] += 1
            return self._send(200, {"ok": True}, headers={
                "Set-Cookie": "cf_clearance=%s; Path=/; HttpOnly; Max-Age=1800" % token})

        # FlareSolverr 模拟
        if path == "/v1":
            req = json.loads(body or b"{}")
            with LOCK:
                STATE["log"].append({"method": "FS", "path": req.get("url"), "cmd": req.get("cmd"),
                                     "cookie_names": sorted(c.get("name") for c in req.get("cookies") or []),
                                     "proxy": req.get("proxy")})
            if STATE["cf"] == "unsolvable":
                return self._send(500, {"status": "error", "message": "Error solving the challenge. Timeout"})
            cookies = list(req.get("cookies") or [])
            cookies.append({"name": "cf_clearance", "value": clearance_for(FS_UA), "domain": "127.0.0.1"})
            return self._send(200, {"status": "ok", "message": "Challenge solved!", "solution": {
                "url": req.get("url"), "status": 200, "cookies": cookies,
                "userAgent": FS_UA, "response": BOARD_HTML}})

        self._record(body)
        if self._challenge():
            return

        if path == "/api/attendance":
            if STATE["require_csrf"] and self.headers.get("x-csrf-challenge") != "simple-token":
                return self._send(403, {"success": False, "message": "high risk action"})
            if self._cookies().get("session") != STATE["session"]:
                if STATE["invalid_style"] == "status":
                    return self._send(404, {"status": 404, "message": "Not Found"})
                return self._send(404, {"success": False, "message": "USER NOT FOUND"})
            if STATE["signed"]:
                return self._send(STATE["already_status"],
                                  {"success": False, "message": "今天已完成签到，请勿重复操作"})
            rnd = (qs.get("random") or ["true"])[0] == "true"
            gain = STATE["gain_random"] if rnd else STATE["gain_fixed"]
            with LOCK:
                STATE["signed"] = True
                STATE["last_gain"] = gain
                STATE["current"] += gain
            return self._send(200, {"success": True, "message": "今天的签到收益是%d个鸡腿" % gain,
                                    "gain": gain, "current": STATE["current"]})

        return self._send(404, {"message": "not found"})


def serve(port=18765):
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    return srv


def start_background(port=18765):
    srv = serve(port)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


if __name__ == "__main__":
    p = int(sys.argv[1]) if len(sys.argv) > 1 else 18765
    print("mock nodeseek on 127.0.0.1:%d" % p, flush=True)
    serve(p).serve_forever()
