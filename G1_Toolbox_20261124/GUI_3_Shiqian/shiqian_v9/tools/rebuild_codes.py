#!/usr/bin/env python
"""
tools/rebuild_codes.py —— 核销码池维护工具（v5 新增）
=====================================================

【什么时候用？】
    · 活动前想检查一下 100 个码有没有重复、格式对不对；
    · 码快发完了，想追加 50 个新码；
    · 文件被同学误删 / 手改乱了，想重建一份干净的（会先自动备份！）；
    · 老师想用 SQL 做统计，导出一份 SQLite 只读镜像。

【用法】在项目根目录执行：

    python tools/rebuild_codes.py --check              # 只检查，不改任何东西
    python tools/rebuild_codes.py --append 50          # 追加 50 个新码（原有记录保持不变）
    python tools/rebuild_codes.py --rebuild --force    # 重建 100 个全新码（会先备份旧文件）
    python tools/rebuild_codes.py --export-sqlite      # 导出 data/codes.db 供 SQL 查询

【安全约定】
    · 任何"写"操作之前都会把原文件备份到 data/backup/，文件名带时间戳；
    · 重建（--rebuild）会让旧小票上的码"复活"，只允许在**活动开始前**做，
      所以必须显式加 --force，防止手滑；
    · 所有读写都走 modules/atomic_csv.py 的文件锁，网页核销后台开着也不会写坏。
"""

import os
import sys
import shutil
import secrets
import argparse
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

from modules.atomic_csv import (  # noqa: E402
    csv_transaction,
    read_rows,
    write_rows_atomic,
    ensure_columns,
)
from modules.redemption_manager import RedemptionManager  # noqa: E402

CODES_CSV = os.path.join("data", "codes.csv")
COLUMNS = ["code", "status", "used_at", "memory"] + RedemptionManager.EXTRA_COLUMNS
CODE_PATTERN_LEN = 6


def gen_codes(count: int, existing: set) -> list:
    """生成 count 个不在 existing 里的 6 位码（100000~999999）"""
    new_codes = []
    while len(new_codes) < count:
        code = str(secrets.randbelow(900000) + 100000)
        if code not in existing:
            existing.add(code)
            new_codes.append(code)
    return new_codes


