"""查询编排：校验、预算判定、加噪、抑制与审计落盘。

预算模型（详见 README）：
- 预算以 epsilon 计量，按查询扣减；
- 一次查询的多个机制之间为串行组合：count/sum 扣 epsilon；
  avg 为带噪求和 ÷ 带噪计数，epsilon 在两个机制间平分，仍扣 epsilon；
- 敏感度决定噪声大小：count 敏感度 1，sum 敏感度为全数据集数值列的
  最大绝对值（全局敏感度上界，与具体分组无关）；
- 扣费来源二选一：个人额度（默认）或共享预算池（pool_id），
  池子扣费要求提交者在池成员名单内，池额度不足时以
  pool_budget_exhausted 拒绝，与个人 budget_exhausted 可区分。
"""
import json
import logging
import time
from typing import Any

from app.config import Settings
from app.dataset import Dataset, validate_fields
from app.errors import (
    AdjustmentConflictError,
    AdjustmentRejectedError,
    BudgetExhaustedError,
    InvalidQueryError,
    PersistenceError,
    PoolAccessDeniedError,
    PoolBudgetExhaustedError,
    QueryConflictError,
    ServiceError,
    UnauthorizedError,
    UnknownPoolError,
)
from app.models import (
    AdjustRequest,
    AdjustResponse,
    AggregationType,
    GroupResult,
    HistoryItem,
    HistoryResponse,
    PoolResponse,
    PoolUpsertRequest,
    QueryRequest,
    QueryResponse,
    SuppressedGroup,
)
from app.noise import group_noise_key, laplace_noise
from app.storage import Ledger

logger = logging.getLogger("pqe.service")


def required_epsilon(req: QueryRequest) -> float:
    """一次查询扣减的总 epsilon（串行组合 = 申请值）。"""
    return float(req.epsilon)


def _validate_request(req: QueryRequest, settings: Settings) -> None:
    validate_fields(req.dimension, req.value_column)
    if req.epsilon > settings.max_query_epsilon + 1e-12:
        raise InvalidQueryError(
            f"epsilon {req.epsilon} exceeds per-query max {settings.max_query_epsilon}"
        )
    if req.aggregation.value in ("sum", "avg") and req.value_column is None:
        raise InvalidQueryError(
            f"{req.aggregation.value} query requires value_column"
        )
    if req.groups is not None and len(req.groups) == 0:
        raise InvalidQueryError("groups filter must not be empty")


def _column_sensitivity(ds: Dataset, column: str) -> float:
    """sum 的全局敏感度：全表 |x| 的最大值。"""
    return float(max((abs(float(r[column])) for r in ds.rows), default=0.0))


def _round_laplace_integer(z: float) -> int:
    return max(0, int(round(z)))


def _compute_groups(
    req: QueryRequest, ds: Dataset, settings: Settings
) -> tuple[list[GroupResult], list[SuppressedGroup]]:
    """在已持有事务锁后执行的确定性计算。"""
    aggregation = req.aggregation.value
    buckets = ds.grouped(req.dimension, req.groups)

    if aggregation == "sum":
        scale_s = _column_sensitivity(ds, req.value_column) / req.epsilon

    # avg：epsilon 平分给计数与求和两个机制
    if aggregation == "avg":
        eps_each = req.epsilon / 2.0
        scale_c = 1.0 / eps_each
        scale_s = _column_sensitivity(ds, req.value_column) / eps_each
    elif aggregation == "count":
        scale_c = 1.0 / req.epsilon

    results: list[GroupResult] = []
    suppressed: list[SuppressedGroup] = []

    for name, rows in buckets.items():
        count = len(rows)
        if count == 0:
            suppressed.append(
                SuppressedGroup(
                    group=name,
                    reason="empty_group",
                    detail="dimension value has no rows in the dataset",
                )
            )
            continue
        if count < settings.min_group_size:
            # 只返回原始行数所在的区间，绝不返回精确值
            suppressed.append(
                SuppressedGroup(
                    group=name,
                    reason="small_group",
                    detail=f"group size below threshold {settings.min_group_size}",
                )
            )
            continue

        if aggregation == "count":
            noise = laplace_noise(scale_c, group_noise_key(req.query_id, name, "count"))
            noisy_count = _round_laplace_integer(count + noise)
            results.append(GroupResult(group=name, value=float(noisy_count), noisy_count=noisy_count))
        elif aggregation == "sum":
            exact_sum = sum(float(r[req.value_column]) for r in rows)
            noise = laplace_noise(scale_s, group_noise_key(req.query_id, name, "sum"))
            noisy_sum = round(exact_sum + noise, 2)
            results.append(GroupResult(group=name, value=float(noisy_sum), noisy_count=None))
        else:  # avg
            exact_count = float(count)
            exact_sum = sum(float(r[req.value_column]) for r in rows)
            noise_c = laplace_noise(scale_c, group_noise_key(req.query_id, name, "avg_count"))
            noise_s = laplace_noise(scale_s, group_noise_key(req.query_id, name, "avg_sum"))
            noisy_count = _round_laplace_integer(exact_count + noise_c)
            if noisy_count < settings.min_count_for_average:
                suppressed.append(
                    SuppressedGroup(
                        group=name,
                        reason="unstable_average",
                        detail="noisy denominator too small; ratio suppressed",
                    )
                )
                continue
            noisy_sum = exact_sum + noise_s
            value = round(noisy_sum / float(noisy_count), 4)
            results.append(GroupResult(group=name, value=value, noisy_count=noisy_count))

    return results, suppressed


