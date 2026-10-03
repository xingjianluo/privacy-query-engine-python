"""多用户查询编号隔离：同名 query_id 不串结果、各算各的账。"""

from app.config import Settings
from app.dataset import load_dataset
from app.models import QueryRequest
from app.service import submit_query
from app.storage import Ledger

PAYLOAD = {
    "query_id": "daily-count",
    "aggregation": "count",
    "dimension": "city",
    "epsilon": 1.0,
}


def test_same_query_id_different_users_are_independent(granted_client):
    client = granted_client
    # alice 与 bob 使用完全相同的查询编号与参数
    r_alice = client.post("/query", json={**PAYLOAD, "principal": "alice"})
    r_bob = client.post("/query", json={**PAYLOAD, "principal": "bob"})
    assert r_alice.status_code == r_bob.status_code == 200
    # bob 的首次提交不是对 alice 结果的重放
    assert r_alice.json()["replayed"] is False
    assert r_bob.json()["replayed"] is False
    # 扣费各算各的
    assert client.get("/budget/alice").json()["spent_epsilon"] == 1.0
    assert client.get("/budget/bob").json()["spent_epsilon"] == 1.0
    # 各自重放各自的，互不影响
    assert client.post("/query", json={**PAYLOAD, "principal": "alice"}).json()["replayed"] is True
    assert client.get("/budget/bob").json()["spent_epsilon"] == 1.0


def test_param_conflict_is_scoped_per_user(granted_client):
    client = granted_client
    assert client.post("/query", json={**PAYLOAD, "principal": "alice"}).status_code == 200
    # bob 用同一编号但不同参数：是他自己的首次提交，不与 alice 冲突
    r = client.post("/query", json={**PAYLOAD, "principal": "bob", "epsilon": 1.5})
    assert r.status_code == 200
    assert r.json()["epsilon_charged"] == 1.5
    # alice 自己改参数才冲突
    r = client.post("/query", json={**PAYLOAD, "principal": "alice", "epsilon": 1.5})
    assert r.status_code == 409
    assert r.json()["error"] == "query_conflict"


def test_isolation_persists_across_restart(settings: Settings):
    ledger = Ledger(settings.ledger_path, settings.lock_path)
    ledger.grant("alice", 10.0)
    ledger.grant("bob", 10.0)
    ds = load_dataset(settings.dataset_path)
    req_a = QueryRequest(query_id="q-001", principal="alice", aggregation="count",
                         dimension="city", epsilon=1.0)
    req_b = QueryRequest(query_id="q-001", principal="bob", aggregation="sum",
                         dimension="city", value_column="salary", epsilon=1.0)
    submit_query(req_a, ledger, ds, settings)
    submit_query(req_b, ledger, ds, settings)

    # 重启后两条记录各自独立存在、各自可重放
    ledger2 = Ledger(settings.ledger_path, settings.lock_path)
    ra = submit_query(req_a, ledger2, ds, settings)
    rb = submit_query(req_b, ledger2, ds, settings)
    assert ra.replayed is True and ra.aggregation.value == "count"
    assert rb.replayed is True and rb.aggregation.value == "sum"
    assert ledger2.get_budget("alice")["spent_epsilon"] == 1.0
    assert ledger2.get_budget("bob")["spent_epsilon"] == 1.0
