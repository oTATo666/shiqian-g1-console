import os
import sys
import time
import json
import shutil
import subprocess
import threading
import csv
from collections import deque

import yaml
import requests
from flask import Flask, render_template, jsonify, request, send_file, Response

app = Flask(__name__)

# 访问机器人（192.168.123.x）时绝不能被系统代理接管：
# 本机挂着 Clash 之类的代理时，requests 默认会走 HTTP_PROXY，
# 结果是"机器人在线但主控台显示离线"。这里统一用不读环境变量的会话。
LOCAL = requests.Session()
LOCAL.trust_env = False

CONFIG_FILE = "config.yaml"
BACKUP_FILE = "config.yaml.bak"
CODES_FILE = "codes.csv"
# 后端业务工程目录：直接复用现有工程里的代码文件，不重复搬运
BACKEND_DIR = os.environ.get(
    "SHIQIAN_BACKEND_DIR",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shiqian_v9"),
)

# 启动后多久确认子进程是否还活着（秒）
START_CHECK_DELAY = 1.5
# 日志环形缓冲上限：只保留最近这么多行，避免长时间运行把内存吃满
LOG_MAX = 500
# 启动检查窗口内先攒着输出的上限（每个模块）
EARLY_OUT_MAX = 200

# 全局子进程管理字典
processes = {
    "asr": None,      # main.py
    "redeem": None,   # web_redeem_server.py
    "mock": None,     # mock_robot_server.py
    "sensor": None,   # sensor_bridge.py（必须用 conda g1 的 python 跑）
    "motion": None,   # motion_bridge.py（同样要 conda g1：要 numpy + DDS）
    "vision": None    # vision_bridge.py（conda g1：OpenCV + 人脸/人形检测）
}

# 每个模块的运行状态：stopped / starting / running / failed / missing
module_state = {
    key: {"state": "stopped", "exit_code": None, "msg": "", "started_at": None, "checked": False}
    for key in processes
}

# 启动检查窗口内暂存的输出（1.5 秒后再决定是"补写进日志"还是"当作失败现场整段贴出"）
early_output = {key: deque(maxlen=EARLY_OUT_MAX) for key in processes}

logs_buffer = deque(maxlen=LOG_MAX)
logs_lock = threading.Lock()
state_lock = threading.RLock()

def add_log(message):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] {message}"
    with logs_lock:
        logs_buffer.append(log_entry)


# ======================================================================
# 密钥处理：密钥不落 config.yaml，接口也不回显
# ----------------------------------------------------------------------
#  存哪：① 系统凭据库（keyring → Windows 凭据管理器）
#        ② 没有 keyring 就用 Windows DPAPI 加密到 data/.secrets.dpapi
#        ③ 两样都没有（非 Windows 且无 keyring）就只认环境变量，拒绝落盘
#  读哪：环境变量 → 系统凭据库 → DPAPI 文件 → 老 config.yaml 里的遗留明文
#  给谁看：GET /api/config 只给 "********"，连长度都不透露
#  没改动：提交上来还是掩码，就当"这格没动"，保留原值；只有清空才算删除
# ======================================================================
SECRET_FIELDS = {
    ("g1", "apikey"): ("SHIQIAN_G1_APIKEY", "G1 机器人接口密钥"),
    ("llm", "apikey"): ("SHIQIAN_LLM_APIKEY", "大模型 API Key"),
    ("redeem", "adminpassword"): ("SHIQIAN_REDEEM_ADMINPASSWORD", "核销后台口令"),
}
SECRET_MASK = "********"
KEYRING_SERVICE = "shiqian_console"
DPAPI_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", ".secrets.dpapi")

# 老版本 config.yaml 里遗留的明文，只在内存里当兜底用，不参与任何输出
_legacy_secrets = {}
_keyring_mod = None
_keyring_ready = False


def _keyring():
    """拿系统凭据库；拿不到就返回 None（不抛异常，不影响主流程）。"""
    global _keyring_mod, _keyring_ready
    if not _keyring_ready:
        _keyring_ready = True
        try:
            import keyring
            keyring.get_keyring()          # 后端不可用时这一行会抛异常
            _keyring_mod = keyring
        except Exception:
            _keyring_mod = None
    return _keyring_mod


