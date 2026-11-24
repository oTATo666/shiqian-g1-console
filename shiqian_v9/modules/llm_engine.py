"""
llm_engine.py —— 大模型推理与动作映射模块 · v5 加固版
======================================================

【这个文件干什么？】
    把同学说的一句话  ->  让大模型提炼成 8~15 字的"记忆点" + 选一个动作 A~Z
    然后返回 {"memory": "……", "action_id": "F"} 给主程序继续用。

【双通道（容灾）设计】
    ① 本地 Qwen-8B（Ollama / vLLM 的 OpenAI 兼容接口，默认 http://127.0.0.1:11434/v1）
       —— 断网可用、不花钱、隐私不出校门
    ② 云端 API（DeepSeek 等，只要兼容 OpenAI 格式）
       —— 本地没启动 / 超时 / 报错时自动降级，保证现场演示不会卡住

【密钥去哪了？】
    云端 API Key 和系统 Prompt **只存在 Fernet 加密的保险箱**（data/.model_vault.enc）里，
    任何配置文件、日志、终端输出都不会出现明文。
    要填 Key，请用后台工具：
        python tools/set_secret.py --cloud-key
    或者设置环境变量后让程序自动导入（也不会落盘）：
        set SHIQIAN_CLOUD_API_KEY=sk-xxxx   （Windows）
    详见 README 第五部分。

【v5 相比 v3 的改进（都是真机上会踩的坑）】
1. 不再依赖 openai 这个包：直接用 requests 发标准 HTTP 请求。
   Ollama、vLLM、DeepSeek、各种中转站都是同一套 /chat/completions 协议，
   少装一个包、少一层版本冲突，出问题时也能一眼看到原始返回。
2. 本地模型熔断：本地没开机的时候，以前每一轮都要先干等 8 秒超时。
   现在探测一次不通就直接走云端，并在 30 秒内不再重试本地（llm.local_retry_seconds 可调）。
3. JSON 容错：模型偶尔会输出 ```json 代码块、中文冒号、多一句"好的"，
   现在会先剥壳再解析，解析失败还会用正则兜底捞 memory / action_id。
4. 动作白名单：只接受现场真的录过的 Pose（config.yaml -> actions.allowed），
   模型胡诌一个 "Z" 也不会让机器人做不存在的动作。
5. 记忆点规范化：去引号、去空白、去"记忆点："前缀，超过 15 字自动截断。
6. 审计不记用户原话：只记"用了哪条通道、选了哪个动作、耗时多少"，
   避免把同学说的私事写进日志。
"""

import re
import json
import time

try:
    import requests
except Exception:                       # 理论上必装，但保证 import 不崩
    requests = None

from modules.security_vault import SecurityVault


class LLMError(Exception):
    """推理通道不可用（超时、连不上、返回格式不对……）"""


