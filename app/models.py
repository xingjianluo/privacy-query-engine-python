"""请求/响应/错误数据模型。"""
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class AggregationType(str, Enum):
    COUNT = "count"
    SUM = "sum"
    AVG = "avg"


class QueryRequest(BaseModel):
    query_id: str = Field(min_length=1, max_length=128)
    principal: str = Field(min_length=1, max_length=128)
    aggregation: AggregationType
    dimension: str = Field(min_length=1, max_length=64)
    # 求和/均值使用的数值列；计数查询可省略
    value_column: str | None = Field(default=None, max_length=64)
    # 本次查询申请的 epsilon，必须落在 (0, max_query_epsilon]
    epsilon: float = Field(gt=0)
    # 可选的维度取值过滤；None 表示对维度全部分组
    groups: list[str] | None = None
    # 扣费来源：None 表示从个人额度扣；给出池 id 表示从共享预算池扣
    pool_id: str | None = Field(default=None, min_length=1, max_length=128)

    def cache_key(self) -> tuple[Any, ...]:
        """除 query_id/principal 之外、决定查询语义的参数组合。

        同一 query_id 重试时这些参数必须完全一致，否则拒绝（防止复用 ID
        顶替为不同查询而不扣预算）。扣费来源也属于语义的一部分：
        同一编号不能一次走个人额度、一次走池子。
        """
        return (
            self.aggregation.value,
            self.dimension,
            self.value_column,
            round(self.epsilon, 10),
            None if self.groups is None else tuple(sorted(self.groups)),
            self.pool_id,
        )


class GroupResult(BaseModel):
    group: str
    value: float
    # 仅 count/avg 查询给出带噪计数；sum 查询为 None（避免无偿泄露计数）
    noisy_count: int | None = None


class SuppressedGroup(BaseModel):
    group: str
    reason: str
    detail: str | None = None


class QueryResponse(BaseModel):
    query_id: str
    principal: str
    aggregation: AggregationType
    dimension: str
    replayed: bool
    epsilon_charged: float
    remaining_budget: float
    # 本次扣费来源：personal 或 pool
    budget_source: str = "personal"
    pool_id: str | None = None
    results: list[GroupResult]
    suppressed: list[SuppressedGroup]


class BudgetResponse(BaseModel):
    principal: str
    remaining_epsilon: float
    spent_epsilon: float
    authorized: bool


class GrantRequest(BaseModel):
    principal: str = Field(min_length=1, max_length=128)
    budget_epsilon: float | None = Field(default=None, gt=0)


class ErrorResponse(BaseModel):
    error: str
    message: str


# ---------- 共享预算池 ----------


class PoolUpsertRequest(BaseModel):
    pool_id: str = Field(min_length=1, max_length=128)
    # 允许使用该池的主体名单（整体替换）
    members: list[str] = Field(min_length=1)
    # 仅创建时生效；已存在的池调整额度须走 /admin/adjust
    budget_epsilon: float = Field(default=0.0, ge=0)


class PoolResponse(BaseModel):
    pool_id: str
    members: list[str]
    budget_epsilon: float
    spent_epsilon: float
    remaining_epsilon: float


# ---------- 预算调整（追加/回收） ----------


class AdjustRequest(BaseModel):
    # 调整单号：幂等键，同一单号重复提交只生效一次
    adjustment_id: str = Field(min_length=1, max_length=128)
    # 调整对象：个人额度或预算池
    target_type: str = Field(pattern="^(principal|pool)$")
    target: str = Field(min_length=1, max_length=128)
    # 正数追加、负数回收；0 无意义，在服务层拒绝
    delta: float


class AdjustResponse(BaseModel):
    adjustment_id: str
    target_type: str
    target: str
    delta: float
    budget_before: float
    budget_after: float
    spent_epsilon: float
    # True 表示该单号此前已生效，本次为幂等重放
    replayed: bool


# ---------- 查询历史 ----------


class HistoryItem(BaseModel):
    query_id: str
    aggregation: AggregationType
    dimension: str
    status: str  # completed / replayed / rejected
    epsilon: float
    error: str | None = None
    created_at: float
    # 仅 completed/replayed 携带；内容只有加噪结果与抑制原因，绝不含精确值
    results: list[GroupResult] | None = None
    suppressed: list[SuppressedGroup] | None = None


class HistoryResponse(BaseModel):
    principal: str
    total: int
    page: int
    page_size: int
    items: list[HistoryItem]
