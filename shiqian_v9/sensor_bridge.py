# -*- coding: utf-8 -*-
r"""
G1 传感器桥接进程（sensor bridge）
==================================

为什么要有它：
    主控台便携版内置的是 Python 3.14，没有 numpy / cyclonedds；
    G1 的 DDS（unitree_sdk2py）只能在 Python 3.10 的 conda 环境 g1 里跑。
    所以这里单独起一个"数据桥"：由主控台用 g1 解释器拉起来，
    在 127.0.0.1:5011 上吐 JSON / MJPEG，主控台只做代理和展示。

对外接口（除了麦克风/播报，其余都是只读）：
    GET  /health              存活与自检
    GET  /state               一份完整快照：链路 / IMU / 关节 / 遥控器 / 麦克风 / 视频
    POST /mic/start           打开麦克风（voice API 1008 mode=1）+ 开始收 UDP 组播
                              body {"auto": true} 时开启自动切句（默认 true）
    POST /mic/stop            关闭麦克风，并把 mode 还原成打开前的值
    GET  /mic.wav             下载最近一次手动录音
    GET  /segments            自动切出来的句子列表
    GET  /segments/<id>.wav   下载/回放某一句
    POST /segments/clear      清空句子列表
    POST /tts                 {"text": "..."} 用机器人自己的喇叭播报
    GET  /video.mjpg          MJPEG 流（浏览器 <img> 直接能放）
    GET  /video/frame.jpg     单帧快照

数据来源：
    rt/lowstate        unitree_hg::LowState_   ~1000Hz  IMU + 35 路电机 + 遥控器
    rt/audio_msg       std_msgs::String_                语音服务的状态/识别结果
    rt/frontvideostream unitree_go::Go2FrontVideoData_  机载视频（有相机时才有）
    239.168.123.161:5555  UDP 组播 16k/mono/s16le 160ms 一包

自动切句（VAD）：
    每包 160ms 切成 8 个 20ms 帧，按帧算 RMS；底噪自适应，
    连续 3 帧超过阈值算"开口"，连续 25 帧低于阈值（500ms）算"说完"，
    切出来的句子直接落成 wav，前端列表里可以逐条回放/下载。
    阈值、最短句长这些都在文件头的 VAD_* 常量里，现场吵就调 VAD_THRESHOLD_RATIO。

用法：
    python sensor_bridge.py                # 用默认 127.0.0.1:5011
    python sensor_bridge.py --port 5011
环境变量：
    SHIQIAN_BRIDGE_HOST / SHIQIAN_BRIDGE_PORT
    SHIQIAN_DDS_IFACE_ADDR     本机在 192.168.123.x 上的地址（留空自动探测）
    SHIQIAN_DDS_IFACE_NAME     网卡名（中文名也行；留空则只用地址绑定）
    SHIQIAN_SDK_PATH           unitree_sdk2py 所在目录（没装进环境时用）
    SHIQIAN_MOTOR_COUNT        上报多少路电机，默认 29（G1 标准版）
    SHIQIAN_VIDEO_URL          外部视频源（MJPEG 或 JPEG 快照地址），留空则用 DDS
    SHIQIAN_MIC_SECONDS        录音缓冲上限，默认 60 秒
"""

import argparse
import json
import os
import re
import socket
import struct
import sys
import threading
import tempfile
import time
import wave
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------- 参数 ----
BRIDGE_HOST = os.environ.get("SHIQIAN_BRIDGE_HOST", "127.0.0.1")
BRIDGE_PORT = int(os.environ.get("SHIQIAN_BRIDGE_PORT", "5011"))
ROBOT_IP = os.environ.get("SHIQIAN_ROBOT_IP", "192.168.123.161")
IFACE_ADDR = os.environ.get("SHIQIAN_DDS_IFACE_ADDR", "").strip()
IFACE_NAME = os.environ.get("SHIQIAN_DDS_IFACE_NAME", "").strip()
SDK_PATH = os.environ.get("SHIQIAN_SDK_PATH", "").strip()
MOTOR_COUNT = int(os.environ.get("SHIQIAN_MOTOR_COUNT", "29"))
VIDEO_URL = os.environ.get("SHIQIAN_VIDEO_URL", "").strip()
MIC_SECONDS = int(os.environ.get("SHIQIAN_MIC_SECONDS", "60"))

MCAST_GROUP = "239.168.123.161"
MCAST_PORT = 5555
SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2
CHANNELS = 1
MAX_WAV_BYTES = SAMPLE_RATE * SAMPLE_WIDTH * MIC_SECONDS

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
WAV_PATH = os.path.join(DATA_DIR, "mic_last.wav")
SEG_DIR = os.path.join(DATA_DIR, "segments")
SEG_MAX = int(os.environ.get("SHIQIAN_SEG_MAX", "40"))

