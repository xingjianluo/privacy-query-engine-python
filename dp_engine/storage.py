"""基于单个 JSON 文件的持久化。

并发安全：
    - 进程内 threading.RLock 串行化同进程请求（含 FastAPI 线程池）；
    - fcntl.flock 排他锁串行化多进程访问（例如 uvicorn 多 worker / 重启并存）。
事务：在持锁区间内 读盘 -> 修改 -> 原子写；异常时不落盘，内存修改随事务丢弃，
    因而不会留下"扣了预算但没有审计记录"之类的半完成状态。
原子写：临时文件 + fsync + os.replace，主状态文件永远是完整的某一版。
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import threading
from collections.abc import Iterator

from .errors import InternalInfrastructureError
from .models import PersistedState

# ---- 测试用故障注入开关（进程内）----
fail_next_write = False
fail_next_read = False

_process_lock = threading.RLock()


class Store:
    def __init__(self, path: str):
        self.path = path
        self._lock_path = path + ".lock"
        self._thread_lock = threading.RLock()

    def load(self) -> PersistedState:
        global fail_next_read
        if fail_next_read:
            fail_next_read = False
            raise InternalInfrastructureError(cause=RuntimeError("state read failed (injected)"))
        if not os.path.exists(self.path):
            return PersistedState()
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
            return PersistedState.model_validate(payload)
        except InternalInfrastructureError:
            raise
        except Exception as exc:  # 文件损坏等一律包装，禁止外泄内部细节
            raise InternalInfrastructureError("状态文件不可读", cause=exc)

    def save(self, state: PersistedState) -> None:
        global fail_next_write
        if fail_next_write:
            fail_next_write = False
            raise InternalInfrastructureError(cause=RuntimeError("state write failed (injected)"))
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        os.makedirs(directory, exist_ok=True)
        tmp_path = f"{self.path}.tmp.{os.getpid()}.{threading.get_ident()}"
        try:
            payload = json.dumps(state.model_dump(), ensure_ascii=False, indent=2)
            with open(tmp_path, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self.path)
        except InternalInfrastructureError:
            raise
        except Exception as exc:
            with contextlib.suppress(OSError):
                os.remove(tmp_path)
            raise InternalInfrastructureError("状态落盘失败", cause=exc)

    @contextlib.contextmanager
    def transaction(self) -> Iterator[PersistedState]:
        """持锁的 读-改-写 事务；正常退出才落盘，异常退出完全回滚。"""
        with self._thread_lock, _process_lock:
            os.makedirs(os.path.dirname(os.path.abspath(self._lock_path)), exist_ok=True)
            with open(self._lock_path, "w") as lock_fh:
                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
                try:
                    state = self.load()
                    yield state
                    # 只有 with 体正常结束才会走到这里
                    self.save(state)
                finally:
                    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
