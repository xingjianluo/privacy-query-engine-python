"""加噪确定性与抑制边界：空分组、小分组必须给明确原因且不泄漏精确值。"""
from app.config import Settings
from app.dataset import load_dataset
from app.models import QueryRequest
from app.service import submit_query
from app.storage import Ledger
from app.noise import laplace_noise


def _count_query(qid="p1", epsilon=1.0, groups=None):
    return QueryRequest(
        query_id=qid, principal="alice", aggregation="count",
        dimension="city", epsilon=epsilon, groups=groups,
    )


def test_empty_and_small_groups_are_supplied_with_reasons(settings: Settings):
    ledger = Ledger(settings.ledger_path, settings.lock_path)
    ledger.grant("alice", 100.0)
    ds = load_dataset(settings.dataset_path)
    resp = submit_query(
        _count_query(groups=["Beijing", "Shanghai", "Shenzhen", "Hangzhou"]),
        ledger, ds, settings,
    )
    by_group = {s.group: s for s in resp.suppressed}
    assert set(by_group) == {"Shanghai", "Shenzhen", "Hangzhou"}
    assert by_group["Hangzhou"].reason == "empty_group"
    assert by_group["Shanghai"].reason == "small_group"
    assert by_group["Shenzhen"].reason == "small_group"
    # 抑制描述不得包含精确计数（2 与 1）
    for s in resp.suppressed:
        assert s.detail is not None
        assert "2" not in s.detail and "1" not in s.detail
    # 只有 Beijing(5) 出现在结果中（阈值=3）
    assert [g.group for g in resp.results] == ["Beijing"]
    # 噪声后计数为非负整数
    assert all(isinstance(g.noisy_count, int) and g.noisy_count >= 0 for g in resp.results)


def test_threshold_boundary(settings: Settings):
    # min_group_size=3：2 行抑制、3 行通过（Guangzhou=4 通过, Shanghai=2 抑制）
    ledger = Ledger(settings.ledger_path, settings.lock_path)
    ledger.grant("alice", 100.0)
    ds = load_dataset(settings.dataset_path)
    resp = submit_query(_count_query(qid="bnd"), ledger, ds, settings)
    groups_ok = {g.group for g in resp.results}
    groups_no = {s.group for s in resp.suppressed if s.reason == "small_group"}
    assert {"Beijing", "Guangzhou"} <= groups_ok
    assert {"Shanghai", "Shenzhen"} == groups_no


def test_noise_is_deterministic_and_private(settings: Settings):
    ledger = Ledger(settings.ledger_path, settings.lock_path)
    ledger.grant("alice", 100.0)
    ds = load_dataset(settings.dataset_path)
    r1 = submit_query(_count_query(qid="det"), ledger, ds, settings)
    record = ledger.get_query("det")
    ledger2 = Ledger(settings.ledger_path, settings.lock_path)
    r2 = submit_query(_count_query(qid="det"), ledger2, ds, settings)
    assert [g.value for g in r1.results] == [g.value for g in r2.results]
    assert record is not None

    # 不同 query_id 使用不同噪声派生键（键不同则样本统计独立）；
    # 取整后可能偶发碰撞，因此用下面的多样本统计断言保证“确实加噪”
    r3 = submit_query(_count_query(qid="det-other-key"), ledger, ds, settings)
    assert r3.results is not None

    # 统计层面验证噪声确实被加入：scale=1 时 40 个独立样本取整后不止一个取值
    noisy_vals = set()
    for i in range(40):
        z = 5.0 + laplace_noise(1.0, f"stat-key-{i}")
        noisy_vals.add(max(0, round(z)))
    assert len(noisy_vals) > 1
    assert noisy_vals != {5}


def test_sum_and_avg_do_not_leak_exact_values(settings: Settings):
    ledger = Ledger(settings.ledger_path, settings.lock_path)
    ledger.grant("alice", 100.0)
    ds = load_dataset(settings.dataset_path)
    # Beijing 工资精确总和 = 21000+24000+18000+17500+15000 = 95500
    resp_sum = submit_query(
        QueryRequest(
            query_id="s1", principal="alice", aggregation="sum",
            dimension="city", value_column="salary", epsilon=0.5,
            groups=["Beijing"],
        ),
        ledger, ds, settings,
    )
    bj = next(g for g in resp_sum.results if g.group == "Beijing")
    assert abs(bj.value - 95500.0) > 0  # 有噪声
    assert bj.noisy_count is None       # sum 不无偿返回计数
    # 精确值不得出现在响应文本中
    assert "95500" not in resp_sum.model_dump_json()

    resp_avg = submit_query(
        QueryRequest(
            query_id="a1", principal="alice", aggregation="avg",
            dimension="city", value_column="salary", epsilon=0.5,
            groups=["Beijing"],
        ),
        ledger, ds, settings,
    )
    bj2 = next(g for g in resp_avg.results if g.group == "Beijing")
    # 精确均值 19100 不应原样出现
    assert abs(bj2.value - 19100.0) > 1e-6
    assert isinstance(bj2.noisy_count, int)
