"""
================================================================================
 web_redeem_server.py —— 网页核销后台（v4 增量功能）
================================================================================

【一句话介绍】
工作人员用浏览器（电脑/手机都行）打开一个网页，输入登录口令 -> 输入同学的 6 位核销码
-> 页面显示这是不是系统发的正品凭证 -> 点一下"确认发放礼品"，codes.csv 里这个码的状态
就从「已使用」变成「已兑换礼品」，并记下时间、操作员和流水号。

【这层只负责"说话"，不负责"动手"】
    接收 HTTP 请求、校验登录态、返回 JSON  ->  本文件
    真正的读写 CSV、真伪校验、状态流转      ->  modules/redemption_manager.py
这样以后要换成小程序/命令行核销，业务逻辑可以原样复用。

【安全设计（与项目原有机制完全对齐）】
1. 登录口令不写在任何配置文件里：校验交给 SecurityVault.verify_passcode(level=1)，
   而口令摘要本身是 modules/core_auth.py 的源码常量（v7 起运行期不可改）。
   网页后台只调用"校验"，没有任何 HTTP 路由能写口令——这是"无后门"的关键。
2. 敏感操作（撤销核销、强行核销）必须再输入二级底层总钥匙，走 level=2 校验。
3. 会话用随机的 HttpOnly Cookie，30 分钟不操作自动过期；每个会话配一个 CSRF 令牌，
   所有写操作都必须带上它，防止别的网页偷偷替你核销。
4. 同一 IP 连续输错口令 5 次，锁定 60 秒，防暴力猜口令。
5. 所有查询/核销/撤销都会写入带零宽水印的审计日志（data/.security_audit.log）。
6. 默认只监听 127.0.0.1（只有本机能访问）。如需手机在同一局域网访问，
   把 config.yaml 里 redeem.host 改成 0.0.0.0（口令固定，改不了，所以更要只在内网用）。

【启动方式】
    双击 `3_点我启动网页核销后台_Windows.bat`
    或者命令行：python web_redeem_server.py
================================================================================
"""

import os
import csv
import io
import time
import threading
import secrets
from datetime import datetime

import yaml
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from modules.security_vault import SecurityVault
from modules.redemption_manager import RedemptionManager
from modules.atomic_csv import csv_transaction, read_rows

# 统一以「本文件所在目录」为工作目录，避免从别的文件夹双击启动时找不到 data/ 目录
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
os.chdir(BASE_DIR)

CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")
UI_PATH = os.path.join(BASE_DIR, "web", "redeem_ui.html")

SESSION_COOKIE = "shiqian_redeem_session"


# ==============================================================================
# 一、读取配置（config.yaml 里只有普通参数，没有任何口令/密钥）
# ==============================================================================
def load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return cfg


CFG = load_config()
REDEEM_CFG = CFG.get("redeem", {}) or {}

# 会话与登录防爆破参数（都在 config.yaml 的 redeem 段里可调）
SESSION_MINUTES = int(REDEEM_CFG.get("session_minutes", 30))
LOGIN_MAX_FAIL = int(REDEEM_CFG.get("login_max_fail", 5))
LOGIN_LOCK_SECONDS = int(REDEEM_CFG.get("login_lock_seconds", 60))


# ==============================================================================
# 二、核心对象
# ==============================================================================
vault = SecurityVault()
redeemer = RedemptionManager(vault, REDEEM_CFG)