# ---- 自动切句（VAD）参数 ----
VAD_FRAME = 320                 # 20ms @16k
VAD_START_FRAMES = 3            # 连续 3 帧超过阈值 = 开口（60ms）
VAD_END_FRAMES = 25             # 连续 25 帧低于阈值 = 说完（500ms）
VAD_PREROLL = 10                # 开口前保留 200ms，避免吃掉第一个字
VAD_MIN_SEC = 0.35              # 短于这个当咳嗽/碰麦，丢掉
VAD_MAX_SEC = 12.0              # 超过就强制断开
VAD_ABS_FLOOR = 0.006           # 绝对最低阈值（安静房间的底）
VAD_THRESHOLD_RATIO = 3.0       # 阈值 = 底噪 * 这个倍数

# --------------------------------------------------------------- 状态 ----
lock = threading.RLock()
state = {
    "started_at": time.time(),
    "dds_ready": False,
    "dds_error": "",
    "iface": "",
    "lowstate": {
        "count": 0,
        "hz": 0.0,
        "last_ts": 0.0,
        "tick": 0,
        "mode_pr": 0,
        "mode_machine": 0,
    },
    "imu": None,
    "motors": [],
    "remote": [],
    "audio_msgs": deque(maxlen=20),
    "video": {
        "source": "none",
        "frames": 0,
        "hz": 0.0,
        "last_ts": 0.0,
        "jpeg": b"",
        "codec": "",
        "note": "",
    },
    "mic": {
        "active": False,
        "auto": False,
        "mode": None,
        "packets": 0,
        "bytes": 0,
        "last_ts": 0.0,
        "rms": 0.0,
        "peak": 0.0,
        "envelope": deque(maxlen=120),
        "recording": False,
        "record_seconds": 0.0,
        "segments": 0,
        "prior_mode": None,
        "error": "",
    },
    "segments": [],
    "video_url": VIDEO_URL,
}


def log(msg):
    print(f"[sensor-bridge] {msg}", flush=True)


