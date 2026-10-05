"""共享预算池：成员控制、池额度不足的独立错误码、并发不超支。

覆盖业务：
1. 池成员访问控制：非成员 pool_access_denied、未知池 unknown_pool；
2. 池额度不足返回与个人不足可区分的 pool_budget_exhausted；
3. 已存在池重复创建只替换成员名单，额度须经 /admin/adjust 变更；
4. 池查询重放不重复扣费，多成员并发共用一池绝不超支、账实相符；
5. 不带 pool_id 的老用法仍走个人额度，行为不变。
"""
import concurrent.futures
import threading

from fastapi.testclient import TestClient

from app.config import Settings
from app.dataset import load_dataset
from app.models import QueryRequest
from app.service import submit_query
from app.storage import Ledger


def _pool_query(client: TestClient, query_id: str, principal: str, pool_id: str = "team-a"):
    payload = {
        "query_id": query_id,
        "principal": principal,
        "aggregation": "count",
        "dimension": "city",
        "epsilon": 1.0,
        "pool_id": pool_id,
    }
    return client.post("/query", json=payload)


def test_pool_charge_and_membership(granted_client):
    client = granted_client
    r = client.post(
        "/admin/pool",
        json={"pool_id": "team-a", "members": ["alice", "bob"], "budget_epsilon": 3.0},
    )
    assert r.status_code == 201
    body = r.json()
    assert body["remaining_epsilon"] == 3.0

    # 成员走池子扣费：池余额减少，个人额度不动
    r = _pool_query(client, "pq-1", "alice")
    assert r.status_code == 200
    assert r.json()["budget_source"] == "pool"
    assert r.json()["pool_id"] == "team-a"
    assert r.json()["remaining_budget"] == 2.0
    assert client.get("/pool/team-a").json()["spent_epsilon"] == 1.0
    assert client.get("/budget/alice").json()["spent_epsilon"] == 0.0

    # 非成员被拒绝，且不扣池子额度
    r = _pool_query(client, "pq-2", "carol")
    assert r.status_code == 403
    assert r.json()["error"] == "pool_access_denied"
    assert client.get("/pool/team-a").json()["spent_epsilon"] == 1.0

    # 不存在的池
    r = _pool_query(client, "pq-3", "alice", pool_id="ghost")
    assert r.status_code == 404
    assert r.json()["error"] == "unknown_pool"


def test_pool_exhaustion_is_distinct_from_personal(granted_client, caplog):
    client = granted_client
    client.post(
        "/admin/pool",
        json={"pool_id": "team-a", "members": ["alice"], "budget_epsilon": 1.5},
    )
    assert _pool_query(client, "pe-1", "alice").status_code == 200
    r = _pool_query(client, "pe-2", "alice")
    assert r.status_code == 429
    assert r.json()["error"] == "pool_budget_exhausted"
    # 与个人额度不足的错误码可区分
    bob = client.post(
        "/query",
        json={
            "query_id": "pe-3", "principal": "bob", "aggregation": "count",
            "dimension": "city", "epsilon": 2.0,
        },
    )
    # bob 预算 2.5，先花掉再触发个人不足
    assert bob.status_code == 200
    r2 = client.post(
        "/query",
        json={
            "query_id": "pe-4", "principal": "bob", "aggregation": "count",
            "dimension": "city", "epsilon": 1.0,
        },
    )
    assert r2.status_code == 429
    assert r2.json()["error"] == "budget_exhausted"

    # 日志区分两种拒绝依据
    texts = [rec.getMessage() for rec in caplog.records if rec.name == "pqe.service"]
    assert any("basis=insufficient_pool_budget_denied source=pool:team-a" in t for t in texts)
    assert any("basis=insufficient_budget_denied source=personal" in t for t in texts)


def test_pool_replay_does_not_recharge(granted_client):
    client = granted_client
    client.post(
        "/admin/pool",
        json={"pool_id": "team-a", "members": ["alice"], "budget_epsilon": 5.0},
    )
    r1 = _pool_query(client, "pr-1", "alice")
    r2 = _pool_query(client, "pr-1", "alice")
    assert r1.status_code == r2.status_code == 200
    assert r2.json()["replayed"] is True
    b1, b2 = r1.json(), r2.json()
    b1.pop("replayed"); b2.pop("replayed")
    assert b1 == b2
    assert client.get("/pool/team-a").json()["spent_epsilon"] == 1.0

    # 同一 query_id 换扣费来源 → 冲突
    r = client.post(
        "/query",
        json={
            "query_id": "pr-1", "principal": "alice", "aggregation": "count",
            "dimension": "city", "epsilon": 1.0,
        },
    )
    assert r.status_code == 409


def test_concurrent_pool_queries_never_overspend(settings: Settings):
    """多成员并发共用一个池：成功扣减总额绝不超过池额度。"""
    ledger = Ledger(settings.ledger_path, settings.lock_path)
    ds = load_dataset(settings.dataset_path)
    from app.models import PoolUpsertRequest
    from app.service import upsert_pool

    upsert_pool(
        PoolUpsertRequest(pool_id="team-x", members=["u1", "u2", "u3"], budget_epsilon=3.0),
        ledger,
    )

    barrier = threading.Barrier(12)

    def call(i: int):
        barrier.wait()
        try:
            submit_query(
                QueryRequest(
                    query_id=f"pool-q-{i}",
                    principal=f"u{(i % 3) + 1}",
                    aggregation="count",
                    dimension="city",
                    epsilon=1.0,
                    pool_id="team-x",
                ),
                ledger, ds, settings,
            )
            return "ok"
        except Exception as exc:
            return type(exc).__name__

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(call, range(12)))

    assert outcomes.count("ok") == 3
    assert outcomes.count("PoolBudgetExhaustedError") == 9
    assert ledger.get_pool("team-x")["spent_epsilon"] == 3.0
    # 每次成功扣减都有一条落盘的查询记录，无丢失、无重复
    with ledger.transaction() as state:
        total_records = sum(len(v) for v in state["queries"].values())
        assert total_records == 3
        completed = [e for e in state["history"] if e["status"] == "completed"]
        assert len(completed) == 3


def test_personal_budget_still_works_without_pool(granted_client):
    """老用法（不带 pool_id）行为不变。"""
    client = granted_client
    r = client.post(
        "/query",
        json={
            "query_id": "legacy-1", "principal": "alice", "aggregation": "count",
            "dimension": "city", "epsilon": 1.0,
        },
    )
    assert r.status_code == 200
    assert r.json()["budget_source"] == "personal"
    assert r.json()["pool_id"] is None
    assert client.get("/budget/alice").json()["spent_epsilon"] == 1.0
