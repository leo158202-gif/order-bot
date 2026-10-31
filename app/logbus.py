"""日志广播：logging -> SSE 推送到界面。"""
import logging
import queue
import re
import threading

PHONE_RE = re.compile(r"(1\d{2})\d{4}(\d{4})")


class PrivateFormatter(logging.Formatter):
    """文件和异常堆栈也打码；不修改原始日志记录或业务数据。"""
    def format(self,record):
        return PHONE_RE.sub(r"\1****\2",super().format(record))


class LogBus:
    def __init__(self):
        self._subs: list[queue.Queue] = []
        self._lock = threading.Lock()
        self.history: list[str] = []  # 最近 200 条，新打开的页面能看到

    def publish(self, line: str) -> None:
        # 电话打码（01 文档安全要求）
        line = PHONE_RE.sub(r"\1****\2", line)
        self.history.append(line)
        self.history = self.history[-200:]
        with self._lock:
            subs = list(self._subs)
        for q in subs:
            try:
                q.put_nowait(line)
            except queue.Full:
                pass

    def subscribe(self) -> queue.Queue:
        q = queue.Queue(maxsize=500)
        with self._lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subs:
                self._subs.remove(q)


BUS = LogBus()


class BusHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
            BUS.publish(line)
        except Exception:
            pass


def attach(root_fmt: str = "%(asctime)s %(levelname)s %(message)s") -> None:
    """把 BusHandler 挂到 root logger，所有模块日志都会进界面。"""
    h = BusHandler()
    h.setFormatter(logging.Formatter(root_fmt, datefmt="%H:%M:%S"))
    logging.getLogger().addHandler(h)
    logging.getLogger().setLevel(logging.INFO)
    # httpx 的 HTTP 请求日志太吵，不进界面（仍进文件）
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
