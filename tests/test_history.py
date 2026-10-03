"""提交历史：筛选、分页、越权拒绝、不泄漏精确值。"""
import json

from fastapi.testclient import TestClient


def _submit(client: TestClient, query_id: str, principal: str = "alice", **over):
    payload = {
        "query_id": query_id,
        "principal": principal,
        "aggregation": "count",
        "dimension": "city",
        "epsilon": 1.0,
        **over,
    }
    return client.post("/query", json=payload)


def _seed_history(client: TestClient):
    # alice：2 条成功（count/city、sum/city）、1 条拒绝（预算超限参数合法但 epsilon 超上限的不记，
    # 这里用预算耗尽制造拒绝）；bob：1 条成功
    _submit(client, "h-1", "alice")  # count/city 成功
    _submit(client, "h-2", "alice", aggregation="sum", value_column="salary")  # sum 成功
    _submit(client, "h-3", "alice", dimension="department")  # count/department 成功
    _submit(client, "h-bob", "bob", epsilon=2.0)  # bob 成功（预算 2.5 -> 剩 0.5）
    _submit(client, "h-bob-denied", "bob", epsilon=1.0)  # bob 预算不足被拒


def test_history_filters_and_pagination(granted_client):
    client = granted_client
    _seed_history(client)

    # alice 全部历史：3 条成功
    r = client.get("/history/alice", params={"requester": "alice"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert {i["query_id"] for i in body["items"]} == {"h-1", "h-2", "h-3"}

    # 按聚合类型筛选
    r = client.get("/history/alice", params={"requester": "alice", "aggregation": "sum"})
    assert [i["query_id"] for i in r.json()["items"]] == ["h-2"]

    # 按维度筛选
    r = client.get("/history/alice", params={"requester": "alice", "dimension": "department"})
    assert [i["query_id"] for i in r.json()["items"]] == ["h-3"]

    # 按状态筛选：bob 1 成功 1 拒绝
    r = client.get("/history/bob", params={"requester": "bob", "status": "rejected"})
    items = r.json()["items"]
    assert [i["query_id"] for i in items] == ["h-bob-denied"]
    assert items[0]["error"] == "budget_exhausted"
    r = client.get("/history/bob", params={"requester": "bob", "status": "completed"})
    assert [i["query_id"] for i in r.json()["items"]] == ["h-bob"]

    # 分页：page_size=2，共 3 条 -> 2 页
    r1 = client.get("/history/alice", params={"requester": "alice", "page": 1, "page_size": 2})
    r2 = client.get("/history/alice", params={"requester": "alice", "page": 2, "page_size": 2})
    b1, b2 = r1.json(), r2.json()
    assert b1["total"] == 3 and len(b1["items"]) == 2
    assert len(b2["items"]) == 1
    ids = [i["query_id"] for i in b1["items"] + b2["items"]]
    assert ids == ["h-1", "h-2", "h-3"]  # 按提交顺序、无重复无遗漏


def test_history_time_range_filter(granted_client):
    client = granted_client
    _submit(client, "t-1", "alice")
    # 取该条目的提交时间戳，构造只包含它的闭区间
    items = client.get("/history/alice", params={"requester": "alice"}).json()["items"]
    ts = items[0]["submitted_at"]
    assert ts is not None
    r = client.get("/history/alice", params={"requester": "alice", "start": ts, "end": ts})
    assert [i["query_id"] for i in r.json()["items"]] == ["t-1"]
    # 起点晚于提交时间 -> 空
    r = client.get("/history/alice", params={"requester": "alice", "start": "2999-01-01T00:00:00+00:00"})
    assert r.json()["total"] == 0
    # 非法时间格式
    r = client.get("/history/alice", params={"requester": "alice", "start": "not-a-time"})
    assert r.status_code == 422
    assert r.json()["error"] == "invalid_query"


def test_history_rejects_cross_user_access(granted_client):
    client = granted_client
    _seed_history(client)
    # alice 查 bob 的历史：403
    r = client.get("/history/bob", params={"requester": "alice"})
    assert r.status_code == 403
    assert r.json()["error"] == "unauthorized"
    # 缺 requester：422 参数校验
    assert client.get("/history/bob").status_code == 422
    # alice 的历史里看不到 bob 的记录
    body = client.get("/history/alice", params={"requester": "alice"}).json()
    assert all(i["principal"] == "alice" for i in body["items"])


def test_history_contains_no_exact_values(granted_client):
    client = granted_client
    # Beijing 工资精确总和 95500、精确均值 19100、精确计数 5
    client.post("/query", json={
        "query_id": "hx-1", "principal": "alice", "aggregation": "sum",
        "dimension": "city", "value_column": "salary", "epsilon": 1.0,
        "groups": ["Beijing"],
    })
    body = client.get("/history/alice", params={"requester": "alice"}).json()
    text = json.dumps(body)
    for leaked in ("95500", "19100"):
        assert leaked not in text
    # 历史条目只含元数据，不含分组结果
    for item in body["items"]:
        assert "results" not in item and "suppressed" not in item