def backup(src: str) -> str:
    """写操作前先备份，出问题还能找回来"""
    folder = os.path.join("data", "backup")
    os.makedirs(folder, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = os.path.join(folder, f"codes_{stamp}.csv")
    shutil.copy2(src, dst)
    print(f"🗂️ 已备份原文件 -> {dst}")
    return dst


def report(rows: list):
    """打印码池统计与异常明细"""
    total = len(rows)
    counter = {}
    seen, dup, bad = {}, [], []
    for row in rows:
        code = (row.get("code") or "").strip()
        state = (row.get("status") or "").strip() or "(空)"
        counter[state] = counter.get(state, 0) + 1
        if not (code.isdigit() and len(code) == CODE_PATTERN_LEN):
            bad.append(code)
        seen[code] = seen.get(code, 0) + 1
    dup = [code for code, n in seen.items() if n > 1]

    print(f"\n📊 码池统计：共 {total} 条")
    for state, count in sorted(counter.items()):
        print(f"   · {state}：{count} 条")
    print(f"   · 未使用余量：{counter.get(RedemptionManager.STATUS_UNUSED, 0)} 条")

    if dup:
        print(f"\n❌ 发现 {len(dup)} 个重复码：{dup[:10]}")
    if bad:
        print(f"\n❌ 发现 {len(bad)} 个格式异常码（应为 6 位数字）：{bad[:10]}")
    if not dup and not bad:
        print("\n✅ 没有重复码，格式全部正常")

    return not dup and not bad


def main():
    parser = argparse.ArgumentParser(description="「时迁」核销码池维护工具")
    parser.add_argument("--check", action="store_true", help="只检查，不修改")
    parser.add_argument("--append", type=int, metavar="N", help="追加 N 个新码（保留原有记录）")
    parser.add_argument("--rebuild", action="store_true", help="重建整个码池（必须配合 --force）")
    parser.add_argument("--force", action="store_true", help="确认执行重建（会重置所有码）")
    parser.add_argument("--size", type=int, default=100, help="重建时的码数量，默认 100")
    parser.add_argument("--export-sqlite", action="store_true", help="导出 SQLite 只读镜像 data/codes.db")
    args = parser.parse_args()

    if not os.path.exists(CODES_CSV):
        print(f"⚠️ 找不到 {CODES_CSV}")
        if args.rebuild and args.force:
            pass
        else:
            print("   如果这是第一次运行，直接启动主程序会自动生成 100 个码；")
            print("   也可以执行：python tools/rebuild_codes.py --rebuild --force")
            if not (args.append or args.export_sqlite):
                return

    fields, rows = read_rows(CODES_CSV) if os.path.exists(CODES_CSV) else ([], [])

    # ---------------- 检查 ----------------
    if args.check or not (args.append or args.rebuild or args.export_sqlite):
        ok = report(rows)
        sys.exit(0 if ok else 1)

    # ---------------- 追加 ----------------
    if args.append:
        if args.append <= 0:
            print("❌ 追加数量必须大于 0")
            return
        if os.path.exists(CODES_CSV):
            backup(CODES_CSV)
        with csv_transaction(CODES_CSV):
            fields, rows = read_rows(CODES_CSV)
            fields = ensure_columns(fields or COLUMNS, rows, COLUMNS)
            existing = {(r.get("code") or "").strip() for r in rows}
            for code in gen_codes(args.append, existing):
                rows.append({
                    "code": code,
                    "status": RedemptionManager.STATUS_UNUSED,
                    "used_at": "",
                    "memory": "",
                    "redeemed_at": "",
                    "redeemed_by": "",
                })
            write_rows_atomic(CODES_CSV, fields, rows)
        print(f"✅ 已追加 {args.append} 个新码，当前共 {len(rows)} 条")
        report(rows)
        return

    # ---------------- 重建 ----------------
    if args.rebuild:
        if not args.force:
            print("⚠️ 重建会把所有码重置为「未使用」，已经发出去的小票会重新变得可兑换！")
            print("   如果确认在活动开始前重建，请重新执行：")
            print(f"   python tools/rebuild_codes.py --rebuild --force --size {args.size}")
            return
        if os.path.exists(CODES_CSV):
            backup(CODES_CSV)
            _, old_rows = read_rows(CODES_CSV)
            used = sum(1 for r in old_rows if (r.get("status") or "").strip()
                       != RedemptionManager.STATUS_UNUSED)
            if used:
                print(f"⚠️ 注意：旧文件里有 {used} 个码已经发出/兑换过，重建后这些码将重新可用。")
        codes = gen_codes(args.size, set())
        new_rows = [{
            "code": code,
            "status": RedemptionManager.STATUS_UNUSED,
            "used_at": "",
            "memory": "",
            "redeemed_at": "",
            "redeemed_by": "",
        } for code in codes]
        with csv_transaction(CODES_CSV):
            write_rows_atomic(CODES_CSV, COLUMNS, new_rows)
        print(f"✅ 已重建码池：{args.size} 个全新唯一码")
        report(new_rows)
        return

    # ---------------- 导出 SQLite ----------------
    if args.export_sqlite:
        # 只读镜像：把 CSV 拷贝进 SQLite，方便老师用 SQL 做统计，
        # 但程序本身仍然以 CSV 为准（避免两个数据源打架）。
        import sqlite3
        db_path = os.path.join("data", "codes.db")
        conn = sqlite3.connect(db_path)
        try:
            conn.execute("DROP TABLE IF EXISTS codes")
            conn.execute(
                "CREATE TABLE codes (code TEXT PRIMARY KEY, status TEXT, used_at TEXT,"
                " memory TEXT, redeemed_at TEXT, redeemed_by TEXT)"
            )
            conn.executemany(
                "INSERT OR REPLACE INTO codes VALUES (?,?,?,?,?,?)",
                [(r.get("code", ""), r.get("status", ""), r.get("used_at", ""),
                  r.get("memory", ""), r.get("redeemed_at", ""), r.get("redeemed_by", ""))
                 for r in rows],
            )
            conn.commit()
        finally:
            conn.close()
        print(f"✅ 已导出 SQLite 镜像：{db_path}（共 {len(rows)} 条，可用 DB Browser 打开查 SQL）")


if __name__ == "__main__":
    main()
