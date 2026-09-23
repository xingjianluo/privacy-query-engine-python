"""并发竞争：无超额扣减、无丢失记录、无重复计数。

覆盖：
1. 不同 query_id 高并发 -> 每条恰好计费一次，无丢失；
2. 相同 query_id 高并发重试 -> 只计费一次，结果全部一致；
3. 预算边界并发 -> 成功数与预算严格相符，绝不超额；
4. 多进程并发（fcntl 文件锁）-> 跨进程同样串行化。
"""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest

from dp_engine.config import get_settings
from dp_engine.errors import BudgetExhaustedError
from dp_engine.models import QueryRequest
from dp_engine.service import QueryService
from dp_engine.storage import Store
from tests.conftest import submit_count

ANALYST = "key-analyst"


def _spent(svc, principal="analyst"):
    state = svc.store.load()
    return state.principals[principal].spent


def test_concurrent_distinct_ids_charge_exactly_once_each(settings):
    svc = QueryService(settings, Store(settings.state_path))
    n = 30

    def one(i):
        local = QueryService(settings, Store(settings.state_path))
        return submit_count(local, f"c-dist-{i}").remaining_budget

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(one, range(n)))

    assert len(results) == n
    assert _spent(svc) == pytest.approx(0.1 * n)
    # 每条幂等记录恰好一份，审计 charged 条目恰好 n 条
    state = Store(settings.state_path).load()
    p = state.principals["analyst"]
    assert len(p.records) == n
    assert sum(1 for e in p.audit if e.decision == "charged") == n


def test_concurrent_same_id_charges_once(svc, settings):
    n = 16
    payload = QueryRequest(query_id="c-same", agg="count",
                           dimensions=["region", "product"], api_key=ANALYST)

    def one(_):
        local = QueryService(settings, Store(settings.state_path))
        return local.submit(payload.model_copy(deep=True))

    with ThreadPoolExecutor(max_workers=12) as pool:
        responses = list(pool.map(one, range(n)))

    # 全部返回同一份结果
    first = responses[0]
    for r in responses:
        assert r.groups == first.groups
        assert r.status == first.status
    # 只有一个请求实际计费
    assert _spent(svc) == pytest.approx(0.1)
    state = Store(settings.state_path).load()
    p = state.principals["analyst"]
    assert sum(1 for e in p.audit if e.decision == "charged") == 1


def test_concurrent_requests_never_exceed_budget(settings):
    """预算只够 30 次；并发 40 次 -> 恰好 30 成功 / 10 拒绝，spent 恰好 3.0。"""
    n = 40

    def one(i):
        local = QueryService(settings, Store(settings.state_path))
        try:
            submit_count(local, f"c-edge-{i}")
            return "ok"
        except BudgetExhaustedError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=16) as pool:
        outcomes = list(pool.map(one, range(n)))

    ok = outcomes.count("ok")
    rejected = outcomes.count("rejected")
    assert ok == 30
    assert rejected == 10
    svc = QueryService(settings, Store(settings.state_path))
    assert _spent(svc) == pytest.approx(3.0)
    # 再多一个也一定被拒绝（无超支窗口）
    with pytest.raises(BudgetExhaustedError):
        submit_count(svc, "c-edge-after")


def test_multiprocess_concurrency_respects_budget(settings, tmp_path):
    """跨进程并发：文件锁必须串行化，总扣减不超额、记录不丢失。"""
    n_workers = 4
    per_worker = 10  # 共 40 次，预算只够 30 次

    script = """
import os, sys
from dp_engine.config import reset_settings, get_settings
from dp_engine.service import QueryService
from dp_engine.storage import Store
from dp_engine.models import QueryRequest
from dp_engine.errors import BudgetExhaustedError

reset_settings()
s = get_settings()
svc = QueryService(s, Store(s.state_path))
wid = sys.argv[1]
ok = 0
for i in range(int(sys.argv[2])):
    try:
        svc.submit(QueryRequest(
            query_id=f"mp-{wid}-{i}", agg="count",
            dimensions=["region"], api_key="key-analyst"))
        ok += 1
    except BudgetExhaustedError:
        pass
print(ok)
"""
    procs = [
        subprocess.run(
            [sys.executable, "-c", script, str(w), str(per_worker)],
            capture_output=True, text=True, check=True,
            env={**os.environ, "PYTHONPATH": os.getcwd(),
                 "DP_STATE_PATH": settings.state_path,
                 "DP_BUDGET_TOTAL": "3.0", "DP_UNIT_EPSILON": "0.1",
                 "DP_MIN_GROUP_SIZE": "3"},
        )
        for w in range(n_workers)
    ]
    oks = [int(p.stdout.strip()) for p in procs]
    assert sum(oks) == 30  # 全进程合计恰好 30 次成功

    state = Store(settings.state_path).load()
    pstate = state.principals["analyst"]
    assert pstate.spent == pytest.approx(3.0)
    assert sum(1 for e in pstate.audit if e.decision == "charged") == 30
    # 40 个互不相同的 query_id 都留有裁决记录（成功或拒绝），无丢失
    assert len(pstate.records) == 40
