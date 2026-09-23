"""预算耗尽、授权缺失、参数非法：错误原因必须可区分。"""
from fastapi.testclient import TestClient


def _query(client: TestClient, query_id: str, principal: str = "alice", **over):
    payload = {
        "query_id": query_id,
        "principal": principal,
        "aggregation": "count",
        "dimension": "city",
        "epsilon": 1.0,
        **over,
    }
    return client.post("/query", json=payload)


def test_budget_exhaustion_is_reported_with_distinct_code(granted_client, caplog):
    client = granted_client
    # bob 预算 2.5：两次 1.0 成功，第三次 1.0 被拒
    r1 = _query(client, "b1", "bob")
    r2 = _query(client, "b2", "bob")
    r3 = _query(client, "b3", "bob")
    assert r1.status_code == r2.status_code == 200
    assert r3.status_code == 429
    body = r3.json()
    assert body["error"] == "budget_exhausted"
    assert "0.5" in body["message"]  # 剩余 0.5，需要 1.0
    # 拒绝后预算状态不变
    info = client.get("/budget/bob").json()
    assert info["spent_epsilon"] == 2.0
    assert info["remaining_epsilon"] == 0.5

    # 日志必须包含扣减前/后数值与判定依据
    texts = [r.getMessage() for r in caplog.records if r.name == "pqe.service"]
    charged = [t for t in texts if "basis=charged" in t]
    denied = [t for t in texts if "basis=insufficient_budget_denied" in t]
    assert len(charged) == 2
    assert len(denied) == 1
    assert "before=0.500000 required=1.000000 after=0.500000" in denied[0]
    assert "before=2.500000 required=1.000000 after=1.500000" in charged[0]


def test_rejected_query_does_not_consume_budget(granted_client):
    client = granted_client
    # epsilon 超限：参数非法，不得扣预算
    r = _query(client, "x1", epsilon=9.0)
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_query"
    # 未授权
    r = _query(client, "x2", principal="carol")
    assert r.status_code == 403
    assert r.json()["error"] == "unauthorized"
    info = client.get("/budget/alice").json()
    assert info["spent_epsilon"] == 0.0


def test_validation_error_variants_are_distinguishable(granted_client):
    client = granted_client
    assert _query(client, "v1", dimension="unknown_dim").json()["error"] == "invalid_query"
    r = _query(client, "v2", aggregation="sum", value_column="weight")
    assert r.status_code == 422 and r.json()["error"] == "invalid_query"
    r = _query(client, "v3", aggregation="sum")  # sum 缺 value_column
    assert r.status_code == 422 and r.json()["error"] == "invalid_query"
    # 模式校验：epsilon<=0
    r = client.post("/query", json={
        "query_id": "v4", "principal": "alice", "aggregation": "count",
        "dimension": "city", "epsilon": 0,
    })
    assert r.status_code == 422 and r.json()["error"] == "invalid_query"


def test_unauthorized_principal_budget_endpoint(client):
    info = client.get("/budget/nobody").json()
    assert info["authorized"] is False
    assert info["remaining_epsilon"] == 0.0


def test_avg_charges_full_requested_epsilon(granted_client):
    client = granted_client
    r = client.post("/query", json={
        "query_id": "a1", "principal": "alice", "aggregation": "avg",
        "dimension": "city", "value_column": "salary", "epsilon": 2.0,
    })
    assert r.status_code == 200
    assert r.json()["epsilon_charged"] == 2.0
    assert r.json()["remaining_budget"] == 8.0
