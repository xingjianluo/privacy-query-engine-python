"""HTTP 层集成：路由、状态码、内部错误脱敏。"""
from fastapi.testclient import TestClient


def test_full_http_flow(granted_client: TestClient):
    client = granted_client
    # 计数查询
    r = client.post("/query", json={
        "query_id": "h1", "principal": "alice", "aggregation": "count",
        "dimension": "city", "epsilon": 1.0,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["query_id"] == "h1"
    assert body["replayed"] is False
    assert {g["group"] for g in body["results"]} == {"Beijing", "Guangzhou"}
    reasons = {s["group"]: s["reason"] for s in body["suppressed"]}
    assert reasons["Shanghai"] == "small_group"
    assert reasons["Shenzhen"] == "small_group"

    # 重放
    assert client.post("/query", json={
        "query_id": "h1", "principal": "alice", "aggregation": "count",
        "dimension": "city", "epsilon": 1.0,
    }).json()["replayed"] is True

    # 预算查询
    assert client.get("/budget/alice").json()["spent_epsilon"] == 1.0


def test_bad_json_body_returns_sanitized_invalid_query(granted_client: TestClient):
    r = granted_client.post("/query", json={"query_id": "z"})
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "invalid_query"
    # 不回显 pydantic 字段级细节
    assert "detail" not in body


def test_internal_error_is_sanitized(settings, monkeypatch):
    # 让 service 抛出非领域异常，HTTP 层必须返回通用 internal_error
    from app.api import create_app
    import app.api as api_mod

    def boom(*_a, **_k):
        raise RuntimeError("secret stack detail /etc/passwd")

    monkeypatch.setattr(api_mod, "submit_query", boom)
    # ServerErrorMiddleware 处理后仍重抛；测试 500 响应体时关闭客户端重抛
    client = TestClient(create_app(settings), raise_server_exceptions=False)
    client.post("/admin/grant", json={"principal": "alice", "budget_epsilon": 10.0})
    r = client.post("/query", json={
        "query_id": "boom", "principal": "alice", "aggregation": "count",
        "dimension": "city", "epsilon": 1.0,
    })
    assert r.status_code == 500
    body = r.json()
    assert body["error"] == "internal_error"
    assert "secret" not in str(body) and "/etc/passwd" not in str(body)
