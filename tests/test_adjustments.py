"""预算调整：追加/回收、调整单幂等、回收过度整笔回滚、跨重启保留。"""
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.storage import Ledger


def _adjust(client: TestClient, adjustment_id: str, target_type: str, target_id: str, delta: float):
    return client.post("/admin/adjustments", json={
        "adjustment_id": adjustment_id,
        "target_type": target_type,
        "target_id": target_id,
        "delta_epsilon": delta,
    })


def test_topup_and_reclaim_principal(granted_client, caplog):
    client = granted_client
    # alice 先花掉 1.0
    client.post("/query", json={
        "query_id": "adj-q1", "principal": "alice", "aggregation": "count",
        "dimension": "city", "epsilon": 1.0,
    })
    # 追加 +5：预算 10 -> 15
    r = _adjust(client, "adj-1", "principal", "alice", 5.0)
    assert r.status_code == 201
    body = r.json()
    assert body["replayed"] is False
    assert body["budget_epsilon"] == 15.0
    assert body["spent_epsilon"] == 1.0
    assert body["remaining_epsilon"] == 14.0
    # 回收 -3：预算 15 -> 12
    r = _adjust(client, "adj-2", "principal", "alice", -3.0)
    assert r.status_code == 201
    assert r.json()["budget_epsilon"] == 12.0
    assert client.get("/budget/alice").json()["remaining_epsilon"] == 11.0

    # 日志包含调整前后数值与判定理由
    texts = [rec.getMessage() for rec in caplog.records if rec.name == "pqe.service"]
    applied = [t for t in texts if "adjustment decision" in t and "basis=applied" in t]
    assert len(applied) == 2
    assert "budget_before=10.000000 budget_after=15.000000" in applied[0]


def test_same_adjustment_id_replays_once(granted_client):
    client = granted_client
    r1 = _adjust(client, "adj-dup", "principal", "alice", 2.0)
    r2 = _adjust(client, "adj-dup", "principal", "alice", 2.0)
    r3 = _adjust(client, "adj-dup", "principal", "alice", 2.0)
    assert r1.status_code == r2.status_code == r3.status_code == 201
    assert r1.json()["replayed"] is False
    assert r2.json()["replayed"] is True
    assert r3.json()["replayed"] is True
    # 返回结果与首次一致（除 replayed 标志）
    b1, b2 = r1.json(), r2.json()
    b1.pop("replayed"); b2.pop("replayed")
    assert b1 == b2
    # 只生效一次：预算 10 -> 12，而非 16
    assert client.get("/budget/alice").json()["remaining_epsilon"] == 12.0
    # 同一 adjustment_id 不同参数：409
    r = _adjust(client, "adj-dup", "principal", "alice", 3.0)
    assert r.status_code == 409
    assert r.json()["error"] == "adjustment_conflict"
    assert client.get("/budget/alice").json()["remaining_epsilon"] == 12.0


def test_reclaim_causing_overdraw_is_fully_rejected(granted_client, caplog):
    client = granted_client
    # alice 花掉 2.0（预算 10，已耗 2）
    for qid in ("ov-1", "ov-2"):
        client.post("/query", json={
            "query_id": qid, "principal": "alice", "aggregation": "count",
            "dimension": "city", "epsilon": 1.0,
        })
    # 回收 -9：新预算 1.0 < 已耗 2.0，整笔拒绝
    r = _adjust(client, "adj-over", "principal", "alice", -9.0)
    assert r.status_code == 409
    assert r.json()["error"] == "adjustment_rejected"
    # 账本保持原样：预算仍 10，已耗仍 2，无负余额
    info = client.get("/budget/alice").json()
    assert info["remaining_epsilon"] == 8.0
    assert info["spent_epsilon"] == 2.0
    # 回收到恰好等于已耗是允许的（剩余归零，不为负）
    r = _adjust(client, "adj-exact", "principal", "alice", -8.0)
    assert r.status_code == 201
    assert client.get("/budget/alice").json()["remaining_epsilon"] == 0.0

    texts = [rec.getMessage() for rec in caplog.records if rec.name == "pqe.service"]
    denied = [t for t in texts if "basis=reclaim_would_overdraw_denied" in t]
    assert len(denied) == 1
    assert "budget_before=10.000000 budget_after=1.000000 spent=2.000000" in denied[0]


def test_adjust_pool_budget(granted_client):
    client = granted_client
    client.post("/admin/pools", json={
        "pool_id": "adj-pool", "budget_epsilon": 2.0, "members": ["alice"],
    })
    client.post("/query", json={
        "query_id": "ap-1", "principal": "alice", "aggregation": "count",
        "dimension": "city", "epsilon": 1.5, "pool_id": "adj-pool",
    })
    # 池子追加 +3
    r = _adjust(client, "adj-p1", "pool", "adj-pool", 3.0)
    assert r.status_code == 201
    assert client.get("/pools/adj-pool").json()["remaining_epsilon"] == 3.5
    # 池子回收过度：新预算 0.5 < 已耗 1.5，拒绝
    r = _adjust(client, "adj-p2", "pool", "adj-pool", -4.5)
    assert r.status_code == 409
    assert r.json()["error"] == "adjustment_rejected"
    assert client.get("/pools/adj-pool").json()["remaining_epsilon"] == 3.5


def test_adjustment_target_must_exist(granted_client):
    client = granted_client
    r = _adjust(client, "adj-nx1", "principal", "ghost", 1.0)
    assert r.status_code == 404
    assert r.json()["error"] == "adjustment_target_not_found"
    r = _adjust(client, "adj-nx2", "pool", "ghost-pool", 1.0)
    assert r.status_code == 404
    assert r.json()["error"] == "adjustment_target_not_found"


def test_adjustments_survive_restart(settings: Settings):
    ledger = Ledger(settings.ledger_path, settings.lock_path)
    ledger.grant("alice", 10.0)
    app = create_app(settings)
    with TestClient(app) as client:
        assert _adjust(client, "adj-rst", "principal", "alice", 4.0).status_code == 201
        assert client.get("/budget/alice").json()["remaining_epsilon"] == 14.0

    # “重启”：全新应用实例，调整结果与幂等记录都在
    app2 = create_app(settings)
    with TestClient(app2) as client2:
        assert client2.get("/budget/alice").json()["remaining_epsilon"] == 14.0
        r = _adjust(client2, "adj-rst", "principal", "alice", 4.0)
        assert r.status_code == 201
        assert r.json()["replayed"] is True
        assert r.json()["budget_epsilon"] == 14.0
        # 重放不二次生效
        assert client2.get("/budget/alice").json()["remaining_epsilon"] == 14.0
