"""共享预算池：成员配置、池内扣减、拒绝原因可区分、并发不超支、原子性。"""
import concurrent.futures
import threading

from fastapi.testclient import TestClient

from app.config import Settings
from app.dataset import load_dataset
from app.models import QueryRequest
from app.service import submit_query
from app.storage import Ledger


def _query(client: TestClient, query_id: str, principal: str, **over):
    payload = {
        "query_id": query_id,
        "principal": principal,
        "aggregation": "count",
        "dimension": "city",
        "epsilon": 1.0,
        **over,
    }
    return client.post("/query", json=payload)


def test_pool_create_and_query_charges_pool(granted_client):
    client = granted_client
    r = client.post("/admin/pools", json={
        "pool_id": "team-a", "budget_epsilon": 3.0, "members": ["alice", "bob"],
    })
    assert r.status_code == 201
    body = r.json()
    assert body["remaining_epsilon"] == 3.0
    assert body["members"] == ["alice", "bob"]

    # alice 走池子扣费：个人额度不动，池子额度减少
    r = _query(client, "pq-1", "alice", pool_id="team-a")
    assert r.status_code == 200
    assert r.json()["pool_id"] == "team-a"
    assert r.json()["remaining_budget"] == 2.0
    assert client.get("/pools/team-a").json()["spent_epsilon"] == 1.0
    assert client.get("/budget/alice").json()["spent_epsilon"] == 0.0

    # 重放不二次扣池子
    r = _query(client, "pq-1", "alice", pool_id="team-a")
    assert r.json()["replayed"] is True
    assert client.get("/pools/team-a").json()["spent_epsilon"] == 1.0


def test_pool_exhaustion_is_distinct_from_personal(granted_client, caplog):
    client = granted_client
    client.post("/admin/pools", json={
        "pool_id": "team-b", "budget_epsilon": 1.0, "members": ["alice"],
    })
    assert _query(client, "pb-1", "alice", pool_id="team-b").status_code == 200
    # 池子耗尽：429 + pool_budget_exhausted
    r = _query(client, "pb-2", "alice", pool_id="team-b")
    assert r.status_code == 429
    assert r.json()["error"] == "pool_budget_exhausted"
    # 个人额度不足仍是另一种错误码（bob 预算 2.5，单笔 2.0 两次后不足）
    assert _query(client, "pb-3", "bob", epsilon=2.0).status_code == 200
    r = _query(client, "pb-4", "bob", epsilon=2.0)
    assert r.status_code == 429
    assert r.json()["error"] == "budget_exhausted"
    # 池子拒绝不扣池子也不扣个人
    assert client.get("/pools/team-b").json()["spent_epsilon"] == 1.0
    assert client.get("/budget/alice").json()["spent_epsilon"] == 0.0

    # 日志能区分来源与判定理由
    texts = [rec.getMessage() for rec in caplog.records if rec.name == "pqe.service"]
    pool_denied = [t for t in texts if "source=pool:team-b" in t and "basis=insufficient_budget_denied" in t]
    personal_denied = [t for t in texts if "source=personal" in t and "basis=insufficient_budget_denied" in t]
    assert len(pool_denied) == 1 and "before=0.000000 required=1.000000" in pool_denied[0]
    assert len(personal_denied) == 1


def test_pool_membership_is_enforced(granted_client):
    client = granted_client
    client.post("/admin/pools", json={
        "pool_id": "team-c", "budget_epsilon": 5.0, "members": ["alice"],
    })
    # bob 不是成员：403 unauthorized
    r = _query(client, "pm-1", "bob", pool_id="team-c")
    assert r.status_code == 403
    assert r.json()["error"] == "unauthorized"
    # 不存在的池子：404 pool_not_found
    r = _query(client, "pm-2", "alice", pool_id="no-such-pool")
    assert r.status_code == 404
    assert r.json()["error"] == "pool_not_found"
    # 成员名单可整体替换：替换后 alice 出局、bob 入局
    r = client.put("/admin/pools/team-c/members", json={"members": ["bob"]})
    assert r.status_code == 200
    assert r.json()["members"] == ["bob"]
    assert _query(client, "pm-3", "alice", pool_id="team-c").status_code == 403
    assert _query(client, "pm-4", "bob", pool_id="team-c").status_code == 200
    # 重复创建同名池子：409
    r = client.post("/admin/pools", json={
        "pool_id": "team-c", "budget_epsilon": 9.0, "members": [],
    })
    assert r.status_code == 409
    assert r.json()["error"] == "pool_conflict"


def test_concurrent_pool_queries_never_overspend(settings: Settings):
    """多人并发共用同一池子：成功扣减总额绝不超过池子额度。"""
    ledger = Ledger(settings.ledger_path, settings.lock_path)
    ledger.create_pool("shared", 3.0, ["u1", "u2", "u3", "u4"])
    ds = load_dataset(settings.dataset_path)
    barrier = threading.Barrier(12)

    def call(i: int):
        principal = f"u{(i % 4) + 1}"
        barrier.wait()
        try:
            submit_query(
                QueryRequest(
                    query_id=f"pool-cq-{i}", principal=principal, aggregation="count",
                    dimension="city", epsilon=1.0, pool_id="shared",
                ),
                ledger, ds, settings,
            )
            return "ok"
        except Exception as exc:  # noqa: BLE001 - 测试需要分类
            return type(exc).__name__

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(call, range(12)))

    assert outcomes.count("ok") == 3
    assert outcomes.count("PoolBudgetExhaustedError") == 9
    pool = ledger.get_pool("shared")
    assert pool["spent_epsilon"] == 3.0
    # 记账与结果一一对应：每条成功记录都有响应，没有孤儿扣减
    with ledger.transaction() as state:
        completed = [r for r in state["queries"].values() if r["status"] == "completed"]
        assert len(completed) == 3
        assert all(r["response"] for r in completed)
        charged = sum(r["epsilon_charged"] for r in completed)
    assert charged == pool["spent_epsilon"] == 3.0
    # 历史：3 条成功 + 9 条拒绝，全部落账
    history = []
    for u in ("u1", "u2", "u3", "u4"):
        history.extend(ledger.list_history(u))
    assert len(history) == 12
    assert sum(1 for h in history if h["status"] == "rejected") == 9


def test_pool_state_survives_restart(settings: Settings):
    ledger = Ledger(settings.ledger_path, settings.lock_path)
    ledger.create_pool("persist", 4.0, ["alice"])
    ledger.grant("alice", 10.0)
    ds = load_dataset(settings.dataset_path)
    req = QueryRequest(
        query_id="pp-1", principal="alice", aggregation="count",
        dimension="city", epsilon=1.5, pool_id="persist",
    )
    resp1 = submit_query(req, ledger, ds, settings)

    ledger2 = Ledger(settings.ledger_path, settings.lock_path)
    assert ledger2.get_pool("persist")["spent_epsilon"] == 1.5
    resp2 = submit_query(req, ledger2, ds, settings)
    assert resp2.replayed is True
    assert resp2.results == resp1.results
    assert ledger2.get_pool("persist")["spent_epsilon"] == 1.5
