import os
import json
import base64
import hashlib
import threading
import getpass
from datetime import datetime
from cryptography.fernet import Fernet

# 【v7】口令校验统一交给口令根模块，本文件不再保存任何口令摘要。
# 这样"口令是什么、能不能改"只有一处答案，审计也只需要看 modules/core_auth.py。
from modules.core_auth import core_auth


class SecurityVault:
    """
    系统安全与版权保护模块：
    1. 隐藏零宽字符水印（不对外显示，嵌入提示词与日志元数据中）
    2. 核心模型人设与 Prompt 加密存储，运行时内存中动态解密，不对外明文暴露
    3. 维护口令解锁机制
    4. 底层接口与 API 查询/修改的高安全等级二次鉴权与操作审计

    【增量说明 · v4】
    为了让「网页核销后台」（web_redeem_server.py）也能复用同一套鉴权机制，
    新增了两个**不依赖交互式终端**的接口（原来的 interactive_admin_console 只能在命令行里用）：
        · verify_passcode(code, level=1)  -> 校验一级 / 二级口令，返回 True/False
        · write_audit(event)              -> 供其他模块写入带水印的加密审计日志
    口令本身依旧只以 SHA-256 哈希形式存在，任何文件里都不会出现明文口令。

    【增量说明 · v5：把口令和密钥彻底"收进后台"】
    1. 新增 get_secret / set_secret / has_secret：
       给 tools/set_secret.py 用，命令行或环境变量填进来的 Key 直接加密进保险箱，
       普通配置文件（config.yaml）里永远不会出现明文。
    2. 新增 import_from_env：支持"设置环境变量 -> 启动时自动导入"，导入后环境变量里
       的内容不会被复制到任何文件里（除非你自己另外存了）。
    3. _save_vault 改成"写临时文件 -> os.replace 原子替换"：
       以前直接覆盖写，万一写到一半断电，整个保险箱就废了（Prompt 和 Key 全丢）。
    4. 新增 _ensure_defaults：老版本保险箱缺少新字段时自动补齐，
       升级到 v5 不需要手动删 data/.model_vault.enc 重新初始化。

    【增量说明 · v7：口令"只读化"】
    按照"底层密码硬编码、不提供任何人工干预或后门接口"的要求：
    1. 两条口令的摘要全部移到 modules/core_auth.py（源码常量，运行期不可改）；
    2. 本模块只做"调用校验"，不再保存 staff_passcode_hash，也不再提供
       change_staff_passcode 的真实实现（调用会一律被拒并写审计）；
    3. 控制台删除了"修改网页登录口令"这一项，界面/接口层面再无修改入口；
    4. 保险箱损坏时仍然自动备份重建（那是"防止密钥全丢"，与口令无关）。
    """

    # 隐藏水印明文（运行时编码为零宽不可见字符，不对外展示）
    _WATERMARK_RAW = "千逐"
    # 审计日志的多线程追加锁（网页后台会有多个请求线程同时写日志）
    _AUDIT_LOCK = threading.Lock()

    # 【v7】历史上这两个常量存过一级 / 二级口令的 SHA-256。
    # 现在口令摘要只存在于 modules/core_auth.py（硬编码、运行期不可改），
    # 这里只留一句注释说明去处，免得后来人以为"删了就没地方改"。
    #   · 一级开门口令  -> core_auth.verify_level1()
    #   · 二级底层总钥匙 -> core_auth.verify_level2()

    # 已经作废、需要从老保险箱里清除的字段：
    #   staff_passcode_hash：v7 起口令固定不可改，留着会让人误以为还能改。
    #   comm_secret / comm_devices：v8 主从加密通信协议已下线，不再使用。
    _RETIRED_KEYS = ("staff_passcode_hash", "comm_secret", "comm_devices")

    # ==================================================================
    # 保险箱里都有哪些字段（v5：集中定义，新增字段时只改这里一处）
    # 注意：这里全部是"初始默认值"，真实值以加密文件 data/.model_vault.enc 为准。
    # ==================================================================
    DEFAULT_SECRETS = {
        "owner_watermark": _WATERMARK_RAW,
        # 注意：这里**没有**任何口令字段。
        # v7 起口令是源码常量（modules/core_auth.py），不进保险箱、不进配置文件，
        # 因此不存在"改保险箱 = 换口令"这条路。
        # 核心 Prompt：只存在保险箱里，任何普通配置文件都看不到原文
        "encrypted_system_prompt": (
            "你现在是一个情绪提炼与动作映射助手。"
            "请根据用户的输入，总结一段8-15字的记忆点，并选择一个最合适的预设动作（A-Z）。"
            '必须严格以JSON格式输出：{"memory": "8-15字总结", "action_id": "A"}'
        ),
        # 本地 Qwen（Ollama / vLLM 的 OpenAI 兼容接口）
        "local_base_url": "http://127.0.0.1:11434/v1",
        "local_model_name": "qwen2.5:7b",
        "local_timeout": 8.0,
        # 云端兜底（DeepSeek 等）
        "cloud_base_url": "https://api.deepseek.com/v1",
        "cloud_api_key": "",
        "cloud_model_name": "deepseek-chat",
        "cloud_timeout": 10.0,
        # 动作触发接口（属于底层接口，按规范也放在保险箱里）
        "action_endpoint": "http://localhost:8080/trigger_action/{action_id}",
        # 云端语音识别（可选，本地 Vosk 不可用时兜底）
        "cloud_asr_url": "https://api.openai.com/v1/audio/transcriptions",
        "cloud_asr_api_key": "",
        "cloud_asr_model": "whisper-1",
    }

    # 允许通过 set_secret 修改的字段白名单（防止手滑写错字段名，写进去却永远不生效）
    SECRET_KEYS = set(DEFAULT_SECRETS.keys())

    def __init__(self, vault_path: str = "data/.model_vault.enc", audit_log: str = "data/.security_audit.log"):
        self.vault_path = vault_path
        self.audit_log = audit_log
        self._unlocked = False
        # 基于固定盐值派生对称加密密钥（加密模型提示词与敏感 API 配置）
        key_seed = hashlib.sha256(b"SHIQIAN_G1_CORE_SEED_407_QIANZHU").digest()
        self._fernet = Fernet(base64.urlsafe_b64encode(key_seed))
        self._zero_width_watermark = self._encode_zero_width(self._WATERMARK_RAW)
        self._init_default_vault()
        # 【v7】口令根自检：常量被动过就第一时间写审计并给出醒目提示。
        # 此时 core_auth 会拒绝一切口令校验（系统进不去），但不会静默放行。
        if not core_auth.is_intact():
            self._write_audit("严重：口令根常量自检失败（modules/core_auth.py 被修改）")
            print(core_auth.tamper_message())

    @staticmethod
    def _encode_zero_width(text: str) -> str:
        """将文本编码为不可见的零宽字符序列 (\u200b 和 \u200c)"""
        bits = "".join(format(ord(ch), "016b") for ch in text)
        return "".join("\u200b" if b == "0" else "\u200c" for b in bits)

    def inject_hidden_watermark(self, content: str) -> str:
        """向内部数据或文本中无感注入隐藏水印（肉眼完全不可见）"""
        if not content:
            return self._zero_width_watermark
        return content[:1] + self._zero_width_watermark + content[1:]

    def verify_hidden_watermark(self, content: str) -> bool:
        """检测隐藏水印是否存在"""
        return self._zero_width_watermark in content

    def _write_audit(self, event: str):
        """
        写入加密审计日志。
        每条日志首字符后面都嵌入了不可见的「千逐」零宽水印，
        用文本编辑器打开看不到，但 verify_hidden_watermark() 能验出来，可用于溯源。
        """
        folder = os.path.dirname(self.audit_log)
        if folder:
            os.makedirs(folder, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = self.inject_hidden_watermark(f"[{ts}] {event}\n")
        # 加锁追加，避免多个网页请求线程同时写导致日志串行错乱
        with self._AUDIT_LOCK:
            with open(self.audit_log, "a", encoding="utf-8") as f:
                f.write(line)

    def _init_default_vault(self):
        """初始化加密模型配置库（包含加密后的核心 Prompt、本地/云端接口参数）"""
        if os.path.exists(self.vault_path):
            self._ensure_defaults()
            return
        folder = os.path.dirname(self.vault_path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        self._save_vault(dict(self.DEFAULT_SECRETS))

    def _ensure_defaults(self):
        """
        老版本保险箱"补字段"。
        v5 新增了 cloud_asr_model 等字段，如果不补，代码里 get 到 None 会报错；
        但如果让用户删掉保险箱重新初始化，之前填的 API Key 就白填了。
        所以这里只做"缺什么补什么"，已有值一律不动。
        """
        try:
            data = self._load_vault_internal()
        except Exception:
            # 保险箱损坏（比如之前断电写坏了）：备份一份再重建，绝不静默丢数据
            try:
                broken = self.vault_path + ".broken"
                os.replace(self.vault_path, broken)
                print(f"⚠️ [安全保险箱] 检测到保险箱损坏，已备份为 {broken} 并重新初始化。")
            except OSError:
                pass
            self._save_vault(dict(self.DEFAULT_SECRETS))
            return

        changed = False
        for key, value in self.DEFAULT_SECRETS.items():
            if key not in data:
                data[key] = value
                changed = True
        # 【v7】清掉作废字段：老保险箱里可能还躺着 staff_passcode_hash。
        # 不清掉的话，虽然代码已经不看它了，但留着容易让人误以为"口令还在库里、还能改"。
        for key in self._RETIRED_KEYS:
            if key in data:
                data.pop(key, None)
                changed = True
        if changed:
            self._save_vault(data)

    def _save_vault(self, data: dict):
        """
        原子保存：先写同目录临时文件并 fsync，再 os.replace 替换。
        这样"写到一半断电"也只会丢掉这次修改，不会把整个保险箱写坏。
        """
        raw_bytes = json.dumps(data, ensure_ascii=False).encode("utf-8")
        encrypted = self._fernet.encrypt(raw_bytes)

        folder = os.path.dirname(os.path.abspath(self.vault_path))
        os.makedirs(folder, exist_ok=True)
        tmp_path = f"{self.vault_path}.{os.getpid()}.tmp"
        try:
            with open(tmp_path, "wb") as f:
                f.write(encrypted)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.vault_path)
            # 在类 Unix 系统上顺手把权限收紧到"只有自己能读写"（Windows 会忽略）
            try:
                os.chmod(self.vault_path, 0o600)
            except OSError:
                pass
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    # ==================================================================
    # 【v5 新增】密钥读写：让 tools/set_secret.py 之类的外部工具能安全地填 Key
    # 注意：这些方法**只负责存取**，不负责鉴权；调用方（命令行工具）必须先做好鉴权。
    # ==================================================================
    def get_secret(self, key: str, default=None):
        """读取保险箱里的一个字段（明文字符串）；不存在返回 default。"""
        try:
            data = self._load_vault_internal()
        except Exception:
            return default
        return data.get(key, default)

    def set_secret(self, key: str, value, note: str = "") -> bool:
        """
        写入/更新保险箱里的一个字段，并立即加密落盘。
        key 只能在 SECRET_KEYS 白名单里，避免手滑写出 "cloud_api_kye" 这种无效字段。
        """
        if key not in self.SECRET_KEYS:
            raise KeyError(f"未知的保险箱字段：{key}（可用字段见 SECRET_KEYS）")
        data = self._load_vault_internal()
        data[key] = value
        self._save_vault(data)
        self._write_audit(note or f"保险箱字段被更新：{key}")
        return True

    def has_secret(self, key: str) -> bool:
        """某个字段是否已经配置了非空值（比如 cloud_api_key）。"""
        value = self.get_secret(key, "")
        return bool(str(value or "").strip())

    def import_from_env(self, env_name: str = "SHIQIAN_CLOUD_API_KEY",
                        key: str = "cloud_api_key") -> bool:
        """
        从环境变量一次性导入密钥，然后加密进保险箱。

        为什么用环境变量？因为它不会像命令行参数那样留在 shell 历史里，
        也不会出现在任何配置文件中，适合"现场临时配一台电脑"。
        返回 True 表示这次真的导入了；False 表示环境变量里没有值（不报错）。
        """
        value = (os.environ.get(env_name) or "").strip()
        if not value:
            return False
        self.set_secret(key, value, note=f"从环境变量 {env_name} 导入并加密了 {key}")
        print(f"🔐 [安全保险箱] 已从环境变量 {env_name} 导入密钥并加密保存（明文未落盘）")
        print(f"   如需从系统环境变量中删除：Windows 用 setx {env_name} \"\" ")
        return True

    def masked_view(self) -> dict:
        """
        脱敏视图：给自检工具和"查看当前配置"用，
        只显示"配没配、大概多长"，绝不返回明文口令或完整密钥。
        """
        try:
            data = self._load_vault_internal()
        except Exception as e:
            return {"ok": False, "error": str(e)}

        def mask(value: str) -> str:
            value = str(value or "")
            if not value:
                return "未配置"
            if len(value) < 8:
                return "已配置（短密钥）"
            return f"{value[:4]}****{value[-4:]}"

        return {
            "ok": True,
            "vault_path": self.vault_path,
            "prompt_len": len(data.get("encrypted_system_prompt", "")),
            # v7：保险箱里不再有口令字段，这里改报"口令来源"，避免界面误显示"未设置"
            "credential_source": "源码常量（硬编码，运行期不可改）",
            "credential_changeable": False,
            "cloud_api_key": mask(data.get("cloud_api_key", "")),
            "cloud_asr_api_key": mask(data.get("cloud_asr_api_key", "")),
            "local_base_url": data.get("local_base_url", ""),
            "local_model_name": data.get("local_model_name", ""),
            "cloud_base_url": data.get("cloud_base_url", ""),
            "cloud_model_name": data.get("cloud_model_name", ""),
            "action_endpoint": data.get("action_endpoint", ""),
        }

    def _load_vault_internal(self) -> dict:
        with open(self.vault_path, "rb") as f:
            encrypted = f.read()
        decrypted = self._fernet.decrypt(encrypted)
        return json.loads(decrypted.decode("utf-8"))

    def get_runtime_model_payload(self) -> dict:
        """供主程序内部推理调用（自动解密并在内存中注入隐藏水印，绝不打印明文）"""
        data = self._load_vault_internal()
        data["encrypted_system_prompt"] = self.inject_hidden_watermark(data["encrypted_system_prompt"])
        return data

    def unlock_with_passcode(self, code: str) -> bool:
        """校验解锁口令（一级开门口令，v7 起由 modules/core_auth.py 统一判定）"""
        if core_auth.verify_level1(code):
            self._unlocked = True
            self._write_audit("一级维护口令解锁成功")
            return True
        self._write_audit("警告：尝试使用错误的一级口令解锁")
        return False

    # ==================================================================
    # 【v4 新增】不依赖命令行的分级口令校验
    # 为什么要加？原来的 _verify_high_security_clearance() 用 getpass 在终端里读口令，
    # 网页后台（独立的 HTTP 进程）根本没法用。这里提供同一套口令的「程序内校验」版本。
    # 【v7】原来的 _sha256() 工具方法已删除：本模块不再自己算任何口令摘要，
    #       比较逻辑只存在于 modules/core_auth.py，避免"两处各算一遍、改了一处忘另一处"。
    # ==================================================================
    @staticmethod
    def mask_code(code: str) -> str:
        """
        核销码脱敏：123456 -> 12**56
        审计日志里只写脱敏后的码，万一日志文件外泄也没法直接拿去兑换礼品。
        """
        code = (code or "").strip()
        if len(code) <= 2:
            return "*" * len(code)
        if len(code) == 3:
            return code[0] + "*" + code[-1]
        return code[:2] + "*" * (len(code) - 4) + code[-2:]

    def verify_passcode(self, code: str, level: int = 1) -> bool:
        """
        分级口令校验（非交互式，供网页后台等子进程使用）

        level = 1：一级开门口令，工作人员登录网页核销后台用。
                   【v7】固定为源码常量，网页后台不再提供修改入口。
        level = 2：二级底层总钥匙，撤销核销、查看密钥等敏感操作用。
                   【v7】同样固定为源码常量，运行期不可改。

        两条口令的摘要都只存在于 modules/core_auth.py；本方法只做转发 + 审计，
        不再读保险箱、不读配置、不读环境变量——因此也没有"绕过"的输入面。

        返回 True 表示口令正确。
        """
        level = 2 if int(level or 1) == 2 else 1
        ok = core_auth.verify(code, level=level)

        if level == 2:
            self._write_audit(
                "通过二级高安全等级鉴权（网页/程序侧）" if ok
                else "警告：二级高安全等级鉴权失败，已阻断（网页/程序侧）"
            )
        else:
            self._write_audit(
                "一级口令校验成功（网页/程序侧）" if ok
                else "警告：一级口令校验失败（网页/程序侧）"
            )
        return ok

    def write_audit(self, event: str):
        """对外公开的审计写入接口（内部仍复用带零宽水印的 _write_audit）"""
        self._write_audit(event)

    def change_staff_passcode(self, new_code: str) -> bool:
        """
        【v7 已废弃，保留只是为了"老代码调用时安全失败"】

        v4~v6 时这个方法能把网页登录口令写进保险箱；现在口令是硬编码常量，
        运行期不允许任何修改，所以它**永远返回 False**，并记一条审计。

        为什么还留着？
          · 万一还有别处的旧代码/旧脚本调用它，让它"安静地失败"比抛 AttributeError
            更容易排查，也不会因为异常中断现场活动；
          · 它不构成后门：返回 False 意味着什么都不会被改。
        """
        self._write_audit(
            f"拦截：尝试在运行期修改固定口令（调用方传入长度 {len(str(new_code or ''))}），已拒绝"
        )
        return False

    def credential_policy(self) -> dict:
        """把"口令怎么管"的结论交给体检工具/文档，避免各处重复描述"""
        return core_auth.policy()

    def _verify_high_security_clearance(self) -> bool:
        """查询 API 或修改底层参数时的二级高安全等级鉴权（底层总钥匙）"""
        if not self._unlocked:
            print("🔒 [安全拦截] 当前处于锁定状态，请先输入一级开门口令解锁！")
            return False
        print("\n🛡️ [高安全等级验证] 查询 API 密钥或更改底层接口属于敏感操作。")
        print("    口令由源码常量固定，无法在此处修改，也没有找回/重置通道。")
        sec_code = getpass.getpass("请输入二级底层总钥匙（输入时屏幕不显示）: ")
        if core_auth.verify_level2(sec_code):
            self._write_audit("通过二级高安全等级鉴权")
            return True
        self._write_audit("警告：二级高安全等级鉴权失败，已阻断访问")
        print("❌ [安全阻断] 二级口令错误，已记录安全审计日志！")
        return False

    def interactive_admin_console(self):
        """管理员安全控制台（需口令解锁 + 二级高安全鉴权方可查询/修改底层）"""
        code = getpass.getpass("🔑 请输入维护口令以解锁高级管理模式（输入时屏幕不显示）：")
        if not self.unlock_with_passcode(code):
            print("❌ 口令错误，拒绝访问！")
            return

        print("✅ 维护模式已解锁。")
        print("   （提示：口令为硬编码常量，本控制台没有、也不会有「修改口令」这一项。）")
        while True:
            print("\n--- 🛡️ 安全管理控制台 ---")
            print("1. 查看当前底层接口与 API 脱敏状态（需二级底层总钥匙）")
            print("2. 修改云端 LLM / 语音识别 Key 或底层模型地址（需二级底层总钥匙）")
            print("3. 锁定并退出控制台")
            choice = input("请选择操作 (1/2/3): ").strip()

            if choice == "3":
                self._unlocked = False
                print("🔒 已重新锁定安全保险箱。")
                break

            if choice in ("1", "2"):
                if not self._verify_high_security_clearance():
                    continue
                secrets = self._load_vault_internal()

                if choice == "1":
                    self._write_audit("管理员查询了底层接口与 API 状态")
                    view = self.masked_view()
                    print(f"  • 本地模型接口: {view['local_base_url']} | 模型: {view['local_model_name']}")
                    print(f"  • 云端 API 地址: {view['cloud_base_url']} | 模型: {view['cloud_model_name']}")
                    print(f"  • 云端 API Key : {view['cloud_api_key']}")
                    print(f"  • 云端语音 Key : {view['cloud_asr_api_key']}")
                    print(f"  • 动作触发接口 : {view['action_endpoint']}")
                    print(f"  • 核心模型指令 : [已加密保护，共 {view['prompt_len']} 字符，禁止明文导出]")

                elif choice == "2":
                    print("\n（直接按回车表示保持原值不变）")
                    new_cloud_key = getpass.getpass("请输入新的云端 API Key（输入时不显示）: ").strip()
                    new_cloud_url = input(f"云端 API 地址 [{secrets.get('cloud_base_url')}]: ").strip()
                    new_cloud_model = input(f"云端模型名称 [{secrets.get('cloud_model_name')}]: ").strip()
                    new_local_url = input(f"本地模型地址 [{secrets.get('local_base_url')}]: ").strip()
                    new_local_model = input(f"本地模型名称 [{secrets.get('local_model_name')}]: ").strip()
                    new_action_url = input(f"动作触发接口 [{secrets.get('action_endpoint')}]: ").strip()
                    new_asr_key = getpass.getpass("云端语音识别 Key（可留空跳过，输入时不显示）: ").strip()

                    if new_cloud_key:
                        secrets["cloud_api_key"] = new_cloud_key
                    if new_cloud_url:
                        secrets["cloud_base_url"] = new_cloud_url
                    if new_cloud_model:
                        secrets["cloud_model_name"] = new_cloud_model
                    if new_local_url:
                        secrets["local_base_url"] = new_local_url
                    if new_local_model:
                        secrets["local_model_name"] = new_local_model
                    if new_action_url:
                        secrets["action_endpoint"] = new_action_url
                    if new_asr_key:
                        secrets["cloud_asr_api_key"] = new_asr_key

                    self._save_vault(secrets)
                    self._write_audit("管理员修改并重新加密了底层接口配置")
                    print("✅ 底层配置已加密保存，立即生效！")
