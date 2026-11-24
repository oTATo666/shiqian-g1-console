"""
redemption_manager.py —— 核销业务核心逻辑（v4 增量模块）
==========================================================

【这个文件干什么？】
网页核销后台（web_redeem_server.py）只负责"说话"（接收 HTTP 请求、返回 JSON），
真正"动手"的逻辑全部放在这里，方便以后：
    · 换成小程序 / 命令行工具 / 微信群机器人，都能直接复用同一套核销逻辑；
    · 单元测试时不用启动网页服务。

【它守住的几条铁律】
1. 所有对 data/codes.csv 的读写，都在 csv_transaction() 里完成：
   进程内线程锁 + 跨进程文件锁 + 原子替换，出票进程和核销进程同时跑也不会互相覆盖。
2. 状态机单向流转，不允许乱跳：
       未使用  ──(发小票)──►  已使用  ──(现场兑换)──►  已兑换礼品
                                  ▲                        │
                                  └────(二级鉴权撤销)──────┘
3. 核销前必须做「真伪校验」：
   · 码不在库里            -> 查无此码，疑似伪造
   · 码在库里但从未发放过  -> 不是有效凭证（防止有人猜中码池里的原始码）
   · 码的记忆点里没有零宽水印 -> 数据被改过，需要二级底层总钥匙才能强行核销
4. 每一次查询/核销/撤销都会写进带不可见水印的审计日志（SecurityVault.write_audit）。
"""

import os
from datetime import datetime, date

from modules.atomic_csv import (
    csv_transaction,
    read_rows,
    write_rows_atomic,
    ensure_columns,
)