def _dpapi(encrypt, data):
    """
    Windows DPAPI：加密后的密文只有当前 Windows 用户能解开。
    非 Windows 或调用失败返回 None，由调用方决定怎么降级。
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _Blob(ctypes.Structure):
            _fields_ = [("cbData", wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        buf = ctypes.create_string_buffer(data, len(data))
        blob_in = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
        blob_out = _Blob()
        crypt32 = ctypes.windll.crypt32
        if encrypt:
            ok = crypt32.CryptProtectData(ctypes.byref(blob_in), "shiqian", None,
                                          None, None, 0, ctypes.byref(blob_out))
        else:
            ok = crypt32.CryptUnprotectData(ctypes.byref(blob_in), None, None,
                                            None, None, 0, ctypes.byref(blob_out))
        if not ok:
            return None
        try:
            return ctypes.string_at(blob_out.pbData, blob_out.cbData)
        finally:
            ctypes.windll.kernel32.LocalFree(blob_out.pbData)
    except Exception:
        return None


def _dpapi_read():
    try:
        with open(DPAPI_FILE, "rb") as f:
            raw = f.read()
    except OSError:
        return {}
    plain = _dpapi(False, raw)
    if not plain:
        return {}
    try:
        store = json.loads(plain.decode("utf-8"))
        return store if isinstance(store, dict) else {}
    except Exception:
        return {}


def _dpapi_write(store):
    blob = _dpapi(True, json.dumps(store, ensure_ascii=False).encode("utf-8"))
    if not blob:
        return False
    try:
        os.makedirs(os.path.dirname(DPAPI_FILE), exist_ok=True)
        tmp = DPAPI_FILE + ".tmp"
        with open(tmp, "wb") as f:
            f.write(blob)
        os.replace(tmp, DPAPI_FILE)
        return True
    except OSError:
        return False


def _secret_key(section, field):
    return f"{section}.{field}"


def secret_backend():
    """当前密钥存在哪一层（只报位置，不含任何值）。"""
    if _keyring() is not None:
        return "keyring"
    if os.name == "nt":
        return "dpapi"
    return "env"


def _store_hint():
    if secret_backend() == "env":
        return ("本机既没有 keyring 也没有 Windows 凭据库，为避免明文落盘已拒绝保存；"
                "请改用环境变量后重启主控台")
    return "系统凭据库写入失败，请检查当前账户权限"


def set_secret(section, field, value):
    """写密钥。返回 False 表示没存成——绝不回退去写 config.yaml。"""
    name = _secret_key(section, field)
    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(KEYRING_SERVICE, name, value)
            _legacy_secrets.pop(name, None)
            return True
        except Exception:
            pass
    if os.name == "nt":
        store = _dpapi_read()
        store[name] = value
        if _dpapi_write(store):
            _legacy_secrets.pop(name, None)
            return True
    return False


def delete_secret(section, field):
    name = _secret_key(section, field)
    kr = _keyring()
    if kr is not None:
        try:
            kr.delete_password(KEYRING_SERVICE, name)
        except Exception:
            pass
    if os.name == "nt":
        store = _dpapi_read()
        if store.pop(name, None) is not None:
            _dpapi_write(store)
    _legacy_secrets.pop(name, None)


def get_secret(section, field):
    """取真值：环境变量 → 系统凭据库 → DPAPI 文件 → 老配置遗留。"""
    name = _secret_key(section, field)
    env_name = SECRET_FIELDS[(section, field)][0]
    val = os.environ.get(env_name)
    if val:
        return val
    kr = _keyring()
    if kr is not None:
        try:
            val = kr.get_password(KEYRING_SERVICE, name)
        except Exception:
            val = None
        if val:
            return val
    if os.name == "nt":
        val = _dpapi_read().get(name)
        if val:
            return val
    return _legacy_secrets.get(name, "") or ""


def secret_status():
    """只告诉界面"配没配"，不回显任何字符。"""
    return {
        "backend": secret_backend(),
        "configured": {_secret_key(s, f): bool(get_secret(s, f)) for (s, f) in SECRET_FIELDS},
    }


def redact_config(cfg):
    """给前端的副本：密钥位只放掩码或空串。"""
    out = {}
    for section, value in (cfg or {}).items():
        out[section] = dict(value) if isinstance(value, dict) else value
    for (section, field) in SECRET_FIELDS:
        if not isinstance(out.get(section), dict):
            out[section] = {}
        out[section][field] = SECRET_MASK if get_secret(section, field) else ""
    return out


def _default_config():
    """默认配置里没有密钥字段——密钥压根不属于这个文件。"""
    return {
        "g1": {
            "roboturl": "http://192.168.1.100:8080",
            "wsurl": "ws://192.168.1.100:8081",
            "actionendpoint": "/triggeraction/{actionid}",
            "actionstatusendpoint": "/actionstatus/{requestid}"
        },
        "server": {
            "consoleport": 5000,
            "redeemport": 5001,
            "mock_port": 5002
        },
        "llm": {
            "apiurl": "",
            "model": ""
        },
        "redeem": {
            "logenabled": True
        },
        "behavior": {
            "actioncooldownsec": 2,
            "requestttlsec": 60,
            "callbacktimeoutsec": 5,
            "tts_volume": 80
        },
        # 首次见面：视觉桥（OpenCV 人脸/人形）+ Livox MID360
        "vision": {
            "enabled": True,
            "host": "127.0.0.1",
            "port": 5013,
            "stream": "http://192.168.123.164:8081/stream",
            "present_on": 2.0,
            "present_off": 4.0,
            "greet": True,
            "greet_text": "你好呀，第一次见。我是时迁。",
            "autostart": True
        },
        "lidar": {"ip": "192.168.123.120"},
        # 语音主程序（子进程）接收文字的口子：主控台在这里投喂文本，
        # 替代"对着黑窗口敲键盘"这条在托管模式下根本不存在的路径
        "worker": {
            "host": "127.0.0.1",
            "port": 5010
        },
        # G1 传感器桥：单独一个进程（Python 3.10 + unitree_sdk2py），
        # 主控台只做代理。python 留空则自动找 conda g1 环境。
        "sensor": {
            "enabled": True,
            "host": "127.0.0.1",
            "port": 5011,
            "python": "",
            "ifaceaddr": "",
            "ifacename": "",
            "motorcount": 29,
            "videourl": "",
            "robotip": "192.168.123.161"
        }
    }


def _write_config_file(data):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True)


def _absorb_and_strip(config):
    """把配置里残留的明文密钥挪进凭据库，并保证返回的字典里没有密钥。"""
    if not isinstance(config, dict):
        return {}
    moved = []
    stripped = False
    for (section, field) in SECRET_FIELDS:
        sect = config.get(section)
        if not isinstance(sect, dict):
            continue
        if field not in sect:
            continue
        raw = sect.pop(field)                # 先摘下来，后面绝不写回文件
        stripped = True
        if not raw:
            continue
        raw = str(raw).strip()
        if not raw or raw == SECRET_MASK:
            continue
        name = _secret_key(section, field)
        _legacy_secrets[name] = raw          # 先兜底，保证本次运行取得到
        if set_secret(section, field, raw):
            moved.append(name)
        else:
            add_log(f"[配置] {SECRET_FIELDS[(section, field)][1]} 暂时仍留在 config.yaml 里：{_store_hint()}")
    if stripped:
        # 只要文件里出现过密钥字段（哪怕是空的），就回写一次把它彻底删掉；
        # 回写后文件里不再有这些键，之后每次都走不到这里。
        try:
            _write_config_file(config)
            if moved:
                add_log(f"[配置] 已把 config.yaml 里 {len(moved)} 项明文密钥移入{secret_backend()}，文件中不再保留")
            else:
                add_log("[配置] 已从 config.yaml 中移除空的密钥字段")
        except OSError:
            add_log("[配置] 密钥字段已从内存配置中摘除，但回写 config.yaml 失败")
    return config


def load_config():
    """读配置：返回的字典里永远没有密钥，密钥要用 get_secret() 单独取。"""
    if not os.path.exists(CONFIG_FILE):
        _write_config_file(_default_config())
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
    except Exception as e:
        add_log(f"[配置] 读取失败（{type(e).__name__}），本轮改用默认配置")
        raw = _default_config()
    return _absorb_and_strip(raw)


def save_config(data):
    """
    存配置。密钥单独进凭据库，config.yaml 只写普通参数。
    返回未能保存的项（空列表表示全部成功）。
    """
    cfg = dict(data or {})
    problems = []
    for (section, field), (env_name, label) in SECRET_FIELDS.items():
        sect = cfg.get(section)
        if not isinstance(sect, dict):
            continue
        incoming = sect.pop(field, None)     # 无论什么情况都不让它落盘
        if incoming is None:
            continue                         # 这次没提交这一格，保留原值
        incoming = str(incoming).strip()
        if incoming == SECRET_MASK:
            continue                         # 界面显示掩码 = 用户没动这一格
        if incoming == "":
            delete_secret(section, field)    # 清空才算删除
            add_log(f"[配置] 已清除{label}")
            continue
        if set_secret(section, field, incoming):
            add_log(f"[配置] 已更新{label}（存入{secret_backend()}，不写配置文件）")
        else:
            problems.append(f"{label}未保存：{_store_hint()}")
    if os.path.exists(CONFIG_FILE):
        shutil.copy(CONFIG_FILE, BACKUP_FILE)

    # 页面只提交固定的几个段，其余段（skills / avatar 等）要原样保留，
    # 否则点一次保存就把没在表单里的配置整段冲掉。
    old = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                old = yaml.safe_load(f) or {}
        except Exception:
            old = {}
    if isinstance(old, dict):
        for section, value in old.items():
            # 老文件里若还有遗留明文密钥，绝不借这次合并写回去
            if isinstance(value, dict):
                for (sec, fld) in SECRET_FIELDS:
                    if sec == section:
                        value.pop(fld, None)
            if section not in cfg:
                cfg[section] = value
            elif isinstance(value, dict) and isinstance(cfg.get(section), dict):
                for k, v in value.items():
                    cfg[section].setdefault(k, v)   # 只补缺，不覆盖用户这次改的值

    _write_config_file(cfg)
    _sensor_py_cache["checked"] = False
    _sensor_py_cache["ts"] = 0.0
    add_log("系统配置已更新并自动备份")
    return problems

@app.route("/")
def index():
    return render_template("console.html")


# ======================================================================
# 虚拟形象与开机画面配置 / 资源上传 API
# ======================================================================
AVATAR_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "avatars")
_AVATAR_SLOTS = {
    "boot": ("boot.mp4", (".mp4", ".webm")),
    "hero": ("hero.png", (".png", ".jpg", ".jpeg", ".webp")),
    "icon": ("icon.png", (".png", ".jpg", ".jpeg", ".webp")),
    "greet": ("greet.jpg", (".png", ".jpg", ".jpeg", ".webp")),
}


def _avatar_cfg():
    cfg = load_config()
    av = dict(cfg.get("avatar") or {})
    av.setdefault("enabled", True)
    av.setdefault("active_pack", "shiqian_sakura")
    av.setdefault("boot_enabled", True)
    av.setdefault("boot_duration_ms", 4200)
    av.setdefault("allow_skip", True)
    av.setdefault("name", "时迁")
    av.setdefault("theme", "sakura_sage")
    return av


def _avatar_pack_urls(pack_name):
    safe = "".join(c for c in str(pack_name or "shiqian_sakura") if c.isalnum() or c in ("_", "-")) or "shiqian_sakura"
    pdir = os.path.join(AVATAR_ROOT, safe)
    urls = {"pack": safe, "boot_url": "", "hero_url": "/static/shiqian.png",
            "icon_url": "/static/shiqian.png", "greet_url": ""}
    if os.path.isdir(pdir):
        for slot, (fname, _) in _AVATAR_SLOTS.items():
            fpath = os.path.join(pdir, fname)
            if os.path.exists(fpath):
                ts = int(os.path.getmtime(fpath))
                urls[f"{slot}_url"] = f"/static/avatars/{safe}/{fname}?v={ts}"
    return urls


@app.route("/api/avatar/info", methods=["GET"])
def api_avatar_info():
    av = _avatar_cfg()
    packs = []
    if os.path.isdir(AVATAR_ROOT):
        for name in sorted(os.listdir(AVATAR_ROOT)):
            if os.path.isdir(os.path.join(AVATAR_ROOT, name)):
                packs.append(name)
    urls = _avatar_pack_urls(av.get("active_pack"))
    return jsonify({
        "ok": True,
        "data": {
            "config": av,
            "packs": packs,
            "assets": urls,
        },
        "msg": "ok",
    })


@app.route("/api/avatar/upload", methods=["POST"])
def api_avatar_upload():
    slot = (request.args.get("slot") or "").strip().lower()
    if slot not in _AVATAR_SLOTS:
        return jsonify({"ok": False, "msg": "未知素材槽位（仅支持 boot / hero / icon / greet）"}), 400
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"ok": False, "msg": "未收到上传文件"}), 400
    target_name, allowed_exts = _AVATAR_SLOTS[slot]
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in allowed_exts:
        return jsonify({"ok": False, "msg": f"格式不支持，请上传 {', '.join(allowed_exts)}"}), 400
    raw = f.read()
    if not raw:
        return jsonify({"ok": False, "msg": "文件内容为空"}), 400
    if len(raw) > 50 * 1024 * 1024:
        return jsonify({"ok": False, "msg": "文件超过 50MB 上限"}), 400
    av = _avatar_cfg()
    pack = (request.args.get("pack") or av.get("active_pack") or "shiqian_sakura").strip()
    safe_pack = "".join(c for c in pack if c.isalnum() or c in ("_", "-")) or "shiqian_sakura"
    pdir = os.path.join(AVATAR_ROOT, safe_pack)
    os.makedirs(pdir, exist_ok=True)
    dst = os.path.join(pdir, target_name)
    with open(dst, "wb") as out:
        out.write(raw)
    add_log(f"[avatar] 已更新形象包 [{safe_pack}] 的 {slot} 素材 ({round(len(raw)/1024, 1)} KB)")
    return jsonify({"ok": True, "data": _avatar_pack_urls(safe_pack), "msg": f"已更新 {slot} 素材"})


@app.route("/api/health", methods=["GET"])
def api_health():
    py_version = sys.version.split()[0]
    req_exists = os.path.exists("requirements.txt")
    codes_exists = os.path.exists(CODES_FILE)
    config_exists = os.path.exists(CONFIG_FILE)
    
    deps_ok = True
    try:
        import yaml, requests
    except ImportError:
        deps_ok = False

    return jsonify({
        "ok": True,
        "data": {
            "python_version": py_version,
            "dependencies_installed": deps_ok,
            "config_exists": config_exists,
            "codes_exists": codes_exists,
            "requirements_exists": req_exists
        },
        "msg": "体检正常"
    })

@app.route("/api/install", methods=["POST"])
def api_install():
    try:
        add_log("开始自动安装/更新依赖包...")
        result = subprocess.run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt"], 
                                capture_output=True, text=True, timeout=120)
        if result.returncode == 0:
            add_log("依赖安装成功")
            return jsonify({"ok": True, "data": result.stdout, "msg": "依赖安装成功"})
        else:
            add_log(f"依赖安装失败: {result.stderr}")
            return jsonify({"ok": False, "data": result.stderr, "msg": "依赖安装失败"})
    except Exception as e:
        return jsonify({"ok": False, "data": str(e), "msg": f"安装异常: {str(e)}"})

@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "GET":
        # 只给掩码，绝不原样返回明文密钥
        return jsonify({"ok": True, "data": redact_config(load_config()),
                        "secrets": secret_status(), "msg": "读取配置成功（密钥不回显）"})
    else:
        try:
            new_data = request.json
            if not new_data:
                return jsonify({"ok": False, "msg": "无效的配置数据"})
            problems = save_config(new_data)
            if problems:
                return jsonify({"ok": False, "data": redact_config(load_config()),
                                "secrets": secret_status(), "msg": "；".join(problems)})
            return jsonify({"ok": True, "data": redact_config(load_config()),
                            "secrets": secret_status(), "msg": "配置保存成功"})
        except Exception as e:
            return jsonify({"ok": False, "msg": f"保存失败: {str(e)}"})

@app.route("/api/rollback_config", methods=["POST"])
def api_rollback():
    if os.path.exists(BACKUP_FILE):
        shutil.copy(BACKUP_FILE, CONFIG_FILE)
        add_log("已成功回滚至上一版配置")
        return jsonify({"ok": True, "data": load_config(), "msg": "配置回滚成功"})
    return jsonify({"ok": False, "msg": "没有找到历史备份文件"})

@app.route("/api/status", methods=["GET"])
def api_status():
    status_dict = {}
    for key, proc in processes.items():
        alive = proc is not None and proc.poll() is None
        pid = proc.pid if alive else None
        with state_lock:
            st = dict(module_state[key])
        # 进程已经悄悄死了但状态还没被检查线程更新时，这里兜一下
        if proc is not None and not alive and st["state"] == "running":
            st["state"] = "failed"
            st["exit_code"] = proc.returncode
        status_dict[key] = {
            "alive": alive,
            "pid": pid,
            "state": st["state"],
            "exit_code": st["exit_code"],
            "msg": st["msg"],
            "started_at": st["started_at"],
        }

    cfg = load_config()
    g1_url = cfg.get("g1", {}).get("roboturl", "http://127.0.0.1:8080")
    g1_connected = False
    try:
        res = LOCAL.get(g1_url, timeout=1.2)
        g1_connected = res.status_code < 500
    except Exception:
        g1_connected = False

    # 传感器桥的实时状态：只有 /health 有响应且 DDS 就绪且收到 2 秒内 lowstate 才算 ready
    sensor = {"bridge": False, "health": False, "dds": False, "ready": False,
              "not_ready_reason": "", "hz": 0.0, "lowstate_age_ms": None,
              "mic": False, "video": False, "error": "",
              "python": _sensor_py_cache["path"] or "", "video_note": ""}
    try:
        rh = LOCAL.get(_sensor_base() + "/health", timeout=0.6)
        sensor["health"] = rh.status_code == 200
    except Exception:
        sensor["health"] = False

    try:
        r = LOCAL.get(_sensor_base() + "/state", timeout=0.9)
        j = r.json()
        sensor["bridge"] = True
        sensor["dds"] = bool(j.get("dds", {}).get("ready"))
        link = j.get("link") or {}
        sensor["hz"] = link.get("lowstate_hz") or 0
        age_ms = link.get("lowstate_age_ms")
        sensor["lowstate_age_ms"] = age_ms
        sensor["mic"] = bool((j.get("mic") or {}).get("active"))
        sensor["video"] = bool((j.get("video") or {}).get("has_frame"))
        sensor["video_note"] = (j.get("video") or {}).get("note") or ""
        sensor["error"] = (j.get("dds") or {}).get("error") or ""
        lowstate_ok = (age_ms is not None and age_ms < 2000)
        sensor["ready"] = bool(sensor["health"] and sensor["dds"] and lowstate_ok)
        if not sensor["ready"]:
            if not sensor["health"]:
                sensor["not_ready_reason"] = "健康检查未响应"
            elif not sensor["dds"]:
                sensor["not_ready_reason"] = f"DDS 未绑定（{sensor['error']}）" if sensor["error"] else "DDS 未绑定"
            else:
                sensor["not_ready_reason"] = "无 lowstate 数据"
        if sensor["ready"]:
            g1_connected = True          # DDS 就绪且持续收到 lowstate，比 HTTP 探测更可信
    except Exception:
        sensor["error"] = "传感器桥未响应"
        sensor["not_ready_reason"] = "传感器桥未响应"

    # 桥是外部起的时候没有子进程句柄，但接口通了 —— 按真实就绪状态反映
    if sensor["bridge"] and not status_dict["sensor"]["alive"]:
        status_dict["sensor"].update({
            "alive": True, "pid": None, "state": "running",
            "msg": "外部进程（非主控台托管）",
        })
    status_dict["sensor"]["ready"] = sensor["ready"]
    status_dict["sensor"]["not_ready_reason"] = sensor["not_ready_reason"]
    if status_dict["sensor"]["alive"] and not sensor["ready"]:
        status_dict["sensor"]["state"] = "degraded"
        status_dict["sensor"]["msg"] = f"桥已启动，机器人未就绪：{sensor['not_ready_reason']}"

    # 动作模组桥的实时状态：只有 /health 有响应且 /state.ready 为 True 才算就绪
    motion = {"bridge": False, "health": False, "dds": False, "reachable": False,
              "lowstate_age_s": None, "mode": None, "ready": False,
              "not_ready_reason": "", "error": ""}
    try:
        mh = LOCAL.get(_motion_base() + "/health", timeout=0.6)
        motion["health"] = mh.status_code == 200
    except Exception:
        motion["health"] = False

    try:
        # 动作桥 /state 会顺带 ping 一次机器人并查 MotionSwitcher，0.9 秒
        # 在机器人刚上电时不够，页面会误报“动作桥未响应”。
        mr = LOCAL.get(_motion_base() + "/state", timeout=3.0)
        mj = mr.json()
        motion["bridge"] = True
        dds_info = mj.get("dds") or {}
        motion["dds"] = bool(dds_info.get("ok"))
        motion["reachable"] = bool(mj.get("reachable"))
        motion["lowstate_age_s"] = mj.get("lowstate_age_s")
        motion["mode"] = mj.get("mode")
        motion["ready"] = bool(motion["health"] and mj.get("ready"))
        if not motion["ready"]:
            if not motion["health"]:
                motion["not_ready_reason"] = "健康检查未响应"
            elif not motion["dds"]:
                dds_msg = dds_info.get("msg") or ""
                motion["not_ready_reason"] = f"DDS 未绑定（{dds_msg}）" if dds_msg else "DDS 未绑定"
            elif not motion["reachable"]:
                motion["not_ready_reason"] = "ping 机器人失败"
            else:
                motion["not_ready_reason"] = "无 lowstate 数据"
    except Exception:
        motion["error"] = "动作桥未响应"
        motion["not_ready_reason"] = "动作桥未响应"

    if motion["bridge"] and not status_dict["motion"]["alive"]:
        status_dict["motion"].update({
            "alive": True, "pid": None, "state": "running",
            "msg": "外部进程（非主控台托管）",
        })
    status_dict["motion"]["ready"] = motion["ready"]
    status_dict["motion"]["not_ready_reason"] = motion["not_ready_reason"]
    if status_dict["motion"]["alive"] and not motion["ready"]:
        status_dict["motion"]["state"] = "degraded"
        status_dict["motion"]["msg"] = f"桥已启动，机器人未就绪：{motion['not_ready_reason']}"

    return jsonify({
        "ok": True,
        "data": {
            "modules": status_dict,
            "g1_connected": g1_connected,
            "sensor": sensor,
            "motion": motion,
        },
        "msg": "获取状态成功"
    })

def _pump_output(key, proc):
    """
    后台线程：持续读子进程 stdout，实时写进环形日志。

    为什么必须要它：Popen 的 stdout 是管道，管道缓冲区满了子进程会阻塞在 write 上，
    再也跑不动 —— 表现出来就是"启动成功但一直没反应"。所以必须有人一直读。
    """
    try:
        for raw in iter(proc.stdout.readline, ""):
            line = raw.rstrip("\r\n")
            with state_lock:
                if not module_state[key]["checked"]:
                    early_output[key].append(line)
                    continue
            add_log(f"[{key}] {line}")
    except Exception as e:                      # 进程被杀时 readline 可能抛异常
        add_log(f"[{key}] 读取输出中断: {type(e).__name__}")
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass
        # 管道读到 EOF = 进程结束了。启动窗口内退出由 _verify_started 汇报；
        # 这里只负责"跑着跑着自己死了"的情况，否则页面上会一直显示在运行。
        code = proc.poll()
        if code is not None:
            with state_lock:
                st = module_state[key]
                unexpected = st["state"] in ("starting", "running")
                if unexpected:
                    st["state"] = "failed"
                    st["exit_code"] = code
            if unexpected:
                add_log(f"⚠️ 模块 [{key}] 进程已退出（退出码 {code}），"
                        f"模块状态已标记为「启动失败」")


def _verify_started(key, script_name, proc):
    """
    启动后 START_CHECK_DELAY 秒确认一次：还活着就算成功；已经退出就是启动失败，
    把它在窗口内吐出的输出整段贴进日志，而不是只留一个 PID 让人猜。
    """
    time.sleep(START_CHECK_DELAY)
    alive = proc.poll() is None
    with state_lock:
        st = module_state[key]
        st["checked"] = True
        buffered = list(early_output[key])
        early_output[key].clear()
        if alive:
            st["state"] = "running"
        else:
            st["state"] = "failed"
            st["exit_code"] = proc.returncode

    if alive:
        for line in buffered:
            add_log(f"[{key}] {line}")
        add_log(f"模块 [{key}] 运行正常（PID {proc.pid}）")
        return

    code = proc.returncode
    add_log(f"❌ 模块 [{key}] 启动失败：进程在 {START_CHECK_DELAY:.1f} 秒内退出（退出码 {code}）")
    if buffered:
        add_log(f"—— [{key}] 退出前的完整输出（{len(buffered)} 行）——")
        for line in buffered:
            add_log(f"[{key}] {line}")
        add_log(f"—— [{key}] 输出结束 ——")
    else:
        add_log(f"[{key}] 没有任何输出就退出了，常见原因：缺依赖、端口被占用、"
                f"脚本里 if __name__ == '__main__' 逻辑要求交互输入")
    add_log(f"排查建议：cd {BACKEND_DIR} && {sys.executable} {script_name}，手动跑一次看报错")


def start_process(key, script_name, extra_args=None, python=None, extra_env=None):
    """启动一个后端模块并立刻接管它的输出。返回 (ok, msg)。"""
    with state_lock:
        proc = processes.get(key)
        if proc is not None and proc.poll() is None:
            return True, "已在运行"

        script = os.path.join(BACKEND_DIR, script_name)
        if not os.path.exists(script):
            module_state[key].update({"state": "missing", "msg": f"未找到 {script_name}"})
            add_log(f"模块 [{key}] 跳过：未找到 {script_name}（用 SHIQIAN_BACKEND_DIR 指定后端工程）")
            return False, f"未找到 {script_name}"

        cfg = load_config()
        worker = cfg.get("worker", {}) or {}
        env = os.environ.copy()
        env.setdefault("PYTHONIOENCODING", "utf-8")
        env["PYTHONUNBUFFERED"] = "1"            # 不缓冲，日志才算"实时"
        env["SHIQIAN_WORKER_HOST"] = str(worker.get("host", "127.0.0.1"))
        env["SHIQIAN_WORKER_PORT"] = str(worker.get("port", 5010))
        # 明确告诉子进程"你被托管了"。只靠 isatty() 在 Windows 上不可靠：
        # 用 git-bash 或某些启动器时，即使 stdin 是空设备也照样返回 True。
        env["SHIQIAN_MANAGED"] = "1"
        env.setdefault("SHIQIAN_BACKEND_DIR", BACKEND_DIR)
        if extra_env:
            env.update({k: str(v) for k, v in extra_env.items() if v is not None})

        exe = python or sys.executable
        # 便携版 runtime 是 embeddable 包，._pth 会开启隔离模式：
        # 脚本目录不进 sys.path、PYTHONPATH 也被忽略 —— 直接 `python main.py`
        # 必然报 ModuleNotFoundError: modules。所以统一经 _backend_runner 起，
        # 由它把后端目录插进 sys.path[0] 再 runpy 执行。
        runner = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_backend_runner.py")
        if os.path.exists(runner):
            cmd = [exe, "-u", runner, script] + list(extra_args or [])
        else:
            cmd = [exe, "-u", script] + list(extra_args or [])
        try:
            p = subprocess.Popen(
                cmd, cwd=BACKEND_DIR,
                stdin=subprocess.DEVNULL,        # 关键：子进程拿到的是"非 TTY"，才会走托管模式
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1, env=env,
            )
        except Exception as e:
            module_state[key].update({"state": "failed", "msg": str(e)})
            add_log(f"❌ 模块 [{key}] 启动失败: {e}")
            return False, str(e)

        processes[key] = p
        early_output[key].clear()
        module_state[key].update({
            "state": "starting", "exit_code": None, "msg": "",
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"), "checked": False,
        })

    add_log(f"模块 [{key}] ({script_name}) 已拉起，PID: {p.pid}，{START_CHECK_DELAY:.1f} 秒后确认存活")

    threading.Thread(target=_pump_output, args=(key, p), daemon=True, name=f"pump-{key}").start()
    threading.Thread(target=_verify_started, args=(key, script_name, p), daemon=True, name=f"verify-{key}").start()
    return True, "已启动"


# ======================================================================
# G1 传感器桥（sensor_bridge.py）
# ----------------------------------------------------------------------
#  这里跑不了 DDS：便携版 runtime 是 Python 3.14，没有 cyclonedds / numpy。
#  所以传感器桥必须用 conda g1（Python 3.10）那个解释器单独跑，
#  主控台只负责拉起它、代理它的 HTTP 和 MJPEG。
# ======================================================================
SENSOR_SCRIPT = "sensor_bridge.py"
_sensor_py_cache = {
    "path": None, "checked": False, "ok": False, "msg": "未检测",
    "ts": 0.0, "cfg_py": None,
}
_SENSOR_PY_FAIL_TTL = 10.0


def _sensor_cfg():
    cfg = load_config()
    return cfg.get("sensor", {}) or {}


def _sensor_base():
    s = _sensor_cfg()
    host = str(s.get("host") or "127.0.0.1")
    try:
        port = int(s.get("port") or 5011)
    except (TypeError, ValueError):
        port = 5011
    return f"http://{host}:{port}"


def _join_win_or_os(base, *parts):
    if "\\" in base and os.sep != "\\":
        return base.rstrip("\\/") + "\\" + "\\".join(parts)
    return os.path.join(base, *parts)


def _parent_dir(path, levels=1):
    p = path.rstrip("\\/")
    sep = "\\" if ("\\" in p and os.sep != "\\") else os.sep
    for _ in range(levels):
        idx = p.rfind(sep)
        if idx <= 0:
            return ""
        p = p[:idx]
    return p


def _candidate_pythons():
    """按优先级列出可能的解释器：配置 > 环境变量 > 激活环境 > conda 各环境。"""
    seen, out = set(), []

    def _norm(p):
        return os.path.normcase(p.replace("/", "\\")) if "\\" in p else os.path.normcase(os.path.normpath(p))

    def add(p, require_exists=False):
        p = (p or "").strip()
        if not p:
            return
        if require_exists and not os.path.exists(p):
            return
        key = _norm(p)
        if key not in seen:
            seen.add(key)
            out.append(p)

    # 1. config & explicit env var (keep order)
    add(_sensor_cfg().get("python") or "", require_exists=False)
    add(os.environ.get("SHIQIAN_SENSOR_PYTHON", ""), require_exists=False)

    # 1b. 项目自带的桥接环境：优先于系统 Python / conda。
    #     便携包可以随包带一个 bridge_env（Python 3.10 venv），
    #     这样换电脑时不用再让用户手填 conda 路径。
    pkg_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for rel in ("bridge_env", "g1_bridge_env"):
        base = os.path.join(pkg_root, rel)
        add(os.path.join(base, "Scripts", "python.exe"), require_exists=True)
        add(os.path.join(base, "python.exe"), require_exists=True)
    # 固定位置的桥接环境（安装脚本默认建在这里，不跟 OneDrive 跑）
    add(os.path.expanduser(r"~\G1_Bridge_Env\Scripts\python.exe"), require_exists=True)
    local_app_dir = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_dir:
        add(_join_win_or_os(local_app_dir, "Shiqian", "bridge_env", "Scripts", "python.exe"),
            require_exists=True)

    # 2. Active conda env (%CONDA_PREFIX%\python.exe)
    conda_prefix = os.environ.get("CONDA_PREFIX", "").strip()
    if conda_prefix:
        add(_join_win_or_os(conda_prefix, "python.exe"), require_exists=True)

    # 3. CONDA_EXE root (two levels up from Scripts\conda.exe)
    conda_exe = os.environ.get("CONDA_EXE", "").strip()
    roots = []
    seen_roots = set()

    def add_root(r):
        r = (r or "").strip()
        if not r:
            return
        k = _norm(r)
        if k not in seen_roots:
            seen_roots.add(k)
            roots.append(r)

    if conda_exe:
        exe_root = _parent_dir(conda_exe, 2)
        if exe_root:
            add_root(exe_root)

    # 4. Output of `conda env list --json` via CONDA_EXE or conda on PATH
    conda_bin = conda_exe or shutil.which("conda")
    if conda_bin:
        try:
            r = subprocess.run(
                [conda_bin, "env", "list", "--json"],
                capture_output=True, text=True, timeout=8,
            )
            if r.returncode == 0 and r.stdout:
                data = json.loads(r.stdout)
                envs_list = data.get("envs") or []
                envs_sorted = sorted(
                    envs_list,
                    key=lambda e: (os.path.basename(str(e).rstrip("\\/")) != "g1", str(e)),
                )
                for env_dir in envs_sorted:
                    add(_join_win_or_os(str(env_dir), "python.exe"), require_exists=True)
        except Exception:
            pass

    # 5. Standard & multi-drive conda roots
    for r in (
        os.path.expanduser(r"~\miniconda3"),
        os.path.expanduser(r"~\anaconda3"),
        os.path.expanduser(r"~\miniforge3"),
        os.path.expanduser(r"~\mambaforge"),
        os.path.expanduser(r"~\micromamba"),
    ):
        add_root(r)

    local_app = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app:
        add_root(_join_win_or_os(local_app, "miniconda3"))
        # 官方 Python 安装器默认装在 %LOCALAPPDATA%\Programs\Python\Python3xx。
        # 以前这里只找 conda，导致“明明装了 Python 3.10 也报无候选路径”。
        py_root = _join_win_or_os(local_app, "Programs", "Python")
        if os.path.isdir(py_root):
            try:
                py_names = sorted(os.listdir(py_root), key=lambda n: (n.lower() != "python310", n))
            except OSError:
                py_names = []
            for name in py_names:
                if name.lower().startswith("python3"):
                    add(_join_win_or_os(py_root, name, "python.exe"), require_exists=True)
        add(_join_win_or_os(local_app, "Programs", "Python", "Python310", "python.exe"),
            require_exists=True)

    add_root(r"C:\ProgramData\miniconda3")
    add_root(r"C:\ProgramData\Anaconda3")
    add(r"C:\Python310\python.exe", require_exists=True)
    add(r"C:\Program Files\Python310\python.exe", require_exists=True)

    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        drive = f"{letter}:\\"
        if os.path.exists(drive):
            for folder in ("miniconda3", "anaconda3", "miniforge3", "Anaconda3"):
                add_root(f"{drive}{folder}")

    for root in roots:
        envs_dir = _join_win_or_os(root, "envs")
        if os.path.exists(envs_dir):
            try:
                names = sorted(os.listdir(envs_dir), key=lambda n: (n != "g1", n))
            except OSError:
                names = []
            for name in names:
                add(_join_win_or_os(envs_dir, name, "python.exe"), require_exists=True)
        add(_join_win_or_os(root, "python.exe"), require_exists=True)

    return out


def detect_sensor_python(force=False):
    """
    找一个能 `import unitree_sdk2py, cyclonedds, numpy` 的解释器。
    成功结果持久化缓存；失败结果最多缓存 10 秒；force=True 或配置变更时强制重测。
    """
    cfg_py = (_sensor_cfg().get("python") or "").strip()
    now = time.time()
    if not force and _sensor_py_cache["checked"] and _sensor_py_cache.get("cfg_py") == cfg_py:
        if _sensor_py_cache["ok"]:
            return _sensor_py_cache
        if (now - _sensor_py_cache.get("ts", 0.0)) < _SENSOR_PY_FAIL_TTL:
            return _sensor_py_cache

    candidates = _candidate_pythons()
    tried = []
    first_err = None
    for path in candidates:
        tried.append(path)
        if not os.path.exists(path):
            continue
        try:
            r = subprocess.run(
                [path, "-c", "import unitree_sdk2py, cyclonedds, numpy; print('ok')"],
                capture_output=True, text=True, timeout=25,
            )
            if r.returncode == 0 and "ok" in (r.stdout or ""):
                _sensor_py_cache.update({
                    "path": path, "checked": True, "ok": True,
                    "msg": "unitree_sdk2py / cyclonedds / numpy 可用",
                    "ts": time.time(), "cfg_py": cfg_py,
                })
                return _sensor_py_cache
            err_lines = (r.stderr or r.stdout or "").strip().splitlines()
            err_line = err_lines[-1] if err_lines else f"退出码 {r.returncode}"
            if first_err is None:
                first_err = f"{path}: {err_line}"
        except Exception as e:
            if first_err is None:
                first_err = f"{path}: {type(e).__name__}: {e}"

    tried_str = ", ".join(tried) if tried else "（无候选路径）"
    err_part = f"；首个存在路径报错：{first_err}" if first_err else "；未找到任何存在的 python.exe"
    msg = f"未找到可用 Python（需 unitree_sdk2py, cyclonedds, numpy）。已尝试：{tried_str}{err_part}"
    _sensor_py_cache.update({
        "path": None, "checked": True, "ok": False,
        "msg": msg, "ts": time.time(), "cfg_py": cfg_py,
    })
    return _sensor_py_cache


def start_sensor(force_detect=False):
    """按配置拉起传感器桥；解释器不对就不硬起，直接把原因写进日志。"""
    cfg = _sensor_cfg()
    if not cfg.get("enabled", True):
        return False, "传感器模块已在配置里关闭"

    # 端口上已经有一个桥在跑（比如刚手工起过）——直接接管它的输出，
    # 不然再起一个会撞端口，日志里只会看到一串 BindError。
    try:
        probe = LOCAL.get(_sensor_base() + "/health", timeout=0.6)
        if probe.status_code == 200:
            with state_lock:
                module_state["sensor"].update({
                    "state": "running", "msg": "外部进程（非主控台托管）",
                    "checked": True, "exit_code": None,
                    "started_at": module_state["sensor"].get("started_at")
                    or time.strftime("%Y-%m-%d %H:%M:%S"),
                })
            add_log(f"[sensor] {_sensor_base()} 上已有传感器桥在跑，直接复用")
            return True, "传感器桥已在运行"
    except Exception:
        pass

    info = detect_sensor_python(force=force_detect or not _sensor_py_cache["ok"])
    if not info["ok"]:
        with state_lock:
            module_state["sensor"].update({"state": "missing", "msg": info["msg"]})
        add_log(f"模块 [sensor] 跳过：{info['msg']}")
        add_log("       解决办法：在「系统设置 → 桥接环境 → Bridge Python (conda g1 python.exe)」里填写 conda g1 的 python.exe 并保存")
        return False, info["msg"]

    env = {
        "SHIQIAN_BRIDGE_HOST": cfg.get("host") or "127.0.0.1",
        "SHIQIAN_BRIDGE_PORT": cfg.get("port") or 5011,
        "SHIQIAN_DDS_IFACE_ADDR": cfg.get("ifaceaddr") or "",
        "SHIQIAN_DDS_IFACE_NAME": cfg.get("ifacename") or "",
        "SHIQIAN_MOTOR_COUNT": cfg.get("motorcount") or 29,
        "SHIQIAN_VIDEO_URL": cfg.get("videourl") or "",
        "SHIQIAN_ROBOT_IP": cfg.get("robotip") or "192.168.123.161",
    }
    add_log(f"[sensor] 使用解释器 {info['path']}")
    return start_process("sensor", SENSOR_SCRIPT, python=info["path"], extra_env=env)


# ======================================================================
# 动作模组桥（motion_bridge.py）
# ----------------------------------------------------------------------
#  G1 BVH 管道导出的 .g1motion 在这里入库/回放。和传感器桥一样，
#  回放要走 DDS，必须用 conda g1 的 Python 3.10 解释器。
# ======================================================================
MOTION_SCRIPT = "motion_bridge.py"


def _motion_cfg():
    cfg = load_config()
    return cfg.get("motion", {}) or {}


def _motion_base():
    m = _motion_cfg()
    host = str(m.get("host") or "127.0.0.1")
    try:
        port = int(m.get("port") or 5012)
    except (TypeError, ValueError):
        port = 5012
    return f"http://{host}:{port}"


def _motion_dir():
    m = _motion_cfg()
    d = str(m.get("dir") or "").strip()
    if d:
        return d
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "motions")


def start_motion(force_detect=False):
    """拉起动作模组桥（复用传感器的解释器探测结果）。"""
    cfg = _motion_cfg()
    if not cfg.get("enabled", True):
        return False, "动作模组模块已在配置里关闭"

    try:
        probe = LOCAL.get(_motion_base() + "/health", timeout=0.6)
        if probe.status_code == 200:
            with state_lock:
                module_state["motion"].update({
                    "state": "running", "msg": "外部进程（非主控台托管）",
                    "checked": True, "exit_code": None,
                    "started_at": module_state["motion"].get("started_at")
                    or time.strftime("%Y-%m-%d %H:%M:%S"),
                })
            add_log(f"[motion] {_motion_base()} 上已有动作桥在跑，直接复用")
            return True, "动作模组桥已在运行"
    except Exception:
        pass

    info = detect_sensor_python(force=force_detect or not _sensor_py_cache["ok"])
    if not info["ok"]:
        with state_lock:
            module_state["motion"].update({"state": "missing", "msg": info["msg"]})
        add_log(f"模块 [motion] 跳过：{info['msg']}")
        add_log("       解决办法：在「系统设置 → 桥接环境 → Bridge Python (conda g1 python.exe)」里填写 conda g1 的 python.exe 并保存")
        return False, info["msg"]

    env = {
        "SHIQIAN_MOTION_HOST": cfg.get("host") or "127.0.0.1",
        "SHIQIAN_MOTION_PORT": cfg.get("port") or 5012,
        "SHIQIAN_MOTION_DIR": _motion_dir(),
        "SHIQIAN_DDS_IFACE_ADDR": cfg.get("ifaceaddr") or "",
        "SHIQIAN_DDS_IFACE_NAME": cfg.get("ifacename") or "",
        "SHIQIAN_ROBOT_IP": cfg.get("robotip") or "192.168.123.161",
        "SHIQIAN_ALLOW_POLICY": "1" if cfg.get("allow_policy") else "0",
        "SHIQIAN_POLICY_ROOT": cfg.get("policy_root") or "",
        "SHIQIAN_POLICY_DIR": cfg.get("policy_dir") or "",
    }
    add_log(f"[motion] 使用解释器 {info['path']}")
    return start_process("motion", MOTION_SCRIPT, python=info["path"], extra_env=env)


# ======================================================================
# 首次见面：视觉桥（vision_bridge.py）+ Livox MID360 状态
# ----------------------------------------------------------------------
#  雷达（192.168.123.120）是 Livox MID360，一直在网上，但机器人新固件
#  默认不启它的驱动，Jetson 上也没有 livox 进程 —— 所以这里只做"在不在线"
#  的探测，不假装能取点云；要真用点云得另编 Livox-SDK2。
# ======================================================================
VISION_SCRIPT = "vision_bridge.py"
VISION_RT = {"present": False, "last_greet": 0.0, "auto_started": False,
             "suppress_until": 0.0}
LIDAR_CACHE = {"ts": 0.0, "online": False, "ip": "192.168.123.120"}


def _vision_cfg():
    return load_config().get("vision", {}) or {}


def _vision_base():
    v = _vision_cfg()
    try:
        port = int(v.get("port") or 5013)
    except (TypeError, ValueError):
        port = 5013
    return f"http://{v.get('host') or '127.0.0.1'}:{port}"


def _console_base():
    port = int(load_config().get("server", {}).get("consoleport") or 5000)
    return f"http://127.0.0.1:{port}"


def _patch_config(section, patch):
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        raw.setdefault(section, {}).update(patch)
        with open(CONFIG_FILE, "w", encoding="utf-8", newline="\n") as f:
            yaml.safe_dump(raw, f, allow_unicode=True, sort_keys=True)
        return True
    except Exception as e:  # noqa: BLE001
        add_log(f"[vision] 保存配置失败：{e}")
        return False


def start_vision(force_detect=False):
    cfg = _vision_cfg()
    if not cfg.get("enabled", True):
        return False, "视觉模块已在配置里关闭"
    try:
        if LOCAL.get(_vision_base() + "/state", timeout=0.6).status_code == 200:
            with state_lock:
                module_state["vision"].update({
                    "state": "running", "msg": "外部进程（非主控台托管）",
                    "checked": True, "exit_code": None,
                    "started_at": module_state["vision"].get("started_at")
                    or time.strftime("%Y-%m-%d %H:%M:%S"),
                })
            return True, "视觉桥已在运行"
    except Exception:
        pass
    info = detect_sensor_python(force=force_detect or not _sensor_py_cache["ok"])
    if not info["ok"]:
        with state_lock:
            module_state["vision"].update({"state": "missing", "msg": info["msg"]})
        add_log(f"模块 [vision] 跳过：{info['msg']}")
        add_log("       解决办法：在「系统设置 → 桥接环境 → Bridge Python (conda g1 python.exe)」里填写 conda g1 的 python.exe 并保存")
        return False, info["msg"]
    env = {
        "SHIQIAN_VISION_HOST": cfg.get("host") or "127.0.0.1",
        "SHIQIAN_VISION_PORT": cfg.get("port") or 5013,
        "SHIQIAN_VISION_STREAM": cfg.get("stream") or "",
        "SHIQIAN_PRESENT_ON": cfg.get("present_on") or 2.0,
        "SHIQIAN_PRESENT_OFF": cfg.get("present_off") or 4.0,
    }
    add_log(f"[vision] 使用解释器 {info['path']}")
    return start_process("vision", VISION_SCRIPT, python=info["path"], extra_env=env)


def _lidar_status(force=False):
    now = time.time()
    if not force and now - LIDAR_CACHE["ts"] < 15:
        return LIDAR_CACHE
    ip = str((load_config().get("lidar", {}) or {}).get("ip") or "192.168.123.120")
    online = False
    try:
        if os.name == "nt":
            r = subprocess.run(["ping", "-n", "1", "-w", "600", ip],
                               capture_output=True, timeout=6)
        else:
            r = subprocess.run(["ping", "-c", "1", "-W", "1", ip],
                               capture_output=True, timeout=6)
        online = r.returncode == 0
    except Exception:
        pass
    LIDAR_CACHE.update({"ts": now, "online": online, "ip": ip})
    return LIDAR_CACHE


def _vision_loop():
    """检测到"有人出现"的上升沿：播报 + 可选地自动开始接待。"""
    while True:
        time.sleep(1.2)
        cfg = _vision_cfg()
        if not cfg.get("enabled", True):
            continue
        try:
            data = LOCAL.get(_vision_base() + "/state", timeout=2.0).json().get("data") or {}
        except Exception:
            continue
        present = bool(data.get("present"))
        prev = VISION_RT["present"]
        if present and not prev:
            VISION_RT["present"] = True
            faces = len(data.get("faces") or [])
            persons = len(data.get("persons") or [])
            add_log(f"[vision] 首次见面：有人出现（人脸 {faces} / 人形 {persons}）")
            if cfg.get("greet", True) and time.time() - VISION_RT["last_greet"] > 30:
                VISION_RT["last_greet"] = time.time()
                try:
                    LOCAL.post(_sensor_base() + "/tts",
                               json={"text": cfg.get("greet_text") or "你好呀，第一次见。我是时迁。"},
                               timeout=8.0)
                except Exception:
                    pass
            if cfg.get("autostart", True) and time.time() > VISION_RT.get("suppress_until", 0):
                try:
                    on = LOCAL.get(_console_base() + "/api/reception/auto", timeout=3).json()
                    if not (on.get("data") or {}).get("on"):
                        LOCAL.post(_console_base() + "/api/reception/auto",
                                   json={"on": True}, timeout=30)
                        VISION_RT["auto_started"] = True
                        add_log("[vision] 有人靠近，自动接待已开启")
                except Exception as e:  # noqa: BLE001
                    add_log(f"[vision] 自动开启接待失败：{type(e).__name__}")
        elif not present and prev:
            VISION_RT["present"] = False
            add_log("[vision] 人离开了")


threading.Thread(target=_vision_loop, name="vision-auto", daemon=True).start()


@app.route("/api/vision/state", methods=["GET"])
def api_vision_state():
    lidar = _lidar_status()
    payload = {"bridge": False, "present": False, "faces": [], "persons": [],
               "detector": "", "error": ""}
    try:
        d = LOCAL.get(_vision_base() + "/state", timeout=2.0).json().get("data") or {}
        payload.update(d)
        payload["bridge"] = True
    except Exception as e:  # noqa: BLE001
        payload["error"] = f"视觉桥未启动（{type(e).__name__}）"
    payload["lidar"] = {"online": lidar["online"], "ip": lidar["ip"],
                        "note": "Livox MID360 · 驱动未接入" if lidar["online"] else "未上线"}
    payload["autostart"] = bool(_vision_cfg().get("autostart", True))
    payload["greet"] = bool(_vision_cfg().get("greet", True))
    return jsonify({"ok": True, "data": payload})


@app.route("/api/vision/start", methods=["POST"])
def api_vision_start():
    ok, msg = start_vision()
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/vision/config", methods=["POST"])
def api_vision_config():
    data = request.get_json(silent=True) or {}
    patch = {}
    for k in ("autostart", "greet"):
        if k in data:
            patch[k] = bool(data[k])
    if patch and _patch_config("vision", patch):
        add_log(f"[vision] 配置更新：{patch}")
    return jsonify({"ok": True, "msg": "已保存", "data": _vision_cfg()})


@app.route("/api/vision/snapshot.jpg", methods=["GET"])
def api_vision_snapshot():
    try:
        r = LOCAL.get(_vision_base() + "/snapshot.jpg", timeout=4.0)
        if r.status_code != 200:
            return jsonify({"ok": False, "msg": "暂无画面"}), 404
        return Response(r.content, mimetype="image/jpeg",
                        headers={"Cache-Control": "no-store"})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "msg": f"取画面失败：{type(e).__name__}"}), 503


@app.route("/api/vision/stream.mjpg", methods=["GET"])
def api_vision_stream():
    try:
        r = LOCAL.get(_vision_base() + "/stream.mjpg", stream=True,
                      timeout=(2.0, 8.0))
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "msg": f"视觉流不可用：{type(e).__name__}"}), 503
    ctype = r.headers.get("Content-Type", "multipart/x-mixed-replace; boundary=vframe")

    def gen():
        try:
            for chunk in r.iter_content(8192):
                if chunk:
                    yield chunk
        except Exception:
            return
        finally:
            r.close()

    return Response(gen(), content_type=ctype,
                    headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.route("/api/start", methods=["POST"])
def api_start():
    started, skipped = [], []
    ok, msg = start_sensor(force_detect=True)  # 先起传感器桥，页面一打开就有数据
    (started if ok else skipped).append("sensor")
    ok, msg = start_motion()                 # 动作模组桥（回放用）
    (started if ok else skipped).append("motion")
    ok, msg = start_vision()                 # 视觉桥（首次见面）
    (started if ok else skipped).append("vision")
    for key, script in (("asr", "main.py"), ("redeem", "web_redeem_server.py")):
        ok, msg = start_process(key, script)
        (started if ok else skipped).append(key)
    if skipped:
        return jsonify({"ok": True, "data": {"started": started, "skipped": skipped},
                        "msg": f"已启动 {len(started)} 个模块，{len(skipped)} 个跳过（详见日志）"})
    return jsonify({"ok": True, "data": {"started": started, "skipped": skipped},
                    "msg": "系统核心模块已启动，1.5 秒后确认状态"})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    for key, p in processes.items():
        if p is not None and p.poll() is None:
            # 先标 stopping：否则输出泵会把它当成"运行中崩溃"记一笔
            with state_lock:
                module_state[key]["state"] = "stopping"
            p.terminate()
            try:
                p.wait(timeout=2)
            except Exception:
                p.kill()
            add_log(f"模块 [{key}] 已停止")
        processes[key] = None
        with state_lock:
            module_state[key].update({"state": "stopped", "checked": True, "exit_code": None})
            early_output[key].clear()
    return jsonify({"ok": True, "msg": "全部模块已停止"})


@app.route("/api/restart", methods=["POST"])
def api_restart():
    api_stop()
    time.sleep(1)
    api_start()
    return jsonify({"ok": True, "msg": "系统已重启"})


@app.route("/api/module/<name>/restart", methods=["POST"])
def api_module_restart(name):
    script_map = {"asr": "main.py", "redeem": "web_redeem_server.py",
                  "mock": "mock_robot_server.py", "sensor": SENSOR_SCRIPT,
                  "motion": MOTION_SCRIPT, "vision": VISION_SCRIPT}
    if name not in script_map:
        return jsonify({"ok": False, "msg": "未知模块"})

    p = processes.get(name)
    if p is not None and p.poll() is None:
        with state_lock:
            module_state[name]["state"] = "stopping"
        p.terminate()
        try:
            p.wait(timeout=2)
        except Exception:
            p.kill()
    processes[name] = None
    with state_lock:
        module_state[name].update({"state": "stopped", "checked": True, "exit_code": None})
        early_output[name].clear()

    if name == "sensor":
        ok, msg = start_sensor(force_detect=True)
    elif name == "vision":
        ok, msg = start_vision(force_detect=True)
    elif name == "motion":
        ok, msg = start_motion(force_detect=True)
    else:
        ok, msg = start_process(name, script_map[name])
    if not ok:
        return jsonify({"ok": False, "msg": f"模块 [{name}] 重启失败：{msg}"})
    return jsonify({"ok": True, "msg": f"模块 [{name}] 已重启，1.5 秒后确认状态"})


@app.route("/api/demo/start", methods=["POST"])
def api_demo_start():
    start_process("mock", "mock_robot_server.py")
    start_process("asr", "main.py")
    start_process("redeem", "web_redeem_server.py")
    add_log("离线演示模式已全量启动（包含模拟机器人）")
    return jsonify({"ok": True, "msg": "离线演示已启动"})

@app.route("/api/demo/stop", methods=["POST"])
def api_demo_stop():
    api_stop()
    return jsonify({"ok": True, "msg": "离线演示已停止"})

def _worker_base():
    """main.py 在托管模式下开的接收口（HTTP 文本入口）"""
    cfg = load_config()
    w = cfg.get("worker", {}) or {}
    host = str(w.get("host") or "127.0.0.1")
    try:
        port = int(w.get("port") or 5010)
    except (TypeError, ValueError):
        port = 5010
    return f"http://{host}:{port}"


@app.route("/api/say", methods=["POST"])
def api_say():
    """
    把一句话投喂给托管中的 main.py。

    子进程的 stdin 被指向 DEVNULL（没有终端），键盘兜底在那边永远读不到东西 ——
    所以文本必须走 HTTP 送进去，替代"对着黑窗口打字"这条不可达的路径。
    """
    data = request.get_json(silent=True) or {}
    text = str(data.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "msg": "内容不能为空"})
    if len(text) > 200:
        return jsonify({"ok": False, "msg": "太长了，请控制在 200 字以内"})

    with state_lock:
        st = dict(module_state.get("asr", {}))
    if st.get("state") != "running":
        return jsonify({"ok": False, "msg": "语音主程序没在运行，先点「一键启动系统」"})

    base = _worker_base()
    # 主程序从"进程活着"到"端口开始监听"要经过自检和语音预热（实测 5~10 秒），
    # 所以这里带重试，别让用户刚点启动就投喂时看到一句冷冰冰的"连不上"
    last_error = None
    for attempt in range(3):
        try:
            res = LOCAL.post(f"{base}/input", json={"text": text}, timeout=2.5)
            payload = res.json() if res.content else {}
            if res.status_code == 200 and payload.get("ok", True):
                # 只记长度，不记原话（总原则第 4 条：日志里不出现观众说的话）
                add_log(f"[操控] 已投喂文本 {len(text)} 字，主流程已入队")
                return jsonify({"ok": True, "msg": "已送达，处理结果看「进程与日志」"})
            return jsonify({"ok": False, "msg": payload.get("msg") or f"主程序返回 {res.status_code}"})
        except requests.exceptions.ConnectionError as e:
            last_error = e
            if attempt < 2:
                time.sleep(0.9)
        except Exception as e:
            return jsonify({"ok": False, "msg": f"投喂失败: {type(e).__name__}"})

    with state_lock:
        st = dict(module_state.get("asr", {}))
    proc = processes.get("asr")
    alive = proc is not None and proc.poll() is None
    if alive:
        hint = ("进程还活着，但接收口还没开始监听 —— 多半在做自检和语音预热（实测要 5~10 秒），"
                "等日志里出现「文本接收口」再点一次")
    elif st.get("state") in ("failed", "missing"):
        hint = "主程序没在运行（看上方日志里的退出原因）"
    else:
        hint = "语音主程序未启动，先点「一键启动系统」"
    return jsonify({"ok": False, "msg": f"连不上 {base}（已重试 3 次）：{hint}"})


@app.route("/api/redeem/list", methods=["GET"])
def api_redeem_list():
    codes = []
    today_count = 0
    if os.path.exists(CODES_FILE):
        try:
            with open(CODES_FILE, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    codes.append(row)
                    if row.get("status") == "redeemed":
                        today_count += 1
        except Exception as e:
            add_log(f"读取 codes.csv 异常: {str(e)}")
    else:
        codes = [
            {"code": "XQ8888", "status": "unused", "time": ""},
            {"code": "XQ6666", "status": "redeemed", "time": "2026-06-06 12:00:00"},
            {"code": "XQ9999", "status": "unused", "time": ""}
        ]
    return jsonify({"ok": True, "data": {"codes": codes, "today_count": today_count}, "msg": "获取码池成功"})

@app.route("/api/redeem", methods=["POST"])
def api_redeem():
    data = request.json or {}
    code_to_check = data.get("code", "").strip()
    if not code_to_check:
        return jsonify({"ok": False, "msg": "兑换码不能为空"})
    
    codes = []
    found = False
    today_count = 0
    if os.path.exists(CODES_FILE):
        with open(CODES_FILE, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames
            for row in reader:
                if row.get("code") == code_to_check:
                    found = True
                    if row.get("status") == "redeemed":
                        return jsonify({"ok": False, "msg": "该兑换码已被核销过！"})
                    elif row.get("status") == "void":
                        return jsonify({"ok": False, "msg": "该兑换码已作废！"})
                    else:
                        row["status"] = "redeemed"
                        row["time"] = time.strftime("%Y-%m-%d %H:%M:%S")
                if row.get("status") == "redeemed":
                    today_count += 1
                codes.append(row)
        
        if found:
            with open(CODES_FILE, "w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(codes)
            add_log(f"核销兑换码成功: {code_to_check}")
            return jsonify({"ok": True, "data": {"today_count": today_count}, "msg": f"核销成功：{code_to_check}"})
    
    return jsonify({"ok": False, "msg": "未在码池中找到该兑换码"})

@app.route("/api/redeem/void", methods=["POST"])
def api_redeem_void():
    data = request.json or {}
    code_to_void = (data.get("code") or "").strip()
    if not os.path.exists(CODES_FILE):
        return jsonify({"ok": False, "msg": "码池文件不存在"})
    with open(CODES_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fields, rows = reader.fieldnames, list(reader)
    hit = False
    for row in rows:
        if row.get("code") == code_to_void:
            row["status"] = "void"
            row["time"] = time.strftime("%Y-%m-%d %H:%M:%S")
            hit = True
    if not hit:
        return jsonify({"ok": False, "msg": "未找到该兑换码"})
    with open(CODES_FILE, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    add_log(f"兑换码 {code_to_void} 已作废")
    return jsonify({"ok": True, "msg": f"兑换码 {code_to_void} 已作废"})

@app.route("/api/redeem/export", methods=["GET"])
def api_redeem_export():
    if os.path.exists(CODES_FILE):
        return send_file(CODES_FILE, as_attachment=True)
    return jsonify({"ok": False, "msg": "暂无可导出的码池文件"})

# ======================================================================
# 传感器桥代理：页面只跟主控台说话，主控台再转发给 5011 上的桥
# ======================================================================
def _sensor_proxy_get(path, timeout=3.0, stream=False):
    return LOCAL.get(_sensor_base() + path, timeout=timeout, stream=stream)


@app.route("/api/sensors/state", methods=["GET"])
def api_sensors_state():
    try:
        r = _sensor_proxy_get("/state", timeout=2.0)
        return Response(r.content, status=r.status_code,
                        content_type="application/json; charset=utf-8")
    except Exception as e:
        return jsonify({"ok": False, "msg": f"传感器桥未启动（{type(e).__name__}）",
                        "hint": "点「启动全部」或下面的「启动传感器桥」"}), 503


@app.route("/api/sensors/mic/<action>", methods=["POST"])
def api_sensors_mic(action):
    if action not in ("start", "stop"):
        return jsonify({"ok": False, "msg": "未知操作"}), 400
    try:
        body = request.get_json(silent=True) or {}
        r = LOCAL.post(_sensor_base() + f"/mic/{action}", json=body, timeout=8.0)
        add_log(f"[sensor] 麦克风 {action} -> {r.status_code}")
        return Response(r.content, status=r.status_code,
                        content_type="application/json; charset=utf-8")
    except Exception as e:
        return jsonify({"ok": False, "msg": f"麦克风操作失败：{type(e).__name__}"}), 503


@app.route("/api/sensors/segments", methods=["GET"])
def api_sensors_segments():
    try:
        r = _sensor_proxy_get("/segments", timeout=3.0)
        return Response(r.content, status=r.status_code,
                        content_type="application/json; charset=utf-8")
    except Exception as e:
        return jsonify({"ok": False, "data": [], "msg": f"取片段失败：{type(e).__name__}"}), 503


@app.route("/api/sensors/segments/clear", methods=["POST"])
def api_sensors_segments_clear():
    try:
        r = LOCAL.post(_sensor_base() + "/segments/clear", timeout=5.0)
        return Response(r.content, status=r.status_code,
                        content_type="application/json; charset=utf-8")
    except Exception as e:
        return jsonify({"ok": False, "msg": f"清空失败：{type(e).__name__}"}), 503


@app.route("/api/sensors/segments/<seg_id>.wav", methods=["GET"])
def api_sensors_segment_wav(seg_id):
    try:
        r = _sensor_proxy_get(f"/segments/{seg_id}.wav", timeout=10.0)
        if r.status_code != 200:
            return jsonify({"ok": False, "msg": "片段不存在"}), 404
        return Response(r.content, mimetype="audio/wav",
                        headers={"Cache-Control": "no-store"})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"取片段失败：{type(e).__name__}"}), 503


@app.route("/api/sensors/tts", methods=["POST"])
def api_sensors_tts():
    data = request.get_json(silent=True) or {}
    text = str(data.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "msg": "内容不能为空"})
    try:
        r = LOCAL.post(_sensor_base() + "/tts", json={"text": text}, timeout=8.0)
        payload = r.json() if r.content else {}
        if r.status_code == 200 and payload.get("ok"):
            add_log(f"[sensor] 机器人播报 {len(text)} 字")
        return Response(r.content, status=r.status_code,
                        content_type="application/json; charset=utf-8")
    except Exception as e:
        return jsonify({"ok": False, "msg": f"播报失败：{type(e).__name__}"}), 503


@app.route("/api/sensors/mic.wav", methods=["GET"])
def api_sensors_wav():
    try:
        r = _sensor_proxy_get("/mic.wav", timeout=15.0)
        if r.status_code != 200:
            return jsonify({"ok": False, "msg": "还没有录音，先打开麦克风采一段"}), 404
        return Response(
            r.content, mimetype="audio/wav",
            headers={"Content-Disposition": 'attachment; filename="g1_mic.wav"'},
        )
    except Exception as e:
        return jsonify({"ok": False, "msg": f"取录音失败：{type(e).__name__}"}), 503


@app.route("/api/sensors/video.jpg", methods=["GET"])
def api_sensors_frame():
    try:
        r = _sensor_proxy_get("/video/frame.jpg", timeout=3.0)
        if r.status_code != 200:
            return jsonify({"ok": False, "msg": "暂无画面"}), 404
        return Response(r.content, mimetype="image/jpeg",
                        headers={"Cache-Control": "no-store"})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"取帧失败：{type(e).__name__}"}), 503


@app.route("/api/sensors/video.mjpg", methods=["GET"])
def api_sensors_mjpeg():
    try:
        r = LOCAL.get(_sensor_base() + "/video.mjpg", stream=True,
                      timeout=(2.0, 8.0), headers={"Accept": "multipart/x-mixed-replace"})
    except Exception as e:
        return jsonify({"ok": False, "msg": f"视频流不可用：{type(e).__name__}"}), 503

    ctype = r.headers.get("Content-Type", "multipart/x-mixed-replace; boundary=shiqianframe")

    def generate():
        try:
            for chunk in r.iter_content(8192):
                if chunk:
                    yield chunk
        except Exception:
            return
        finally:
            r.close()

    return Response(generate(), content_type=ctype,
                    headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.route("/api/sensors/start", methods=["POST"])
def api_sensors_start():
    ok, msg = start_sensor(force_detect=True)
    if not ok:
        return jsonify({"ok": False, "msg": msg, "python": _sensor_py_cache["path"] or ""})
    return jsonify({"ok": True, "msg": msg})


@app.route("/api/sensors/info", methods=["GET"])
def api_sensors_info():
    info = detect_sensor_python(force=bool(request.args.get("force")))
    s = _sensor_cfg()
    return jsonify({"ok": True, "data": {
        "python": info["path"] or "",
        "python_ok": info["ok"],
        "python_msg": info["msg"],
        "bridge": _sensor_base(),
        "config": s,
        "candidates": _candidate_pythons(),
    }, "msg": "ok"})


# ======================================================================
# 动作模组 API（转发到 motion_bridge）
# ======================================================================
def _motion_proxy_json(path, timeout=8.0):
    r = LOCAL.get(_motion_base() + path, timeout=timeout)
    return Response(r.content, status=r.status_code,
                    content_type="application/json; charset=utf-8")


@app.route("/api/motion/state", methods=["GET"])
def api_motion_state():
    try:
        return _motion_proxy_json("/state", timeout=3.0)
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "msg": f"动作模组桥未启动（{type(e).__name__}）",
                        "library": [], "session": None, "playing": False}), 503


@app.route("/api/motion/start", methods=["POST"])
def api_motion_start():
    ok, msg = start_motion(force_detect=True)
    return jsonify({"ok": ok, "msg": msg})


@app.route("/api/motion/upload", methods=["POST"])
def api_motion_upload():
    name = (request.args.get("name") or "").strip() or "motion.g1motion"
    raw = request.get_data()
    if not raw:
        return jsonify({"ok": False, "msg": "没有收到文件内容"}), 400
    try:
        r = LOCAL.post(_motion_base() + "/upload", params={"name": name},
                       data=raw, timeout=120.0,
                       headers={"Content-Type": "application/octet-stream"})
        try:
            payload = r.json()
            if r.status_code == 200 and payload.get("ok"):
                add_log(f"[motion] 导入动作模组 {name}")
        except Exception:
            pass
        return Response(r.content, status=r.status_code,
                        content_type="application/json; charset=utf-8")
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "msg": f"上传失败：{type(e).__name__}"}), 503


@app.route("/api/motion/play", methods=["POST"])
def api_motion_play():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "msg": "请选择要播放的动作模组"}), 400
    body = {"name": name,
            "speed": float(data.get("speed") or 0.5),
            "mode": str(data.get("mode") or "arm_sdk")}
    try:
        r = LOCAL.post(_motion_base() + "/play", json=body, timeout=20.0)
        if r.status_code == 200:
            add_log(f"[motion] 回放 {name} | {body['speed']}x | {body['mode']}")
        return Response(r.content, status=r.status_code,
                        content_type="application/json; charset=utf-8")
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "msg": f"回放请求失败：{type(e).__name__}"}), 503


@app.route("/api/motion/stop", methods=["POST"])
def api_motion_stop():
    try:
        r = LOCAL.post(_motion_base() + "/stop", timeout=10.0)
        add_log("[motion] 发送停止")
        return Response(r.content, status=r.status_code,
                        content_type="application/json; charset=utf-8")
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "msg": f"停止失败：{type(e).__name__}"}), 503


@app.route("/api/motion/delete", methods=["POST"])
def api_motion_delete():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "msg": "缺少文件名字"}), 400
    try:
        r = LOCAL.post(_motion_base() + "/delete", json={"name": name}, timeout=10.0)
        return Response(r.content, status=r.status_code,
                        content_type="application/json; charset=utf-8")
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "msg": f"删除失败：{type(e).__name__}"}), 503


@app.route("/api/motion/curve", methods=["GET"])
def api_motion_curve():
    name = (request.args.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "msg": "缺少 name"}), 400
    try:
        r = LOCAL.get(_motion_base() + "/curve", params={"name": name}, timeout=15.0)
        return Response(r.content, status=r.status_code,
                        content_type="application/json; charset=utf-8")
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "msg": f"取曲线失败：{type(e).__name__}"}), 503



# ======================================================================
# 本地大模型：Ollama / qwen3:8b
# ----------------------------------------------------------------------
#  方案里"云端大模型"这一步落到本机就是 Ollama：默认 127.0.0.1:11434，
#  模型 qwen3:8b。qwen3 是会思考的模型，必须带 think=false，否则
#  JSON 会被 thinking 占满、content 为空。也兼容 OpenAI 风格接口。
# ======================================================================
DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
TICKET_FILE = os.path.join(DATA_DIR, "tickets.csv")
TICKET_FIELDS = ["id", "code", "memory", "pattern", "pose", "text", "created", "status"]
PATTERN_NAMES = {"star": "星形", "heart": "心形", "wave": "波浪",
                 "emblem": "校徽", "spark": "光芒", "circle": "圆环"}


def _llm_cfg():
    return load_config().get("llm", {}) or {}


def _llm_base():
    return str(_llm_cfg().get("apiurl") or "http://127.0.0.1:11434").rstrip("/")


def _llm_model():
    return str(_llm_cfg().get("model") or "qwen3:8b")


def _llm_provider():
    p = str(_llm_cfg().get("provider") or "").strip().lower()
    if p:
        return p
    base = _llm_base()
    return "ollama" if ("11434" in base or "ollama" in base) else "openai"


def llm_chat(messages, json_mode=False, timeout=None, temperature=0.7, max_tokens=400):
    """统一对话入口：默认本机 Ollama，可切 OpenAI 兼容接口。"""
    cfg = _llm_cfg()
    timeout = timeout or float(cfg.get("timeout") or 90)
    base, model, provider = _llm_base(), _llm_model(), _llm_provider()
    if provider == "ollama":
        payload = {"model": model, "messages": messages, "stream": False, "think": False,
                   "options": {"temperature": temperature, "num_predict": max_tokens}}
        if json_mode:
            payload["format"] = "json"
        r = LOCAL.post(base + "/api/chat", json=payload, timeout=timeout)
        r.raise_for_status()
        return ((r.json().get("message") or {}).get("content") or "").strip()
    key = get_secret("llm", "apikey")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    url = base + "/chat/completions" if base.endswith("/v1") else base + "/v1/chat/completions"
    body = {"model": model, "messages": messages,
            "temperature": temperature, "max_tokens": max_tokens}
    if json_mode:
        body["response_format"] = {"type": "json_object"}
    r = LOCAL.post(url, json=body, headers=headers, timeout=timeout)
    r.raise_for_status()
    return (r.json()["choices"][0]["message"]["content"] or "").strip()


@app.route("/api/llm/status", methods=["GET"])
def api_llm_status():
    base, model, provider = _llm_base(), _llm_model(), _llm_provider()
    models, ok, msg = [], False, ""
    t0 = time.time()
    try:
        if provider == "ollama":
            r = LOCAL.get(base + "/api/tags", timeout=3.0)
            models = [m.get("name") for m in (r.json().get("models") or [])]
            ok = model in models or any(model.split(":")[0] == (m or "").split(":")[0] for m in models)
            msg = "本机 Ollama 就绪" if ok else f"Ollama 在跑，但没有模型 {model}"
        else:
            ok, msg = True, "外部接口（未探测）"
    except Exception as e:  # noqa: BLE001
        msg = f"连不上 {base}（{type(e).__name__}）· 先启动 Ollama"
    return jsonify({"ok": ok, "msg": msg, "data": {
        "provider": provider, "model": model, "base": base,
        "models": models, "latency_ms": int((time.time() - t0) * 1000)}})


# ---------------- 小票与礼品码 ----------------
def _ensure_data():
    os.makedirs(DATA_DIR, exist_ok=True)
    if not os.path.exists(TICKET_FILE):
        with open(TICKET_FILE, "w", encoding="utf-8", newline="") as f:
            csv.DictWriter(f, fieldnames=TICKET_FIELDS).writeheader()


def _append_ticket(t):
    _ensure_data()
    with open(TICKET_FILE, encoding="utf-8") as f:
        existing = {row.get("id") for row in csv.DictReader(f)}
    if t.get("id") in existing:
        return False
    with open(TICKET_FILE, "a", encoding="utf-8", newline="") as f:
        csv.DictWriter(f, fieldnames=TICKET_FIELDS).writerow(
            {k: t.get(k, "") for k in TICKET_FIELDS})
    return True


def _next_ticket_id():
    """
    票号： #年-月日-序号（如 #2026-1118-0001）。
    以前是 #2026-0001 这种按天重排的号，跨天必然和昨天的号撞车，
    而 _append_ticket 又按 id 去重 —— 结果第二天的票一张都写不进去。
    """
    _ensure_data()
    today = time.strftime("%Y-%m-%d")
    seq = 0
    with open(TICKET_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("created") or "").startswith(today):
                seq += 1
    return f"#{time.strftime('%Y-%m%d')}-{seq + 1:04d}"


def _alloc_code():
    """从码池取一个 unused 码；池子空了就追加一个 6 位码。"""
    if os.path.exists(CODES_FILE):
        with open(CODES_FILE, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fields = reader.fieldnames or ["code", "status", "time"]
            rows = list(reader)
        for row in rows:
            if (row.get("status") or "") == "unused":
                code = row["code"]
                row["status"] = "issued"
                row["time"] = time.strftime("%Y-%m-%d %H:%M:%S")
                with open(CODES_FILE, "w", encoding="utf-8", newline="") as f:
                    w = csv.DictWriter(f, fieldnames=fields)
                    w.writeheader()
                    w.writerows(rows)
                return code
        import random
        code = "".join(random.choice("0123456789") for _ in range(6))
        rows.append({"code": code, "status": "unused", "time": ""})
        with open(CODES_FILE, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        return code
    import random
    return "".join(random.choice("0123456789") for _ in range(6))


RECEPTION_SYSTEM = (
    "你是校园接待机器人 G1 的作品设计师。观众对机器人说一句话，你要把它变成一件可以带走的小作品。\n"
    "只输出一个 JSON 对象，字段如下，不要输出解释、markdown 或代码块：\n"
    "memory: 8~15 个汉字的短句，是这句话的记忆点，要温暖、具体、有画面感；\n"
    "pattern: 从 star/heart/wave/emblem/spark/circle 里选最贴切的一个；\n"
    "pose: 从用户给的姿势列表里选一个最贴切的 id；\n"
    "reply: 机器人对观众说的一句话，20 字以内，口语化、温暖。"
)


@app.route("/api/llm/start", methods=["POST"])
def api_llm_start():
    """没连上本机 Ollama 时，自己把 `ollama serve` 拉起来（已退出则重启）。"""
    base = _llm_base()
    try:
        LOCAL.get(base + "/api/tags", timeout=1.0)
        return jsonify({"ok": True, "msg": "模型服务已在运行"})
    except Exception:
        pass
    exe = shutil.which("ollama") or os.path.expanduser(
        r"~\AppData\Local\Programs\Ollama\ollama.exe")
    if not os.path.exists(exe) and not shutil.which("ollama"):
        return jsonify({"ok": False, "msg": "没找到 ollama，可执行文件不在 PATH 里"}), 404
    try:
        flags = 0x08000000 if os.name == "nt" else 0      # CREATE_NO_WINDOW
        subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, creationflags=flags)
        add_log("[llm] 已拉起 ollama serve，等待就绪 …")
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "msg": f"启动失败：{e}"}), 500
    t0 = time.time()
    while time.time() - t0 < 20:
        try:
            LOCAL.get(base + "/api/tags", timeout=1.0)
            return jsonify({"ok": True, "msg": "模型服务已就绪"})
        except Exception:
            time.sleep(0.8)
    return jsonify({"ok": False, "msg": "20 秒内没起来，检查 Ollama 是否被杀软拦住"}), 504


def _reception_generate(text, poses):
    """
    一句话 → 一件作品。返回可直接 jsonify 的 data 字典。
    自动接待和手动「生成作品」都走这里，避免两套逻辑跑偏。
    """
    pose_hint = "、".join(f"{p.get('id')}={p.get('name')}" for p in poses[:24]) \
        or "（动作库为空，pose 请留空）"
    t0 = time.time()
    fallback = False
    try:
        raw = llm_chat([
            {"role": "system", "content": RECEPTION_SYSTEM},
            {"role": "user", "content": f"观众的话：{text}\n可用姿势：{pose_hint}"},
        ], json_mode=True, max_tokens=320)
        info = json.loads(raw)
    except Exception as e:  # noqa: BLE001
        fallback = True
        info = {"memory": text[:15], "pattern": "star",
                "pose": (poses[0].get("id") if poses else ""),
                "reply": "这句话，我记住了。"}
        add_log(f"[reception] 大模型不可用，走兜底模板：{type(e).__name__}")
    memory = str(info.get("memory") or text).strip()[:24]
    pattern = str(info.get("pattern") or "star").strip().lower()
    if pattern not in PATTERN_NAMES:
        pattern = "star"
    pose = str(info.get("pose") or "").strip()
    if not poses:
        pose = ""                      # 动作库为空就别瞎指一个不存在的动作
    elif pose not in [p.get("id") for p in poses]:
        pose = poses[0].get("id")
    ticket = {
        "id": _next_ticket_id(),
        "code": _alloc_code(),
        "memory": memory,
        "pattern": pattern,
        "pose": pose,
        "text": text,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": "issued",
    }
    _append_ticket(ticket)
    ms = int((time.time() - t0) * 1000)
    add_log(f"[reception] {ticket['id']} 「{memory}」 图案={PATTERN_NAMES[pattern]} 姿势={pose} {ms}ms"
            + (" (兜底)" if fallback else ""))
    return {"ticket": ticket, "reply": str(info.get("reply") or ""),
            "pattern_name": PATTERN_NAMES[pattern], "latency_ms": ms,
            "fallback": fallback}


@app.route("/api/reception/generate", methods=["POST"])
def api_reception_generate():
    data = request.get_json(silent=True) or {}
    text = str(data.get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "msg": "先说一句话再生成"}), 400
    return jsonify({"ok": True, "data": _reception_generate(text, data.get("poses") or []),
                    "msg": "ok"})


@app.route("/api/reception/ticket", methods=["POST"])
def api_reception_save_ticket():
    data = request.get_json(silent=True) or {}
    t = data.get("ticket") or {}
    if not t.get("id"):
        return jsonify({"ok": False, "msg": "缺少小票编号"}), 400
    added = _append_ticket(t)
    if added:
        add_log(f"[reception] 记录出票 {t.get('id')} 礼品码 {t.get('code')}")
    return jsonify({"ok": True, "msg": "已记录" if added else "这张票已经记过了"})


@app.route("/api/reception/today", methods=["GET"])
def api_reception_today():
    _ensure_data()
    today = time.strftime("%Y-%m-%d")
    rows = []
    with open(TICKET_FILE, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("created") or "").startswith(today):
                rows.append(row)
    return jsonify({"ok": True, "data": {"tickets": rows, "count": len(rows)}, "msg": "ok"})


# ======================================================================
# 自动接待：开一次，后面全自动
# ----------------------------------------------------------------------
#  循环（后台线程，1.2 秒一拍）：
#    传感器桥切出一句 → 拿到离线识别文本 → 生成作品 → 机器人播报 reply
#    → 摆姿势 → 记进今日作品。
#  三个原则：
#    1) 不主动启动 Ollama：模型没起就用兜底模板，绝不偷偷拉进程；
#    2) 只处理"开启之后新出现"的句子，不把历史片段重放一遍；
#    3) 同一时刻只处理一条，生成期间不再收音排队（现场不串音）。
# ======================================================================
RC_AUTO = {
    "on": False, "busy": False, "count": 0, "note": "未开启",
    "seen": [], "last": None, "mic": False,
}
RC_AUTO_LOCK = threading.RLock()


def _rc_poses():
    """姿势候选只来自动作库（.g1motion / 36 列 rl_sar CSV），不再有假占位。"""
    poses = []
    try:
        r = LOCAL.get(_motion_base() + "/state", timeout=1.0)
        for item in (r.json().get("library") or [])[:24]:
            name = item.get("name") or ""
            if name:
                poses.append({"id": name,
                              "name": name.replace(".g1motion", "").replace(".csv", "")})
    except Exception:
        pass
    return poses


def _rc_auto_pose(pose):
    if not pose:
        return
    try:
        speed = float((load_config().get("behavior") or {}).get("replay_speed") or 0.5)
        LOCAL.post(_motion_base() + "/play",
                   json={"name": pose, "speed": speed, "mode": "arm_sdk"}, timeout=20.0)
        add_log(f"[auto] 回放姿势 {pose}")
    except Exception as e:  # noqa: BLE001
        add_log(f"[auto] 姿势下发失败：{type(e).__name__}")


def _rc_auto_loop():
    while True:
        time.sleep(1.2)
        if not RC_AUTO["on"] or RC_AUTO["busy"]:
            continue
        try:
            d = LOCAL.get(_sensor_base() + "/state", timeout=2.0).json()
        except Exception:
            RC_AUTO["note"] = "传感器桥没响应，先点「启动传感器桥」"
            continue
        segs = d.get("segments") or []
        if not segs:
            RC_AUTO["note"] = "等待观众说话…"
            continue
        pending = [s for s in segs if s["id"] not in RC_AUTO["seen"]]
        ready = [s for s in pending if (s.get("text") or "").strip()]
        if not ready:
            asr = (d.get("mic") or {}).get("asr") or {}
            if pending and not asr.get("ready"):
                why = asr.get("error") or "vosk 未安装"
                RC_AUTO["note"] = f"切到 {len(pending)} 句，但离线识别没就绪（{why}）"
            elif pending:
                RC_AUTO["note"] = f"切到 {len(pending)} 句，等识别结果…"
            else:
                RC_AUTO["note"] = "等待观众说话…"
            continue
        # 取最新一句，把更早的都标记已读，避免现场有杂音时一直追着旧句子跑
        for old_seg in pending:
            if old_seg["id"] not in RC_AUTO["seen"]:
                RC_AUTO["seen"].append(old_seg["id"])
        seg = ready[-1]
        RC_AUTO["seen"] = RC_AUTO["seen"][-200:]
        text = (seg.get("text") or "").strip()
        if len(text) < 2:
            RC_AUTO["note"] = f"识别到「{text}」，太短不像一句话，跳过"
            continue
        if text == RC_AUTO.get("last_text") and time.time() - RC_AUTO.get("last_text_ts", 0) < 60:
            RC_AUTO["note"] = f"「{text}」刚生成过，跳过重复"
            continue
        RC_AUTO["busy"] = True
        try:
            RC_AUTO["note"] = f"正在为「{text[:14]}」生成作品…"
            data = _reception_generate(text, _rc_poses())
            t = data["ticket"]
            say = (data.get("reply") or t["memory"]).strip()
            try:
                LOCAL.post(_sensor_base() + "/tts", json={"text": say}, timeout=8.0)
            except Exception:
                pass
            _rc_auto_pose(t.get("pose"))
            RC_AUTO["count"] += 1
            RC_AUTO["last_text"] = text
            RC_AUTO["last_text_ts"] = time.time()
            RC_AUTO["last"] = {"ticket": t, "reply": say, "text": text,
                               "at": time.strftime("%H:%M:%S")}
            RC_AUTO["note"] = f"已完成 {t['id']}「{t['memory']}」"
            add_log(f"[auto] {t['id']} 「{t['memory']}」← 「{text[:20]}」")
        except Exception as e:  # noqa: BLE001
            RC_AUTO["note"] = f"生成失败：{type(e).__name__}"
            add_log(f"[auto] 生成失败：{type(e).__name__}: {e}")
        finally:
            RC_AUTO["busy"] = False


threading.Thread(target=_rc_auto_loop, name="rc-auto", daemon=True).start()


@app.route("/api/reception/auto", methods=["GET", "POST"])
def api_reception_auto():
    if request.method == "GET":
        with RC_AUTO_LOCK:
            asr = {}
            try:
                asr = (LOCAL.get(_sensor_base() + "/state", timeout=1.5).json()
                       .get("mic") or {}).get("asr") or {}
            except Exception:
                pass
            return jsonify({"ok": True, "data": {
                "on": RC_AUTO["on"], "busy": RC_AUTO["busy"], "count": RC_AUTO["count"],
                "note": RC_AUTO["note"], "last": RC_AUTO["last"],
                "asr_ready": bool(asr.get("ready")), "asr_error": asr.get("error") or ""},
                "msg": "ok"})

    on = bool((request.get_json(silent=True) or {}).get("on"))
    if on:
        ok, msg = start_sensor()
        if not ok:
            return jsonify({"ok": False, "msg": f"传感器桥起不来：{msg}"}), 503
        # 先确认 DDS 真的连上机器人，否则"开启"只是个假动作
        st = {}
        try:
            st = LOCAL.get(_sensor_base() + "/state", timeout=3.0).json()
        except Exception:
            pass
        if not (st.get("dds") or {}).get("ready"):
            why = (st.get("dds") or {}).get("error") or "传感器桥没响应"
            return jsonify({"ok": False,
                            "msg": f"连不上机器人：{why}。检查机器人是否上电、网线是否插好"}), 503
        # 开麦：auto=true 走自动切句
        try:
            LOCAL.post(_sensor_base() + "/mic/start", json={"auto": True}, timeout=10.0)
        except Exception as e:  # noqa: BLE001
            return jsonify({"ok": False, "msg": f"麦克风打不开：{type(e).__name__}"}), 503
        time.sleep(1.2)
        try:
            m = LOCAL.get(_sensor_base() + "/state", timeout=3.0).json().get("mic") or {}
            if m.get("error"):
                return jsonify({"ok": False, "msg": f"麦克风异常：{m['error']}"}), 503
        except Exception:
            pass
        # 只处理开启之后的新句子：把当前已有的 id 全部标记为"看过"
        seen = []
        try:
            segs = LOCAL.get(_sensor_base() + "/state", timeout=2.0).json().get("segments") or []
            seen = [s["id"] for s in segs]
        except Exception:
            pass
        with RC_AUTO_LOCK:
            RC_AUTO.update({"on": True, "busy": False, "count": 0, "seen": seen,
                            "last": None, "note": "已开启，等待观众说话…", "mic": True})
        add_log("[auto] 自动接待已开启（麦克风自动切句 + 离线识别）")
        return jsonify({"ok": True, "msg": "自动接待已开启"})

    with RC_AUTO_LOCK:
        RC_AUTO["on"] = False
        RC_AUTO["note"] = "已关闭"
        RC_AUTO["mic"] = False
    VISION_RT["suppress_until"] = time.time() + 300
    try:
        LOCAL.post(_sensor_base() + "/mic/stop", timeout=8.0)
    except Exception:
        pass
    add_log("[auto] 自动接待已关闭")
    return jsonify({"ok": True, "msg": "自动接待已关闭"})


@app.route("/api/llm/stop", methods=["POST"])
def api_llm_stop():
    """关掉本机 Ollama（只杀监听 11434 的那个进程，不碰别的东西）。"""
    base = _llm_base()
    try:
        LOCAL.get(base + "/api/tags", timeout=1.0)
    except Exception:
        return jsonify({"ok": True, "msg": "Ollama 本来就没在跑"})

    pids = _port_pids(11434)
    if not pids:
        return jsonify({"ok": False, "msg": "找不到占用 11434 的进程"}), 404
    killed = []
    for pid in pids:
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                               capture_output=True, timeout=8)
            else:
                os.kill(pid, 15)
            killed.append(pid)
        except Exception as e:  # noqa: BLE001
            add_log(f"[llm] 关闭 PID {pid} 失败：{e}")
    time.sleep(1.0)
    alive = False
    try:
        LOCAL.get(base + "/api/tags", timeout=1.0)
        alive = True
    except Exception:
        alive = False
    if alive:
        return jsonify({"ok": False, "msg": "进程已发信号但端口还在，稍后再试"}), 504
    add_log(f"[llm] 已关闭 Ollama（PID {killed}）")
    return jsonify({"ok": True, "msg": f"已关闭 Ollama（PID {', '.join(map(str, killed))}）"})


def _port_pids(port):
    """谁在监听这个端口（Windows netstat / Linux ss）。"""
    pids = []
    try:
        if os.name == "nt":
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                                 capture_output=True, text=True, timeout=8).stdout
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3] == "LISTENING":
                    if parts[4].isdigit():
                        pids.append(int(parts[4]))
        else:
            out = subprocess.run(["ss", "-lptn", f"sport = :{port}"],
                                 capture_output=True, text=True, timeout=8).stdout
            import re
            pids = [int(m) for m in re.findall(r"pid=(\d+)", out)]
    except Exception as e:  # noqa: BLE001
        add_log(f"[llm] 查端口失败：{e}")
    return sorted(set(pids))


@app.route("/api/logs", methods=["GET"])
def api_logs():
    with logs_lock:
        recent = list(logs_buffer)[-100:]
    return jsonify({"ok": True, "data": recent, "msg": "获取日志成功",
                    "capacity": LOG_MAX, "total_kept": len(recent)})

if __name__ == "__main__":
    cfg = load_config()
    port = cfg.get("server", {}).get("consoleport", 5000)
    # 这个控制台没有登录体系，默认只监听本机回环地址：
    # 想给同一局域网的平板/手机看，再显式设置 SHIQIAN_CONSOLE_HOST=0.0.0.0。
    host = os.environ.get("SHIQIAN_CONSOLE_HOST") or cfg.get("server", {}).get("host") or "127.0.0.1"
    add_log(f"时迁主控台 1.1.0 后端启动中，监听 {host}:{port}")
    add_log(f"[配置] 密钥存放位置：{secret_backend()}；config.yaml 不含任何密钥")
    app.run(host=host, port=port, debug=False)
