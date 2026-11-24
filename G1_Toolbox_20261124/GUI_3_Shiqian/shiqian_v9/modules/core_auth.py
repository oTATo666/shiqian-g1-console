"""
modules/core_auth.py —— 系统口令根（唯一权威来源 · v7 新增）
================================================================

【这个文件是干什么的？】
整个系统只有这一处知道"口令对不对"。所有需要验口令的地方
（主程序控制台、网页核销后台、命令行工具）都调用这里，
而不是各自在代码里写一份 "if code == 'xxx'"。

这么做的好处很直接：**想找后门，只需要审这一个文件**，不到 200 行。

【两条口令分别是什么角色？】
    · 一级：开门口令。只负责"打开控制台 / 登录网页核销后台"，
            看不到 API Key，改不了底层接口，也不能撤销核销。
    · 二级：底层总钥匙。凡是碰密钥、碰数据状态的操作都必须再用它确认。

【"硬编码 / 不可修改 / 无后门" 具体是怎么做到的？】
1. 口令的明文**不在任何文件里**：源码里只有 PBKDF2 摘要（60 多位的十六进制串）。
   也就是说，即使别人拿到整个压缩包，也 grep 不到任何一条口令的字面量。
2. 本模块只提供"校验"能力，**没有任何 set / change / reset / import 接口**。
   没有写入口，就没有"运行时改口令"这条路。
3. 不读环境变量、不读 config.yaml、不读数据库、不读保险箱。
   想靠"改配置""设个变量""往库里塞一行"来换口令，全都无效。
4. 网页后台只调用 verify_level1()，没有任何 HTTP 路由能写入口令；
   主程序控制台里也没有"改口令"这一项（v4 曾有，v7 已删除）。
5. 启动自检（_GUARD）：常量块被人手工改过时，is_intact() 会变 False，
   此时**所有校验一律返回 False**——宁可系统进不去，也不静默放行。

【必须说清的边界（不夸大）】
    这是"硬编码"，不是"不可破解的硬件根信任"。拿到源码的人当然可以改常量后重新打包，
    这是任何纯软件方案的共同限制。_GUARD 能挡住"随手改一行数字就绕过"这类改动，
    但挡不住看懂算法的人把摘要和 _GUARD 一起重算。
    真要做到"谁都改不了"，只能上硬件安全模块 / 签名校验，那不是这个项目的范围。

【口令能不能换？】
    当前版本**不提供换口令工具、也没有任何运行期写入口**。
    确需更换时只能按本文件的摘要算法重算两条常量与 _GUARD，改源码后重新打包；
    python -m modules.core_auth --check 是只读的常量自检（不带口令、不交互）。
"""

import base64
import hashlib
import hmac

# ======================================================================
# 一、常量区（改这里 = 换口令；摘要与 _GUARD 必须一起重算，不要手改）
# ======================================================================

# PBKDF2 迭代轮数。轮数越高越难被离线暴力破解，但每次校验也越慢。
# 15 万轮在普通笔记本上约 60~90 毫秒，人手输入口令完全感觉不到。
_ITERATIONS = 150_000

# 两条口令各用一条独立的固定盐（改盐 = 换口令，会影响摘要）
_SALT_L1_B64 = "c2hpcWlhbi1jb3JlLWwxLXY3LTlmM2EyYw=="
_SALT_L2_B64 = "c2hpcWlhbi1jb3JlLWwyLXY3LWM0MWI3ZQ=="

# 一级口令（开门口令）的 PBKDF2-HMAC-SHA256 摘要
_DIGEST_L1 = "a262a06e5b78bbc15c92d7ac47c2ba6596608bd904e00caa1ef68b3657bb1f16"

# 二级口令（底层总钥匙）的 PBKDF2-HMAC-SHA256 摘要
_DIGEST_L2 = "77d549616adc2c4cfb4e87ac167d580550b1975013fb5900f60e0359fa60680d"

# 自检指纹：把上面 5 个值拼起来再哈希一次。
# 如果有人只改 _DIGEST_L2 而不重算它，指纹就对不上，系统会直接拒绝一切口令。
_GUARD = "417cb6c9fc4155e06a65715f256a10a882dc7c55054895c2adaee6dc97ddbe9d"

