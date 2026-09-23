"""查询编排：校验、预算判定、加噪、抑制与审计落盘。

预算模型（详见 README）：
- 预算以 epsilon 计量，按查询扣减；
- 一次查询的多个机制之间为串行组合：count/sum 扣 epsilon；
  avg 为带噪求和 ÷ 带噪计数，epsilon 在两个机制间平分，仍扣 epsilon；
- 敏感度决定噪声大小：count 敏感度 1，sum 敏感度为全数据集数值列的
  最大绝对值（全局敏感度上界，与具体分组无关）。
"""
import json
import logging
from typing import Any

from app.config import Settings
from app.dataset import Dataset, validate_fields
from app.errors import (
    BudgetExhaustedError,
    InvalidQueryError,
    QueryConflictError,
    UnauthorizedError,
)
from app.models import (
    GroupResult,
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


def submit_query(req: QueryRequest, ledger: Ledger, ds: Dataset, settings: Settings) -> QueryResponse:
    """提交一次 DP 查询。整段校验-扣减-计算-落盘在同一事务内完成。"""
    _validate_request(req, settings)
    need = required_epsilon(req)
    key = list(req.cache_key())

    def tx(state: dict[str, Any]) -> QueryResponse:
        existing = state["queries"].get(req.query_id)
        if existing is not None:
            if existing["cache_key"] != key:
                raise QueryConflictError(
                    f"query_id '{req.query_id}' was submitted with different parameters"
                )
            if existing["status"] == "completed":
                logger.info(
                    "budget decision | query_id=%s principal=%s replayed=true "
                    "before=%.6f required=%.6f after=%.6f basis=idempotent_cache_hit",
                    req.query_id,
                    req.principal,
                    existing["remaining_budget"],
                    0.0,
                    existing["remaining_budget"],
                )
                return QueryResponse(**{**existing["response"], "replayed": True})

        entry = state["principals"].get(req.principal)
        if entry is None or not entry.get("authorized", False):
            raise UnauthorizedError(f"principal '{req.principal}' is not authorized")

        budget = float(entry["budget_epsilon"])
        spent_before = float(entry["spent_epsilon"])
        remaining_before = budget - spent_before
        if remaining_before + 1e-9 < need:
            logger.info(
                "budget decision | query_id=%s principal=%s replayed=false "
                "before=%.6f required=%.6f after=%.6f basis=insufficient_budget_denied",
                req.query_id,
                req.principal,
                remaining_before,
                need,
                remaining_before,
            )
            raise BudgetExhaustedError(
                f"remaining epsilon {remaining_before:.6f} < required {need:.6f}",
                remaining=remaining_before,
                required=need,
            )

        # 计算在锁内完成，结果落盘与扣减原子提交
        results, suppressed = _compute_groups(req, ds, settings)
        spent_after = spent_before + need
        entry["spent_epsilon"] = spent_after
        remaining_after = budget - spent_after

        response = QueryResponse(
            query_id=req.query_id,
            principal=req.principal,
            aggregation=req.aggregation,
            dimension=req.dimension,
            replayed=False,
            epsilon_charged=need,
            remaining_budget=round(remaining_after, 9),
            results=results,
            suppressed=suppressed,
        )
        record = {
            "status": "completed",
            "principal": req.principal,
            "cache_key": key,
            "epsilon_charged": need,
            "remaining_budget": round(remaining_after, 9),
            "response": json.loads(response.model_dump_json()),
        }
        state["queries"][req.query_id] = record
        logger.info(
            "budget decision | query_id=%s principal=%s replayed=false "
            "before=%.6f required=%.6f after=%.6f basis=charged",
            req.query_id,
            req.principal,
            remaining_before,
            need,
            remaining_after,
        )
        return response

    return ledger.run_transaction(tx)
