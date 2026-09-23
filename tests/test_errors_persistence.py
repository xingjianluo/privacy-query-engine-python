"""错误区分、跨重启持久化、故障原子性与内部细节隐藏。"""

from __future__ import annotations

import pytest

from dp_engine import service, storage
from dp_engine.errors import (
    InternalInfrastructureError,
    InvalidParameterError,
)
from dp_engine.service import QueryService
from dp_engine.storage import Store
from tests.conftest import submit_count

ANALYST = "key-analyst"


# --------------------------------------------------------------------- #
# 错误类型可区分（HTTP 层）
# --------------------------------------------------------------------- #
def test_error_codes_via_api(client):
    cases = [
        (401, {"query_id": "e", "agg": "count"}, None),
        (403, {"query_id": "e", "agg": "mean", "column": "score",
               "api_key": "key-counter"}, None),
        (422, {"query_id": "e", "agg": "sum", "api_key": ANALYST}, None),
        (422, {"query_id": "e", "agg": "count", "dimensions": ["nope"],
               "api_key": ANALYST}, None),
        (422, {"query_id": "", "agg": "count", "api_key": ANALYST}, None),
        (422, {"query_id": "e", "agg": "count",
               "filters": {"hacker": 1}, "api_key": ANALYST}, None),
    ]
    seen = set()
    for status, body, _ in cases:
        r = client.post("/query", json=body)
        assert r.status_code == status, (body, r.json())
        seen.add(r.json()["error"]["code"])
    # 401/403/422 三种业务拒绝彼此可区分
    assert seen == {"unauthorized", "forbidden", "invalid_parameter"}


def test_invalid_parameter_messages_are_specific(svc):
    from dp_engine.models import QueryRequest

    with pytest.raises(InvalidParameterError, match="不是数值列"):
        svc.submit(QueryRequest(query_id="e", agg="sum", column="region",
                                api_key=ANALYST))
    with pytest.raises(InvalidParameterError, match="重复维度"):
        svc.submit(QueryRequest(query_id="e", agg="count",
                                dimensions=["region", "region"], api_key=ANALYST))


# --------------------------------------------------------------------- #
# 跨重启持久化
# --------------------------------------------------------------------- #
def test_budget_and_records_persist_across_restart(settings):
    s1 = QueryService(settings, Store(settings.state_path))
    submit_count(s1, "p-1")
    submit_count(s1, "p-2")

    s2 = QueryService(settings, Store(settings.state_path))  # “重启”
    # 预算消耗保留
    entries = s2.get_audit("analyst")
    assert sum(e['charged_budget'] for e in entries) == pytest.approx(0.2)
    # 已处理 query_id 的结果缓存保留
    r_again = submit_count(s2, "p-1")
    assert r_again.cached is True
    assert r_again.remaining_budget == pytest.approx(2.8)


# --------------------------------------------------------------------- #
# 故障原子性
# --------------------------------------------------------------------- #
def test_compute_failure_leaves_no_budget_change(settings):
    s = QueryService(settings, Store(settings.state_path))
    service.fail_next_compute = True
    with pytest.raises(InternalInfrastructureError):
        submit_count(s, "f-compute")

    state = Store(settings.state_path).load()
    assert "analyst" not in state.principals or state.principals["analyst"].spent == 0.0
    # 无半完成记录
    pstate = state.principals.get("analyst")
    assert pstate is None or "f-compute" not in pstate.records


def test_storage_write_failure_rolls_back(settings):
    s = QueryService(settings, Store(settings.state_path))
    submit_count(s, "f-ok-1")  # 先建立正常状态 0.1
    storage.fail_next_write = True
    with pytest.raises(InternalInfrastructureError):
        submit_count(s, "f-write-fail")

    # 磁盘状态仍是上一次成功提交的版本
    state = Store(settings.state_path).load()
    assert state.principals["analyst"].spent == pytest.approx(0.1)
    assert "f-write-fail" not in state.principals["analyst"].records


def test_storage_read_failure_does_not_expose_internals(settings, client):
    # HTTP 层：基础设施错误只暴露固定错误信封
    storage.fail_next_read = True
    # 通过直接调用服务验证（读故障发生在事务内）
    s = QueryService(settings, Store(settings.state_path))
    try:
        submit_count(s, "f-read-fail")
    except InternalInfrastructureError as exc:
        assert exc.code == "internal_error"
        assert exc.message == "服务内部错误，请稍后重试"
        assert "state read failed" not in exc.message
    else:
        pytest.fail("应当抛出内部错误")


def test_internal_error_envelope_hides_infrastructure_detail(client):
    """基础设施异常 -> 500 固定文案，具体 cause 只在服务端日志。"""
    service.fail_next_compute = True
    r = client.post("/query", json={
        "query_id": "h-500", "agg": "count",
        "dimensions": ["region"], "api_key": ANALYST,
    })
    assert r.status_code == 500
    assert r.json()["error"] == {
        "code": "internal_error", "message": "服务内部错误，请稍后重试",
    }
    assert "compute" not in r.text and "Traceback" not in r.text
    # 事务回滚：无预算被扣
    from dp_engine.storage import Store as _Store
    state = _Store(client.app.state.settings.state_path).load()
    assert all(ps.spent == 0.0 for ps in state.principals.values())


def test_unexpected_error_envelope_hides_details(client, monkeypatch):
    """完全未预期异常 -> 500 incident 信封，不含堆栈/内部信息。"""
    def boom(_req):
        raise RuntimeError("secret internal detail XYZ")

    monkeypatch.setattr(client.app.state.service, "submit", boom)
    r = client.post("/query", json={
        "query_id": "h-boom", "agg": "count",
        "dimensions": ["region"], "api_key": ANALYST,
    })
    assert r.status_code == 500
    body = r.json()["error"]
    assert body["code"] == "internal_error"
    assert "incident" in body["message"]
    assert "XYZ" not in r.text and "Traceback" not in r.text and "RuntimeError" not in r.text


def test_budget_exhausted_http_envelope(client):
    for i in range(30):
        client.post("/query", json={"query_id": f"h-{i}", "agg": "count",
                                    "dimensions": ["region"], "api_key": ANALYST})
    r = client.post("/query", json={"query_id": "h-over", "agg": "count",
                                    "dimensions": ["region"], "api_key": ANALYST})
    assert r.status_code == 402
    body = r.json()["error"]
    assert body["code"] == "budget_exhausted"
    assert body["required_budget"] == pytest.approx(0.1)
    assert body["remaining_budget"] == pytest.approx(0.0)
    # 重试得到同样的错误码
    r2 = client.post("/query", json={"query_id": "h-over", "agg": "count",
                                     "dimensions": ["region"], "api_key": ANALYST})
    assert r2.status_code == 402 and r2.json()["error"]["code"] == "budget_exhausted"
