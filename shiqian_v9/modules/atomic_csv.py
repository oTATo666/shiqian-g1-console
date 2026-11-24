"""
atomic_csv.py —— 「时迁」项目的 CSV 原子读写小工具箱（增量模块）
================================================================

【为什么需要这个文件？】
原来的出票程序（ticket_manager.py）用 `threading.Lock()` 保护 codes.csv，
这在一个程序内部是绝对安全的。

但现在我们新增了「网页核销后台」（web_redeem_server.py），它是**另一个独立进程**：
    ┌──────────────────┐        ┌────────────────────┐
    │  main.py 出票进程 │        │  网页核销后台进程   │
    └────────┬─────────┘        └─────────┬──────────┘
             └──────────► data/codes.csv ◄┘
两个进程同时读写同一个 CSV 时，`threading.Lock()` 是**管不到对方**的
（线程锁只在同一个进程内部有效），于是就可能出现：
    · 出票进程读到"未使用" -> 网页进程同时把同一个码核销掉 -> 出票进程把旧内容写回去，核销记录被覆盖丢失。

所以本模块提供两层锁，缺一不可：
    第 1 层：threading.Lock（进程内的多个线程互斥，比如 FastAPI 的多个请求线程）
    第 2 层：文件锁（跨进程互斥，Windows 用 msvcrt，Mac/Linux 用 fcntl）
并且写入统一采用「写临时文件 -> os.replace 原子替换」，
保证任何时刻 codes.csv 要么是旧的完整内容，要么是新的完整内容，绝不会出现写了一半的残缺文件。

【怎么用？】—— 记住一个写法就够了：

    with csv_transaction("data/codes.csv"):
        fields, rows = read_rows("data/codes.csv")
        ...            # 在这里改 rows
        write_rows_atomic("data/codes.csv", fields, rows)

只要把「读 -> 改 -> 写」三步都放在 `with csv_transaction(...)` 里面，
就一定不会和别人抢同一个文件。
"""

import csv
import os
import time
import threading
from contextlib import contextmanager

# Windows 与 Mac/Linux 的文件加锁方式不同，这里做自动适配
if os.name == "nt":
    import msvcrt
else:
    import fcntl

# 第 1 层锁：进程内线程锁。
# 用 RLock（可重入锁）是为了允许在同一个线程里嵌套使用 csv_transaction，
# 例如「出票时要同时锁 codes.csv 和 issued_tickets.csv」，不会自己把自己锁死。
_THREAD_LOCK = threading.RLock()

# 拿不到文件锁时最多等待多少秒（防止某个进程卡死导致其他程序永远等待）
_LOCK_TIMEOUT = 10.0


@contextmanager
def csv_transaction(path: str):
    """
    给某个 CSV 文件加「进程内线程锁 + 跨进程文件锁」，用法见文件开头说明。

    注意：锁文件是 `xxx.csv.lock`（一个 0 字节的占位文件），
         它只是用来当"钥匙"，不要删除也不要打开编辑。
    """
    with _THREAD_LOCK:
        folder = os.path.dirname(os.path.abspath(path))
        os.makedirs(folder, exist_ok=True)
        lock_path = path + ".lock"

        # 用 "a+b" 打开锁文件：文件不存在会自动创建，且不会清空已有内容
        lock_file = open(lock_path, "a+b")
        locked = False
        try:
            _acquire_file_lock(lock_file, path)
            locked = True
            yield
        finally:
            if locked:
                _release_file_lock(lock_file)
            lock_file.close()


def _acquire_file_lock(lock_file, path: str):
    """跨进程加锁（拿不到就小睡 20ms 再试，最多等 _LOCK_TIMEOUT 秒）"""
    deadline = time.time() + _LOCK_TIMEOUT
    while True:
        try:
            lock_file.seek(0)
            if os.name == "nt":
                # LK_NBLCK = 不阻塞的独占锁；被别的进程占着会立刻抛 OSError
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError:
            if time.time() > deadline:
                raise RuntimeError(
                    f"等待文件锁超时（{path}）：可能有另一个程序正在写入该文件，请稍后重试。"
                )
            time.sleep(0.02)


def _release_file_lock(lock_file):
    """释放跨进程文件锁"""
    try:
        lock_file.seek(0)
        if os.name == "nt":
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass  # 文件已关闭/已被系统回收时忽略即可


def read_rows(path: str):
    """
    读取整个 CSV，返回 (表头字段列表, 每一行的字典列表)。
    文件不存在时返回 ([], [])，方便调用方自己决定初始化逻辑。

    编码用 utf-8-sig：这样用 Excel 打开也不会出现中文乱码，
    同时还能正确读取带 BOM 的旧文件（例如 codes.csv）。
    零宽水印字符属于普通 Unicode 字符，读写过程中会原样保留，不会被破坏。
    """
    if not os.path.exists(path):
        return [], []
    with open(path, "r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = [dict(row) for row in reader]
    return fieldnames, rows


def write_rows_atomic(path: str, fieldnames: list, rows: list):
    """
    原子写：先把内容写进同目录的临时文件，再用 os.replace 原子替换正式文件。

    · 同目录是必须的（跨磁盘的 os.replace 不是原子操作）
    · 出现任何异常时临时文件会被删掉，正式文件保持原样，不会变脏
    """
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    tmp_path = os.path.join(folder, f".{os.path.basename(path)}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with open(tmp_path, "w", newline="", encoding="utf-8-sig") as f:
            # extrasaction="ignore"：行里多余的键不会报错，方便后续版本加字段
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows([{k: ("" if v is None else v) for k, v in row.items()} for row in rows])
            f.flush()
            os.fsync(f.fileno())  # 强制落盘，避免突然断电留下空文件
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def ensure_columns(fieldnames: list, rows: list, extra_columns: list) -> list:
    """
    给老 CSV「补列」：如果 codes.csv 里还没有 redeemed_at / redeemed_by 这些新列，
    就在这里补上（老数据对应位置填空字符串），返回新的表头。

    这样 v3 版本生成的老 codes.csv 可以无缝升级，不需要手动改文件。
    """
    fields = list(fieldnames)
    for col in extra_columns:
        if col not in fields:
            fields.append(col)
            for row in rows:
                row.setdefault(col, "")
    return fields


def append_row_atomic(path: str, fieldnames: list, row: dict):
    """
    往 CSV 末尾追加一行的「原子版本」。
    严格来说 CSV 的纯追加也能做到原子，但为了和上面的读写保持同一套锁与编码，
    这里统一走 read + write_rows_atomic（本项目数据量为百级，性能完全够用）。
    """
    fields, rows = read_rows(path)
    if not fields:
        fields = list(fieldnames)
    fields = ensure_columns(fields, rows, list(row.keys()))
    rows.append(row)
    write_rows_atomic(path, fields, rows)