def _append_history(
    state: dict[str, Any],
    req: QueryRequest,
    status: str,
    error: str | None = None,
    response: QueryResponse | None = None,
) -> None:
    """追加一条提交历史事件（与扣减/重放同事务，保证不漏记）。

    只携带加噪结果与抑制原因，绝不写入精确值。
    """
    state["history"].append(
        {
            "principal": req.principal,
            "query_id": req.query_id,
            "aggregation": req.aggregation.value,
            "dimension": req.dimension,
            "status": status,
            "epsilon": float(req.epsilon),
            "error": error,
            "created_at": time.time(),
            "results": (
                [json.loads(r.model_dump_json()) for r in response.results]
                if response is not None
                else None
            ),
            "suppressed": (
                [json.loads(s.model_dump_json()) for s in response.suppressed]
                if response is not None
                else None
            ),
        }
    )


def _record_rejection(ledger: Ledger, req: QueryRequest, exc: ServiceError) -> None:
    """把被拒绝的提交记入历史（独立事务；失败不掩盖原始错误）。"""
    try:
        ledger.run_transaction(
            lambda state: _append_history(state, req, "rejected", error=exc.code)
        )
    except Exception:  # 历史落盘失败不影响拒绝本身
        logger.warning("failed to record rejection history", exc_info=True)


def _charge_entry(entry: dict[str, Any], need: float) -> tuple[float, float]:
    """返回 (扣减前剩余, 扣减后剩余) 并完成扣减；调用前需已判定额度充足。"""
    budget = float(entry["budget_epsilon"])
    spent_before = float(entry["spent_epsilon"])
    entry["spent_epsilon"] = spent_before + need
    return budget - spent_before, budget - spent_before - need


def submit_query(req: QueryRequest, ledger: Ledger, ds: Dataset, settings: Settings) -> QueryResponse:
    """提交一次 DP 查询。整段校验-扣减-计算-落盘在同一事务内完成。"""
    try:
        return _submit(req, ledger, ds, settings)
    except PersistenceError:
        raise
    except ServiceError as exc:
        _record_rejection(ledger, req, exc)
        raise