# ==============================================================================
# 三、会话管理（登录态 + CSRF 令牌，全部放内存，进程退出即失效）
# ==============================================================================
class SessionStore:
    """
    极简会话表：{token: {"operator": 名字, "csrf": 令牌, "last_active": 时间戳}}
    · token 用 secrets.token_urlsafe(32) 生成，猜不出来
    · 每次请求都会刷新 last_active，超过 SESSION_MINUTES 不操作就自动作废
    """

    def __init__(self, ttl_minutes: int):
        self.ttl = ttl_minutes * 60
        self._data = {}
        self._lock = threading.Lock()

    def create(self, operator: str) -> dict:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        with self._lock:
            self._gc_locked()
            self._data[token] = {
                "operator": operator,
                "csrf": csrf,
                "last_active": time.time(),
                "login_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }
        return {"token": token, "csrf": csrf}

    def get(self, token: str):
        if not token:
            return None
        with self._lock:
            self._gc_locked()
            item = self._data.get(token)
            if item is None:
                return None
            item["last_active"] = time.time()
            return item

    def drop(self, token: str):
        with self._lock:
            self._data.pop(token, None)

    def _gc_locked(self):
        """清理过期会话（顺带防止内存无限增长）"""
        now = time.time()
        expired = [k for k, v in self._data.items() if now - v["last_active"] > self.ttl]
        for k in expired:
            self._data.pop(k, None)


SESSIONS = SessionStore(SESSION_MINUTES)


class LoginGuard:
    """
    登录防爆破：同一个 IP 在窗口期内失败太多次，就暂时锁住它。
    现场活动结束后重启程序即自动清零，不需要落盘。
    """

    def __init__(self, max_fail: int, lock_seconds: int):
        self.max_fail = max_fail
        self.lock_seconds = lock_seconds
        self._fails = {}
        self._lock = threading.Lock()

    def blocked(self, ip: str) -> int:
        """返回还需要等待多少秒（0 表示没被锁）"""
        with self._lock:
            item = self._fails.get(ip) or {}
            until = item.get("blocked_until", 0)
            remain = int(until - time.time())
            return max(remain, 0)

    def fail(self, ip: str):
        with self._lock:
            item = self._fails.setdefault(ip, {"count": 0, "blocked_until": 0})
            item["count"] += 1
            if item["count"] >= self.max_fail:
                item["blocked_until"] = time.time() + self.lock_seconds
                item["count"] = 0

    def success(self, ip: str):
        with self._lock:
            self._fails.pop(ip, None)


GUARD = LoginGuard(LOGIN_MAX_FAIL, LOGIN_LOCK_SECONDS)


# ==============================================================================
# 四、FastAPI 应用
# ==============================================================================
app = FastAPI(title="时迁 · 网页核销后台", docs_url=None, redoc_url=None)


def client_ip(request: Request) -> str:
    return (request.client.host if request.client else "unknown") or "unknown"


def current_session(request: Request):
    return SESSIONS.get(request.cookies.get(SESSION_COOKIE, ""))


async def read_json(request: Request) -> dict:
    """
    安全地读取 JSON 请求体。
    这里不用 Pydantic 模型，是为了让代码更"傻瓜式"：没有字段它就是空字典，
    兼容不同版本的 FastAPI，学生改起来也没有心智负担。
    """
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def json_error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"ok": False, "verdict": message, "level": "error"}, status_code=status)


def csrf_ok(request: Request, sess: dict) -> bool:
    """校验 CSRF 令牌（浏览器自动带 Cookie，我们额外要求一个自定义请求头）"""
    return (request.headers.get("x-csrf-token", "") or "") == sess.get("csrf", "")


