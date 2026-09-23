"""预算耗尽：成本按敏感度扣减、耗尽后拒绝、错误可区分、日志含扣减前后数值与依据。"""

from __future__ import annotations

import logging

import pytest

from dp_engine.errors import (
    BudgetExhaustedError,
    ForbiddenError,
    InvalidParameterError,
    UnauthorizedError,
)
from tests.conftest import submit_count

ANALYST = "key-analyst"


def test_cost_follows_sensitivity(svc, caplog):
    """count 敏感度 1 -> 0.1；sum(score, 域宽 1) -> 0.1；sum(units, 域宽 9) -> 0.9。"""
    from dp_engine.models import QueryRequest

    with caplog.at_level(logging.INFO, logger="dp_engine.budget"):
        r1 = submit_count(svc, "b-cost-count")
        r2 = svc.submit(QueryRequest(
            query_id="b-cost-sum-score", agg="sum", column="score",
            dimensions=["region"], api_key=ANALYST,
        ))
        r3 = svc.submit(QueryRequest(
            query_id="b-cost-sum-units", agg="sum", column="units",
            dimensions=["region"], api_key=ANALYST,
        ))

    assert r1.charged_budget == pytest.approx(0.1)
    assert r2.charged_budget == pytest.approx(0.1)
    assert r3.charged_budget == pytest.approx(0.9)
    # 累计 1.1
    assert r3.remaining_budget == pytest.approx(1.9)

    # 日志包含扣减前后数值与判定依据
    text = "\n".join(r.message for r in caplog.records)
    assert "判定依据=扣减" in text
    assert "扣减前已用=0.000000" in text
    assert "扣减后已用=0.100000" in text
    assert "敏感度=9.000000" in text
    assert "需要成本=0.900000" in text
    print("\n[预算日志样本]\n" + "\n".join(r.message for r in caplog.records))


def test_budget_exhaustion_is_rejected_and_distinguishable(svc, caplog):
    """总预算 3.0，count 每次 0.1：恰好 30 次成功，第 31 次拒绝且错误码唯一。"""
    for i in range(30):
        submit_count(svc, f"b-ex-{i}")

    with caplog.at_level(logging.INFO, logger="dp_engine.budget"):
        with pytest.raises(BudgetExhaustedError) as exc:
            submit_count(svc, "b-ex-over")

    err = exc.value
    assert err.code == "budget_exhausted"
    assert err.http_status == 402
    assert err.remaining == pytest.approx(0.0)
    assert err.required == pytest.approx(0.1)

    text = "\n".join(r.message for r in caplog.records)
    assert "判定依据=拒绝" in text
    assert "总预算=3.000000" in text
    assert "实际扣减=0.000000" in text
    print("\n[预算耗尽日志]\n" + "\n".join(r.message for r in caplog.records))


def test_error_types_are_distinguishable(svc, client):
    with pytest.raises(UnauthorizedError):
        submit_count(svc, "x", api_key=None)
    with pytest.raises(ForbiddenError):
        from dp_engine.models import QueryRequest

        svc.submit(QueryRequest(
            query_id="x", agg="sum", column="score",
            dimensions=["region"], api_key="key-counter",
        ))
    with pytest.raises(InvalidParameterError):
        from dp_engine.models import QueryRequest

        svc.submit(QueryRequest(
            query_id="x", agg="sum", column="unknown",
            dimensions=["region"], api_key=ANALYST,
        ))

    # HTTP 层四个错误码均可区分
    assert client.post("/query", json={"query_id": "x", "agg": "count"}
                       ).json()["error"]["code"] == "unauthorized"
    assert client.post("/query", json={"query_id": "x", "agg": "sum", "column": "score",
                                       "api_key": "key-counter"}
                       ).json()["error"]["code"] == "forbidden"
    assert client.post("/query", json={"query_id": "x", "agg": "median",
                                       "api_key": ANALYST}
                       ).json()["error"]["code"] == "invalid_parameter"


def test_rejected_query_does_not_charge_and_is_audited(svc):
    for i in range(30):
        submit_count(svc, f"b-aud-{i}")
    with pytest.raises(BudgetExhaustedError):
        submit_count(svc, "b-aud-over")
    entries = svc.get_audit("analyst")
    rejected = [e for e in entries if e["decision"] == "rejected_budget"]
    assert len(rejected) == 1
    e = rejected[0]
    assert e["budget_before"] == pytest.approx(3.0)
    assert e["budget_after"] == pytest.approx(3.0)
    assert e["charged_budget"] == 0.0
    assert "需要 0.100000 > 剩余" in e["detail"]
