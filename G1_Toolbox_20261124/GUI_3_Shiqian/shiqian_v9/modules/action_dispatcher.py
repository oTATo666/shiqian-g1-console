"""
action_dispatcher.py —— 动作执行与光影雕塑接口 · v5 加固版
==========================================================

【这个文件干什么？】
    大模型选出了动作编号 "F"  ->  这里把信号发给 G1 机器人 / 光影控制端
    机器人播放预先录制好的大关节定格 Pose，灯光同步变化。

【接口形态（三选一，改 config.yaml 的 action.transport 即可）】
    http   （默认）轻量 HTTP POST：http://localhost:8080/trigger_action/F
            —— 机器人控制端只要是个 FastAPI/Flask 就能接，最简单，推荐
    ws     WebSocket：ws://127.0.0.1:8080/ws/actions
            —— 适合"连上就一直推指令"的场景，需要 pip install websocket-client
    redis  Redis 队列：rpush g1:actions {...}
            —— 适合机器人端和控制端不在同一台电脑、需要可靠排队的场景

【v5 相比 v3 的改进】
1. 动作编号严格校验 A-Z：以前如果把 action_id 拼进 URL 而不校验，
   一个形如 "../../admin" 的字符串就能打到别的接口上（URL 注入）。
   现在不是单个大写字母直接拒发。
2. 失败自动重试（config: action.retries），仍失败就**写进离线队列**，
   下次机器人开机时自动补发 —— 现场断网、机器人重启都不会丢掉同学的动作。
3. 每条指令带 request_id（幂等号），机器人端可以据此去重，
   避免网络抖动导致同一句"挥手"被执行两遍。
4. 带 health() 探测：启动时就能告诉你"机器人控制端到底通没通"，
   而不是等到第一句话说完才发现。
5. 返回结构化结果而不是 True/False，主程序可以据此提示"已排队，稍后补发"。
"""

import os
import re
import json
import time
import uuid
import threading
from datetime import datetime

try:
    import requests
except Exception:
    requests = None

from modules.security_vault import SecurityVault
from modules.atomic_csv import csv_transaction


