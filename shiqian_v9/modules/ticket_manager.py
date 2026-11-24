"""
ticket_manager.py —— 小票与核销码分发模块 · v5 加固版
======================================================

【这个文件干什么？】
    每完成一次"记忆点 + 动作"，就发一张小票，小票上有一个**绝不重复**的 6 位核销码。
    同学拿着这张小票，到礼品台凭码兑换 3D 打印小礼品。

【为什么用 CSV 而不是 SQLite？】
    · 学生团队要能直接用 Excel 打开检查"哪些码发了、哪些还没发"，CSV 最直观；
    · 数据量只有 100 行，性能根本不是问题；
    · 真正的风险不是"数据库选型"，而是"两个程序同时写同一个文件"。
      所以这里用 modules/atomic_csv.py 提供的
      「进程内线程锁 + 跨进程文件锁 + 临时文件原子替换」三层保护，
      出票程序（main.py）和核销后台（web_redeem_server.py）同时跑也不会发重、不会覆盖。
    （如果以后数据量涨到几万条，再换 SQLite：只要保证
      `UPDATE codes SET status='已使用' WHERE code=(SELECT code FROM codes WHERE status='未使用' LIMIT 1)`
      在一个事务里完成，思路和这里的"锁内读-改-写"是一回事。）

【v5 相比 v3 的改进】
1. 码池自愈：codes.csv 被误删 / 被清空时，自动重新生成 100 个唯一 6 位码，
   不用再手工跑脚本（以前会直接抛异常，活动现场很难受）。
2. 完整性体检：启动时检查"有没有重复码 / 有没有格式不对的码"，
   发现异常会明确提示用 tools/rebuild_codes.py 重建，而不是带着脏数据继续发。
3. 余量预警：剩余不足 10 张时每发一张都提醒一次，方便提前补码。
4. 流水号改成"取历史最大值 +1"：以前用"行数 +1"，手工删掉一行就会重号。
5. 发码时顺带清空 redeemed_at / redeemed_by 等旧字段，
   避免同一行被复用时把上一轮的核销信息带出来。
"""

import os
import re
import secrets
from datetime import datetime

from modules.portable_printer import PortablePrinterDriver
from modules.atomic_csv import (
    csv_transaction,
    read_rows,
    write_rows_atomic,
    append_row_atomic,
    ensure_columns,
)
# 核销后台会给 codes.csv 增加这两列（核销时间 / 操作员）。
# 这里也主动补齐，保证「出票」和「核销」两个进程写出来的表头完全一致。
from modules.redemption_manager import RedemptionManager

CODES_V4_COLUMNS = RedemptionManager.EXTRA_COLUMNS

# 核销码规范：6 位纯数字（100000~999999，不会有前导 0，同学念起来不容易错）
CODE_PATTERN = re.compile(r"^\d{6}$")

BASE_COLUMNS = ["code", "status", "used_at", "memory"]


