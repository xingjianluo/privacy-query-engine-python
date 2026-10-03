"""查询编排：校验、预算判定、加噪、抑制与审计落盘。

预算模型（详见 README）：
- 预算以 epsilon 计量，按查询扣减；
- 扣费来源二选一：个人额度（默认）或本人所属的共享预算池（pool_id）；
- 一次查询的多个机制之间为串行组合：count/sum 扣 epsilon；
  avg 为带噪求和 ÷ 带噪计数，epsilon 在两个机制间平分，仍扣 epsilon；
- 敏感度决定噪声大小：count 敏感度 1，sum 敏感度为全数据集数值列的
  最大绝对值（全局敏感度上界，与具体分组无关）。

预算调整（追加/回收）：
- 每次调整以 adjustment_id 为幂等键，重复提交只生效一次并返回首次结果；
- 回收后若已消耗额度将超过新额度，整笔拒绝，账本保持原样。
"""
import json
import logging
from datetime import UTC, datetime
from typing import Any

from app.config import Settings
from app.dataset import Dataset, validate_fields
from app.errors import (
    AdjustmentConflictError,
    AdjustmentRejectedError,
    AdjustmentTargetNotFoundError,
    BudgetExhaustedError,
    InvalidQueryError,
    PersistenceError,
    PoolBudgetExhaustedError,
    PoolNotFoundError,
    QueryConflictError,
    ServiceError,
    UnauthorizedError,
)
from app.models import (
    AdjustmentRequest,
    AdjustmentResponse,
    GroupResult,
    HistoryItem,
    HistoryResponse,
    QueryRequest,
    QueryResponse,
    SuppressedGroup,
)
from app.noise import group_noise_key, laplace_noise
from app.storage import Ledger, query_key

logger = logging.getLogger("pqe.service")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


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


def _history_entry(
    req: QueryRequest,
    status: str,
    error: str | None,
    epsilon_charged: float,
    submitted_at: str,
) -> dict[str, Any]:
    """历史条目：只含元数据，绝不含分组结果或原始精确值。"""
    return {
        "principal": req.principal,
        "query_id": req.query_id,
        "aggregation": req.aggregation.value,
        "dimension": req.dimension,
        "epsilon": float(req.epsilon),
        "status": status,
        "error": error,
        "pool_id": req.pool_id,
        "epsilon_charged": epsilon_charged,
        "submitted_at": submitted_at,
    }