# ------------------------------------------------------------------------------
# 4.1 页面
# ------------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    """返回核销后台的单页界面（HTML/CSS/JS 都在 web/redeem_ui.html 里）"""
    with open(UI_PATH, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/favicon.ico")
def favicon():
    return Response(status_code=204)


# ------------------------------------------------------------------------------
# 4.2 登录 / 登出 / 当前状态
# ------------------------------------------------------------------------------
@app.post("/api/login")
async def api_login(request: Request):
    """
    工作人员登录：输入一级维护口令 + 自己的名字。
    口令校验完全交给口令根模块（SecurityVault.verify_passcode -> modules/core_auth）。
    本接口只"验"不"改"：改口令的通道在 v7 已被彻底移除。
    """
    ip = client_ip(request)
    remain = GUARD.blocked(ip)
    if remain > 0:
        return json_error(f"口令尝试次数过多，请 {remain} 秒后再试", status=429)

    body = await read_json(request)
    passcode = str(body.get("passcode", "")).strip()
    operator = str(body.get("operator", "")).strip()[:20] or "工作人员"

    if not passcode:
        return json_error("请输入登录口令")
    if not vault.verify_passcode(passcode, level=1):
        GUARD.fail(ip)
        vault.write_audit(f"网页核销后台：登录口令错误（来源 {ip}）")
        return json_error("登录口令错误（已记录审计日志）", status=401)

    GUARD.success(ip)
    sess = SESSIONS.create(operator)
    vault.write_audit(f"网页核销后台：{operator} 登录成功（来源 {ip}）")

    resp = JSONResponse({"ok": True, "operator": operator, "csrf": sess["csrf"]})
    # HttpOnly：JS 读不到这个 Cookie，可以防 XSS 偷走会话
    resp.set_cookie(
        SESSION_COOKIE, sess["token"],
        httponly=True, samesite="lax", max_age=SESSION_MINUTES * 60, path="/",
    )
    return resp


@app.post("/api/logout")
async def api_logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE, "")
    sess = SESSIONS.get(token)
    if sess:
        vault.write_audit(f"网页核销后台：{sess['operator']} 主动退出")
    SESSIONS.drop(token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@app.get("/api/session")
def api_session(request: Request):
    """页面打开时先问一句"我还登录着吗"，同时拿到 CSRF 令牌和概览数据"""
    sess = current_session(request)
    if not sess:
        return JSONResponse({"logged_in": False, "session_minutes": SESSION_MINUTES})
    return JSONResponse({
        "logged_in": True,
        "operator": sess["operator"],
        "login_at": sess["login_at"],
        "csrf": sess["csrf"],
        "session_minutes": SESSION_MINUTES,
        "stats": redeemer.stats(),
        "recent": redeemer.recent_redemptions(8),
    })


# ------------------------------------------------------------------------------
# 4.3 查询真伪（只读）
# ------------------------------------------------------------------------------
@app.post("/api/verify")
async def api_verify(request: Request):
    sess = current_session(request)
    if not sess:
        return json_error("登录已过期，请重新登录", status=401)
    if not csrf_ok(request, sess):
        return json_error("安全令牌校验失败，请刷新页面重试", status=403)

    body = await read_json(request)
    code = RedemptionManager.normalize_code(body.get("code", ""))
    return JSONResponse(redeemer.verify(code))


# ------------------------------------------------------------------------------
# 4.4 核销（把状态改成「已兑换礼品」）
# ------------------------------------------------------------------------------
@app.post("/api/redeem")
async def api_redeem(request: Request):
    sess = current_session(request)
    if not sess:
        return json_error("登录已过期，请重新登录", status=401)
    if not csrf_ok(request, sess):
        return json_error("安全令牌校验失败，请刷新页面重试", status=403)

    body = await read_json(request)
    code = RedemptionManager.normalize_code(body.get("code", ""))
    admin_code = str(body.get("admin_code", "")).strip()

    result = redeemer.redeem(code, operator=sess["operator"], admin_code=admin_code)
    if result.get("ok"):
        result["stats"] = redeemer.stats()
        result["recent"] = redeemer.recent_redemptions(8)
    return JSONResponse(result)


# ------------------------------------------------------------------------------
# 4.5 撤销核销（二级底层总钥匙鉴权）
# ------------------------------------------------------------------------------
@app.post("/api/revoke")
async def api_revoke(request: Request):
    sess = current_session(request)
    if not sess:
        return json_error("登录已过期，请重新登录", status=401)
    if not csrf_ok(request, sess):
        return json_error("安全令牌校验失败，请刷新页面重试", status=403)

    body = await read_json(request)
    code = RedemptionManager.normalize_code(body.get("code", ""))
    admin_code = str(body.get("admin_code", "")).strip()
    if not admin_code:
        return json_error("撤销属于敏感操作，请输入二级底层总钥匙", status=403)

    result = redeemer.revoke(code, admin_code=admin_code, operator=sess["operator"])
    if result.get("ok"):
        result["stats"] = redeemer.stats()
        result["recent"] = redeemer.recent_redemptions(8)
    return JSONResponse(result)


# ------------------------------------------------------------------------------
# 4.6 导出核销流水（供活动结束后对账）
# ------------------------------------------------------------------------------
@app.get("/api/export")
def api_export(request: Request):
    sess = current_session(request)
    if not sess:
        return json_error("登录已过期，请重新登录", status=401)

    path = redeemer.redeem_log_csv
    with csv_transaction(path):
        fields, rows = read_rows(path)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fields or ["redeem_no"], extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)

    vault.write_audit(f"网页核销后台：{sess['operator']} 导出了核销流水（{len(rows)} 条）")
    filename = f"redeem_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    # 前面加 \ufeff 是为了让 Excel 双击打开不乱码
    return PlainTextResponse(
        "\ufeff" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ------------------------------------------------------------------------------
# 4.7 健康检查（部署到局域网时用来确认服务活着）
# ------------------------------------------------------------------------------
@app.get("/api/health")
def api_health():
    return {"ok": True, "service": "shiqian-redeem", "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


# ==============================================================================
# 五、启动入口
# ==============================================================================
def main():
    host = str(REDEEM_CFG.get("host", "127.0.0.1"))
    port = int(REDEEM_CFG.get("port", 8090))

    print("=" * 62)
    print("   🎫 「时迁」网页核销后台 · 现场礼品兑换服务")
    print("=" * 62)
    print(f"   访问地址： http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}")
    print(f"   监听模式： {'仅本机可访问（推荐）' if host == '127.0.0.1' else '局域网可访问 ⚠️ 请先修改默认口令'}")
    print(f"   登录口令： 一级维护口令（默认与主流程一致，可在主程序 admin 控制台第 4 项修改）")
    print(f"   敏感操作： 撤销核销需二级底层总钥匙")
    print(f"   数据文件： {redeemer.codes_csv}")
    print("   按 Ctrl+C 可安全停止服务")
    print("=" * 62)

    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
