"""预算调整：追加/回收、调整单号幂等、回收负余额整笔回滚、跨重启持久化。

覆盖业务：
1. 追加/回收个人额度与池额度，调整前后额度与已耗写日志；
2. 同一 adjustment_id 重复提交只生效一次且返回一致（replayed）；
3. 同单号不同参数 adjustment_conflict；回收致负余额时整笔回滚且单号可复用；
4. 调整记录跨重启保留，重放仍命中幂等。
"""
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings


def _adjust(client: TestClient, adjustment_id: str, target: str, delta: float,
            target_type: str = "principal"):
    return client.post(
        "/admin/adjust",
        json={
            "adjustment_id": adjustment_id,
            "target_type": target_type,
            "target": target,
            "delta": delta,
        },
    )


def test_topup_and_reclaim_principal(granted_client):
    client = granted_client
    # 追加
    r = _adjust(client, "adj-1", "alice", 5.0)
    assert r.status_code == 200
    body = r.json()
    assert body["budget_before"] == 10.0
    assert body["budget_after"] == 15.0
    assert body["replayed"] is False
    assert client.get("/budget/alice").json()["remaining_epsilon"] == 15.0
    # 回收
    r = _adjust(client, "adj-2", "alice", -4.0)
    assert r.status_code == 200
    assert r.json()["budget_after"] == 11.0


def test_same_adjustment_id_applies_only_once(granted_client, caplog):
    client = granted_client
    r1 = _adjust(client, "adj-dup", "alice", 5.0)
    r2 = _adjust(client, "adj-dup", "alice", 5.0)
    r3 = _adjust(client, "adj-dup", "alice", 5.0)
    assert r1.status_code == r2.status_code == r3.status_code == 200
    b1, b2, b3 = r1.json(), r2.json(), r3.json()
    assert b1["replayed"] is False
    assert b2["replayed"] is True and b3["replayed"] is True
    # 返回结果与第一次一致（除 replayed 标志）
    for b in (b1, b2, b3):
        b.pop("replayed")
    assert b1 == b2 == b3
    # 只生效一次
    assert client.get("/budget/alice").json()["remaining_epsilon"] == 15.0

    # 同一单号不同参数 → 冲突
    r = _adjust(client, "adj-dup", "alice", 7.0)
    assert r.status_code == 409
    assert r.json()["error"] == "adjustment_conflict"
    assert client.get("/budget/alice").json()["remaining_epsilon"] == 15.0

    texts = [rec.getMessage() for rec in caplog.records if rec.name == "pqe.service"]
    applied = [t for t in texts if "budget adjustment" in t and "basis=applied" in t]
    replayed = [t for t in texts if "basis=idempotent_replay" in t]
    assert len(applied) == 1 and len(replayed) == 2
    assert "before=10.000000 after=15.000000" in applied[0]


def test_reclaim_below_spent_is_rejected_atomically(granted_client, caplog):
    client = granted_client
    # alice 先消耗 2.0
    for i in range(2):
        assert client.post(
            "/query",
            json={
                "query_id": f"sp-{i}", "principal": "alice", "aggregation": "count",
                "dimension": "city", "epsilon": 1.0,
            },
        ).status_code == 200
    # 回收到 1.0 < 已消耗 2.0 → 整笔拒绝
    r = _adjust(client, "adj-neg", "alice", -9.0)
    assert r.status_code == 422
    assert r.json()["error"] == "adjustment_rejected"
    assert "2.000000" in r.json()["message"]
    # 账本保持原样：预算、已消耗都不变
    info = client.get("/budget/alice").json()
    assert info["spent_epsilon"] == 2.0
    assert info["remaining_epsilon"] == 8.0
    # 拒绝的调整单号未占位：修正参数后可复用同一单号
    r = _adjust(client, "adj-neg", "alice", -5.0)
    assert r.status_code == 200
    assert r.json()["budget_after"] == 5.0

    texts = [rec.getMessage() for rec in caplog.records if rec.name == "pqe.service"]
    assert any("basis=rejected_would_underspend" in t for t in texts)


def test_adjust_pool_budget(granted_client):
    client = granted_client
    client.post(
        "/admin/pool",
        json={"pool_id": "team-b", "members": ["alice"], "budget_epsilon": 2.0},
    )
    r = _adjust(client, "adj-pool-1", "team-b", 3.0, target_type="pool")
    assert r.status_code == 200
    assert r.json()["budget_after"] == 5.0
    assert client.get("/pool/team-b").json()["remaining_epsilon"] == 5.0
    # 不存在的池
    r = _adjust(client, "adj-pool-2", "ghost", 1.0, target_type="pool")
    assert r.status_code == 422
    assert r.json()["error"] == "adjustment_rejected"
    # 未授权主体
    r = _adjust(client, "adj-pool-3", "nobody", 1.0)
    assert r.status_code == 422


def test_adjustments_survive_restart(settings: Settings):
    app1 = create_app(settings)
    with TestClient(app1) as client:
        client.post("/admin/grant", json={"principal": "alice", "budget_epsilon": 10.0})
        assert _adjust(client, "adj-restart", "alice", 2.5).status_code == 200
    # “重启”：新建应用实例，账本从磁盘恢复
    app2 = create_app(settings)
    with TestClient(app2) as client:
        info = client.get("/budget/alice").json()
        assert info["remaining_epsilon"] == 12.5
        # 同一单号重放：不重复生效，返回首次结果
        r = _adjust(client, "adj-restart", "alice", 2.5)
        assert r.status_code == 200
        assert r.json()["replayed"] is True
        assert r.json()["budget_after"] == 12.5
        assert client.get("/budget/alice").json()["remaining_epsilon"] == 12.5
