"""请求/响应/错误数据模型。"""
from enum import Enum
from typing import Any, Literal

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
    # 可选的共享预算池 ID；None 表示从个人额度扣减（默认，兼容旧客户端）
    pool_id: str | None = Field(default=None, min_length=1, max_length=128)

    def cache_key(self) -> tuple[Any, ...]:
        """除 query_id/principal 之外、决定查询语义的参数组合。

        同一 query_id 重试时这些参数必须完全一致，否则拒绝（防止复用 ID
        顶替为不同查询而不扣预算）。pool_id 决定扣费来源，也属于语义参数。
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
    # 本次扣费来源：None 表示个人额度，否则为共享预算池 ID
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


class PoolCreateRequest(BaseModel):
    pool_id: str = Field(min_length=1, max_length=128)
    budget_epsilon: float = Field(gt=0)
    # 允许使用该池的主体列表（创建时配置，之后可整体替换）
    members: list[str] = Field(default_factory=list, max_length=256)


class PoolMembersRequest(BaseModel):
    members: list[str] = Field(default_factory=list, max_length=256)


class PoolResponse(BaseModel):
    pool_id: str
    budget_epsilon: float
    spent_epsilon: float
    remaining_epsilon: float
    members: list[str]


class AdjustmentRequest(BaseModel):
    """一次预算调整（追加/回收）。adjustment_id 为幂等键。"""

    adjustment_id: str = Field(min_length=1, max_length=128)
    target_type: Literal["principal", "pool"]
    target_id: str = Field(min_length=1, max_length=128)
    # 正数 = 追加额度；负数 = 回收额度；不允许为 0（service 层校验）
    delta_epsilon: float

    def fingerprint(self) -> tuple[Any, ...]:
        """幂等校验用：同一 adjustment_id 重试时参数必须完全一致。"""
        return (self.target_type, self.target_id, round(self.delta_epsilon, 10))


class AdjustmentResponse(BaseModel):
    adjustment_id: str
    target_type: str
    target_id: str
    delta_epsilon: float
    budget_epsilon: float
    spent_epsilon: float
    remaining_epsilon: float
    replayed: bool


class HistoryItem(BaseModel):
    """一条提交历史。只含元数据，绝不含原始精确值。"""

    query_id: str
    principal: str
    aggregation: str
    dimension: str
    epsilon: float
    status: str  # completed / rejected
    error: str | None = None
    pool_id: str | None = None
    epsilon_charged: float = 0.0
    submitted_at: str | None = None


class HistoryResponse(BaseModel):
    principal: str
    total: int
    page: int
    page_size: int
    items: list[HistoryItem]


class ErrorResponse(BaseModel):
    error: str
    message: str