class TicketManager:
    """唯一核销码 + 小票生成 + 便携打印。"""

    def __init__(self, cfg: dict, printer_cfg: dict):
        self.codes_csv = cfg.get("codes_csv_path", "data/codes.csv")
        self.records_csv = cfg.get("records_csv_path", "data/issued_tickets.csv")
        self.pool_size = int(cfg.get("pool_size", 100))
        self.low_watermark = int(cfg.get("low_watermark", 10))
        self.printer = PortablePrinterDriver(printer_cfg)

        self._ensure_files()
        self._ensure_pool()

    # ==================================================================
    # 一、初始化：确保文件与码池存在
    # ==================================================================
    def _ensure_files(self):
        """确保小票流水文件存在（表头写好，Excel 打开就能看）"""
        folder = os.path.dirname(self.records_csv)
        if folder:
            os.makedirs(folder, exist_ok=True)
        if not os.path.exists(self.records_csv):
            with csv_transaction(self.records_csv):
                write_rows_atomic(
                    self.records_csv,
                    ["ticket_no", "code", "action_id", "memory", "issued_at"],
                    [],
                )

    def _ensure_pool(self):
        """
        码池自愈：文件不存在 / 完全空 -> 生成 100 个唯一码。
        注意：如果文件里还有数据（哪怕全用完了），这里**不会**重新生成，
        因为重新生成会让旧小票的码"复活"，造成一个码被兑换两次。
        """
        with csv_transaction(self.codes_csv):
            fields, rows = read_rows(self.codes_csv)
            if rows:
                return
            codes = self._generate_unique_codes(self.pool_size)
            fields = BASE_COLUMNS + CODES_V4_COLUMNS
            rows = [{
                "code": code,
                "status": RedemptionManager.STATUS_UNUSED,
                "used_at": "",
                "memory": "",
                "redeemed_at": "",
                "redeemed_by": "",
            } for code in codes]
            write_rows_atomic(self.codes_csv, fields, rows)
        print(f"[小票模块] 已自动生成 {self.pool_size} 个唯一核销码 -> {self.codes_csv}")

    @staticmethod
    def _generate_unique_codes(count: int) -> list:
        """
        生成 count 个互不相同的 6 位码。
        用 secrets 而不是 random：secrets 是密码学安全的随机源，
        别人更难通过"猜码"的方式伪造凭证。
        """
        codes, seen = [], set()
        while len(codes) < count:
            code = str(secrets.randbelow(900000) + 100000)   # 100000 ~ 999999
            if code not in seen:
                seen.add(code)
                codes.append(code)
        return codes

    # ==================================================================
    # 二、码池体检（启动自检、tools/selfcheck.py 都会调用）
    # ==================================================================
    def pool_status(self) -> dict:
        """统计码池使用情况，并检查重复码 / 格式异常。"""
        fields, rows = read_rows(self.codes_csv)
        status = {
            "total": len(rows),
            "unused": 0,
            "issued": 0,       # 已出票（待兑换）
            "redeemed": 0,     # 已兑换礼品
            "duplicates": [],
            "bad_format": [],
            "ok": True,
        }
        seen = {}
        for row in rows:
            code = (row.get("code") or "").strip()
            if not CODE_PATTERN.match(code):
                status["bad_format"].append(code)
            else:
                seen[code] = seen.get(code, 0) + 1
            state = (row.get("status") or "").strip()
            if state == RedemptionManager.STATUS_UNUSED:
                status["unused"] += 1
            elif state == RedemptionManager.STATUS_ISSUED:
                status["issued"] += 1
            else:
                status["redeemed"] += 1

        status["duplicates"] = [code for code, n in seen.items() if n > 1]
        status["ok"] = not status["duplicates"] and not status["bad_format"]
        status["remaining"] = status["unused"]
        return status

    def preflight(self) -> dict:
        """启动时打印一行码池状态，并给出明确建议。"""
        info = self.pool_status()
        if info["ok"]:
            print(f"[小票自检] ✅ 码池共 {info['total']} 个，未发 {info['unused']}，"
                  f"已发 {info['issued']}，已兑换 {info['redeemed']}")
            if info["unused"] <= self.low_watermark:
                print(f"[小票自检] ⚠️ 剩余不足 {self.low_watermark} 张，请尽快补码"
                      f"（python tools/rebuild_codes.py --append 100）")
        else:
            print(f"[小票自检] ❌ 码池异常：重复码 {len(info['duplicates'])} 个、"
                  f"格式错误 {len(info['bad_format'])} 个，请运行 python tools/rebuild_codes.py 检查")
        return info

    # ==================================================================
    # 三、出票主流程
    # ==================================================================
    def issue_ticket(self, memory: str, watermarked_memory: str,
                     action_id: str, action_name: str = "") -> dict:
        """
        加锁读取一条"未使用"的核销码 -> 状态改为"已使用" -> 写流水 -> 出票。
        整个「读 -> 改 -> 写」都在同一把文件锁里，绝不会发重。
        """
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with csv_transaction(self.codes_csv):
            fieldnames, rows = read_rows(self.codes_csv)
            if not fieldnames:
                fieldnames = BASE_COLUMNS
            fieldnames = ensure_columns(fieldnames, rows, CODES_V4_COLUMNS)

            # 发码前先做一次重复码检查：一旦重复，宁可停下也不发（否则可能一码兑两份）
            codes = [(row.get("code") or "").strip() for row in rows]
            if len(codes) != len(set(codes)):
                raise RuntimeError(
                    "codes.csv 里存在重复的核销码，为避免一码兑两份已停止发码。\n"
                    "请运行：python tools/rebuild_codes.py --check  查看详情"
                )

            selected = None
            for row in rows:
                if (row.get("status") or "").strip() == RedemptionManager.STATUS_UNUSED:
                    row["status"] = RedemptionManager.STATUS_ISSUED
                    row["used_at"] = now_str
                    # CSV 中存入带不可见零宽水印的记忆点，表面看与正常文字无异
                    row["memory"] = watermarked_memory
                    # 复用同一行时，必须把上一轮的核销痕迹清干净
                    row["redeemed_at"] = ""
                    row["redeemed_by"] = ""
                    selected = row
                    break

            if selected is None:
                raise RuntimeError(
                    "核销码池已发完！请运行 python tools/rebuild_codes.py --append 100 追加新码。"
                )

            write_rows_atomic(self.codes_csv, fieldnames, rows)

            # 流水号：取历史最大值 +1，避免手工删行导致重号
            rec_fields, rec_rows = read_rows(self.records_csv)
            ticket_no = self._next_ticket_no(rec_rows)

            append_row_atomic(
                self.records_csv,
                ["ticket_no", "code", "action_id", "memory", "issued_at"],
                {
                    "ticket_no": ticket_no,
                    "code": selected["code"],
                    "action_id": action_id,
                    "memory": watermarked_memory,
                    "issued_at": now_str,
                },
            )
            remaining = sum(
                1 for row in rows
                if (row.get("status") or "").strip() == RedemptionManager.STATUS_UNUSED
            )

        ticket = {
            "ticket_no": ticket_no,
            "code": selected["code"],
            "action_id": action_id,
            "action_name": action_name or f"Pose-{action_id}",
            "memory": memory,
            "issued_at": now_str,
            "remaining": remaining,
        }
        self._print_console_ticket(ticket)
        self.printer.print_ticket(ticket)

        if remaining <= self.low_watermark:
            print(f"⚠️ [小票提醒] 码池只剩 {remaining} 张，请尽快补码！")
        return ticket

    def _next_ticket_no(self, rec_rows: list) -> str:
        """
        生成 #2026-0001 这样的流水号。
        取所有历史编号里的最大序号 +1，而不是"行数+1"——
        因为手工删掉一行记录时，行数会倒退，就会发出重复的编号。
        """
        year = datetime.now().year
        max_seq = 0
        for row in rec_rows:
            raw = (row.get("ticket_no") or "").strip()
            m = re.match(r"^#\d{4}-(\d+)$", raw)
            if m:
                try:
                    max_seq = max(max_seq, int(m.group(1)))
                except ValueError:
                    pass
        return f"#{year}-{max_seq + 1:04d}"

    @staticmethod
    def _print_console_ticket(t: dict):
        print("\n" + "=" * 44)
        print("   🎫 G1 校园数字共创 · 专属创作凭证")
        print(f"   作品编号：{t['ticket_no']}")
        print(f"   完成时间：{t['issued_at']}")
        print(f"   特定记忆点：「{t['memory']}」")
        print(f"   光影姿态编号：[ Pose-{t['action_id']} ] {t.get('action_name', '')}")
        print(f"   专属核销码：{t['code']} (凭码兑换3D礼品)")
        print(f"   码池剩余：{t.get('remaining', '?')} 张")
        print("=" * 44 + "\n")
