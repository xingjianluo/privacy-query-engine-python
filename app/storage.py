"""持久化：预算账本、授权、查询记录（跨重启保留）。

并发安全语义：
- 进程内用 ``threading.RLock`` 串行化事务（FastAPI 线程池内并发）；
- 进程间用 ``fcntl.flock`` 排他锁（多 worker / 多进程）；
- 状态通过临时文件 + ``os.replace`` 原子落盘，读侧永远只见完整版本；
- ``transaction`` 在锁内执行校验-计算-提交，异常时不写入任何状态，
  因此不会留下半完成的预算扣减。
"""
import fcntl
import json
import logging
import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from app.errors import PersistenceError, UnauthorizedError

logger = logging.getLogger("pqe.storage")


class Ledger:
    def __init__(self, path: Path, lock_path: Path) -> None:
        self._path = path
        self._lock_path = lock_path
        self._thread_lock = threading.RLock()
        self._state: dict[str, Any] | None = None
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)

    # ---------- 状态读写 ----------
    def _load(self) -> dict[str, Any]:
        if self._path.exists():
            with self._path.open("r", encoding="utf-8") as fh:
                return json.load(fh)
        return {"principals": {}, "queries": {}}

    def _persist(self) -> None:
        tmp = self._path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(self._state, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._path)

    @contextmanager
    def transaction(self) -> Iterator[dict[str, Any]]:
        """在排他锁保护下读写内存状态；正常退出时原子落盘。"""
        with self._thread_lock:
            lock_file = self._lock_path.open("a+")
            try:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                except OSError as exc:
                    raise PersistenceError("lock acquisition failed") from exc
                try:
                    self._state = self._load()
                    yield self._state
                    self._persist()
                except (PersistenceError, UnauthorizedError):
                    raise
                except OSError as exc:
                    self._state = None
                    raise PersistenceError("ledger write failed") from exc
            finally:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                finally:
                    lock_file.close()

    # ---------- 授权 ----------
    def is_authorized(self, state: dict[str, Any], principal: str) -> bool:
        return bool(state["principals"].get(principal, {}).get("authorized", False))

    def grant(self, principal: str, budget_epsilon: float) -> None:
        with self.transaction() as state:
            entry = state["principals"].setdefault(
                principal, {"authorized": True, "spent_epsilon": 0.0, "budget_epsilon": budget_epsilon}
            )
            entry["authorized"] = True

    # ---------- 查询 ----------
    def get_query(self, query_id: str) -> dict[str, Any] | None:
        with self.transaction() as state:
            record = state["queries"].get(query_id)
            return None if record is None else json.loads(json.dumps(record))

    def get_budget(self, principal: str) -> dict[str, Any]:
        with self.transaction() as state:
            entry = state["principals"].get(principal)
            if entry is None:
                return {"authorized": False, "spent_epsilon": 0.0, "budget_epsilon": 0.0}
            return {
                "authorized": bool(entry.get("authorized", False)),
                "spent_epsilon": float(entry["spent_epsilon"]),
                "budget_epsilon": float(entry["budget_epsilon"]),
            }

    def run_transaction(self, fn: Callable[[dict[str, Any]], Any]) -> Any:
        """供 service 层使用：在锁内执行包含业务计算的事务。"""
        with self.transaction() as state:
            return fn(state)

    def reset(self) -> None:
        with self.transaction() as state:
            state["principals"] = {}
            state["queries"] = {}
