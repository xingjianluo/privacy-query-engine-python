"""抑制边界：小分组、空分组/无匹配数据必须以明确原因返回，且不泄漏精确值。"""

from __future__ import annotations

import pytest

from dp_engine.dp_engine import (
    SUPPRESS_NO_DATA,
    SUPPRESS_SMALL_GROUP,
)
from dp_engine.models import QueryRequest

ANALYST = "key-analyst"


def test_small_groups_are_suppressed_with_reason(svc):
    # 分组规模 north/A=4, north/B=2, south/A=3, south/B=1，阈值 k=3
    r = svc.submit(QueryRequest(
        query_id="s-1", agg="count",
        dimensions=["region", "product"], api_key=ANALYST,
    ))
    by_key = {tuple(g.key): g for g in r.groups}
    ok_keys = {("north", "A"), ("south", "A")}
    suppressed_keys = {("north", "B"), ("south", "B")}

    for key in ok_keys:
        g = by_key[key]
        assert g.status == "ok" and g.value is not None
    for key in suppressed_keys:
        g = by_key[key]
        assert g.status == "suppressed"
        assert g.suppress_reason == SUPPRESS_SMALL_GROUP
        assert g.value is None
        # 不泄漏真实行数
        assert g.group_size is None
    assert r.suppression_threshold == 3


def test_no_rows_match_filter_is_overall_suppressed(svc):
    r = svc.submit(QueryRequest(
        query_id="s-2", agg="count", dimensions=["region"],
        filters={"region": "antarctica"}, api_key=ANALYST,
    ))
    assert r.status == "suppressed"
    assert r.suppress_reason == SUPPRESS_NO_DATA
    assert r.groups == []
    # 整体抑制不泄漏精确值，但查询本身仍消耗预算
    assert r.charged_budget > 0


def test_filter_narrows_to_small_group(svc):
    # gender=F 过滤后 north/B 只有 1 行 -> 抑制；north/A 有 2 行 -> 也抑制
    r = svc.submit(QueryRequest(
        query_id="s-3", agg="count",
        dimensions=["region", "product"],
        filters={"gender": "F"}, api_key=ANALYST,
    ))
    assert all(g.status == "suppressed" for g in r.groups)
    assert {g.suppress_reason for g in r.groups} == {SUPPRESS_SMALL_GROUP}


def test_no_exact_values_leak_for_sum_and_mean(svc):
    # 精确 sum(units) north/A = 2+4+6+8 = 20；响应中绝不能出现该精确值
    r = svc.submit(QueryRequest(
        query_id="s-sum", agg="sum", column="units",
        dimensions=["region", "product"], api_key=ANALYST,
    ))
    values = {tuple(g.key): g.value for g in r.groups if g.status == "ok"}
    assert 20.0 not in values.values()
    # 小分组无值
    assert all(g.value is None for g in r.groups if g.status == "suppressed")

    # mean 对边界分组 k=3 也成立（south/A 恰好 3 行，允许输出加噪均值）
    rm = svc.submit(QueryRequest(
        query_id="s-mean", agg="mean", column="score",
        dimensions=["region", "product"], api_key=ANALYST,
    ))
    by_key = {tuple(g.key): g for g in rm.groups}
    assert by_key[("south", "A")].status == "ok"
    assert by_key[("south", "B")].status == "suppressed"
    # 精确均值 1.7/3 ≈ 0.5667 不应原样出现
    assert by_key[("south", "A")].value != pytest.approx(1.7 / 3, abs=1e-12)


def test_aggregation_without_dimensions(svc):
    # 无维度 = 全表单组 10 行，>= k，正常返回
    r = svc.submit(QueryRequest(
        query_id="s-total", agg="count", dimensions=[], api_key=ANALYST,
    ))
    assert len(r.groups) == 1
    g = r.groups[0]
    assert g.key == [] and g.status == "ok"
    # 精确总数 10 不能原样出现
    assert g.value != 10.0
