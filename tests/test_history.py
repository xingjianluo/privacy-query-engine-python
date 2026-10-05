"""查询历史：筛选、分页、越权拒绝、不泄漏精确值。

覆盖业务：
1. 仅本人可见：viewer 不一致返回 unauthorized；
2. 按聚合/维度/状态/时间范围筛选，行为正确且可组合；
3. 分页 total/items 正确，超出末页为空；
4. 成功、重放、被拒提交均入历史，条目只含加噪结果与抑制原因。
"""
from fastapi.testclient import TestClient


def _count(client: TestClient, query_id: str, principal: str = "alice", **over):
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
    # alice：2 个 count/city 成功、1 个 sum/department 成功、1 个被拒（epsilon 超限）
    assert _count(client, "h1").status_code == 200
    assert _count(client, "h2").status_code == 200
    assert _count(
        client, "h3", aggregation="sum", dimension="department", value_column="age"
    ).status_code == 200
    assert _count(client, "h4", epsilon=9.0).status_code == 422
    # bob：1 个成功（不应出现在 alice 的历史里）
    assert _count(client, "h1", principal="bob").status_code == 200


def test_history_only_own_records_and_no_exact_values(granted_client):
    client = granted_client
    _seed_history(client)
    r = client.get("/history/alice", params={"viewer": "alice"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 4
    ids = [i["query_id"] for i in body["items"]]
    assert sorted(ids) == ["h1", "h2", "h3", "h4"]
    # 历史条目不携带精确值：结果只有带噪字段，且小分组只有抑制原因
    for item in body["items"]:
        assert "exact" not in str(item).lower()
        if item["results"]:
            for g in item["results"]:
                assert set(g.keys()) <= {"group", "value", "noisy_count"}
    # 越权查看别人历史 → 拒绝
    r = client.get("/history/alice", params={"viewer": "bob"})
    assert r.status_code == 403
    assert r.json()["error"] == "unauthorized"
    r = client.get("/history/bob", params={"viewer": "alice"})
    assert r.status_code == 403
    # bob 看自己只有自己的 1 条
    r = client.get("/history/bob", params={"viewer": "bob"})
    assert r.json()["total"] == 1


def test_history_filters(granted_client):
    client = granted_client
    _seed_history(client)
    # 按聚合类型
    r = client.get("/history/alice", params={"viewer": "alice", "aggregation": "sum"})
    assert [i["query_id"] for i in r.json()["items"]] == ["h3"]
    # 按维度
    r = client.get("/history/alice", params={"viewer": "alice", "dimension": "city"})
    assert r.json()["total"] == 3
    # 按状态
    r = client.get("/history/alice", params={"viewer": "alice", "status": "rejected"})
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["query_id"] == "h4"
    assert items[0]["error"] == "invalid_query"
    r = client.get("/history/alice", params={"viewer": "alice", "status": "completed"})
    assert r.json()["total"] == 3
    # 组合筛选
    r = client.get(
        "/history/alice",
        params={"viewer": "alice", "aggregation": "count", "status": "completed"},
    )
    assert sorted(i["query_id"] for i in r.json()["items"]) == ["h1", "h2"]


def test_history_time_range_filter(granted_client):
    client = granted_client
    assert _count(client, "t1").status_code == 200
    mid = client.get("/history/alice", params={"viewer": "alice"}).json()["items"][0][
        "created_at"
    ]
    assert _count(client, "t2").status_code == 200
    # start 排除较早的 t1
    r = client.get("/history/alice", params={"viewer": "alice", "start": mid + 1e-6})
    assert [i["query_id"] for i in r.json()["items"]] == ["t2"]
    # end 排除较晚的 t2
    r = client.get("/history/alice", params={"viewer": "alice", "end": mid + 1e-6})
    assert [i["query_id"] for i in r.json()["items"]] == ["t1"]


def test_history_pagination(granted_client):
    client = granted_client
    for i in range(5):
        assert _count(client, f"p{i}").status_code == 200
    r = client.get("/history/alice", params={"viewer": "alice", "page": 1, "page_size": 2})
    body = r.json()
    assert body["total"] == 5
    assert [i["query_id"] for i in body["items"]] == ["p0", "p1"]
    r = client.get("/history/alice", params={"viewer": "alice", "page": 3, "page_size": 2})
    assert [i["query_id"] for i in r.json()["items"]] == ["p4"]
    # 超出末页返回空
    r = client.get("/history/alice", params={"viewer": "alice", "page": 4, "page_size": 2})
    assert r.json()["items"] == []


def test_history_records_rejected_budget_denials(granted_client):
    client = granted_client
    # bob 预算 2.5：两个成功后第三个被预算拒绝，拒绝也进历史
    for i in range(2):
        assert _count(client, f"rb{i}", principal="bob").status_code == 200
    assert _count(client, "rb2", principal="bob").status_code == 429
    r = client.get("/history/bob", params={"viewer": "bob", "status": "rejected"})
    items = r.json()["items"]
    assert len(items) == 1
    assert items[0]["query_id"] == "rb2"
    assert items[0]["error"] == "budget_exhausted"
