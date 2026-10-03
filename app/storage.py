"""持久化：预算账本、授权、预算池、调整单、查询记录与历史（跨重启保留）。

并发安全语义：
- 进程内用 ``threading.RLock`` 串行化事务（FastAPI 线程池内并发）；
- 进程间用 ``fcntl.flock`` 排他锁（多 worker / 多进程）；
- 状态通过临时文件 + ``os.replace`` 原子落盘，读侧永远只见完整版本；
- ``transaction`` 在锁内执行校验-计算-提交，异常时不写入任何状态，
  因此不会留下半完成的预算扣减。

账本 schema 版本：
- v1（初始版本）：``{"principals": {...}, "queries": {query_id: ...}}``，
  查询记录以裸 query_id 为键，cache_key 为 5 元组；
- v2（当前版本）：查询记录按 ``(principal, query_id)`` 命名空间隔离，
  cache_key 扩展为 6 元组（追加 pool_id），新增 ``pools``、
  ``adjustments``、``history``。加载时自动迁移 v1 数据，老记录可照常重放。
"""
import fcntl
import json
import logging
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.errors import (
    PersistenceError,
    PoolConflictError,
    PoolNotFoundError,
    UnauthorizedError,
)

logger = logging.getLogger("pqe.storage")

SCHEMA_VERSION = 2


def query_key(principal: str, query_id: str) -> str:
    """查询记录的命名空间键：不同主体的同名 query_id 互不冲突。"""
    return json.dumps([principal, query_id], ensure_ascii=False)


def _empty_state() -> dict[str, Any]:
    return {
        "version": SCHEMA_VERSION,
        "principals": {},
        "queries": {},
        "pools": {},
        "adjustments": {},
        "history": [],
    }


def _migrate(state: dict[str, Any]) -> dict[str, Any]:
    """把 v1 账本就地升级为 v2；v2 账本原样返回。"""
    if state.get("version") == SCHEMA_VERSION:
        return state
    migrated = _empty_state()
    migrated["principals"] = state.get("principals", {})
    for old_key, record in state.get("queries", {}).items():
        record = dict(record)
        principal = record.get("principal", "")
        # cache_key 从 5 元组扩展为 6 元组（pool_id=None 表示个人额度）
        cache_key = list(record.get("cache_key", []))
        if len(cache_key) == 5:
            cache_key.append(None)
        record["cache_key"] = cache_key
        record.setdefault("submitted_at", None)
        record.setdefault("pool_id", None)
        migrated["queries"][query_key(principal, old_key)] = record
        # 老的已完成记录补入历史（无时间戳，时间范围筛选时不参与）
        migrated["history"].append(
            {
                "principal": principal,
                "query_id": old_key,
                "aggregation": record.get("response", {}).get("aggregation"),
                "dimension": record.get("response", {}).get("dimension"),
                "epsilon": cache_key[3] if len(cache_key) > 3 else None,
                "status": "completed",
                "error": None,
                "pool_id": None,
                "epsilon_charged": record.get("epsilon_charged", 0.0),
                "submitted_at": None,
            }
        )
    logger.info(
        "ledger migrated to v2: %d query records re-keyed per principal",
        len(migrated["queries"]),
    )
    return migrated


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
                return _migrate(json.load(fh))
        return _empty_state()

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
    def get_query(self, principal: str, query_id: str) -> dict[str, Any] | None:
        with self.transaction() as state:
            record = state["queries"].get(query_key(principal, query_id))
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

    # ---------- 预算池 ----------
    def create_pool(self, pool_id: str, budget_epsilon: float, members: list[str]) -> None:
        with self.transaction() as state:
            if pool_id in state["pools"]:
                raise PoolConflictError(f"pool '{pool_id}' already exists")
            state["pools"][pool_id] = {
                "budget_epsilon": float(budget_epsilon),
                "spent_epsilon": 0.0,
                "members": sorted(set(members)),
            }

    def get_pool(self, pool_id: str) -> dict[str, Any] | None:
        with self.transaction() as state:
            pool = state["pools"].get(pool_id)
            return None if pool is None else json.loads(json.dumps(pool))

    def set_pool_members(self, pool_id: str, members: list[str]) -> None:
        with self.transaction() as state:
            pool = state["pools"].get(pool_id)
            if pool is None:
                raise PoolNotFoundError(f"pool '{pool_id}' does not exist")
            pool["members"] = sorted(set(members))

    # ---------- 历史 ----------
    def list_history(self, principal: str) -> list[dict[str, Any]]:
        """返回某主体的全部提交历史（按提交顺序），不含其他主体的记录。"""
        with self.transaction() as state:
            items = [h for h in state["history"] if h.get("principal") == principal]
            return json.loads(json.dumps(items))

    def run_transaction(self, fn: Callable[[dict[str, Any]], Any]) -> Any:
        """供 service 层使用：在锁内执行包含业务计算的事务。"""
        with self.transaction() as state:
            return fn(state)

    def reset(self) -> None:
        with self.transaction() as state:
            state.update(_empty_state())
