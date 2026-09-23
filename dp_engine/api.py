"""FastAPI 应用与错误处理装配。

统一错误信封：{"error": {"code", "message", ...}}
- DPError 子类映射到各自稳定的错误码与 HTTP 状态；
- pydantic 校验错误 -> 422 invalid_parameter（带字段级细节，不涉及内部实现）；
- 其余未预期异常 -> 500 internal_error，仅返回固定文案，细节只进服务端日志。
"""

from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .config import get_settings, reset_settings
from .errors import DPError
from .models import QueryRequest, QueryResponse
from .service import QueryService
from .storage import Store

logger = logging.getLogger("dp_engine.api")


def create_app() -> FastAPI:
    reset_settings()
    settings = get_settings()
    store = Store(settings.state_path)
    service = QueryService(settings, store)

    app = FastAPI(
        title="Local DP Aggregation Query Service",
        version="0.1.0",
    )
    app.state.service = service
    app.state.settings = settings

    # 注意：Starlette 按处理器注册顺序匹配，需先注册最宽泛的 Exception，
    # 再注册更具体的类型，否则 DPError（也是 Exception）会被通用处理器截获。
    @app.exception_handler(Exception)
    async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        incident = uuid.uuid4().hex
        logger.exception("未预期异常 incident=%s: %r", incident, exc)
        return JSONResponse(
            status_code=500,
            content={"error": {
                "code": "internal_error",
                "message": f"服务内部错误，请联系管理员（incident={incident}）",
            }},
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {"field": ".".join(str(p) for p in e["loc"] if p != "body"),
             "issue": e["msg"]}
            for e in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content={"error": {
                "code": "invalid_parameter",
                "message": "请求参数不合法",
                "details": details,
            }},
        )

    @app.exception_handler(DPError)
    async def handle_dp_error(_: Request, exc: DPError) -> JSONResponse:
        # 内部 cause 只记录到日志，绝不回传
        if exc.cause is not None:
            logger.error("错误码=%s 的内部原因: %r", exc.code, exc.cause)
        body: dict = {"code": exc.code, "message": exc.message}
        if exc.code == "budget_exhausted":
            body["remaining_budget"] = round(exc.remaining, 6)
            body["required_budget"] = round(exc.required, 6)
        return JSONResponse(status_code=exc.http_status, content={"error": body})

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/query", response_model=QueryResponse)
    def submit_query(req: QueryRequest, request: Request) -> QueryResponse:
        # 允许用 X-API-Key 头携带凭证（重试场景下客户端不必把 Key 放进 body）
        header_key = request.headers.get("x-api-key")
        if header_key and not req.api_key:
            req.api_key = header_key
        return request.app.state.service.submit(req)

    @app.get("/audit")
    def get_audit(request: Request, principal: str, api_key: str | None = None) -> dict:
        """查询某主体的预算审计；请求者必须就是该主体本人。"""
        from .authorizer import authenticate

        key = api_key or request.headers.get("x-api-key")
        caller = authenticate(key)
        if caller != principal:
            from .errors import ForbiddenError

            raise ForbiddenError("只能查询本主体的审计记录")
        entries = request.app.state.service.get_audit(principal)
        return {"principal": principal, "entries": entries}

    return app


app = create_app()
