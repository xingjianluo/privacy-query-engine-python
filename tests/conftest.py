"""测试公共夹具：每个用例使用独立的临时状态文件与受控预算参数。"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from dp_engine import config, service, storage
from dp_engine.api import create_app
from dp_engine.config import get_settings, reset_settings
from dp_engine.service import QueryService
from dp_engine.storage import Store

# 单测中打印预算扣减前后数值与判定依据（caplog 与控制台均可见）
_BUDGET_LOGGER = logging.getLogger("dp_engine.budget")
_BUDGET_LOGGER.setLevel(logging.INFO)


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path, monkeypatch):
    """隔离状态文件、故障注入开关与配置缓存。"""
    state_path = tmp_path / "state.json"
    monkeypatch.setenv("DP_STATE_PATH", str(state_path))
    monkeypatch.setenv("DP_BUDGET_TOTAL", "3.0")
    monkeypatch.setenv("DP_UNIT_EPSILON", "0.1")
    monkeypatch.setenv("DP_MIN_GROUP_SIZE", "3")
    reset_settings()
    storage.fail_next_write = False
    storage.fail_next_read = False
    service.fail_next_compute = False
    yield
    reset_settings()


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
def store(settings):
    return Store(settings.state_path)


@pytest.fixture
def svc(settings, store):
    return QueryService(settings, store)


@pytest.fixture
def client():
    app = create_app()
    # 关闭服务端异常上抛，使 FastAPI 的 500 处理器在测试中可被观测
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


def submit_count(svc, query_id: str, *, api_key: str = "key-analyst",
                 dimensions=("region",), filters=None, agg="count", column=None):
    from dp_engine.models import QueryRequest

    return svc.submit(QueryRequest(
        query_id=query_id, agg=agg, column=column,
        dimensions=list(dimensions), filters=filters or {}, api_key=api_key,
    ))