class DualLLMRouter:
    """双通道大模型路由器：本地优先，云端兜底，都挂了用内置模板。"""

    # 全部 26 个预设姿态（A-Z）。实际可用的由 config.yaml 的 actions.allowed 再收窄。
    ALL_ACTIONS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    # 离线兜底金句（按用户输入做哈希挑选，避免每次都是同一句）
    OFFLINE_MEMORIES = [
        "把此刻的心事折成纸飞机",
        "愿所有努力都不被辜负",
        "夏天的风会记得你的名字",
        "把平凡的日子过成诗",
        "热烈的青春不需要解释",
        "今天也要好好爱自己",
        "所有的相遇都是久别重逢",
        "一步一步也能走到山顶",
    ]

    def __init__(self, vault: SecurityVault, mode: str = "auto",
                 cfg: dict = None, actions: dict = None):
        self.vault = vault
        self.mode = (mode or "auto").strip().lower()
        self.cfg = cfg or {}
        actions = actions or {}

        # ---------- 动作白名单 ----------
        allowed = str(actions.get("allowed") or self.cfg.get("allowed_actions") or self.ALL_ACTIONS).upper()
        self.allowed_actions = "".join(ch for ch in allowed if "A" <= ch <= "Z") or self.ALL_ACTIONS
        # 姿态中文名（小票、日志、语音播报里可以显示"仰望星空"而不是干巴巴的 F）
        self.action_names = {
            str(k).strip().upper(): str(v).strip()
            for k, v in (actions.get("names") or {}).items()
        }

        # ---------- 超时与熔断 ----------
        self.probe_timeout = float(self.cfg.get("local_probe_timeout", 1.2))
        self.local_retry_seconds = float(self.cfg.get("local_retry_seconds", 30))
        self.enable_local_probe = bool(self.cfg.get("local_probe", True))
        self._local_down_until = 0.0        # 时间戳：在这个时刻之前不再尝试本地
        self._local_probe_at = 0.0          # 上次探测时间（避免频繁探测）
        self._local_probe_ok = None
        self._last_error = ""
        self._cloud_missing_warned = False

    # ==================================================================
    # 一、从加密保险箱读取运行时配置
    # ==================================================================
    def _payload(self) -> dict:
        """
        每次推理都重新解一次保险箱（文件很小，开销可以忽略）。
        好处：管理员在控制台里改完 Key / 接口地址，下一句话立刻生效，不用重启程序。
        """
        return self.vault.get_runtime_model_payload()

    # ==================================================================
    # 二、通用 HTTP 调用（OpenAI 兼容协议）
    # ==================================================================
    def _chat(self, base_url: str, api_key: str, model: str, system_prompt: str,
              user_text: str, timeout: float, json_mode: bool = True) -> str:
        """
        调用 {base_url}/chat/completions，返回模型输出的纯文本。

        Ollama（11434）、vLLM（8000）、DeepSeek、各类中转站都用这套协议，
        所以这里只写一份代码就能全兼容。
        """
        if requests is None:
            raise LLMError("缺少 requests 库，请先运行 1_点我安装环境_Windows.bat")

        url = (base_url or "").rstrip("/") + "/chat/completions"
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_text},
            ],
            "temperature": float(self.cfg.get("temperature", 0.4)),
            "max_tokens": int(self.cfg.get("max_tokens", 120)),
            "stream": False,
        }
        if json_mode:
            # 支持的服务端会强制输出 JSON；不支持的会在下面自动去掉重试
            payload["response_format"] = {"type": "json_object"}

        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
        except Exception as e:
            raise LLMError(f"连接失败：{type(e).__name__}")

        # 有些本地模型 / 老版本服务不认识 response_format，会返回 400/422。
        # 这时候去掉这个字段再问一次，而不是直接判失败。
        if resp.status_code in (400, 422) and json_mode:
            payload.pop("response_format", None)
            try:
                resp = requests.post(url, headers=headers, json=payload, timeout=timeout)
            except Exception as e:
                raise LLMError(f"连接失败：{type(e).__name__}")

        if resp.status_code >= 400:
            # 只保留状态码，绝不把响应体（可能包含密钥回显）抛到终端
            raise LLMError(f"服务返回 HTTP {resp.status_code}")

        try:
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except Exception:
            raise LLMError("返回内容不是标准的 chat/completions 格式")

    def probe_local(self, base_url: str) -> bool:
        """
        轻量探测本地模型是否在线：请求 GET /models，1.2 秒内没回就当没开机。
        比"直接发推理请求等 8 秒超时"快得多。
        """
        if requests is None or not base_url:
            return False
        try:
            resp = requests.get(base_url.rstrip("/") + "/models", timeout=self.probe_timeout)
            return resp.status_code < 500
        except Exception:
            return False

    # ==================================================================
    # 三、解析与规范化
    # ==================================================================
    @staticmethod
    def _strip_wrappers(raw: str) -> str:
        """去掉 ```json 代码块、前后废话，留下最像 JSON 的那一段。"""
        text = (raw or "").strip()
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end > start:
            return text[start:end + 1]
        return text

    def _parse_payload(self, raw: str):
        """
        把模型输出解析成 (memory, action_id)。
        顺序：标准 json.loads -> 去尾逗号/换引号修复 -> 正则兜底捞字段。
        """
        text = self._strip_wrappers(raw)

        # 常见的几种"不标准 JSON"，逐个修一修再交给 json.loads
        candidates = [
            text,
            re.sub(r",\s*([}\]])", r"\1", text),          # 尾逗号 {"a":1,}
            text.replace("'", '"'),                       # 单引号
            re.sub(r",\s*([}\]])", r"\1", text.replace("'", '"')),
        ]
        data = None
        for candidate in candidates:
            try:
                data = json.loads(candidate)
                break
            except Exception:
                continue
        if not isinstance(data, dict):
            data = {}

        memory = data.get("memory")
        action = data.get("action_id", data.get("action", data.get("actionId")))

        # 正则兜底：模型有时会输出 {"memory": "…", "action_id": "F",} 这种带尾逗号的
        # 注意单引号、双引号、中文引号都要兼容
        if not memory:
            m = re.search(r"""["']?memory["']?\s*[:：]\s*["'“]([^"'”\n]+)["'”]""",
                          text, re.IGNORECASE)
            memory = m.group(1) if m else ""
        if not action:
            m = re.search(r"""["']?action_?id["']?\s*[:：]\s*["'“]?([A-Za-z])""",
                          text, re.IGNORECASE)
            action = m.group(1) if m else ""

        return memory, action

    @staticmethod
    def _normalize_memory(raw: str) -> str:
        """规范化记忆点：去引号/括号/前缀/空白，最长 15 字。"""
        text = str(raw or "").translate(
            dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)
        ).strip()
        text = text.strip('"\'“”「」『』【】')
        for prefix in ("记忆点：", "记忆点:", "总结：", "总结:", "memory:", "Memory:"):
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
        text = re.sub(r"\s+", "", text)
        text = text.strip("。，,、；;！!？?")
        if len(text) > 15:
            text = text[:15]
        return text

    def _normalize_action(self, raw) -> str:
        """
        把模型给的动作编号规整成单个大写字母，并且必须在白名单里。
        兼容 "F" / "f" / "F." / "Pose-F" / "动作 F" 这些写法。
        """
        s = str(raw or "").strip().upper()
        if s and len(s) == 1 and s in self.allowed_actions:
            return s
        m = re.search(r"POSE[-\s_:：]*([A-Z])", s)
        if m and m.group(1) in self.allowed_actions:
            return m.group(1)
        for ch in s:
            if "A" <= ch <= "Z" and ch in self.allowed_actions:
                return ch
        return ""

    # ==================================================================
    # 四、推理主流程
    # ==================================================================
    def infer(self, user_text: str) -> dict:
        """
        输入一句话，返回：
            {
              "memory": "8~15字的记忆点",
              "watermarked_memory": "带不可见版权水印的同一句话",
              "action_id": "F",
              "action_name": "仰望星空",     # 没配置就是 "Pose-F"
              "source": "本地Qwen / 云端API / 离线模板",
              "latency_ms": 320,
              "memory_len_ok": True,
              "degraded": False
            }
        """
        start = time.time()
        user_text = str(user_text or "").strip()
        if not user_text:
            return self._fallback("")

        payload = self._payload()
        system_prompt = payload.get("encrypted_system_prompt", "")
        local_base = payload.get("local_base_url", "http://127.0.0.1:11434/v1")
        local_model = payload.get("local_model_name", "qwen2.5:7b")
        local_timeout = float(payload.get("local_timeout", 8.0))
        cloud_base = payload.get("cloud_base_url", "https://api.deepseek.com/v1")
        cloud_model = payload.get("cloud_model_name", "deepseek-chat")
        cloud_timeout = float(payload.get("cloud_timeout", 10.0))
        cloud_key = (payload.get("cloud_api_key") or "").strip()

        tried_notes = []

        # ---------------- 通道 1：本地 Qwen ----------------
        if self.mode in ("auto", "local_only"):
            skip_reason = ""
            if self.mode == "auto" and time.time() < self._local_down_until:
                skip_reason = "本地通道熔断中，直接走云端"
            # 探测只在 auto 模式下作为"快速跳过"用；
            # local_only 是用户强制指定，即使探测接口不支持也照样发一次真实请求。
            elif self.mode == "auto" and self.enable_local_probe and not self._local_alive(local_base):
                skip_reason = "本地模型未启动（探测失败），直接走云端"

            if skip_reason:
                tried_notes.append(skip_reason)
            else:
                try:
                    raw = self._chat(local_base, "", local_model, system_prompt,
                                     user_text, local_timeout)
                    result = self._build_result(raw, "本地Qwen", start)
                    if result:
                        return result
                    tried_notes.append("本地返回内容无法解析")
                except LLMError as e:
                    self._mark_local_down()
                    tried_notes.append(f"本地异常（{e}）")
                    if self.mode == "local_only":
                        return self._fallback(user_text, note=str(e))

        # ---------------- 通道 2：云端 API ----------------
        if self.mode in ("auto", "cloud_only"):
            if not cloud_key:
                if not self._cloud_missing_warned:
                    print("[大脑路由] 尚未配置云端 API Key（可在安全控制台或用 tools/set_secret.py 填写）")
                    self._cloud_missing_warned = True
                tried_notes.append("云端未配置 Key")
            else:
                try:
                    raw = self._chat(cloud_base, cloud_key, cloud_model, system_prompt,
                                     user_text, cloud_timeout)
                    result = self._build_result(raw, "云端API", start)
                    if result:
                        return result
                    tried_notes.append("云端返回内容无法解析")
                except LLMError as e:
                    tried_notes.append(f"云端异常（{e}）")

        # ---------------- 通道 3：离线模板 ----------------
        return self._fallback(user_text, note="；".join(tried_notes))

    def _local_alive(self, base_url: str) -> bool:
        """
        本地存活判断，带 3 秒缓存：同一轮对话里连续两句话不必再探测一次。
        """
        now = time.time()
        if self._local_probe_ok is not None and now - self._local_probe_at < 3:
            return self._local_probe_ok
        ok = self.probe_local(base_url)
        self._local_probe_at = now
        self._local_probe_ok = ok
        if not ok:
            self._mark_local_down()
        return ok

    def _mark_local_down(self):
        """本地不可用 -> 记下熔断截止时间，30 秒内不再白等。"""
        self._local_down_until = time.time() + self.local_retry_seconds

    def _build_result(self, raw: str, source: str, start: float):
        """把模型原始输出组装成统一结果；解析不出记忆点就返回 None 交给下一通道。"""
        memory_raw, action_raw = self._parse_payload(raw)
        memory = self._normalize_memory(memory_raw)
        if not memory:
            return None

        action_id = self._normalize_action(action_raw)
        if not action_id:
            # 动作不合法：用默认第一个动作，但把记忆点保留下来（不能因为动作编号坏了就整句丢掉）
            action_id = self.allowed_actions[0]

        latency = int((time.time() - start) * 1000)
        result = {
            "memory": memory,
            "watermarked_memory": self.vault.inject_hidden_watermark(memory),
            "action_id": action_id,
            "action_name": self.action_name(action_id),
            "source": source,
            "latency_ms": latency,
            "memory_len_ok": 8 <= len(memory) <= 15,
            "degraded": False,
        }
        # 审计只记"通道 / 动作 / 耗时 / 字数"，不记用户原话，避免隐私进日志
        self.vault.write_audit(
            f"大模型推理 通道={source} 动作={action_id} 耗时={latency}ms 记忆点字数={len(memory)}"
        )
        return result

    def action_name(self, action_id: str) -> str:
        """动作编号 -> 中文名（没配置就显示 Pose-X）"""
        return self.action_names.get(action_id, f"Pose-{action_id}")

    def _fallback(self, user_text: str, note: str = "") -> dict:
        """
        所有通道都不可用时的兜底：从内置金句里按输入哈希挑一句。
        保证现场永远不会因为"模型没开机"而卡住演示。
        """
        index = sum(ord(c) for c in user_text) % len(self.OFFLINE_MEMORIES) if user_text else 0
        memory = self.OFFLINE_MEMORIES[index]
        action_id = self.allowed_actions[index % len(self.allowed_actions)]
        if note:
            self._last_error = note
            print(f"[大脑路由] 已启用离线内置模板（原因：{note}）")
        else:
            print("[大脑路由] 已启用离线内置模板")
        self.vault.write_audit(f"大模型推理 通道=离线模板 动作={action_id} 原因={note[:60]}")
        return {
            "memory": memory,
            "watermarked_memory": self.vault.inject_hidden_watermark(memory),
            "action_id": action_id,
            "action_name": self.action_name(action_id),
            "source": "离线模板",
            "latency_ms": 0,
            "memory_len_ok": 8 <= len(memory) <= 15,
            "degraded": True,
            "note": note,
        }

    # ==================================================================
    # 五、自检
    # ==================================================================
    def health(self) -> dict:
        """给启动自检 / tools/selfcheck.py 用的通道状态。"""
        try:
            payload = self._payload()
        except Exception as e:
            return {"ok": False, "error": f"保险箱读取失败：{e}"}

        local_base = payload.get("local_base_url", "")
        local_ok = self.probe_local(local_base)
        key = (payload.get("cloud_api_key") or "").strip()

        def mask(k: str) -> str:
            if not k:
                return "未配置"
            return f"{k[:4]}****{k[-4:]}" if len(k) >= 8 else "已配置(短密钥)"

        return {
            "ok": True,
            "mode": self.mode,
            "local_url": local_base,
            "local_model": payload.get("local_model_name", ""),
            "local_reachable": local_ok,
            "cloud_url": payload.get("cloud_base_url", ""),
            "cloud_model": payload.get("cloud_model_name", ""),
            "cloud_key": mask(key),
            "cloud_ready": bool(key),
            "allowed_actions": self.allowed_actions,
        }