def _submit(req: QueryRequest, ledger: Ledger, ds: Dataset, settings: Settings) -> QueryResponse:
    _validate_request(req, settings)
    need = required_epsilon(req)
    key = list(req.cache_key())
    source = "personal" if req.pool_id is None else f"pool:{req.pool_id}"

    def tx(state: dict[str, Any]) -> QueryResponse:
        # 查询记录按主体隔离：不同用户的相同 query_id 互不可见
        my_queries = state["queries"].setdefault(req.principal, {})
        existing = my_queries.get(req.query_id)
        if existing is not None:
            # 账本从 JSON 读回的 cache_key 是 list，内存里可能是 tuple：
            # 统一按 tuple 比较，避免旧记录重放被误判为参数冲突
            if tuple(existing["cache_key"]) != tuple(key):
                raise QueryConflictError(
                    f"query_id '{req.query_id}' was submitted with different parameters"
                )
            if existing["status"] == "completed":
                logger.info(
                    "budget decision | query_id=%s principal=%s replayed=true "
                    "before=%.6f required=%.6f after=%.6f basis=idempotent_cache_hit source=%s",
                    req.query_id,
                    req.principal,
                    existing["remaining_budget"],
                    0.0,
                    existing["remaining_budget"],
                    source,
                )
                response = QueryResponse(**{**existing["response"], "replayed": True})
                _append_history(state, req, "replayed", response=response)
                return response

        # 选定扣费账户：个人额度或共享预算池
        if req.pool_id is None:
            entry = state["principals"].get(req.principal)
            if entry is None or not entry.get("authorized", False):
                raise UnauthorizedError(f"principal '{req.principal}' is not authorized")
        else:
            entry = state["pools"].get(req.pool_id)
            if entry is None:
                raise UnknownPoolError(f"pool '{req.pool_id}' does not exist")
            if req.principal not in entry.get("members", []):
                raise PoolAccessDeniedError(
                    f"principal '{req.principal}' is not a member of pool '{req.pool_id}'"
                )

        budget = float(entry["budget_epsilon"])
        remaining_before = budget - float(entry["spent_epsilon"])
        if remaining_before + 1e-9 < need:
            logger.info(
                "budget decision | query_id=%s principal=%s replayed=false "
                "before=%.6f required=%.6f after=%.6f basis=%s source=%s",
                req.query_id,
                req.principal,
                remaining_before,
                need,
                remaining_before,
                "insufficient_budget_denied"
                if req.pool_id is None
                else "insufficient_pool_budget_denied",
                source,
            )
            if req.pool_id is None:
                raise BudgetExhaustedError(
                    f"remaining epsilon {remaining_before:.6f} < required {need:.6f}",
                    remaining=remaining_before,
                    required=need,
                )
            raise PoolBudgetExhaustedError(
                f"pool '{req.pool_id}' remaining epsilon {remaining_before:.6f} "
                f"< required {need:.6f}",
                remaining=remaining_before,
                required=need,
            )

        # 计算在锁内完成，结果落盘与扣减原子提交
        results, suppressed = _compute_groups(req, ds, settings)
        _, remaining_after = _charge_entry(entry, need)

        response = QueryResponse(
            query_id=req.query_id,
            principal=req.principal,
            aggregation=req.aggregation,
            dimension=req.dimension,
            replayed=False,
            epsilon_charged=need,
            remaining_budget=round(remaining_after, 9),
            budget_source="personal" if req.pool_id is None else "pool",
            pool_id=req.pool_id,
            results=results,
            suppressed=suppressed,
        )
        record = {
            "status": "completed",
            "principal": req.principal,
            "cache_key": key,
            "epsilon_charged": need,
            "remaining_budget": round(remaining_after, 9),
            "created_at": time.time(),
            "response": json.loads(response.model_dump_json()),
        }
        my_queries[req.query_id] = record
        _append_history(state, req, "completed", response=response)
        logger.info(
            "budget decision | query_id=%s principal=%s replayed=false "
            "before=%.6f required=%.6f after=%.6f basis=charged source=%s",
            req.query_id,
            req.principal,
            remaining_before,
            need,
            remaining_after,
            source,
        )
        return response

    return ledger.run_transaction(tx)


# ---------- 共享预算池 ----------


def _pool_response(pool_id: str, pool: dict[str, Any]) -> PoolResponse:
    budget = float(pool["budget_epsilon"])
    spent = float(pool["spent_epsilon"])
    return PoolResponse(
        pool_id=pool_id,
        members=list(pool["members"]),
        budget_epsilon=budget,
        spent_epsilon=spent,
        remaining_epsilon=round(budget - spent, 9),
    )


def upsert_pool(req: PoolUpsertRequest, ledger: Ledger) -> PoolResponse:
    """创建预算池或整体替换其成员名单。

    已存在的池忽略请求里的 budget_epsilon——额度只能经 /admin/adjust 变更，
    保证所有额度变动都有调整单号可查。
    """

    def tx(state: dict[str, Any]) -> PoolResponse:
        pool = state["pools"].get(req.pool_id)
        members = sorted({m for m in req.members})
        if pool is None:
            pool = {
                "members": members,
                "budget_epsilon": float(req.budget_epsilon),
                "spent_epsilon": 0.0,
            }
            state["pools"][req.pool_id] = pool
            logger.info(
                "pool upsert | pool_id=%s members=%s budget=%.6f basis=created",
                req.pool_id,
                ",".join(members),
                pool["budget_epsilon"],
            )
        else:
            pool["members"] = members
            logger.info(
                "pool upsert | pool_id=%s members=%s budget=%.6f basis=members_updated",
                req.pool_id,
                ",".join(members),
                pool["budget_epsilon"],
            )
        return _pool_response(req.pool_id, pool)

    return ledger.run_transaction(tx)


def get_pool(pool_id: str, ledger: Ledger) -> PoolResponse:
    pool = ledger.get_pool(pool_id)
    if pool is None:
        raise UnknownPoolError(f"pool '{pool_id}' does not exist")
    return _pool_response(pool_id, pool)


# ---------- 预算调整（追加/回收，幂等） ----------


