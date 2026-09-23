"""可区分的错误类型。对外只暴露错误码与可读消息，不暴露内部异常细节。"""

from __future__ import annotations


class DPError(Exception):
    """所有业务错误的基类，携带稳定的错误码与 HTTP 状态码。"""

    code = "internal_error"
    http_status = 500

    def __init__(self, message: str = "", *, cause: Exception | None = None):
        super().__init__(message or self.code)
        self.message = message or self.code
        # cause 仅用于服务端日志，禁止回传给客户端
        self.cause = cause


class InvalidParameterError(DPError):
    code = "invalid_parameter"
    http_status = 422


class UnauthorizedError(DPError):
    code = "unauthorized"
    http_status = 401


class ForbiddenError(DPError):
    code = "forbidden"
    http_status = 403


class BudgetExhaustedError(DPError):
    code = "budget_exhausted"
    http_status = 402

    def __init__(self, message: str = "", *, remaining: float = 0.0, required: float = 0.0, cause: Exception | None = None):
        super().__init__(message, cause=cause)
        self.remaining = remaining
        self.required = required


class QueryConflictError(DPError):
    code = "query_conflict"
    http_status = 409


class InternalInfrastructureError(DPError):
    code = "internal_error"
    http_status = 500

    def __init__(self, message: str | None = None, *, cause: Exception | None = None):
        # 对外固定通用文案；任何具体细节必须放进 cause（仅写服务端日志）
        super().__init__("服务内部错误，请稍后重试", cause=cause)