class RedemptionManager:
    """核销业务：查真伪、改状态、撤销、统计、流水"""

    # 三个状态常量（改这里就能统一改全项目的叫法）
    STATUS_UNUSED = "未使用"       # 码池里还没发出去的码
    STATUS_ISSUED = "已使用"        # 已经出过小票（v3 老版本就叫"已使用"）
    STATUS_REDEEMED = "已兑换礼品"  # v4 新增：现场已经领走礼品

    # codes.csv 在 v4 新增的列（老文件会自动补列，不用手工改）
    # 核销流水号只写在 redeem_log.csv 里，不再往 codes.csv 里塞一份，避免两处数据不同步
    EXTRA_COLUMNS = ["redeemed_at", "redeemed_by"]

    def __init__(self, vault, cfg: dict):
        self.vault = vault
        self.codes_csv = cfg.get("codes_csv_path", "data/codes.csv")
        self.records_csv = cfg.get("records_csv_path", "data/issued_tickets.csv")
        self.redeem_log_csv = cfg.get("redeem_log_csv_path", "data/redeem_log.csv")
        self.redeemed_status = cfg.get("redeemed_status", self.STATUS_REDEEMED)
        self._ensure_files()

    # ------------------------------------------------------------------
    # 初始化与工具方法
    # ------------------------------------------------------------------
    def _ensure_files(self):
        """确保核销流水文件存在（表头写好，方便直接用 Excel 打开查看）"""
        folder = os.path.dirname(self.redeem_log_csv)
        if folder:
            os.makedirs(folder, exist_ok=True)
        if not os.path.exists(self.redeem_log_csv):
            with csv_transaction(self.redeem_log_csv):
                write_rows_atomic(
                    self.redeem_log_csv,
                    ["redeem_no", "code", "ticket_no", "memory", "operator", "operated_at", "result"],
                    [],
                )

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _strip_zero_width(text: str) -> str:
        """
        去掉零宽字符（\\u200b / \\u200c / \\u200d / \\ufeff）。
        数据文件里的水印要保留，但**回显给网页/前端**时去掉，
        这样版权水印只在内部流转数据里存在，界面上干干净净。
        """
        for ch in ("\u200b", "\u200c", "\u200d", "\ufeff"):
            text = (text or "").replace(ch, "")
        return text

    @staticmethod
    def normalize_code(code: str) -> str:
        """把用户输入清洗成 6 位纯数字（手机输入法常带空格或全角字符）"""
        text = str(code or "")
        # 先把全角数字统统换成半角，再把空格、横杠等杂字符去掉
        table = str.maketrans("０１２３４５６７８９", "0123456789")
        text = text.translate(table)
        return "".join(ch for ch in text if ch.isdigit())

    # ------------------------------------------------------------------
    # 内部：按核销码找到对应的小票记录
    # ------------------------------------------------------------------
    def _find_ticket(self, code: str) -> dict:
        """在 issued_tickets.csv 里按核销码反查小票（作品编号 / 姿态 / 记忆点）"""
        if not os.path.exists(self.records_csv):
            return {}
        fields, rows = read_rows(self.records_csv)
        for row in reversed(rows):  # 从最新的一张开始找
            if (row.get("code") or "").strip() == code:
                return row
        return {}

    # ------------------------------------------------------------------
    # 1) 查真伪（只读，不改任何数据）
    # ------------------------------------------------------------------
    def verify(self, code: str) -> dict:
        """
        校验核销码真伪，返回给网页后台展示用的信息。

        返回字典里的关键字段：
            ok           : 是否为系统发放的正品凭证
            can_redeem   : 现在能不能直接核销
            verdict      : 一句话结论（给工作人员看的大白话）
            level        : ok / warn / error，前端据此配色
        """
        code = (code or "").strip()
        if not (code.isdigit() and len(code) == 6):
            return self._result(False, False, "error", "核销码应为 6 位数字，请重新输入", code=code)

        with csv_transaction(self.codes_csv):
            fields, rows = read_rows(self.codes_csv)
        row = next((r for r in rows if (r.get("code") or "").strip() == code), None)

        if row is None:
            # 码池里根本没有这个码：要么输错了，要么是伪造的
            self.vault.write_audit(f"核销查询：查无此码 {self.vault.mask_code(code)}")
            return self._result(False, False, "error", "查无此码，疑似伪造凭证", code=code)

        status = (row.get("status") or "").strip()
        raw_memory = row.get("memory") or ""
        has_watermark = self.vault.verify_hidden_watermark(raw_memory)
        ticket = self._find_ticket(code)
        memory = self._strip_zero_width(raw_memory)

        base = {
            "code": code,
            "status": status,
            "memory": memory,
            "ticket_no": (ticket.get("ticket_no") or "").strip(),
            "action_id": (ticket.get("action_id") or "").strip(),
            "issued_at": (row.get("used_at") or ticket.get("issued_at") or "").strip(),
            "redeemed_at": (row.get("redeemed_at") or "").strip(),
            "redeemed_by": (row.get("redeemed_by") or "").strip(),
            "watermark": has_watermark,
        }

        # —— 情况 A：码在池子里，但从没发出过小票（有人拿码池原始码来碰运气）——
        if status == self.STATUS_UNUSED or not (row.get("used_at") or "").strip():
            self.vault.write_audit(f"核销查询：命中未发放的原始码 {self.vault.mask_code(code)}，已拒绝")
            return self._result(False, False, "warn", "该码尚未发放（不是有效凭证），请拒绝兑换", **base)

        # —— 情况 B：已经兑换过 ——
        if status == self.redeemed_status:
            self.vault.write_audit(f"核销查询：重复出示已兑换码 {self.vault.mask_code(code)}")
            return self._result(
                True, False, "warn",
                f"该凭证已于 {base['redeemed_at'] or '此前'} 兑换过，请勿重复发放礼品",
                **base,
            )

        # —— 情况 C：正常待核销的凭证 ——
        if status == self.STATUS_ISSUED:
            if not has_watermark:
                self.vault.write_audit(f"核销查询：水印异常 {self.vault.mask_code(code)}")
                return self._result(
                    True, False, "warn",
                    "凭证存在但版权水印缺失，数据可能被改动，需二级口令授权后方可核销",
                    **base,
                )
            self.vault.write_audit(f"核销查询：正品凭证 {self.vault.mask_code(code)}")
            return self._result(True, True, "ok", "正品凭证 · 等待发放礼品", **base)

        # —— 情况 D：状态被手工改成别的奇怪值 ——
        self.vault.write_audit(f"核销查询：状态异常 {self.vault.mask_code(code)} -> {status}")
        return self._result(True, False, "warn", f"凭证状态异常（{status}），请人工复核", **base)

    # ------------------------------------------------------------------
    # 2) 核销（写操作：把状态改成"已兑换礼品"）
    # ------------------------------------------------------------------
    def redeem(self, code: str, operator: str = "现场工作人员", admin_code: str = "") -> dict:
        """
        核销一张凭证。整个过程在同一个文件锁事务内完成，防止两个人同时核销同一个码。

        admin_code：二级底层总钥匙（modules/core_auth.py 里的硬编码常量）。
                    只有「水印缺失」这种异常情况才需要，正常核销不用麻烦管理员。
        """
        code = (code or "").strip()
        operator = (operator or "现场工作人员").strip()[:20]

        # 先做一轮只读校验，把明显不合格的直接挡掉（省得白跑一次写锁）
        check = self.verify(code)
        if not check["ok"]:
            return check
        if check.get("status") == self.redeemed_status:
            # 凭证是真的，但这次核销并没有发生 —— 所以 ok 要返回 False
            return self._as_already(check)
        if not check["can_redeem"]:
            # 水印异常：必须二级鉴权才允许"强行核销"
            if not self.vault.verify_passcode(admin_code, level=2):
                check["need_admin"] = True
                check["verdict"] = "数据异常，需输入二级底层总钥匙授权核销"
                return check

        with csv_transaction(self.codes_csv):
            fields, rows = read_rows(self.codes_csv)
            fields = ensure_columns(fields, rows, self.EXTRA_COLUMNS)

            target = None
            for row in rows:
                if (row.get("code") or "").strip() == code:
                    target = row
                    break

            # 拿到锁之后**再检查一次状态**（双检锁）：
            # 因为刚才的 verify 是在锁外做的，这期间可能有另一个工作人员先核销掉了。
            if target is None:
                return self._result(False, False, "error", "查无此码，疑似伪造凭证", code=code)
            if (target.get("status") or "").strip() == self.redeemed_status:
                return self._as_already(self._result(
                    True, False, "warn", "该凭证刚刚已被兑换过，请勿重复发放礼品",
                    code=code, status=target.get("status"),
                    redeemed_at=target.get("redeemed_at", ""),
                    redeemed_by=target.get("redeemed_by", ""),
                ))

            now_str = self._now()

            target["status"] = self.redeemed_status
            target["redeemed_at"] = now_str
            target["redeemed_by"] = operator

            write_rows_atomic(self.codes_csv, fields, rows)

        ticket = self._find_ticket(code)
        memory_raw = (target.get("memory") or "")
        # 流水中保留带水印的原始记忆点（内部溯源用），界面回显时再剥掉
        redeem_no = self._append_log(
            code=code,
            ticket_no=(ticket.get("ticket_no") or "").strip(),
            memory=memory_raw,
            operator=operator,
            result=self.redeemed_status,
        )

        self.vault.write_audit(
            f"核销成功 {self.vault.mask_code(code)} 操作员={operator} 流水号={redeem_no}"
        )
        return self._result(
            True, False, "ok", "核销成功，请把 3D 打印礼品交给同学～",
            code=code,
            status=self.redeemed_status,
            memory=self._strip_zero_width(memory_raw),
            ticket_no=(ticket.get("ticket_no") or "").strip(),
            action_id=(ticket.get("action_id") or "").strip(),
            issued_at=(target.get("used_at") or "").strip(),
            redeemed_at=now_str,
            redeemed_by=operator,
            redeem_no=redeem_no,
            watermark=True,
            redeemed_now=True,
        )

    # ------------------------------------------------------------------
    # 3) 撤销核销（敏感操作，必须二级底层总钥匙）
    # ------------------------------------------------------------------
    def revoke(self, code: str, admin_code: str, operator: str = "管理员") -> dict:
        """把「已兑换礼品」回滚成「已使用」，用于发错礼品、误操作等场景"""
        code = (code or "").strip()
        if not self.vault.verify_passcode(admin_code, level=2):
            return self._result(False, False, "error", "二级底层总钥匙错误，撤销已被阻断", code=code)

        with csv_transaction(self.codes_csv):
            fields, rows = read_rows(self.codes_csv)
            fields = ensure_columns(fields, rows, self.EXTRA_COLUMNS)
            target = next((r for r in rows if (r.get("code") or "").strip() == code), None)
            if target is None:
                return self._result(False, False, "error", "查无此码", code=code)
            if (target.get("status") or "").strip() != self.redeemed_status:
                return self._result(
                    False, False, "warn", "该凭证当前不是「已兑换」状态，无需撤销",
                    code=code, status=target.get("status", ""),
                )

            target["status"] = self.STATUS_ISSUED
            target["redeemed_at"] = ""
            target["redeemed_by"] = ""
            write_rows_atomic(self.codes_csv, fields, rows)

        self._append_log(
            code=code, ticket_no="", memory="", operator=operator,
            result="撤销核销（回滚为已使用）",
        )
        self.vault.write_audit(f"撤销核销 {self.vault.mask_code(code)} 操作员={operator}")
        return self._result(
            True, True, "ok", "已撤销核销，该凭证恢复为「待兑换」",
            code=code, status=self.STATUS_ISSUED, revoked=True,
        )

    # ------------------------------------------------------------------
    # 4) 统计与流水（给网页后台的看板用）
    # ------------------------------------------------------------------
    def stats(self) -> dict:
        """码池总览：总数 / 未使用 / 待兑换 / 已兑换 / 今日核销"""
        with csv_transaction(self.codes_csv):
            fields, rows = read_rows(self.codes_csv)

        total = len(rows)
        unused = sum(1 for r in rows if (r.get("status") or "").strip() == self.STATUS_UNUSED)
        redeemed = sum(1 for r in rows if (r.get("status") or "").strip() == self.redeemed_status)
        issued = total - unused - redeemed
        today = date.today().strftime("%Y-%m-%d")
        today_redeemed = sum(
            1 for r in rows
            if (r.get("status") or "").strip() == self.redeemed_status
            and (r.get("redeemed_at") or "").startswith(today)
        )
        return {
            "total": total,
            "unused": unused,
            "issued": max(issued, 0),
            "redeemed": redeemed,
            "today_redeemed": today_redeemed,
        }

    def recent_redemptions(self, limit: int = 8) -> list:
        """最近几条核销流水（新的在最前面）"""
        with csv_transaction(self.redeem_log_csv):
            fields, rows = read_rows(self.redeem_log_csv)
        items = []
        for row in reversed(rows[-200:]):
            items.append({
                "redeem_no": row.get("redeem_no", ""),
                "code": self.vault.mask_code(row.get("code", "")),
                "ticket_no": row.get("ticket_no", ""),
                "memory": self._strip_zero_width(row.get("memory", "")),
                "operator": row.get("operator", ""),
                "operated_at": row.get("operated_at", ""),
                "result": row.get("result", ""),
            })
            if len(items) >= limit:
                break
        return items

    # ------------------------------------------------------------------
    # 内部小工具
    # ------------------------------------------------------------------
    def _next_redeem_no(self) -> str:
        """生成核销流水号：R20261113-0001（当天递增）"""
        today = date.today().strftime("%Y%m%d")
        fields, rows = read_rows(self.redeem_log_csv)
        seq = 1
        for row in rows:
            no = row.get("redeem_no") or ""
            if no.startswith(f"R{today}-"):
                try:
                    seq = max(seq, int(no.split("-")[-1]) + 1)
                except ValueError:
                    pass
        return f"R{today}-{seq:04d}"

    def _append_log(self, code: str, ticket_no: str, memory: str, operator: str, result: str) -> str:
        """
        追加一条核销流水，返回本次的流水号。

        ⚠️ 关键点：流水号的"分配"和"追加"必须在**同一个日志文件锁**里完成，
        否则两个工作人员同时核销时，可能各自算出同一个 R2026xxxx-0007，出现重号。
        """
        with csv_transaction(self.redeem_log_csv):
            fields, rows = read_rows(self.redeem_log_csv)
            if not fields:
                fields = ["redeem_no", "code", "ticket_no", "memory", "operator", "operated_at", "result"]
            no = self._next_redeem_no()
            rows.append({
                "redeem_no": no,
                "code": code,
                "ticket_no": ticket_no,
                "memory": memory,
                "operator": operator,
                "operated_at": self._now(),
                "result": result,
            })
            write_rows_atomic(self.redeem_log_csv, fields, rows)
        return no

    @staticmethod
    def _as_already(result: dict) -> dict:
        """
        凭证是真的，但"本次核销"并没有发生（之前已经兑换过）。
        这时 ok 必须是 False —— 否则网页后台会以为核销成功、给同学重复发礼品。
        前端可以用 already_redeemed 字段区分"假码"和"已兑换过"。
        """
        result["ok"] = False
        result["can_redeem"] = False
        result["already_redeemed"] = True
        result["level"] = "warn"
        return result

    @staticmethod
    def _result(ok, can_redeem, level, verdict, **extra) -> dict:
        """
        统一的结果格式，前端只需要认这 4 个字段：
            ok / can_redeem / level(ok|warn|error) / verdict
        其它字段（记忆点、作品编号等）都是附带的展示信息。
        """
        data = {"ok": ok, "can_redeem": can_redeem, "level": level, "verdict": verdict}
        data.update({k: v for k, v in extra.items() if v is not None})
        return data
