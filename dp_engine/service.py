"""查询编排。

一次 submit 的固定顺序（错误类型因此可区分）：
    1. authenticate  -> 401 unauthorized
    2. authorize     -> 403 forbidden
    3. 语义校验       -> 422 invalid_parameter（类型/必填由 pydantic 在入口拦截）
    4. 开事务（持文件锁，读-改-原子写）：
       a. query_id 已存在且指纹一致 -> 回放结果，cached=true，不再扣预算；
       b. query_id 已存在但指纹不同 -> 409 query_conflict；
       c. 按敏感度计算成本，预算不足 -> 记录一次 budget_rejected（可审计、重试稳定），
          事务提交后抛 402 budget_exhausted；
       d. 预算充足 -> 先加噪聚合，再扣预算；预算/幂等记录/审计在同一原子事务落盘。
事务内部不向调用方抛 402/返回值：事务内只决定 outcome，事务成功提交后再响应，
以保证"拒绝记录已持久化"与"错误对外抛出"的先后顺序；任何基础设施异常使事务回滚，
绝不留下半完成的预算状态。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from .authorizer import authenticate, authorize
from .dataset import COLUMN_DOMAINS, dimension_columns, get_rows, numeric_columns
from .dp_engine import aggregate, budget_cost, sensitivity
from .errors import (
    BudgetExhaustedError,
    InternalInfrastructureError,
    InvalidParameterError,
    QueryConflictError,
)
from .models import (
    AuditEntry,
    IdempotentRecord,
    PrincipalState,
    QueryRequest,
    QueryResponse,
)

logger = logging.getLogger("dp_engine.budget")

# 测试用故障注入：模拟聚合计算阶段抛出内部异常
fail_next_compute = False

_BUDGET_EPS = 1e-9


def _fingerprint(req: QueryRequest) -> str:
    material = json.dumps(
        {
            "agg": req.agg,
            "column": req.column,
            "dimensions": list(req.dimensions),
            "filters": req.filters,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return material


def _validate_semantics(req: QueryRequest) -> None:
    if req.agg in ("sum", "mean"):
        if not req.column:
            raise InvalidParameterError(f"{req.agg} 聚合必须指定数值列 column")
        if req.column not in numeric_columns():
            raise InvalidParameterError(
                f"列 {req.column!r} 不是数值列，可用于 {req.agg} 的列: {sorted(numeric_columns())}"
            )
    elif req.agg == "count" and req.column is not None:
        raise InvalidParameterError("count 聚合不接受 column 参数")

    if len(req.dimensions) != len(set(req.dimensions)):
        raise InvalidParameterError("dimensions 中存在重复维度")
    bad_dims = [d for d in req.dimensions if d not in dimension_columns()]
    if bad_dims:
        raise InvalidParameterError(
            f"非法维度列: {bad_dims}，可用维度: {sorted(dimension_columns())}"
        )

    known = numeric_columns() | dimension_columns()
    bad_filters = [k for k in req.filters if k not in known]
    if bad_filters:
        raise InvalidParameterError(
            f"过滤条件包含未知列: {bad_filters}，可用列: {sorted(known)}"
        )


class QueryService:
    def __init__(self, settings, store):
        self.settings = settings
        self.store = store

    # ------------------------------------------------------------------ #
    def submit(self, req: QueryRequest) -> QueryResponse:
        # 1-2. 认证与授权（先于业务，错误码稳定可区分）
        principal = authenticate(req.api_key)
        authorize(principal, req.agg)
        # 3. 语义校验
        _validate_semantics(req)

        fingerprint = _fingerprint(req)
        domain = COLUMN_DOMAINS.get(req.column) if req.column else None
        sens = sensitivity(req.agg, domain, self.settings.min_group_size)
        cost = budget_cost(req.agg, domain, self.settings)
        total = self.settings.budget_total

        # 事务内只写入 outcome；事务提交后再返回/抛错
        outcome: dict | None = None
        query_conflict: QueryConflictError | None = None
        infra_error: InternalInfrastructureError | None = None

        try:
            with self.store.transaction() as state:
                pstate = state.principals.setdefault(principal, PrincipalState())

                # 4a/4b. 幂等回放 / query_id 冲突
                existing = pstate.records.get(req.query_id)
                if existing is not None:
                    # 幂等空间按主体隔离：不同主体可各自使用相同 id，且互不可见
                    if existing.fingerprint != fingerprint:
                        query_conflict = QueryConflictError(
                            "query_id 已被不同参数的查询占用，请更换 query_id"
                        )
                        # 抛错使事务只读回滚（本次没有任何写入）
                        raise query_conflict
                    replay = self._build_replay(existing, pstate.spent, total)
                    pstate.audit.append(self._audit(
                        req, principal, fingerprint, sens, 0.0,
                        pstate.spent, pstate.spent, "cached",
                        "相同 query_id+参数的重复提交，回放缓存结果，不重复扣减预算",
                    ))
                    outcome = {"kind": "cached_rejected" if replay[0] == "rejected" else "ok",
                               "response": replay[1],
                               "remaining": replay[2]}
                    # 正常退出 with -> 审计条目原子落盘（仍不扣预算）
                else:
                    before = pstate.spent
                    available = max(0.0, total - before)  # 钳制浮点误差

                    # 4c. 预算判定
                    if before + cost > total + _BUDGET_EPS:
                        logger.info(
                            "预算判定 query_id=%s principal=%s 判定依据=拒绝 | "
                            "敏感度=%.6f unit_epsilon=%.6f 需要成本=%.6f | "
                            "扣减前已用=%.6f 总预算=%.6f 剩余=%.6f 扣减后(预估)=%.6f 实际扣减=0.000000",
                            req.query_id, principal, sens, self.settings.unit_epsilon, cost,
                            before, total, available, before + cost,
                        )
                        rejected = QueryResponse(
                            query_id=req.query_id,
                            agg=req.agg,
                            column=req.column,
                            dimensions=list(req.dimensions),
                            status="budget_rejected",
                            groups=[],
                            suppress_reason=None,
                            suppression_threshold=self.settings.min_group_size,
                            charged_budget=0.0,
                            remaining_budget=round(available, 6),
                            cached=False,
                        )
                        pstate.records[req.query_id] = IdempotentRecord(
                            principal=principal,
                            fingerprint=fingerprint,
                            status="budget_rejected",
                            response=rejected.model_dump(),
                            charged_budget=0.0,
                        )
                        pstate.audit.append(self._audit(
                            req, principal, fingerprint, sens, 0.0, before, before,
                            "rejected_budget",
                            f"需要 {cost:.6f} > 剩余 {available:.6f}"
                            f"（敏感度 {sens:.6f} × unit_epsilon {self.settings.unit_epsilon:.6f}）",
                        ))
                        outcome = {"kind": "rejected", "response": rejected,
                                   "remaining": available, "required": cost}
                    else:
                        # 4d. 预算充足：先计算（失败 -> 事务回滚，分文不扣）
                        global fail_next_compute
                        if fail_next_compute:
                            fail_next_compute = False
                            infra_error = InternalInfrastructureError(
                                cause=RuntimeError("compute failure (injected)")
                            )
                            raise infra_error
                        try:
                            groups, overall_reason = aggregate(
                                req, self.settings, get_rows()
                            )
                        except InternalInfrastructureError:
                            raise
                        except Exception as exc:
                            # 内部细节不外泄；事务随异常回滚
                            raise InternalInfrastructureError("聚合计算失败", cause=exc)

                        after = before + cost
                        logger.info(
                            "预算判定 query_id=%s principal=%s 判定依据=扣减 | "
                            "敏感度=%.6f unit_epsilon=%.6f 需要成本=%.6f | "
                            "扣减前已用=%.6f 总预算=%.6f 剩余=%.6f 扣减后已用=%.6f",
                            req.query_id, principal, sens, self.settings.unit_epsilon, cost,
                            before, total, available, after,
                        )
                        pstate.spent = after
                        status = "suppressed" if overall_reason else "result"
                        response = QueryResponse(
                            query_id=req.query_id,
                            agg=req.agg,
                            column=req.column,
                            dimensions=list(req.dimensions),
                            status=status,
                            groups=groups,
                            suppress_reason=overall_reason,
                            suppression_threshold=self.settings.min_group_size,
                            charged_budget=round(cost, 6),
                            remaining_budget=round(total - after, 6),
                            cached=False,
                        )
                        pstate.records[req.query_id] = IdempotentRecord(
                            principal=principal,
                            fingerprint=fingerprint,
                            status=status,
                            response=response.model_dump(),
                            charged_budget=round(cost, 6),
                        )
                        pstate.audit.append(self._audit(
                            req, principal, fingerprint, sens, cost, before, after,
                            "charged",
                            f"需要 {cost:.6f} <= 剩余 {available:.6f}"
                            f"（敏感度 {sens:.6f} × unit_epsilon {self.settings.unit_epsilon:.6f}）",
                        ))
                        outcome = {"kind": "ok", "response": response}
        except QueryConflictError:
            raise
        except InternalInfrastructureError:
            raise

        assert outcome is not None, "事务结束但未产生任何裁决"
        kind = outcome["kind"]
        if kind == "ok":
            return outcome["response"]
        if kind == "rejected":
            raise BudgetExhaustedError(
                f"隐私预算不足：需要 {outcome['required']:.6f}，"
                f"剩余 {outcome['remaining']:.6f}",
                remaining=outcome["remaining"],
                required=outcome["required"],
            )
        # cached_rejected：拒绝也被幂等回放，错误类型与首次一致
        raise BudgetExhaustedError(
            "隐私预算不足（该查询首次提交时已被拒绝，裁决已幂等缓存）",
            remaining=outcome["remaining"],
            required=0.0,
        )

    # ------------------------------------------------------------------ #
    def _build_replay(self, record: IdempotentRecord, spent: float,
                      total: float) -> tuple[str, QueryResponse, float]:
        """构造回放响应：结果载荷（状态/分组/噪声值/成本）与首次完全一致。

        remaining_budget 反映当前真实余额，cached=true 表明是幂等回放；
        任何情况下回放都不会再次扣减预算。
        """
        payload = dict(record.response)
        payload["cached"] = True
        payload["remaining_budget"] = round(max(0.0, total - spent), 6)
        logger.info(
            "预算判定 query_id=%s principal=%s 判定依据=幂等回放 | "
            "实际扣减=0.000000 当前已用=%.6f 剩余=%.6f",
            payload.get("query_id"), record.principal, spent, total - spent,
        )
        kind = "rejected" if record.status == "budget_rejected" else "ok"
        return kind, QueryResponse.model_validate(payload), round(max(0.0, total - spent), 6)

    # ------------------------------------------------------------------ #
    def get_audit(self, principal: str) -> list[dict]:
        with self.store.transaction() as state:
            pstate = state.principals.get(principal)
            if pstate is None:
                return []
            return [e.model_dump() for e in pstate.audit]

    @staticmethod
    def _audit(req, principal, fingerprint, sens, cost, before, after,
               decision, detail) -> AuditEntry:
        return AuditEntry(
            query_id=req.query_id,
            principal=principal,
            agg=req.agg,
            column=req.column,
            dimensions=list(req.dimensions),
            fingerprint=fingerprint,
            sensitivity=round(sens, 6),
            charged_budget=round(cost, 6),
            budget_before=round(before, 6),
            budget_after=round(after, 6),
            decision=decision,
            detail=detail,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
