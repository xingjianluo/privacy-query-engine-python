"""可区分的领域错误类型。

每个错误对应稳定的 ``code``，调用方与测试据此区分失败原因；
对外响应只携带 code 与安全的简短 message，绝不暴露内部堆栈。
"""


class ServiceError(Exception):
    code = "internal_error"
    http_status = 500

    def __init__(self, message: str = "") -> None:
        super().__init__(message)
        self.message = message or self.code


class InvalidQueryError(ServiceError):
    """查询参数非法（未知维度/列、epsilon 超限、空分组列表等）。"""

    code = "invalid_query"
    http_status = 422


class UnauthorizedError(ServiceError):
    """主体未被授权使用查询服务。"""

    code = "unauthorized"
    http_status = 403


class BudgetExhaustedError(ServiceError):
    """剩余隐私预算不足以支付本次查询的 epsilon。"""

    code = "budget_exhausted"
    http_status = 429

    def __init__(self, message: str = "", remaining: float = 0.0, required: float = 0.0) -> None:
        super().__init__(message)
        self.remaining = remaining
        self.required = required


class QueryConflictError(ServiceError):
    """query_id 已存在但参数与首次提交不一致。"""

    code = "query_conflict"
    http_status = 409


class PoolBudgetExhaustedError(ServiceError):
    """共享预算池剩余额度不足以支付本次查询（与个人预算不足可区分）。"""

    code = "pool_budget_exhausted"
    http_status = 429

    def __init__(self, message: str = "", remaining: float = 0.0, required: float = 0.0) -> None:
        super().__init__(message)
        self.remaining = remaining
        self.required = required


class PoolAccessDeniedError(ServiceError):
    """主体不在所请求预算池的成员名单内。"""

    code = "pool_access_denied"
    http_status = 403


class UnknownPoolError(ServiceError):
    """引用了不存在的预算池。"""

    code = "unknown_pool"
    http_status = 404


class AdjustmentRejectedError(ServiceError):
    """预算调整被整体拒绝（如回收后余额将低于已消耗、目标不存在）。"""

    code = "adjustment_rejected"
    http_status = 422


class AdjustmentConflictError(ServiceError):
    """同一调整单号以不同参数重复提交。"""

    code = "adjustment_conflict"
    http_status = 409


class PersistenceError(ServiceError):
    """持久化层失败（对客户端仅暴露通用内部错误信息）。"""

    code = "persistence_failure"
    http_status = 500