class ActionDispatcher:
    """把动作编号安全、可靠地送到机器人 / 光影控制端。"""

    # 只允许单个大写字母：既是动作编号规范，也是最基础的"防路径注入"手段
    _ACTION_PATTERN = re.compile(r"^[A-Z]$")

    def __init__(self, vault: SecurityVault, cfg: dict = None):
        self.vault = vault
        self.cfg = cfg or {}
        self.transport = (self.cfg.get("transport") or "http").strip().lower()
        self.retries = max(0, int(self.cfg.get("retries", 1)))
        self.retry_interval = float(self.cfg.get("retry_interval", 0.3))
        self.timeout = float(self.cfg.get("timeout", 3.0))
        self.queue_path = self.cfg.get("queue_path", "data/action_queue.jsonl")
        self.queue_max = int(self.cfg.get("queue_max", 200))

        self._stats = {"sent": 0, "queued": 0, "rejected": 0, "replayed": 0}
        self._last_error = ""
        self._last_action = ""
        self._lock = threading.Lock()

    # ==================================================================
    # 一、对外主入口
    # ==================================================================
    def trigger(self, action_id: str, watermarked_memory: str = "",
                action_name: str = "") -> dict:
        """
        下发一个动作。永远不抛异常（返回 dict 说明结果），
        因为"动作没发出去"不应该让出票流程中断。

        返回示例：
            {"ok": True,  "action_id": "F", "transport": "http",  "queued": False}
            {"ok": False, "action_id": "F", "queued": True, "error": "连接失败"}
        """
        action_id = (action_id or "").strip().upper()

        # ---------- 第 0 步：严格校验 ----------
        if not self._ACTION_PATTERN.match(action_id):
            self._stats["rejected"] += 1
            self._last_error = f"非法动作编号：{action_id!r}"
            print(f"🤖 [动作控制] 拒绝下发：动作编号必须是 A-Z 单个字母，收到 {action_id!r}")
            return {"ok": False, "action_id": action_id, "queued": False,
                    "error": self._last_error}

        payload = self._build_payload(action_id, watermarked_memory, action_name)

        # ---------- 第 1 步：按配置的通道发送，失败重试 ----------
        last_error = ""
        for attempt in range(self.retries + 1):
            try:
                self._send(payload)
                with self._lock:
                    self._stats["sent"] += 1
                    self._last_action = action_id
                print(f"🤖 [动作控制] 已下发动作 [{action_id}] "
                      f"{action_name or ''} （通道：{self.transport}）")
                # 顺手补发离线队列，保证"上一轮没发出去的"也能补上
                self.flush_queue()
                return {"ok": True, "action_id": action_id, "transport": self.transport,
                        "queued": False, "request_id": payload["request_id"]}
            except Exception as e:
                last_error = str(e)
                if attempt < self.retries:
                    time.sleep(self.retry_interval)

        # ---------- 第 2 步：还是失败 -> 进离线队列 ----------
        self._last_error = last_error
        queued = self._enqueue(payload, last_error)
        with self._lock:
            if queued:
                self._stats["queued"] += 1
        if queued:
            print(f"🤖 [动作控制] 控制端暂时连不上（{last_error}），"
                  f"动作 [{action_id}] 已排队，等它上线自动补发")
        else:
            print(f"🤖 [动作控制] 控制端连不上且队列已满，动作 [{action_id}] 未发送")
        return {"ok": False, "action_id": action_id, "transport": self.transport,
                "queued": queued, "error": last_error, "request_id": payload["request_id"]}

    def _build_payload(self, action_id: str, memory: str, action_name: str) -> dict:
        """组装统一格式的指令包（三个通道共用同一份数据结构）。"""
        return {
            "action_id": action_id,
            "pose": f"Pose-{action_id}",
            "action_name": action_name or f"Pose-{action_id}",
            # 带零宽水印的记忆点：机器人端/日志里肉眼看不出来，但可溯源
            "memory": memory or "",
            "request_id": uuid.uuid4().hex,     # 幂等号：机器人端可据此去重
            "ts": datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
            "source": "shiqian-v5",
        }

    # ==================================================================
    # 二、三种通道的具体实现
    # ==================================================================
    def _send(self, payload: dict):
        """按 transport 选择发送方式；失败统一抛异常，由上层重试/排队。"""
        if self.transport == "ws":
            self._send_ws(payload)
        elif self.transport == "redis":
            self._send_redis(payload)
        else:
            self._send_http(payload)

    def _endpoint(self) -> str:
        """从加密保险箱读出动作接口地址（属于底层接口，不放普通配置文件）。"""
        payload = self.vault.get_runtime_model_payload()
        return payload.get("action_endpoint", "http://localhost:8080/trigger_action/{action_id}")

    def _send_http(self, payload: dict):
        if requests is None:
            raise RuntimeError("缺少 requests 库")
        template = self._endpoint()
        url = template.format(action_id=payload["action_id"]) if "{action_id}" in template \
            else template.rstrip("/") + "/" + payload["action_id"]
        try:
            resp = requests.post(url, json=payload, timeout=self.timeout)
        except Exception as e:
            # requests 原始异常信息又长又难懂，翻译成同学看得懂的一句话
            raise RuntimeError(self._friendly_net_error(e))
        if resp.status_code >= 400:
            raise RuntimeError(f"控制端返回 HTTP {resp.status_code}")

    @staticmethod
    def _friendly_net_error(e: Exception) -> str:
        """把底层网络异常翻译成人类语言（保留类型名方便排查）"""
        name = type(e).__name__
        text = str(e)
        if "Connection refused" in text or "积极拒绝" in text or "10061" in text:
            return "连接被拒绝（机器人控制端没启动？）"
        if "timed out" in text or "Timeout" in name:
            return "连接超时（控制端没响应）"
        if "Name or service" in text or "getaddrinfo" in text:
            return "地址解析失败（接口地址写错了？）"
        return name

    def _send_ws(self, payload: dict):
        try:
            import websocket          # pip install websocket-client（可选依赖）
        except Exception:
            raise RuntimeError("未安装 websocket-client，无法使用 WS 通道")
        ws_url = self.cfg.get("ws_url", "ws://127.0.0.1:8080/ws/actions")
        conn = websocket.create_connection(ws_url, timeout=self.timeout)
        try:
            conn.send(json.dumps(payload, ensure_ascii=False))
        finally:
            conn.close()

    def _send_redis(self, payload: dict):
        try:
            import redis               # pip install redis（可选依赖）
        except Exception:
            raise RuntimeError("未安装 redis 库，无法使用 Redis 通道")
        client = redis.Redis.from_url(
            self.cfg.get("redis_url", "redis://127.0.0.1:6379/0"), socket_timeout=self.timeout
        )
        client.rpush(self.cfg.get("redis_key", "g1:actions"),
                     json.dumps(payload, ensure_ascii=False))

    # ==================================================================
    # 三、离线队列（断网/机器人未开机时保命）
    # ==================================================================
    def _enqueue(self, payload: dict, reason: str) -> bool:
        """把没发出去的指令追加到 JsonL 队列文件（带文件锁，防并发写坏）。"""
        try:
            folder = os.path.dirname(os.path.abspath(self.queue_path))
            os.makedirs(folder, exist_ok=True)
            line = json.dumps({**payload, "queued_reason": reason}, ensure_ascii=False)
            with csv_transaction(self.queue_path):
                # 队列太长就别再堆了，避免磁盘被塞满
                if self._queue_size_unlocked() >= self.queue_max:
                    return False
                with open(self.queue_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            return True
        except Exception as e:
            print(f"[动作控制] 写入离线队列失败：{e}")
            return False

    def _queue_size_unlocked(self) -> int:
        try:
            with open(self.queue_path, "r", encoding="utf-8") as f:
                return sum(1 for _ in f)
        except FileNotFoundError:
            return 0

    def queue_size(self) -> int:
        with csv_transaction(self.queue_path):
            return self._queue_size_unlocked()

    def flush_queue(self) -> int:
        """
        尝试补发队列里所有积压的动作，返回补发成功的条数。
        什么时候调用？
            · 每次成功下发之后（顺手补）
            · 程序启动时（上次没发完的）
        """
        try:
            with csv_transaction(self.queue_path):
                if not os.path.exists(self.queue_path):
                    return 0
                with open(self.queue_path, "r", encoding="utf-8") as f:
                    lines = [ln for ln in f.read().splitlines() if ln.strip()]
                if not lines:
                    return 0

                remaining, sent = [], 0
                for index, line in enumerate(lines):
                    try:
                        payload = json.loads(line)
                    except Exception:
                        continue                    # 坏行直接丢弃，别把队列卡死
                    try:
                        self._send(payload)
                        sent += 1
                    except Exception:
                        # 这条不通，后面的也大概率不通：整条保留 + 后面全部保留
                        remaining.extend(lines[index:])
                        break

                # 原子重写队列：只保留没发成功的
                tmp = self.queue_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    for line in remaining:
                        f.write(line + "\n")
                os.replace(tmp, self.queue_path)
                if sent:
                    self._stats["replayed"] += sent
                    print(f"[动作控制] 已补发 {sent} 条积压动作")
                return sent
        except Exception as e:
            print(f"[动作控制] 补发队列失败：{e}")
            return 0

    # ==================================================================
    # 四、自检
    # ==================================================================
    def health(self) -> dict:
        """探测机器人/光影控制端是否在线（启动自检、tools/selfcheck.py 都会用）。"""
        info = {
            "transport": self.transport,
            "endpoint": "",
            "reachable": False,
            "queue": 0,
            "error": "",
        }
        try:
            template = self._endpoint()
            info["endpoint"] = template
            if self.transport == "http":
                origin = re.match(r"^(https?://[^/]+)", template)
                if origin and requests is not None:
                    base = origin.group(1)
                    for probe in (base + "/health", base + "/"):
                        try:
                            resp = requests.get(probe, timeout=1.5)
                            info["reachable"] = resp.status_code < 500
                            if info["reachable"]:
                                break
                        except Exception:
                            continue
            else:
                info["reachable"] = None   # WS / Redis 通道不做主动探测
        except Exception as e:
            info["error"] = str(e)

        try:
            info["queue"] = self.queue_size()
        except Exception:
            pass
        return info

    def stats(self) -> dict:
        with self._lock:
            return dict(self._stats, last_error=self._last_error, last_action=self._last_action)

    def preflight(self):
        """启动时打印一行人类可读的状态。"""
        info = self.health()
        flag = "✅" if info.get("reachable") else ("⚠️" if info.get("reachable") is None else "❌")
        line = (f"[动作自检] {flag} 通道={info['transport']} 接口={info['endpoint']}"
                f"  离线队列={info['queue']} 条")
        print(line)
        return info