# ------------------------------------------------------------ DDS 层 ----
def local_ip_for(host=ROBOT_IP):
    """不发包，只让内核选路，拿到本机在该网段的地址。"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect((host, 9))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    return ""


def _is_local_ip(addr):
    if not addr:
        return False
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.bind((addr, 0))
        return True
    except OSError:
        return False
    finally:
        s.close()


_ORIG_CHANNEL_CFG = None


def _channel_cfg_without_tracing(cfg):
    """
    unitree_sdk2py 自带的 ChannelConfigHasInterface 里写死了
    <Tracing><OutputFile>/tmp/cdds.LOG</OutputFile></Tracing>。
    Windows 上没有 /tmp，CycloneDDS 会因为日志文件打不开而让
    Domain 初始化直接失败（表现为 channel factory init error）。
    这里把 Tracing 整段去掉：桥接不需要 DDS 调试日志。
    """
    return re.sub(r"<Tracing>.*?</Tracing>", "", cfg or "", flags=re.S)


def init_dds():
    """
    Windows 上 cyclonedds 会把网卡名强制 encode('ascii')，
    中文网卡（"以太网"）会直接炸 —— 所以用 address= 方式绑定，
    和项目里 scripts/g1_read_state.py 的做法保持一致。
    """
    global IFACE_ADDR, _ORIG_CHANNEL_CFG
    if SDK_PATH and SDK_PATH not in sys.path:
        sys.path.insert(0, SDK_PATH)
    try:
        import unitree_sdk2py  # noqa: F401
    except Exception as e:
        return False, f"import unitree_sdk2py 失败：{e}"

    from unitree_sdk2py.core import channel
    if _ORIG_CHANNEL_CFG is None:
        _ORIG_CHANNEL_CFG = channel.ChannelConfigHasInterface

    cfg_addr = (IFACE_ADDR or "").strip()
    if cfg_addr:
        if _is_local_ip(cfg_addr):
            addr = cfg_addr
        else:
            log(f"警告：配置的网卡地址 {cfg_addr} 不属于本机，回退到自动探测")
            addr = local_ip_for()
    else:
        addr = local_ip_for()
    if not addr:
        return False, "找不到 192.168.123.x 网段的网卡（网线插好了吗）"

    iface = IFACE_NAME
    if iface:
        try:
            iface.encode("ascii")
        except UnicodeEncodeError:
            iface = ""  # 中文名走地址绑定

    try:
        base_cfg = _channel_cfg_without_tracing(_ORIG_CHANNEL_CFG)
        if iface:
            channel.ChannelConfigHasInterface = base_cfg
            channel.ChannelFactoryInitialize(0, iface)
            used = iface
        else:
            channel.ChannelConfigHasInterface = base_cfg.replace(
                'name="$__IF_NAME__$"', f'address="{addr}"'
            )
            channel.ChannelFactoryInitialize(0, "eth0")
            used = f"address={addr}"
    except Exception as e:
        return False, f"DDS 初始化失败：{e}"

    with lock:
        state["iface"] = used
    return True, used


def on_lowstate(msg):
    now = time.time()
    with lock:
        ls = state["lowstate"]
        ls["count"] += 1
        ls["last_ts"] = now
        ls["tick"] = msg.tick
        ls["mode_pr"] = msg.mode_pr
        ls["mode_machine"] = msg.mode_machine
        imu = msg.imu_state
        state["imu"] = {
            "quat": [round(float(v), 5) for v in imu.quaternion],
            "rpy": [round(float(v), 5) for v in imu.rpy],
            "gyro": [round(float(v), 5) for v in imu.gyroscope],
            "acc": [round(float(v), 5) for v in imu.accelerometer],
            "temp": int(imu.temperature),
        }
        motors = []
        for i in range(MOTOR_COUNT):
            m = msg.motor_state[i]
            motors.append(
                {
                    "i": i,
                    "q": round(float(m.q), 4),
                    "dq": round(float(m.dq), 3),
                    "tau": round(float(m.tau_est), 2),
                    "t": int(m.temperature[0]),
                }
            )
        state["motors"] = motors
        state["remote"] = list(msg.wireless_remote)


def update_rates():
    """每 0.5 秒算一次实际频率，顺便把麦克风最近包龄算出来。"""
    last_count = {"low": 0, "video": 0, "t": time.time()}
    while True:
        time.sleep(0.5)
        now = time.time()
        dt = now - last_count["t"]
        with lock:
            ls = state["lowstate"]
            hz = (ls["count"] - last_count["low"]) / dt if dt > 0 else 0
            ls["hz"] = round(hz, 1)
            last_count["low"] = ls["count"]

            v = state["video"]
            vhz = (v["frames"] - last_count["video"]) / dt if dt > 0 else 0
            v["hz"] = round(vhz, 1)
            last_count["video"] = v["frames"]
        last_count["t"] = now


# ------------------------------------------------------------ 视频层 ----
def start_video_dds():
    """订阅机载视频话题。没有相机时这里不会有任何帧，属于正常。"""
    try:
        from unitree_sdk2py.core.channel import ChannelSubscriber
        from unitree_sdk2py.idl.unitree_go.msg.dds_ import Go2FrontVideoData_

        def cb(msg):
            blob = bytes(msg.video720p) or bytes(msg.video360p)
            if not blob:
                return
            now = time.time()
            with lock:
                v = state["video"]
                v["frames"] += 1
                v["last_ts"] = now
                if blob[:2] == b"\xff\xd8":
                    v["jpeg"] = blob
                    v["codec"] = "jpeg"
                elif blob[:4] == b"\x00\x00\x00\x01" or blob[:3] == b"\x00\x00\x01":
                    v["codec"] = "h264"
                    v["note"] = "机载视频是 H.264，浏览器不能直接放；需要机载推 MJPEG"
                else:
                    v["codec"] = "unknown"

        ChannelSubscriber("rt/frontvideostream", Go2FrontVideoData_).Init(cb, 5)
        with lock:
            state["video"]["source"] = "dds"
            state["video"]["note"] = "已订阅 rt/frontvideostream，等待相机出图"
        log("已订阅 rt/frontvideostream")
    except Exception as e:
        log(f"订阅视频话题失败：{e}")


def start_video_url(url):
    """外部视频源：MJPEG 流或 JPEG 快照。给以后接 RTSP 转 MJPEG 留的口子。"""
    import urllib.request

    # 必须绕开系统代理：本机挂着 Clash 时，urllib 会把发往 192.168.123.x 的
    # 请求也塞进代理，结果就是"流明明在推，桥这边一直 Timeout"。
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    with lock:
        state["video"]["source"] = "url"
        state["video"]["note"] = f"外部源 {url}"

    def loop():
        while True:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "shiqian/1.2"})
                with opener.open(req, timeout=5) as resp:
                    ctype = (resp.headers.get("Content-Type") or "").lower()
                    if "multipart" in ctype:
                        buf = b""
                        while True:
                            chunk = resp.read(8192)
                            if not chunk:
                                break
                            buf += chunk
                            while True:
                                start = buf.find(b"\xff\xd8")
                                end = buf.find(b"\xff\xd9", start + 2) if start >= 0 else -1
                                if start < 0 or end < 0:
                                    buf = buf[-1 << 20:] if len(buf) > (1 << 20) else buf
                                    break
                                jpg = buf[start:end + 2]
                                buf = buf[end + 2:]
                                with lock:
                                    v = state["video"]
                                    v["jpeg"] = jpg
                                    v["codec"] = "jpeg"
                                    v["frames"] += 1
                                    v["last_ts"] = time.time()
                        time.sleep(0.5)
                    else:
                        data = resp.read()
                        if data[:2] == b"\xff\xd8":
                            with lock:
                                v = state["video"]
                                v["jpeg"] = data
                                v["codec"] = "jpeg"
                                v["frames"] += 1
                                v["last_ts"] = time.time()
                        time.sleep(0.25)
            except Exception as e:
                with lock:
                    state["video"]["note"] = f"外部视频源连不上：{type(e).__name__}"
                time.sleep(2.0)

    threading.Thread(target=loop, name="video-url", daemon=True).start()


# ------------------------------------------------------------ 离线识别 ----
# 用 vosk 的小型中文模型，把切出来的每句话转成文字，供「自动接待」直接用。
# 模型不在 / vosk 没装 都不影响麦克风和切片，只是 text 一直为空。
VOSK_MODEL = (os.environ.get("SHIQIAN_VOSK_MODEL")
              or os.path.join(BASE_DIR, "models", "vosk-model-small-cn-0.22"))
_asr = {"model": None, "ready": False, "loading": False,
        "error": "", "path": VOSK_MODEL}
_asr_lock = threading.Lock()


def _ascii_model_dir(src):
    r"""
    vosk 的 C++ 层在 Windows 上打不开带中文的绝对路径（报 "does not contain model files"），
    所以模型在中文目录里时，先复制一份到 %LOCALAPPDATA%\G1\ 下再用。
    """
    try:
        src.encode("ascii")
        return src
    except UnicodeEncodeError:
        pass
    base = os.path.join(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir(),
                        "G1", os.path.basename(src.rstrip("\\/")))
    if not os.path.isdir(base):
        try:
            import shutil
            os.makedirs(os.path.dirname(base), exist_ok=True)
            shutil.copytree(src, base)
        except Exception as e:  # noqa: BLE001
            log(f"模型复制到 ASCII 路径失败：{e}")
            return src
    return base


def asr_load():
    with _asr_lock:
        if _asr["ready"] or _asr["loading"]:
            return
        _asr["loading"] = True
    try:
        from vosk import Model, SetLogLevel

        SetLogLevel(-1)
        if not os.path.isdir(VOSK_MODEL):
            raise RuntimeError(f"没找到模型目录 {VOSK_MODEL}")
        _asr["model"] = Model(_ascii_model_dir(VOSK_MODEL))
        _asr["ready"] = True
        log(f"离线识别就绪：{os.path.basename(VOSK_MODEL)}")
    except Exception as e:  # noqa: BLE001
        _asr["error"] = f"{type(e).__name__}: {e}"
        log(f"离线识别不可用：{_asr['error']}")
    finally:
        _asr["loading"] = False


def asr_transcribe(path):
    """wav 文件 -> 文本（中文不带空格）。失败返回空串。"""
    if not _asr["ready"]:
        asr_load()
    if not _asr["ready"]:
        return ""
    try:
        from vosk import KaldiRecognizer

        with wave.open(path, "rb") as w:
            frames = w.readframes(w.getnframes())
        rec = KaldiRecognizer(_asr["model"], SAMPLE_RATE)
        rec.AcceptWaveform(frames)
        text = json.loads(rec.FinalResult()).get("text") or ""
        return text.replace(" ", "").strip()
    except Exception as e:  # noqa: BLE001
        _asr["error"] = f"{type(e).__name__}: {e}"
        log(f"识别失败：{_asr['error']}")
        return ""


# ------------------------------------------------------------ 麦克风 ----
class MicWorker:
    """
    一条线程干三件事：
      1) 打开 voice 服务的麦克风推流（mode=1）并加入 UDP 组播；
      2) 实时算电平/波形给页面；
      3) auto=True 时用 VAD 自动切句，每句落一个 wav。

    注意：mode 本来就是 1 的时候不再重复 set_mode —— 那会让机器人
    把"唤醒模式"再播一遍，现场听着像故障。
    """

    def __init__(self):
        self.thread = None
        self.stop_evt = threading.Event()
        self.sock = None
        self.frames = bytearray()
        self.voice = None
        self.auto = True
        self._reset_vad()
        self._seg_seq = 0

    def _reset_vad(self):
        self._pre = deque(maxlen=VAD_PREROLL)
        self._in_speech = False
        self._silence_run = 0
        self._speech_run = 0
        self._seg = []
        self._seg_start = 0.0
        self._noise = 0.006

    def _voice_client(self):
        """voice 服务的裸 RPC 客户端：1001=TTS 1007=GET_MODE 1008=SET_MODE"""
        if self.voice is not None:
            return self.voice
        from unitree_sdk2py.rpc.client import Client

        c = Client("voice", False)
        c.SetTimeout(5.0)
        c._SetApiVerson("1.0.0.0")
        for api_id in range(1001, 1020):
            c._RegistApi(api_id, 0)
        self.voice = c
        return c

    def tts(self, text, speaker_id=0):
        """用机器人自己的喇叭念一句话（API 1001）。"""
        c = self._voice_client()
        index = int(time.time() * 10) % 1000000
        code, _ = c._Call(
            1001, json.dumps({"index": index, "text": text, "speaker_id": speaker_id})
        )
        if code == 0:
            log(f"TTS 已下发：{text[:30]}{'…' if len(text) > 30 else ''}")
        return code

    def start(self, auto=True):
        with lock:
            if state["mic"]["active"]:
                self.auto = bool(auto)
                state["mic"]["auto"] = self.auto
                return True, "麦克风已在采集"
            state["mic"].update({
                "error": "", "packets": 0, "bytes": 0, "auto": bool(auto),
                "active": True, "segments": 0,
            })
            state["mic"]["envelope"].clear()
        self.auto = bool(auto)
        self._reset_vad()
        self.stop_evt.clear()
        self.frames = bytearray()
        self.thread = threading.Thread(target=self._run, name="mic", daemon=True)
        self.thread.start()
        return True, "麦克风已打开" + ("（自动切句）" if self.auto else "（整段录音）")

    def stop(self):
        self.stop_evt.set()
        t = self.thread
        if t is not None:
            t.join(timeout=2.5)
        self.thread = None
        return True, "麦克风已关闭"

    def _run(self):
        prev_mode = None
        changed = False
        try:
            voice = self._voice_client()
            code, data = voice._Call(1007, "{}")
            if code == 0 and data:
                try:
                    prev_mode = json.loads(data).get("mode")
                except Exception:
                    prev_mode = None

            if prev_mode == 1:
                # 已经是"麦克风+ASR 打开"的状态：直接收，不要再触发一次唤醒播报
                with lock:
                    state["mic"]["mode"] = 1
                    state["mic"]["prior_mode"] = 1
                log("voice 已是 mode=1，跳过 set_mode（避免重复播报唤醒提示）")
            else:
                voice._Call(1008, json.dumps({"mode": 1}))
                changed = True
                with lock:
                    state["mic"]["mode"] = 1
                    state["mic"]["prior_mode"] = prev_mode
                log(f"voice mode -> 1（原值 {prev_mode}）")
                time.sleep(0.8)

            addr = IFACE_ADDR or local_ip_for()
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 262144)
            sock.bind((addr, MCAST_PORT))
            mreq = struct.pack(
                "4s4s", socket.inet_aton(MCAST_GROUP), socket.inet_aton(addr)
            )
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            sock.settimeout(0.5)
            self.sock = sock
            log(f"已加入组播 {MCAST_GROUP}:{MCAST_PORT} @ {addr}")

            while not self.stop_evt.is_set():
                try:
                    data, _ = sock.recvfrom(8192)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if data:
                    self._handle(data)
        except Exception as e:
            with lock:
                state["mic"]["error"] = f"{type(e).__name__}: {e}"
            log(f"麦克风线程异常：{e}")
        finally:
            try:
                if self.sock:
                    self.sock.close()
            except Exception:
                pass
            self.sock = None
            if self._in_speech and self._seg:
                self._emit_segment()          # 收工时把没说完的半句也留下
            if changed:
                try:
                    restore = prev_mode if prev_mode in (1, 2) else 1
                    self._voice_client()._Call(1008, json.dumps({"mode": restore}))
                    log(f"voice mode 还原为 {restore}")
                except Exception as e:
                    log(f"还原 voice mode 失败：{e}")
            with lock:
                m = state["mic"]
                m["active"] = False
                m["mode"] = None
                m["rms"] = 0.0
                m["peak"] = 0.0
            if self.frames:
                save_wav(bytes(self.frames))

    # ---------------- 每个 160ms 包 ----------------
    def _handle(self, data):
        now = time.time()
        n = len(data) // SAMPLE_WIDTH
        if not n:
            return
        samples = struct.unpack("<%dh" % n, data[: n * SAMPLE_WIDTH])
        peak = 0
        total = 0.0
        step = max(1, n // 400)
        cnt = 0
        for v in samples[::step]:
            total += v * v
            if abs(v) > peak:
                peak = abs(v)
            cnt += 1
        rms = (total / cnt) ** 0.5 / 32768.0 if cnt else 0.0

        with lock:
            m = state["mic"]
            m["packets"] += 1
            m["bytes"] += len(data)
            m["last_ts"] = now
            m["rms"] = round(rms, 4)
            m["peak"] = round(peak / 32768.0, 4)
            m["envelope"].append(round(rms, 4))
            if len(self.frames) < MAX_WAV_BYTES:
                self.frames.extend(data)
                m["record_seconds"] = round(
                    len(self.frames) / (SAMPLE_RATE * SAMPLE_WIDTH), 1
                )
            m["recording"] = len(self.frames) < MAX_WAV_BYTES

        if self.auto:
            self._vad(samples, now)

    # ---------------- 自动切句 ----------------
    def _vad(self, samples, now):
        thr_floor = VAD_ABS_FLOOR
        for i in range(0, len(samples) - VAD_FRAME + 1, VAD_FRAME):
            frame = samples[i: i + VAD_FRAME]
            energy = sum(v * v for v in frame) / len(frame)
            rms = (energy ** 0.5) / 32768.0

            if not self._in_speech:
                # 底噪慢速跟随：只在"非人声"时更新，避免把人声算进噪声
                self._noise = 0.97 * self._noise + 0.03 * rms
                thr = max(self._noise * VAD_THRESHOLD_RATIO, thr_floor)
                self._pre.append(frame)
                if rms > thr:
                    self._speech_run += 1
                    if self._speech_run >= VAD_START_FRAMES:
                        self._in_speech = True
                        self._silence_run = 0
                        self._seg = [f for pf in self._pre for f in pf]
                        self._seg_start = now - (len(self._seg) / SAMPLE_RATE)
                else:
                    self._speech_run = 0
            else:
                self._seg.extend(frame)
                thr = max(self._noise * VAD_THRESHOLD_RATIO, thr_floor)
                if rms < thr:
                    self._silence_run += 1
                else:
                    self._silence_run = 0
                too_long = (len(self._seg) / SAMPLE_RATE) >= VAD_MAX_SEC
                if self._silence_run >= VAD_END_FRAMES or too_long:
                    self._emit_segment()

    def _emit_segment(self):
        frames = self._seg
        self._seg = []
        self._in_speech = False
        self._silence_run = 0
        self._speech_run = 0
        self._pre.clear()
        dur = len(frames) / SAMPLE_RATE
        if dur < VAD_MIN_SEC:
            return
        try:
            os.makedirs(SEG_DIR, exist_ok=True)
            self._seg_seq += 1
            stamp = time.strftime("%H%M%S")
            seg_id = f"{stamp}_{int(time.time() * 1000) % 1000:03d}_{self._seg_seq % 1000:03d}"
            path = os.path.join(SEG_DIR, seg_id + ".wav")
            pcm = struct.pack("<%dh" % len(frames), *frames)
            with wave.open(path, "wb") as w:
                w.setnchannels(CHANNELS)
                w.setsampwidth(SAMPLE_WIDTH)
                w.setframerate(SAMPLE_RATE)
                w.writeframes(pcm)
            peak = max((abs(v) for v in frames[::7]), default=0) / 32768.0
            meta = {
                "id": seg_id,
                "ts": time.time(),
                "time": time.strftime("%H:%M:%S", time.localtime(self._seg_start or time.time())),
                "dur": round(dur, 2),
                "peak": round(peak, 3),
                "bytes": len(pcm),
                "text": "",
            }
            with lock:
                segs = state["segments"]
                segs.append(meta)
                state["mic"]["segments"] = len(segs)
                while len(segs) > SEG_MAX:
                    old = segs.pop(0)
                    try:
                        os.remove(os.path.join(SEG_DIR, old["id"] + ".wav"))
                    except OSError:
                        pass
            log(f"切出一句：{dur:.2f}s  peak={peak:.2f}")
            # 识别放到后台线程：模型首次加载 ~1 秒，别卡住收音
            threading.Thread(target=self._fill_text, args=(meta, path),
                             name="asr", daemon=True).start()
        except Exception as e:
            log(f"切句保存失败：{e}")

    def _fill_text(self, meta, path):
        try:
            text = asr_transcribe(path)
            if text:
                meta["text"] = text
                log(f"识别：{text}")
        except Exception as e:  # noqa: BLE001
            log(f"识别线程异常：{e}")


def save_wav(pcm):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = WAV_PATH + ".tmp"
        with wave.open(tmp, "wb") as w:
            w.setnchannels(CHANNELS)
            w.setsampwidth(SAMPLE_WIDTH)
            w.setframerate(SAMPLE_RATE)
            w.writeframes(pcm)
        os.replace(tmp, WAV_PATH)
        log(f"录音已保存：{WAV_PATH}（{len(pcm) / 32000:.1f}s）")
    except Exception as e:
        log(f"保存 wav 失败：{e}")


mic = MicWorker()


# ------------------------------------------------------------ HTTP 层 ----
def snapshot():
    now = time.time()
    with lock:
        ls = dict(state["lowstate"])
        m = state["mic"]
        v = state["video"]
        audio = list(state["audio_msgs"])
        snap = {
            "ok": True,
            "ts": now,
            "started_at": state["started_at"],
            "dds": {
                "ready": state["dds_ready"],
                "error": state["dds_error"],
                "iface": state["iface"],
            },
            "link": {
                "lowstate_hz": ls["hz"],
                "lowstate_age_ms": round((now - ls["last_ts"]) * 1000) if ls["last_ts"] else None,
                "tick": ls["tick"],
                "mode_pr": ls["mode_pr"],
                "mode_machine": ls["mode_machine"],
            },
            "imu": state["imu"],
            "motors": state["motors"],
            "remote_hex": " ".join(f"{b:02x}" for b in state["remote"][:20]),
            "audio": audio[-3:],
            "mic": {
                "active": m["active"],
                "auto": m.get("auto", False),
                "mode": m["mode"],
                "packets": m["packets"],
                "rms": m["rms"],
                "peak": m["peak"],
                "envelope": list(m["envelope"]),
                "recording": m["recording"],
                "record_seconds": m["record_seconds"],
                "segments": len(state["segments"]),
                "age_ms": round((now - m["last_ts"]) * 1000) if m["last_ts"] else None,
                "error": m["error"],
                "asr": {"ready": _asr["ready"], "error": _asr["error"],
                        "model": os.path.basename(VOSK_MODEL)},
            },
            "segments": list(state["segments"])[-20:],
            "video": {
                "source": v["source"],
                "frames": v["frames"],
                "hz": v["hz"],
                "codec": v["codec"],
                "age_ms": round((now - v["last_ts"]) * 1000) if v["last_ts"] else None,
                "has_frame": bool(v["jpeg"]),
                "note": v["note"],
            },
            "sensors": sensor_inventory(now),
        }
    return snap


def sensor_inventory(now):
    with lock:
        ls = state["lowstate"]
        imu = state["imu"]
        m = state["mic"]
        v = state["video"]
        audio = list(state["audio_msgs"])
    items = [
        {
            "name": "本体 IMU",
            "ok": bool(imu) and (now - (ls["last_ts"] or 0)) < 1.0,
            "detail": "四元数 / rpy / 陀螺 / 加速度 / 温度 · 躯干",
        },
        {
            "name": "关节编码器",
            "ok": len(state["motors"]) > 0 and (now - (ls["last_ts"] or 0)) < 1.0,
            "detail": f"{MOTOR_COUNT} 路 q/dq/tau_est/温度",
        },
        {
            "name": "遥控器",
            "ok": any(state["remote"]),
            "detail": "wireless_remote 原始字节",
        },
        {
            "name": "麦克风阵列",
            "ok": m["active"] and (now - (m["last_ts"] or 0)) < 1.5,
            "detail": "4 麦 · 16k 单声道 · UDP 组播",
        },
        {
            "name": "语音服务",
            "ok": bool(audio),
            "detail": "rt/audio_msg（播放状态/识别结果）",
        },
        {
            "name": "头部相机",
            "ok": v["frames"] > 0 and (now - (v["last_ts"] or 0)) < 3.0,
            "detail": "rt/frontvideostream",
        },
        {
            "name": "运动状态",
            "ok": False,
            "detail": "rt/sportmodestate 未发布（当前为 mocap/sim2real 模式）",
        },
        {
            "name": "3D 激光雷达",
            "ok": False,
            "detail": "rt/lidar_state 无数据",
        },
        {
            "name": "电池 BMS",
            "ok": False,
            "detail": "rt/bms_state 无数据",
        },
        {
            "name": "灵巧手触觉",
            "ok": False,
            "detail": "rt/dex3/*/state 无数据（未装 Dex3）",
        },
    ]
    return items


class Handler(BaseHTTPRequestHandler):
    server_version = "ShiqianSensorBridge/1.0"

    def log_message(self, fmt, *args):  # 静音，避免刷屏
        return

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST")
        self.end_headers()

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/mic/start":
            auto = True
            if self.headers.get("Content-Length"):
                try:
                    body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
                    auto = bool(body.get("auto", True))
                except Exception:
                    auto = True
            ok, msg = mic.start(auto=auto)
            return self._json({"ok": ok, "msg": msg, "auto": auto})
        if path == "/mic/stop":
            ok, msg = mic.stop()
            return self._json({"ok": ok, "msg": msg})
        if path == "/segments/clear":
            with lock:
                for s in state["segments"]:
                    try:
                        os.remove(os.path.join(SEG_DIR, s["id"] + ".wav"))
                    except OSError:
                        pass
                state["segments"].clear()
                state["mic"]["segments"] = 0
            return self._json({"ok": True, "msg": "片段已清空"})
        if path == "/tts":
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}") if n else {}
            except Exception:
                return self._json({"ok": False, "msg": "请求体不是合法 JSON"}, 400)
            text = str(body.get("text") or "").strip()
            if not text:
                return self._json({"ok": False, "msg": "内容不能为空"}, 400)
            if len(text) > 200:
                return self._json({"ok": False, "msg": "太长，控制在 200 字内"}, 400)
            try:
                code = mic.tts(text, int(body.get("speaker_id") or 0))
            except Exception as e:
                return self._json({"ok": False, "msg": f"播报失败：{e}"}, 500)
            if code == 0:
                return self._json({"ok": True, "msg": "已交给机器人喇叭播报"})
            return self._json({"ok": False, "msg": f"voice 返回错误码 {code}"}, 502)
        if path == "/video/reconnect":
            return self._json({"ok": True, "msg": "重连已触发"})
        return self._json({"ok": False, "msg": "not found"}, 404)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/health":
            return self._json(snapshot())
        if path == "/state":
            return self._json(snapshot())
        if path == "/segments":
            with lock:
                segs = list(state["segments"])
            return self._json({"ok": True, "data": segs})
        if path.startswith("/segments/") and path.endswith(".wav"):
            seg_id = os.path.basename(path[len("/segments/"): -4])
            fpath = os.path.join(SEG_DIR, seg_id + ".wav")
            if not os.path.exists(fpath):
                return self._json({"ok": False, "msg": "片段不存在"}, 404)
            data = open(fpath, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/mic.wav":
            if not os.path.exists(WAV_PATH):
                return self._json({"ok": False, "msg": "还没有录音"}, 404)
            data = open(WAV_PATH, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Content-Disposition", 'attachment; filename="g1_mic.wav"')
            self.end_headers()
            self.wfile.write(data)
            return
        if path == "/video/frame.jpg":
            with lock:
                jpg = state["video"]["jpeg"]
            if not jpg:
                return self._json({"ok": False, "msg": "暂无画面"}, 404)
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(jpg)))
            self.end_headers()
            self.wfile.write(jpg)
            return
        if path == "/video.mjpg":
            return self._mjpeg()
        return self._json({"ok": False, "msg": "not found"}, 404)

    def _mjpeg(self):
        boundary = "shiqianframe"
        self.send_response(200)
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={boundary}")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        last_sent = 0.0
        try:
            while True:
                with lock:
                    jpg = state["video"]["jpeg"]
                    ts = state["video"]["last_ts"]
                if jpg and ts != last_sent:
                    last_sent = ts
                    self.wfile.write(
                        f"--{boundary}\r\nContent-Type: image/jpeg\r\n"
                        f"Content-Length: {len(jpg)}\r\n\r\n".encode()
                    )
                    self.wfile.write(jpg)
                    self.wfile.write(b"\r\n")
                time.sleep(0.08)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


def main():
    parser = argparse.ArgumentParser(description="G1 传感器桥接")
    parser.add_argument("--port", type=int, default=BRIDGE_PORT)
    parser.add_argument("--host", default=BRIDGE_HOST)
    args = parser.parse_args()

    log(f"启动：http://{args.host}:{args.port}")
    # 识别模型预热：切第一句时才加载会慢 0.3~1s，页面还会误显示"未就绪"
    threading.Thread(target=asr_load, name="asr-preload", daemon=True).start()
    ok, info = init_dds()
    with lock:
        state["dds_ready"] = ok
        state["dds_error"] = "" if ok else str(info)
    if ok:
        log(f"DDS 就绪（{info}）")
        try:
            from unitree_sdk2py.core.channel import ChannelSubscriber
            from unitree_sdk2py.idl.unitree_hg.msg.dds_ import LowState_
            from unitree_sdk2py.idl.std_msgs.msg.dds_ import String_

            ChannelSubscriber("rt/lowstate", LowState_).Init(on_lowstate, 10)

            def on_audio(msg):
                text = msg.data
                with lock:
                    state["audio_msgs"].append(text[:200])

            ChannelSubscriber("rt/audio_msg", String_).Init(on_audio, 10)
            log("已订阅 rt/lowstate 与 rt/audio_msg")
            start_video_dds()
        except Exception as e:
            log(f"订阅失败：{e}")
            with lock:
                state["dds_error"] = f"订阅失败：{e}"
    else:
        log(f"DDS 不可用：{info}")

    if VIDEO_URL:
        start_video_url(VIDEO_URL)

    threading.Thread(target=update_rates, name="rates", daemon=True).start()

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("收到 Ctrl+C，退出")
    finally:
        try:
            mic.stop()
        except Exception:
            pass


if __name__ == "__main__":
    main()
