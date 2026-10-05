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
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

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
        - 旧版没有查询历史分区，需要把旧版已成功执行的查询补录进 history，
          使老用户在“查看自己的提交历史”时能看到这些记录。补录条目带
          ``legacy=True`` 标记且 ``created_at=None``：仅在不指定时间范围时
          纳入历史；补录依据历史里的 legacy 标记去重，反复加载/重启不会
          重复补录。补录只读旧记录的加噪结果与抑制原因，绝不带出精确值。
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
                # 旧版 cache_key 没有 pool_id 分量，补 None（个人额度）
                if len(record["cache_key"]) == 5:
                    record["cache_key"] = [*record["cache_key"], None]
                # 标记为旧版记录（新记录必有 float 型 created_at），
                # 供历史补录在反复加载时识别范围
                record["legacy"] = True
                nested.setdefault(record["principal"], {})[key] = record
            else:
                # 已是按主体分组的嵌套结构
                nested[key] = record
        state["queries"] = nested
        Ledger._backfill_legacy_history(state)
        return state

    @staticmethod
    def _backfill_legacy_history(state: dict[str, Any]) -> None:
        """把旧版已成功的查询记录补录进历史分区（幂等）。

        - 只补 status=completed 的查询记录（旧版拒绝不写 queries）；
        - 按历史中已有的 ``legacy`` 标记去重：同 (principal, query_id)
          的老记录无论加载/迁移多少次，都只补一条；
        - 补录不改动 queries/principals，因此旧查询的重放与已扣预算不受影响；
        - 补录条目只复制旧响应里的加噪结果（results）与抑制原因（suppressed）。
        """
        history: list[dict[str, Any]] = state["history"]
        already = {
            (e.get("principal"), e.get("query_id"))
            for e in history
            if isinstance(e, dict) and e.get("legacy")
        }
        added = 0
        for principal, user_queries in state["queries"].items():
            if not isinstance(user_queries, dict):
                continue
            for query_id, record in user_queries.items():
                if not isinstance(record, dict):
                    continue
                if record.get("status") != "completed":
                    continue
                # 只补旧版记录：旧记录带 legacy 标记，且没有真实提交时间。
                # 新版记录必有 float 型 created_at，绝不能在这里重复补录。
                is_legacy = record.get("legacy") is True or record.get("created_at") is None
                if not is_legacy:
                    continue
                if (principal, query_id) in already:
                    continue
                response = record.get("response") or {}
                history.append(
                    {
                        "principal": principal,
                        "query_id": query_id,
                        "aggregation": response.get("aggregation"),
                        "dimension": response.get("dimension"),
                        "status": "completed",
                        "epsilon": float(
                            record.get("epsilon_charged", response.get("epsilon_charged", 0.0))
                        ),
                        "error": None,
                        # 老记录本身没有提交时间：None 表示仅在不带时间范围时纳入
                        "created_at": None,
                        # 只有加噪结果与抑制原因，绝不带原始精确值
                        "results": response.get("results"),
                        "suppressed": response.get("suppressed"),
                        # 迁移补录标记，用于幂等去重
                        "legacy": True,
                    }
                )
                already.add((principal, query_id))
                added += 1
        if added:
            logger.info("ledger migration | backfilled_legacy_history=%d", added)

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
