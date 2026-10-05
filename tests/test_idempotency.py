"""重复提交幂等性与跨重启预算/结果持久化。

覆盖业务：
1. 相同 query_id 重试返回逐字段一致的缓存载荷且只扣一次预算；
2. 相同编号不同参数返回 query_conflict，防止顶替查询；
3. 预算耗尽后已完成查询仍可重放；
4. 新建存储/应用实例模拟重启后预算与查询结果均保留。
"""
from app.api import create_app
from app.config import Settings
from app.dataset import load_dataset
from app.service import submit_query
from app.storage import Ledger
from app.models import QueryRequest
from fastapi.testclient import TestClient

PAYLOAD = {
    "query_id": "dup-1",
    "principal": "alice",
    "aggregation": "count",
    "dimension": "city",
    "epsilon": 1.0,
}


def test_duplicate_submit_returns_identical_dp_payload(granted_client):
    client = granted_client
    r1 = client.post("/query", json=PAYLOAD)
    r2 = client.post("/query", json=PAYLOAD)
    r3 = client.post("/query", json=PAYLOAD)
    assert r1.status_code == 200
    b1, b2, b3 = r1.json(), r2.json(), r3.json()
    assert b1["replayed"] is False
    assert b2["replayed"] is True
    # 仅重放元数据标志不同，DP 载荷完全一致
    for b in (b1, b2, b3):
        b.pop("replayed")
    assert b1 == b2 == b3
    # 只扣一次预算
    info = client.get("/budget/alice").json()
    assert info["spent_epsilon"] == 1.0
    assert r2.json()["remaining_budget"] == 9.0


def test_same_id_different_params_is_conflict(granted_client):
    client = granted_client
    assert client.post("/query", json=PAYLOAD).status_code == 200
    changed = {**PAYLOAD, "epsilon": 1.5}
    r = client.post("/query", json=changed)
    assert r.status_code == 409
    assert r.json()["error"] == "query_conflict"
    # 冲突不扣预算
    assert client.get("/budget/alice").json()["spent_epsilon"] == 1.0


def test_replay_keeps_result_even_after_budget_exhausted(granted_client):
    client = granted_client
    # bob 仅够一次 1.0；先成功，再耗尽，原查询仍可重放
    assert client.post("/query", json={**PAYLOAD, "query_id": "bob-1", "principal": "bob"}).status_code == 200
    assert client.post("/query", json={**PAYLOAD, "query_id": "bob-2", "principal": "bob", "epsilon": 1.5}).status_code == 200
    # 预算 2.5 耗尽
    assert client.post("/query", json={**PAYLOAD, "query_id": "bob-3", "principal": "bob"}).status_code == 429
    replay = client.post("/query", json={**PAYLOAD, "query_id": "bob-1", "principal": "bob"})
    assert replay.status_code == 200
    assert replay.json()["replayed"] is True
    assert client.get("/budget/bob").json()["spent_epsilon"] == 2.5


def test_budget_and_result_survive_restart(settings: Settings):
    ledger = Ledger(settings.ledger_path, settings.lock_path)
    ledger.grant("alice", 5.0)
    ds = load_dataset(settings.dataset_path)
    req = QueryRequest(
        query_id="rst-1", principal="alice", aggregation="avg",
        dimension="city", value_column="salary", epsilon=1.5,
    )
    resp1 = submit_query(req, ledger, ds, settings)

    # “重启”：全新 Ledger/服务实例，从磁盘恢复
    ledger2 = Ledger(settings.ledger_path, settings.lock_path)
    resp2 = submit_query(req, ledger2, ds, settings)
    assert resp2.replayed is True
    assert resp2.results == resp1.results
    assert resp2.epsilon_charged == 1.5
    assert resp2.remaining_budget == resp1.remaining_budget == 3.5
    assert ledger2.get_budget("alice")["spent_epsilon"] == 1.5

    # HTTP 层重启同样可重放
    app = create_app(settings)
    with TestClient(app) as c:
        r = c.post("/query", json={
            "query_id": "rst-1", "principal": "alice", "aggregation": "avg",
            "dimension": "city", "value_column": "salary", "epsilon": 1.5,
        })
        assert r.status_code == 200
        assert r.json()["replayed"] is True
        assert c.get("/budget/alice").json()["spent_epsilon"] == 1.5