# 单次输入的最大长度。防止有人用 10MB 的字符串把 PBKDF2 拖成 DoS。
_MAX_INPUT_LEN = 64


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class CoreAuth:
    """
    口令校验器。

    用法（只有这三种，没有第四种）：
        au = CoreAuth()
        au.verify_level1("……")   -> True / False   开门口令
        au.verify_level2("……")   -> True / False   底层总钥匙
        au.policy()               -> dict           给体检工具看的"口令策略说明"

    注意：本类**没有** set_password / change / reset 之类的方法，这是刻意设计。
    """

    LEVEL1_NAME = "一级开门口令"
    LEVEL2_NAME = "二级底层总钥匙"

    def __init__(self):
        # 初始化时先算一次自检指纹；之后每次校验都会用到这个结果
        self._intact = self._check_integrity()
        self._tamper_flag = not self._intact

    # ------------------------------------------------------------------
    # 二、自检：常量块有没有被手工改过
    # ------------------------------------------------------------------
    @staticmethod
    def _check_integrity() -> bool:
        """
        把 5 个常量的当前值重新拼一遍，和 _GUARD 比对。
        对得上 = 没人动过；对不上 = 有人改了常量却没同步指纹。
        """
        try:
            material = "|".join([
                _DIGEST_L1, _DIGEST_L2, _SALT_L1_B64, _SALT_L2_B64, str(_ITERATIONS),
            ])
            return hmac.compare_digest(_sha256_hex(material), _GUARD)
        except Exception:
            # 任何异常都当作"不可信"，绝不能因为算不出来就放行
            return False

    def is_intact(self) -> bool:
        """口令常量是否完好（False 表示被动过，系统会拒绝一切口令）"""
        return self._intact

    # ------------------------------------------------------------------
    # 三、真正的校验
    # ------------------------------------------------------------------
    def _verify(self, code: str, digest: str, salt_b64: str) -> bool:
        """
        校验一个候选口令。

        步骤：清洗输入 -> 长度闸门 -> PBKDF2 派生 -> 固定时间比较。
        固定时间比较（hmac.compare_digest）是为了不让攻击者通过"响应快慢"
        猜出前几位是不是蒙对了。
        """
        if not self._intact:
            # 常量被动过：一律不通过。管理员看到提示应该重新部署一份干净的源码。
            return False

        if not isinstance(code, str):
            return False

        candidate = code.strip()
        if not candidate or len(candidate) > _MAX_INPUT_LEN:
            return False

        try:
            salt = base64.b64decode(salt_b64)
        except Exception:
            return False

        derived = hashlib.pbkdf2_hmac("sha256", candidate.encode("utf-8"), salt, _ITERATIONS)
        return hmac.compare_digest(derived.hex(), digest)

    def verify_level1(self, code: str) -> bool:
        """一级开门口令（控制台解锁 / 网页核销后台登录）"""
        return self._verify(code, _DIGEST_L1, _SALT_L1_B64)

    def verify_level2(self, code: str) -> bool:
        """二级底层总钥匙（敏感操作、密钥查看）"""
        return self._verify(code, _DIGEST_L2, _SALT_L2_B64)

    def verify(self, code: str, level: int = 1) -> bool:
        """按级别校验的统一入口（level 只认 1 和 2，其它值一律当失败）"""
        try:
            level = int(level)
        except (TypeError, ValueError):
            return False
        if level == 2:
            return self.verify_level2(code)
        if level == 1:
            return self.verify_level1(code)
        return False

    # ------------------------------------------------------------------
    # 四、给体检工具/文档用的策略说明（不含任何口令信息）
    # ------------------------------------------------------------------
    def policy(self) -> dict:
        return {
            "source": "modules/core_auth.py 源码常量（硬编码）",
            "kdf": "pbkdf2-hmac-sha256",
            "iterations": _ITERATIONS,
            "input_max_len": _MAX_INPUT_LEN,
            "changeable_at_runtime": False,     # 运行期无法更改
            "changeable_by_interface": False,   # 界面/接口都没有修改入口
            "readable_from_config": False,      # 不读 config.yaml / 环境变量 / 保险箱
            "levels": {1: self.LEVEL1_NAME, 2: self.LEVEL2_NAME},
            "integrity": self._intact,
        }

    def tamper_message(self) -> str:
        """自检失败时给用户看的一句话（由上层决定打印还是写审计）"""
        return (
            "⚠️ [口令根自检失败] modules/core_auth.py 的口令常量被修改过，"
            "系统已拒绝所有口令校验。请用官方发布的原版文件覆盖后重启。"
        )


# 全局单例：整个进程共用一个实例，避免每个模块各建一份
core_auth = CoreAuth()


# ======================================================================
# 五、自测（python -m modules.core_auth 可直接跑）
# 注意：这里**不写任何口令字面量**，而是现场让你输入，
#       否则"源码里 grep 不到口令"这条承诺就自己破了。
# ======================================================================
if __name__ == "__main__":
    import sys

    # 非交互只读自检：供 5_点我填后台密钥.bat 的第 5 项调用。
    # 原构建期工具下线后，这项自检收口到本模块，不再需要单独文件。
    if "--check" in sys.argv:
        _au = CoreAuth()
        _p = _au.policy()
        print("常量自检：", "✅ 完好" if _au.is_intact() else "❌ 被动过（口令校验已全部失效）")
        print("口令来源：", _p["source"])
        print("运行期可改：", "否" if not _p["changeable_at_runtime"] else "是")
        sys.exit(0 if _au.is_intact() else 1)

    import getpass

    au = CoreAuth()
    print("常量自检：", "✅ 完好" if au.is_intact() else "❌ 被动过（口令校验已全部失效）")

    print("\n--- 结构类用例（不需要知道口令）---")
    structural = [
        ("空口令拒绝", au.verify_level1(""), False),
        ("None 拒绝", au.verify_level1(None), False),
        ("超长输入拒绝", au.verify_level2("x" * 500), False),
        ("非法级别拒绝", au.verify("whatever", 3), False),
        ("非法级别拒绝（0）", au.verify("whatever", 0), False),
    ]
    bad = 0
    for name, got, want in structural:
        ok = (got == want)
        bad += 0 if ok else 1
        print(("  ✅ " if ok else "  ❌ ") + name)

    print("\n--- 实际口令比对（输入不回显，不会写进任何文件）---")
    c1 = getpass.getpass("请输入【一级开门口令】：")
    c2 = getpass.getpass("请输入【二级底层总钥匙】：")
    r1 = au.verify_level1(c1)
    r2 = au.verify_level2(c2)
    print(("  ✅ " if r1 else "  ❌ ") + "一级口令正确")
    print(("  ✅ " if r2 else "  ❌ ") + "二级口令正确")
    print(("  ✅ " if not au.verify_level1(c2) else "  ❌ ") + "二级口令不能当一级用（分级要独立）")
    print(("  ✅ " if not au.verify_level2(c1) else "  ❌ ") + "一级口令不能当二级用（分级要独立）")

    bad += (0 if r1 else 1) + (0 if r2 else 1)
    print("\n全部通过" if bad == 0 else f"\n有 {bad} 项未通过（口令错或常量被改过）")
