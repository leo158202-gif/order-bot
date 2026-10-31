"""按数据库绝对路径互斥批次；锁文件存在不代表正在持锁。"""
from __future__ import annotations

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import threading


class BatchBusyError(Exception):
    """同一数据库已有批次持有执行锁。"""


_registry_guard = threading.Lock()
_thread_locks: dict[str, threading.Lock] = {}


def _lock_descriptor(descriptor: int) -> None:
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


@contextmanager
def batch_execution_lock(db_identity: str | os.PathLike = "data/db.sqlite"):
    """非阻塞获取线程及OS锁；关闭句柄或进程退出即释放，不打开数据库。

    调用方须将会话创建、业务执行及关闭都置于此上下文内。同一物理数据库
    的所有入口必须传同一路径；内存数据库应由调用方提供独立的临时文件标识。
    """
    database = Path(db_identity).expanduser().resolve()
    identity = os.path.normcase(str(database))
    with _registry_guard:
        local_lock = _thread_locks.setdefault(identity, threading.Lock())
    if not local_lock.acquire(blocking=False):
        raise BatchBusyError("正在处理上一批，请稍后")

    descriptor = None
    try:
        lock_path = database.with_name(database.name + ".batch.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            _lock_descriptor(descriptor)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise BatchBusyError("正在处理上一批，请稍后") from error
            raise
        # Windows可锁EOF以外的字节；先获锁再初始化，避免两个新进程写锁区竞态。
        if os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"\0")
        yield
    finally:
        try:
            if descriptor is not None:
                os.close(descriptor)
        finally:
            local_lock.release()
