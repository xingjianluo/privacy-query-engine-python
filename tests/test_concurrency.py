"""并发提交：不得超额扣减、不得重复计数、不得丢失记录。"""
import concurrent.futures
import threading

from app.config import Settings
from app.dataset import load_dataset
from app.models import QueryRequest
from app.service import submit_query
from app.storage import Ledger, query_key


def _fire_concurrent(fn, n: int):
    barrier = threading.Barrier(n)

    def wrapped():
        barrier.wait()
        return fn()

    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(lambda _: wrapped(), range(n)))


def test_concurrent_same_query_id_charges_once(settings: Settings):
    ledger = Ledger(settings.ledger_path, settings.lock_path)
    ledger.grant("alice", 10.0)
    ds = load_dataset(settings.dataset_path)

    def call():
        return submit_query(
            QueryRequest(
                query_id="same", principal="alice", aggregation="count",
                dimension="city", epsilon=1.0,
            ),
            ledger, ds, settings,
        )

    responses = _fire_concurrent(call, 16)
    payloads = [{**r.model_dump(), "replayed": None} for r in responses]
    assert all(p == payloads[0] for p in payloads)
    flags = sorted(r.replayed for r in responses)
    assert flags == [False] + [True] * 15
    assert ledger.get_budget("alice")["spent_epsilon"] == 1.0
    # 只落盘一条查询记录（按主体命名空间隔离的键）
    with ledger.transaction() as state:
        assert list(state["queries"].keys()) == [query_key("alice", "same")]


def test_concurrent_distinct_queries_never_overspend(settings: Settings):
    ledger = Ledger(settings.ledger_path, settings.lock_path)
    ledger.grant("bob", 3.0)  # 仅允许 3 个 epsilon=1.0 的查询
    ds = load_dataset(settings.dataset_path)

    def call(i: int):
        try:
            submit_query(
                QueryRequest(
                    query_id=f"cq-{i}", principal="bob", aggregation="count",
                    dimension="city", epsilon=1.0,
                ),
                ledger, ds, settings,
            )
            return "ok"
        except Exception as exc:  # noqa: BLE001 - 测试需要分类
            return type(exc).__name__

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        outcomes = list(pool.map(call, range(12)))

    assert outcomes.count("ok") == 3
    assert outcomes.count("BudgetExhaustedError") == 9
    assert ledger.get_budget("bob")["spent_epsilon"] == 3.0
    with ledger.transaction() as state:
        assert len(state["queries"]) == 3


def test_persistence_failure_leaves_no_half_state(settings: Settings, monkeypatch):
    ledger = Ledger(settings.ledger_path, settings.lock_path)
    ledger.grant("alice", 10.0)
    ds = load_dataset(settings.dataset_path)

    def flaky_persist():
        # grant 在打补丁之前已完成提交；此后第一次持久化即查询提交，直接失败
        raise OSError("simulated disk failure")

    monkeypatch.setattr(ledger, "_persist", flaky_persist)
    from app.errors import PersistenceError

    try:
        submit_query(
            QueryRequest(
                query_id="pf-1", principal="alice", aggregation="count",
                dimension="city", epsilon=1.0,
            ),
            ledger, ds, settings,
        )
        assert False, "expected PersistenceError"
    except PersistenceError as exc:
        # 对外错误不含内部异常细节
        assert "simulated disk failure" not in str(exc)

    monkeypatch.undo()
    # 重新从磁盘加载：无半完成扣减、无查询记录
    fresh = Ledger(settings.ledger_path, settings.lock_path)
    assert fresh.get_budget("alice")["spent_epsilon"] == 0.0
    with fresh.transaction() as state:
        assert "pf-1" not in state["queries"]
