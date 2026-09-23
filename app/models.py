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

    def cache_key(self) -> tuple[Any, ...]:
        """除 query_id/principal 之外、决定查询语义的参数组合。

        同一 query_id 重试时这些参数必须完全一致，否则拒绝（防止复用 ID
        顶替为不同查询而不扣预算）。
        """
        return (
            self.aggregation.value,
            self.dimension,
            self.value_column,
            round(self.epsilon, 10),
            None if self.groups is None else tuple(sorted(self.groups)),
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