def adjust_budget(req: AdjustRequest, ledger: Ledger) -> AdjustResponse:
    """对个人额度或预算池做一次追加（delta>0）/回收（delta<0）。

    - adjustment_id 为幂等键：同单号同参数重放首次结果，不重复生效；
      同单号不同参数 → adjustment_conflict；
    - 回收后若 新额度 < 已消耗，整笔拒绝，账本保持原样。
    """
    if req.delta == 0:
        raise AdjustmentRejectedError("delta must be non-zero")
    params = [req.target_type, req.target, round(req.delta, 10)]

    def tx(state: dict[str, Any]) -> AdjustResponse:
        existing = state["adjustments"].get(req.adjustment_id)
        if existing is not None:
            if existing["params"] != params:
                raise AdjustmentConflictError(
                    f"adjustment '{req.adjustment_id}' was submitted with different parameters"
                )
            logger.info(
                "budget adjustment | adjustment_id=%s target=%s:%s delta=%.6f "
                "before=%.6f after=%.6f spent=%.6f basis=idempotent_replay",
                req.adjustment_id,
                req.target_type,
                req.target,
                req.delta,
                existing["response"]["budget_before"],
                existing["response"]["budget_after"],
                existing["response"]["spent_epsilon"],
            )
            return AdjustResponse(**{**existing["response"], "replayed": True})

        if req.target_type == "principal":
            entry = state["principals"].get(req.target)
            if entry is None or not entry.get("authorized", False):
                raise AdjustmentRejectedError(
                    f"principal '{req.target}' is not authorized"
                )
        else:
            entry = state["pools"].get(req.target)
            if entry is None:
                raise AdjustmentRejectedError(f"pool '{req.target}' does not exist")

        budget_before = float(entry["budget_epsilon"])
        spent = float(entry["spent_epsilon"])
        budget_after = budget_before + req.delta
        if budget_after + 1e-9 < spent:
            logger.info(
                "budget adjustment | adjustment_id=%s target=%s:%s delta=%.6f "
                "before=%.6f after=%.6f spent=%.6f basis=rejected_would_underspend",
                req.adjustment_id,
                req.target_type,
                req.target,
                req.delta,
                budget_before,
                budget_after,
                spent,
            )
            raise AdjustmentRejectedError(
                f"reclaim rejected: spent {spent:.6f} would exceed new budget "
                f"{budget_after:.6f}"
            )

        entry["budget_epsilon"] = budget_after
        response = AdjustResponse(
            adjustment_id=req.adjustment_id,
            target_type=req.target_type,
            target=req.target,
            delta=req.delta,
            budget_before=round(budget_before, 9),
            budget_after=round(budget_after, 9),
            spent_epsilon=round(spent, 9),
            replayed=False,
        )
        state["adjustments"][req.adjustment_id] = {
            "params": params,
            "response": json.loads(response.model_dump_json()),
        }
        logger.info(
            "budget adjustment | adjustment_id=%s target=%s:%s delta=%.6f "
            "before=%.6f after=%.6f spent=%.6f basis=applied",
            req.adjustment_id,
            req.target_type,
            req.target,
            req.delta,
            budget_before,
            budget_after,
            spent,
        )
        return response

    return ledger.run_transaction(tx)


# ---------- 查询历史 ----------


def get_history(
    viewer: str,
    principal: str,
    ledger: Ledger,
    aggregation: str | None = None,
    dimension: str | None = None,
    status: str | None = None,
    start: float | None = None,
    end: float | None = None,
    page: int = 1,
    page_size: int = 20,
) -> HistoryResponse:
    """按主体查看提交历史；只能查看自己的记录。"""
    if viewer != principal:
        raise UnauthorizedError(
            f"principal '{viewer}' cannot view history of '{principal}'"
        )
    events = ledger.get_history(principal)

    def match(e: dict[str, Any]) -> bool:
        if aggregation is not None and e["aggregation"] != aggregation:
            return False
        if dimension is not None and e["dimension"] != dimension:
            return False
        if status is not None and e["status"] != status:
            return False
        ts = e.get("created_at")
        # 旧版迁移补录的老记录没有提交时间：仅在未指定时间范围时纳入，
        # 绝不把它们塞进任何用户指定的时间段
        if ts is None:
            return start is None and end is None
        if start is not None and ts < start:
            return False
        return not (end is not None and ts > end)

    matched = [e for e in events if match(e)]
    # 老记录（无时间）排在最前，其余按提交时间升序
    matched.sort(key=lambda e: (e.get("created_at") is not None, e.get("created_at") or 0.0))
    total = len(matched)
    offset = (page - 1) * page_size
    items = [
        HistoryItem(
            query_id=e["query_id"],
            aggregation=AggregationType(e["aggregation"]),
            dimension=e["dimension"],
            status=e["status"],
            epsilon=float(e["epsilon"]),
            error=e.get("error"),
            created_at=e.get("created_at"),
            legacy=bool(e.get("legacy")),
            results=(
                [GroupResult(**r) for r in e["results"]]
                if e.get("results") is not None
                else None
            ),
            suppressed=(
                [SuppressedGroup(**s) for s in e["suppressed"]]
                if e.get("suppressed") is not None
                else None
            ),
        )
        for e in matched[offset : offset + page_size]
    ]
    return HistoryResponse(
        principal=principal, total=total, page=page, page_size=page_size, items=items
    )
