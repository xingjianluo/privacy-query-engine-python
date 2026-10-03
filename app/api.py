"""FastAPI 路由与错误映射。

内部异常统一转换为 ``internal_error`` 响应，仅记录详细日志，
不向客户端暴露堆栈或内部细节。
"""
import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.config import Settings, get_settings
from app.dataset import load_dataset
from app.errors import (
    BudgetExhaustedError,
    ServiceError,
)
from app.models import (
    AdjustRequest,
    AdjustResponse,
    BudgetResponse,
    GrantRequest,
    HistoryResponse,
    PoolResponse,
    PoolUpsertRequest,
    QueryRequest,
    QueryResponse,
)
from app.service import adjust_budget, get_history, get_pool, submit_query, upsert_pool
from app.storage import Ledger

logger = logging.getLogger("pqe.api")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    ledger = Ledger(settings.ledger_path, settings.lock_path)
    dataset = load_dataset(settings.dataset_path)

    app = FastAPI(title="Local DP Aggregation Query Service", version="0.1.0", debug=False)

    def _service_error_response(exc: ServiceError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": exc.code, "message": exc.message},
        )

    @app.exception_handler(ServiceError)
    async def _on_service_error(_: Request, exc: ServiceError) -> JSONResponse:
        if exc.code == BudgetExhaustedError.code:
            logger.info("request rejected: %s (%s)", exc.code, exc.message)
        else:
            logger.warning("request rejected: %s (%s)", exc.code, exc.message)
        return _service_error_response(exc)

    @app.exception_handler(RequestValidationError)
    async def _on_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # 不回显异常原文，只给出稳定错误码
        return JSONResponse(
            status_code=422,
            content={"error": "invalid_query", "message": "request payload failed validation"},
        )

    @app.exception_handler(Exception)
    async def _on_unexpected_error(_: Request, exc: Exception) -> JSONResponse:
        ref = uuid.uuid4().hex[:12]
        logger.exception("unexpected internal error ref=%s", ref)
        return JSONResponse(
            status_code=500,
            content={"error": "internal_error", "message": f"internal error, ref={ref}"},
        )

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.post("/admin/grant", response_model=BudgetResponse, status_code=201)
    def grant(body: GrantRequest) -> BudgetResponse:
        budget = body.budget_epsilon
        if budget is None:
            budget = settings.default_budget_epsilon
        ledger.grant(body.principal, budget)
        info = ledger.get_budget(body.principal)
        return BudgetResponse(
            principal=body.principal,
            remaining_epsilon=info["budget_epsilon"] - info["spent_epsilon"],
            spent_epsilon=info["spent_epsilon"],
            authorized=True,
        )

    @app.get("/budget/{principal}", response_model=BudgetResponse)
    def budget(principal: str) -> BudgetResponse:
        info = ledger.get_budget(principal)
        return BudgetResponse(
            principal=principal,
            remaining_epsilon=info["budget_epsilon"] - info["spent_epsilon"],
            spent_epsilon=info["spent_epsilon"],
            authorized=info["authorized"],
        )

    @app.post("/query", response_model=QueryResponse)
    async def query(body: QueryRequest) -> QueryResponse:
        return submit_query(body, ledger, dataset, settings)

    # ---------- 共享预算池 ----------

    @app.post("/admin/pool", response_model=PoolResponse, status_code=201)
    def create_pool(body: PoolUpsertRequest) -> PoolResponse:
        return upsert_pool(body, ledger)

    @app.get("/pool/{pool_id}", response_model=PoolResponse)
    def pool(pool_id: str) -> PoolResponse:
        return get_pool(pool_id, ledger)

    # ---------- 预算调整（追加/回收） ----------

    @app.post("/admin/adjust", response_model=AdjustResponse, status_code=200)
    def adjust(body: AdjustRequest) -> AdjustResponse:
        return adjust_budget(body, ledger)

    # ---------- 查询历史（仅本人可见） ----------

    @app.get("/history/{principal}", response_model=HistoryResponse)
    def history(
        principal: str,
        viewer: str,
        aggregation: str | None = None,
        dimension: str | None = None,
        status: str | None = None,
        start: float | None = None,
        end: float | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> HistoryResponse:
        return get_history(
            viewer,
            principal,
            ledger,
            aggregation=aggregation,
            dimension=dimension,
            status=status,
            start=start,
            end=end,
            page=page,
            page_size=page_size,
        )

    return app
