"""重复提交：相同 query_id+参数结果一致、不重复扣预算；跨重启一致；参数冲突可区分。"""

from __future__ import annotations

import pytest

from dp_engine.errors import BudgetExhaustedError, QueryConflictError
from dp_engine.models import QueryRequest
from dp_engine.service import QueryService
from dp_engine.storage import Store
from tests.conftest import submit_count

ANALYST = "key-analyst"


def _count_req(qid, dims=("region", "product"), **kw):
    return QueryRequest(
        query_id=qid, agg="count", dimensions=list(dims),
        api_key=kw.pop("api_key", ANALYST), **kw,
    )


def test_repeat_submission_returns_identical_result(svc):
    r1 = svc.submit(_count_req("id-1"))
    r2 = svc.submit(_count_req("id-1"))

    assert r1.cached is False
    assert r2.cached is True
    # 结果载荷逐字段一致
    assert r2.status == r1.status
    assert r2.groups == r1.groups
    assert r2.charged_budget == r1.charged_budget
    # 重复提交没有再次扣预算
    assert r2.remaining_budget == r1.remaining_budget

    # 幂等记录只有一份，审计为 charged + cached
    entries = svc.get_audit("analyst")
    assert [e["decision"] for e in entries] == ["charged", "cached"]
    assert entries[1]["charged_budget"] == 0.0


def test_idempotent_result_survives_restart(settings):
    svc1 = QueryService(settings, Store(settings.state_path))
    r1 = svc1.submit(_count_req("id-restart"))

    # 模拟进程重启：新建 service/store，但复用同一状态文件
    svc2 = QueryService(settings, Store(settings.state_path))
    r2 = svc2.submit(_count_req("id-restart"))

    assert r2.cached is True
    assert r2.groups == r1.groups
    # 预算消耗跨重启保留
    spent_after = svc2.get_audit("analyst")
    assert sum(e["charged_budget"] for e in spent_after) == pytest.approx(0.1)


def test_same_id_different_params_is_conflict(svc):
    svc.submit(_count_req("id-c", dims=("region",)))
    with pytest.raises(QueryConflictError) as exc:
        svc.submit(_count_req("id-c", dims=("product",)))
    assert exc.value.code == "query_conflict"
    assert exc.value.http_status == 409
    # 冲突是只读拒绝，不扣预算、不留记录
    entries = svc.get_audit("analyst")
    assert len(entries) == 1


def test_same_id_is_namespaced_per_principal(svc):
    """不同主体使用相同 query_id 互不影响：各自计费、各自缓存、互不可见。"""
    r_a = svc.submit(_count_req("id-p", api_key=ANALYST))
    r_c = svc.submit(_count_req("id-p", api_key="key-counter"))
    assert r_a.cached is False and r_c.cached is False
    assert r_a.remaining_budget == r_c.remaining_budget == pytest.approx(2.9)
    # 各自回放都命中自己的缓存
    assert svc.submit(_count_req("id-p", api_key=ANALYST)).cached is True
    assert svc.submit(_count_req("id-p", api_key="key-counter")).cached is True


def test_rejected_query_is_also_idempotent(svc):
    for i in range(30):
        submit_count(svc, f"id-r-{i}")
    with pytest.raises(BudgetExhaustedError) as e1:
        submit_count(svc, "id-r-over")
    with pytest.raises(BudgetExhaustedError) as e2:
        submit_count(svc, "id-r-over")
    # 两次拒绝错误类型一致，且都不扣预算
    assert e1.value.code == e2.value.code == "budget_exhausted"
    entries = svc.get_audit("analyst")
    rejects = [e for e in entries if e["decision"] == "rejected_budget"]
    assert len(rejects) == 1


def test_different_query_ids_get_independent_noise(svc):
    r1 = svc.submit(_count_req("id-n1"))
    r2 = svc.submit(_count_req("id-n2"))
    # 不同 query_id 派生不同噪声种子（至少一个分组噪声不同）
    values = {(tuple(g.key), g.value) for g in r1.groups}
    values2 = {(tuple(g.key), g.value) for g in r2.groups}
    assert values != values2
