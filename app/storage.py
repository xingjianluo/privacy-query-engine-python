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
                state = json.load(fh)
        else:
            state = {}
        return self._migrate(state)

    @staticmethod
    def _migrate(state: dict[str, Any]) -> dict[str, Any]:
        """把旧版账本结构就地升级为当前结构（幂等，可反复执行）。

        旧版差异：
        - 缺少 pools / adjustments / history 三个分区；
        - queries 为扁平的 ``{query_id: record}``，现按
          ``{principal: {query_id: record}}`` 隔离（record 自带 principal 字段，
          可无损归位）；旧记录没有 created_at，补 None；
        - 旧版没有 history 分区：旧版已成功执行（status=completed）的查询
          需补录到对应用户的历史中，否则升级后用户看不到自己的老记录。
          补录条目带 ``legacy=True`` 标记且 ``created_at=None``：
          迁移可反复执行而不重复补录，老记录无提交时间故不做时间猜测。
        """
        state.setdefault("principals", {})
        state.setdefault("pools", {})
        state.setdefault("adjustments", {})
        state.setdefault("history", [])
        queries = state.get("queries", {})
        nested: dict[str, Any] = {}
        for key, record in queries.items():
            if isinstance(record, dict) and "cache_key" in record:
                # 旧版扁平记录：按记录内的 principal 归位
                record.setdefault("created_at", None)
                # 旧版 cache_key 没有 pool_id 分量，补 None（个人额度）；
                # JSON 读回的分量是 list，而 service 比对用 tuple，统一转 tuple，
                # 否则旧记录重放会被误判为参数冲突（409）
                record["cache_key"] = Ledger._normalize_cache_key(record["cache_key"])
                nested.setdefault(record["principal"], {})[key] = record
            else:
                # 已是按主体分组的嵌套结构
                if isinstance(record, dict):
                    for sub in record.values():
                        if isinstance(sub, dict) and "cache_key" in sub:
                            sub["cache_key"] = Ledger._normalize_cache_key(
                                sub["cache_key"]
                            )
                nested[key] = record
        state["queries"] = nested
        Ledger._backfill_legacy_history(state)
        return state

    @staticmethod
    def _normalize_cache_key(cache_key: Any) -> tuple[Any, ...]:
        """归一化缓存键：groups 分量（JSON list）转 tuple，并补齐 pool_id 分量。"""
        normalized = tuple(
            tuple(part) if isinstance(part, list) else part for part in cache_key
        )
        if len(normalized) == 5:
            normalized = (*normalized, None)
        return normalized

    @staticmethod
    def _backfill_legacy_history(state: dict[str, Any]) -> None:
        """把旧版成功查询补录进历史分区（幂等，可反复执行）。

        判定一条查询记录是否为旧版记录：``created_at is None``——现版本成功
        落盘的记录必带浮点时间戳。已补录过的 (principal, query_id) 不重复补。
        补录内容只取旧记录里已持久化的加噪响应（results/suppressed），
        绝不触碰或推导任何原始精确值，也不改动预算与查询记录本身。
        """
        history: list[dict[str, Any]] = state["history"]
        backfilled = {
            (event["principal"], event["query_id"])
            for event in history
            if isinstance(event, dict) and event.get("legacy")
        }
        backfilled_count = 0
        for principal, records in state["queries"].items():
            if not isinstance(records, dict):
                continue
            for query_id, record in records.items():
                if not isinstance(record, dict) or "cache_key" not in record:
                    continue
                # 旧版只持久化成功完成的查询；新记录必有 created_at
                if record.get("status") != "completed" or record.get("created_at") is not None:
                    continue
                if (principal, query_id) in backfilled:
                    continue
                cache_key = record["cache_key"]
                response = record.get("response") or {}
                history.append(
                    {
                        "principal": principal,
                        "query_id": query_id,
                        "aggregation": response.get("aggregation", cache_key[0]),
                        "dimension": response.get("dimension", cache_key[1]),
                        "status": "completed",
                        "epsilon": float(record.get("epsilon_charged", cache_key[3])),
                        "error": None,
                        # 老记录没有提交时间：保留 None，由历史查询决定纳入规则
                        "created_at": None,
                        "results": response.get("results"),
                        "suppressed": response.get("suppressed"),
                        # 迁移补录标记：去重与时间筛选均依赖它
                        "legacy": True,
                    }
                )
                backfilled.add((principal, query_id))
                backfilled_count += 1
        if backfilled_count:
            logger.info(
                "ledger migration | basis=legacy_history_backfilled count=%d",
                backfilled_count,
            )

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
            record = state["queries"].get(principal, {}).get(query_id)
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
    def get_pool(self, pool_id: str) -> dict[str, Any] | None:
        with self.transaction() as state:
            pool = state["pools"].get(pool_id)
            return None if pool is None else json.loads(json.dumps(pool))

    # ---------- 历史 ----------
    def get_history(self, principal: str) -> list[dict[str, Any]]:
        with self.transaction() as state:
            return json.loads(
                json.dumps([e for e in state["history"] if e["principal"] == principal])
            )

    def run_transaction(self, fn: Callable[[dict[str, Any]], Any]) -> Any:
        """供 service 层使用：在锁内执行包含业务计算的事务。"""
        with self.transaction() as state:
            return fn(state)

    def reset(self) -> None:
        with self.transaction() as state:
            state["principals"] = {}
            state["queries"] = {}
            state["pools"] = {}
            state["adjustments"] = {}
            state["history"] = []
