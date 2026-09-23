"""请求/响应/状态的数据模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

AggType = Literal["count", "sum", "mean"]
RecordStatus = Literal["result", "suppressed", "budget_rejected"]


class QueryRequest(BaseModel):
    query_id: str = Field(min_length=1, max_length=128)
    agg: AggType
    column: str | None = None
    dimensions: list[str] = Field(default_factory=list)
    filters: dict[str, Any] = Field(default_factory=dict)
    api_key: str | None = None


class GroupResult(BaseModel):
    key: list[Any]
    status: Literal["ok", "suppressed"]
    value: float | None = None
    suppress_reason: str | None = None
    # 始终为 None：真实行数属精确值，即使抑制分组也不泄漏
    group_size: int | None = None


class QueryResponse(BaseModel):
    query_id: str
    agg: AggType
    column: str | None
    dimensions: list[str]
    status: RecordStatus
    groups: list[GroupResult] = Field(default_factory=list)
    suppress_reason: str | None = None
    suppression_threshold: int = 0  # 公开的最小分组阈值 k
    charged_budget: float
    remaining_budget: float
    cached: bool


class IdempotentRecord(BaseModel):
    """以 query_id 为键的已处理记录，保证重试结果一致且不重复扣预算。"""

    principal: str
    fingerprint: str
    status: RecordStatus
    response: dict[str, Any]
    charged_budget: float


class AuditEntry(BaseModel):
    query_id: str
    principal: str
    agg: AggType
    column: str | None
    dimensions: list[str]
    fingerprint: str
    sensitivity: float
    charged_budget: float
    budget_before: float
    budget_after: float
    decision: str  # charged / rejected_budget / cached
    detail: str
    timestamp: str


class PrincipalState(BaseModel):
    spent: float = 0.0
    # query_id -> IdempotentRecord
    records: dict[str, IdempotentRecord] = Field(default_factory=dict)
    # 该主体下所有查询的审计条目
    audit: list[AuditEntry] = Field(default_factory=list)


class PersistedState(BaseModel):
    principals: dict[str, PrincipalState] = Field(default_factory=dict)
