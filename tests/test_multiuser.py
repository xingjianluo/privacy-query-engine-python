"""多用户查询编号隔离：同名 query_id 不串结果、不串扣费；旧账本兼容。"""
import json

from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings


def _count(client: TestClient, query_id: str, principal: str, epsilon: float = 1.0):
    return client.post(
        "/query",
        json={
            "query_id": query_id,
            "principal": principal,
            "aggregation": "count",
            "dimension": "city",
            "epsilon": epsilon,
        },
    )


def test_same_query_id_different_users_isolated(granted_client):
    client = granted_client
    # alice 与 bob 使用同一个编号 daily-count
    ra1 = _count(client, "daily-count", "alice")
    rb1 = _count(client, "daily-count", "bob")
    assert ra1.status_code == rb1.status_code == 200
    # 各自都是首次执行，而不是重放别人的结果
    assert ra1.json()["replayed"] is False
    assert rb1.json()["replayed"] is False
    # 扣费各算各的
    assert client.get("/budget/alice").json()["spent_epsilon"] == 1.0
    assert client.get("/budget/bob").json()["spent_epsilon"] == 1.0

    # 各自重放各自的结果，互不串扰
    ra2 = _count(client, "daily-count", "alice")
    rb2 = _count(client, "daily-count", "bob")
    assert ra2.json()["replayed"] is True
    assert rb2.json()["replayed"] is True
    a1, a2 = ra1.json(), ra2.json()
    a1.pop("replayed"); a2.pop("replayed")
    assert a1 == a2
    # 重放不重复扣费
    assert client.get("/budget/alice").json()["spent_epsilon"] == 1.0
    assert client.get("/budget/bob").json()["spent_epsilon"] == 1.0

    # alice 的编号参数冲突只影响 alice 自己
    r = _count(client, "daily-count", "alice", epsilon=1.5)
    assert r.status_code == 409
    # bob 用不同参数重提交自己的同名编号，同样按各自记录判定
    r = _count(client, "daily-count", "bob", epsilon=1.5)
    assert r.status_code == 409


def test_legacy_ledger_migrates_and_keeps_working(settings: Settings):
    """旧版扁平 queries 结构的账本：加载后旧记录可重放、新查询按新结构隔离。"""
    # 手工构造一份旧版账本（扁平 queries、无 pools/adjustments/history）
    legacy = {
        "principals": {
            "alice": {"authorized": True, "spent_epsilon": 1.0, "budget_epsilon": 10.0}
        },
        "queries": {
            "q-001": {
                "status": "completed",
                "principal": "alice",
                "cache_key": ["count", "city", None, 1.0, None, None],
                "epsilon_charged": 1.0,
                "remaining_budget": 9.0,
                "response": {
                    "query_id": "q-001",
                    "principal": "alice",
                    "aggregation": "count",
                    "dimension": "city",
                    "replayed": False,
                    "epsilon_charged": 1.0,
                    "remaining_budget": 9.0,
                    "results": [{"group": "Beijing", "value": 5.0, "noisy_count": 5}],
                    "suppressed": [],
                },
            }
        },
    }
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.ledger_path.write_text(json.dumps(legacy), encoding="utf-8")

    app = create_app(settings)
    with TestClient(app) as client:
        # 旧记录可重放，不重新扣费
        r = _count(client, "q-001", "alice")
        assert r.status_code == 200
        assert r.json()["replayed"] is True
        assert r.json()["results"][0]["noisy_count"] == 5
        assert client.get("/budget/alice").json()["spent_epsilon"] == 1.0
        # 新用户用同一编号不受影响
        client.post("/admin/grant", json={"principal": "bob", "budget_epsilon": 5.0})
        r = _count(client, "q-001", "bob")
        assert r.status_code == 200
        assert r.json()["replayed"] is False
        assert client.get("/budget/bob").json()["spent_epsilon"] == 1.0

    # 落盘后的账本已升级为隔离结构，且包含新分区
    disk = json.loads(settings.ledger_path.read_text(encoding="utf-8"))
    assert sorted(disk["queries"].keys()) == ["alice", "bob"]
    assert "q-001" in disk["queries"]["alice"]
    assert disk["pools"] == {} and disk["adjustments"] == {} 
    assert isinstance(disk["history"], list)