def _resolve_charge_target(
    req: QueryRequest, state: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    """定位本次扣费的账本条目（个人或预算池），返回 (条目, 来源标签)。"""
    if req.pool_id is None:
        entry = state["principals"].get(req.principal)
        if entry is None or not entry.get("authorized", False):
            raise UnauthorizedError(f"principal '{req.principal}' is not authorized")
        return entry, "personal"
    pool = state["pools"].get(req.pool_id)
    if pool is None:
        raise PoolNotFoundError(f"pool '{req.pool_id}' does not exist")
    if req.principal not in pool.get("members", []):
        raise UnauthorizedError(
            f"principal '{req.principal}' is not a member of pool '{req.pool_id}'"
        )
    return pool, f"pool:{req.pool_id}"


def submit_query(req: QueryRequest, ledger: Ledger, ds: Dataset, settings: Settings) -> QueryResponse:
    """提交一次 DP 查询。整段校验-扣减-计算-落盘在同一事务内完成。"""
    _validate_request(req, settings)
    need = required_epsilon(req)
    key = list(req.cache_key())
    qkey = query_key(req.principal, req.query_id)

    def tx(state: dict[str, Any]) -> QueryResponse:
        existing = state["queries"].get(qkey)
        if existing is not None:
            if existing["cache_key"] != key:
                raise QueryConflictError(
                    f"query_id '{req.query_id}' was submitted with different parameters"
                )
            if existing["status"] == "completed":
                logger.info(
                    "budget decision | query_id=%s principal=%s source=%s replayed=true "
                    "before=%.6f required=%.6f after=%.6f basis=idempotent_cache_hit",
                    req.query_id,
                    req.principal,
                    "personal" if req.pool_id is None else f"pool:{req.pool_id}",
                    existing["remaining_budget"],
                    0.0,
                    existing["remaining_budget"],
                )
                return QueryResponse(**{**existing["response"], "replayed": True})

        target, source = _resolve_charge_target(req, state)

        budget = float(target["budget_epsilon"])
        spent_before = float(target["spent_epsilon"])
        remaining_before = budget - spent_before
        if remaining_before + 1e-9 < need:
            logger.info(
                "budget decision | query_id=%s principal=%s source=%s replayed=false "
                "before=%.6f required=%.6f after=%.6f basis=insufficient_budget_denied",
                req.query_id,
                req.principal,
                source,
                remaining_before,
                need,
                remaining_before,
            )
            message = (
                f"remaining epsilon {remaining_before:.6f} < required {need:.6f} ({source})"
            )
            if req.pool_id is None:
                raise BudgetExhaustedError(message, remaining=remaining_before, required=need)
            raise PoolBudgetExhaustedError(message, remaining=remaining_before, required=need)

        # 计算在锁内完成，结果落盘与扣减原子提交
        results, suppressed = _compute_groups(req, ds, settings)
        spent_after = spent_before + need
        target["spent_epsilon"] = spent_after
        remaining_after = budget - spent_after

        response = QueryResponse(
            query_id=req.query_id,
            principal=req.principal,
            aggregation=req.aggregation,
            dimension=req.dimension,
            replayed=False,
            epsilon_charged=need,
            remaining_budget=round(remaining_after, 9),
            pool_id=req.pool_id,
            results=results,
            suppressed=suppressed,
        )
        submitted_at = _now_iso()
        record = {
            "status": "completed",
            "principal": req.principal,
            "pool_id": req.pool_id,
            "cache_key": key,
            "epsilon_charged": need,
            "remaining_budget": round(remaining_after, 9),
            "submitted_at": submitted_at,
            "response": json.loads(response.model_dump_json()),
        }
        state["queries"][qkey] = record
        state["history"].append(
            _history_entry(req, "completed", None, need, submitted_at)
        )
        logger.info(
            "budget decision | query_id=%s principal=%s source=%s replayed=false "
            "before=%.6f required=%.6f after=%.6f basis=charged",
            req.query_id,
            req.principal,
            source,
            remaining_before,
            need,
            remaining_after,
        )
        return response

    try:
        return ledger.run_transaction(tx)
    except ServiceError as exc:
        # 领域拒绝（预算不足、未授权、池子不存在、参数冲突等）补记一条
        # 拒绝历史；持久化失败不补记（账本本身已不可用）。补记失败不影响
        # 原始拒绝结论的返回。
        if exc.code != PersistenceError.code:
            _record_rejection(ledger, req, exc)
        raise


def _record_rejection(ledger: Ledger, req: QueryRequest, exc: ServiceError) -> None:
    entry = _history_entry(req, "rejected", exc.code, 0.0, _now_iso())

    def tx(state: dict[str, Any]) -> None:
        state["history"].append(entry)

    try:
        ledger.run_transaction(tx)
    except ServiceError:
        logger.warning(
            "failed to record rejection history | query_id=%s principal=%s error=%s",
            req.query_id,
            req.principal,
            exc.code,
        )


def adjust_budget(req: AdjustmentRequest, ledger: Ledger) -> AdjustmentResponse:
    """对个人额度或共享预算池做追加（正）/回收（负）调整。

    幂等：同一 adjustment_id 重复提交（参数一致）只生效一次并返回首次结果；
    参数不一致则 409。回收导致已消耗额度超过新额度时整笔拒绝，账本不变。
    """
    if req.delta_epsilon == 0:
        raise InvalidQueryError("delta_epsilon must not be zero")
    fingerprint = list(req.fingerprint())

    def tx(state: dict[str, Any]) -> AdjustmentResponse:
        existing = state["adjustments"].get(req.adjustment_id)
        if existing is not None:
            if existing["fingerprint"] != fingerprint:
                raise AdjustmentConflictError(
                    f"adjustment_id '{req.adjustment_id}' was submitted with different parameters"
                )
            logger.info(
                "adjustment decision | adjustment_id=%s target=%s:%s delta=%.6f "
                "budget_before=%.6f budget_after=%.6f spent=%.6f basis=idempotent_replay",
                req.adjustment_id,
                req.target_type,
                req.target_id,
                req.delta_epsilon,
                existing["response"]["budget_epsilon"] - req.delta_epsilon,
                existing["response"]["budget_epsilon"],
                existing["response"]["spent_epsilon"],
            )
            return AdjustmentResponse(**{**existing["response"], "replayed": True})

        if req.target_type == "principal":
            target = state["principals"].get(req.target_id)
            if target is None or not target.get("authorized", False):
                raise AdjustmentTargetNotFoundError(
                    f"principal '{req.target_id}' is not authorized"
                )
        else:
            target = state["pools"].get(req.target_id)
            if target is None:
                raise AdjustmentTargetNotFoundError(
                    f"pool '{req.target_id}' does not exist"
                )

        budget_before = float(target["budget_epsilon"])
        spent = float(target["spent_epsilon"])
        budget_after = round(budget_before + req.delta_epsilon, 9)
        if budget_after + 1e-9 < spent:
            logger.info(
                "adjustment decision | adjustment_id=%s target=%s:%s delta=%.6f "
                "budget_before=%.6f budget_after=%.6f spent=%.6f "
                "basis=reclaim_would_overdraw_denied",
                req.adjustment_id,
                req.target_type,
                req.target_id,
                req.delta_epsilon,
                budget_before,
                budget_after,
                spent,
            )
            raise AdjustmentRejectedError(
                f"spent epsilon {spent:.6f} would exceed adjusted budget {budget_after:.6f}"
            )

        target["budget_epsilon"] = budget_after
        response = AdjustmentResponse(
            adjustment_id=req.adjustment_id,
            target_type=req.target_type,
            target_id=req.target_id,
            delta_epsilon=req.delta_epsilon,
            budget_epsilon=budget_after,
            spent_epsilon=spent,
            remaining_epsilon=round(budget_after - spent, 9),
            replayed=False,
        )
        state["adjustments"][req.adjustment_id] = {
            "fingerprint": fingerprint,
            "submitted_at": _now_iso(),
            "response": json.loads(response.model_dump_json()),
        }
        logger.info(
            "adjustment decision | adjustment_id=%s target=%s:%s delta=%.6f "
            "budget_before=%.6f budget_after=%.6f spent=%.6f basis=applied",
            req.adjustment_id,
            req.target_type,
            req.target_id,
            req.delta_epsilon,
            budget_before,
            budget_after,
            spent,
        )
        return response

    return ledger.run_transaction(tx)


def _parse_time(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise InvalidQueryError(f"invalid {label} timestamp: {value!r}") from exc
        # pragma: no cover
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def query_history(
    ledger: Ledger,
    principal: str,
    requester: str,
    aggregation: str | None = None,
    dimension: str | None = None,
    start: str | None = None,
    end: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> HistoryResponse:
    """按主体查看自己的提交历史，支持筛选与分页；越权访问被拒绝。"""
    if requester != principal:
        raise UnauthorizedError("requester may only view their own history")
    if status is not None and status not in ("completed", "rejected"):
        raise InvalidQueryError(f"unknown status filter '{status}'")
    start_dt = _parse_time(start, "start") if start else None
    end_dt = _parse_time(end, "end") if end else None

    items = ledger.list_history(principal)
    if aggregation is not None:
        items = [h for h in items if h.get("aggregation") == aggregation]
    if dimension is not None:
        items = [h for h in items if h.get("dimension") == dimension]
    if status is not None:
        items = [h for h in items if h.get("status") == status]
    if start_dt is not None or end_dt is not None:
        def _in_range(h: dict[str, Any]) -> bool:
            ts = h.get("submitted_at")
            if ts is None:  # 迁移自旧账本的记录无时间戳，不参与时间范围筛选
                return False
            moment = _parse_time(ts, "submitted_at")
            if start_dt is not None and moment < start_dt:
                return False
            return not (end_dt is not None and moment > end_dt)

        items = [h for h in items if _in_range(h)]

    total = len(items)
    offset = (page - 1) * page_size
    page_items = items[offset : offset + page_size]
    return HistoryResponse(
        principal=principal,
        total=total,
        page=page,
        page_size=page_size,
        items=[HistoryItem(**h) for h in page_items],
    )
