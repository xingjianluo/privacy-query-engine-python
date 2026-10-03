"""老账本（v1 schema）兼容：自动迁移后老记录可重放、预算不回滚。"""
import json

from app.config import Settings
from app.dataset import load_dataset
from app.models import QueryRequest
from app.service import submit_query
from app.storage import Ledger


def _write_v1_ledger(settings: Settings) -> dict:
    """手工构造一份 v1 格式的账本：裸 query_id 键、5 元组 cache_key。"""
    response = {
        "query_id": "old-1",
        "principal": "alice",
        "aggregation": "count",
        "dimension": "city",
        "replayed": False,
        "epsilon_charged": 1.0,
        "remaining_budget": 9.0,
        "results": [{"group": "Beijing", "value": 6.0, "noisy_count": 6}],
        "suppressed": [],
    }
    state = {
        "principals": {
            "alice": {"authorized": True, "spent_epsilon": 1.0, "budget_epsilon": 10.0}
        },
        "queries": {
            "old-1": {
                "status": "completed",
                "principal": "alice",
                "cache_key": ["count", "city", None, 1.0, None],
                "epsilon_charged": 1.0,
                "remaining_budget": 9.0,
                "response": response,
            }
        },
    }
    settings.ledger_path.parent.mkdir(parents=True, exist_ok=True)
    with settings.ledger_path.open("w", encoding="utf-8") as fh:
        json.dump(state, fh)
    return response


def test_v1_ledger_migrates_and_replays(settings: Settings):
    old_response = _write_v1_ledger(settings)
    ds = load_dataset(settings.dataset_path)

    ledger = Ledger(settings.ledger_path, settings.lock_path)
    # 预算状态原样恢复
    assert ledger.get_budget("alice")["spent_epsilon"] == 1.0
    # 老查询按原参数重放：返回首次结果、不二次扣费
    req = QueryRequest(
        query_id="old-1", principal="alice", aggregation="count",
        dimension="city", epsilon=1.0,
    )
    resp = submit_query(req, ledger, ds, settings)
    assert resp.replayed is True
    assert resp.remaining_budget == 9.0
    assert [g.model_dump() for g in resp.results] == old_response["results"]
    assert ledger.get_budget("alice")["spent_epsilon"] == 1.0

    # 迁移已落盘：版本号与新结构齐全
    with ledger.transaction() as state:
        assert state["version"] == 2
        assert "pools" in state and "adjustments" in state and "history" in state
        assert len(state["queries"]) == 1
        # 老记录补入历史，可在历史入口看到
    history = ledger.list_history("alice")
    assert [h["query_id"] for h in history] == ["old-1"]
    assert history[0]["status"] == "completed"

    # 新功能在老账本上照常工作：新查询、池子、调整
    ledger.create_pool("after-migration", 2.0, ["alice"])
    resp2 = submit_query(
        QueryRequest(
            query_id="new-1", principal="alice", aggregation="count",
            dimension="city", epsilon=1.0, pool_id="after-migration",
        ),
        ledger, ds, settings,
    )
    assert resp2.replayed is False
    assert ledger.get_pool("after-migration")["spent_epsilon"] == 1.0
