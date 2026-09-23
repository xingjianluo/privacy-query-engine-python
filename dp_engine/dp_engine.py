"""差分隐私核心：敏感度、预算成本、确定性加噪与分组聚合。

预算模型（epsilon 型，Laplace 机制）：
    cost(查询) = unit_epsilon * sensitivity
    scale     = sensitivity / epsilon = 1 / unit_epsilon   （对每个输出分组一致）
    敏感度：
        count: 1
        sum  : U - L            （数值列裁剪到 [L, U] 后单条记录最大贡献）
        mean : (U - L) / k      （k = min_group_size，对分组大小的保守估计）

确定性：噪声以 (query_id, 分组键) 派生种子，因此相同 query_id+参数的重试
得到逐位一致的结果；不同 query_id 使用不同噪声，不会相互抵消。
"""

from __future__ import annotations

import hashlib
import math
import random
from collections import OrderedDict

from .dataset import COLUMN_DOMAINS
from .errors import InvalidParameterError
from .models import GroupResult, QueryRequest

SUPPRESS_SMALL_GROUP = "group_below_min_size"
SUPPRESS_EMPTY_GROUP = "empty_group"
SUPPRESS_NO_DATA = "no_rows_match_filter"


def sensitivity(agg: str, domain: tuple[float, float] | None, min_group_size: int) -> float:
    if agg == "count":
        return 1.0
    if agg == "sum":
        assert domain is not None
        return float(domain[1] - domain[0])
    if agg == "mean":
        assert domain is not None
        return float(domain[1] - domain[0]) / float(min_group_size)
    raise InvalidParameterError(f"不支持的聚合类型: {agg}")


def budget_cost(agg: str, domain: tuple[float, float] | None, settings) -> float:
    """成本 = unit_epsilon * 敏感度。"""
    s = sensitivity(agg, domain, settings.min_group_size)
    return settings.unit_epsilon * s


def _seed_for(query_id: str, group_key: tuple) -> int:
    material = query_id + "|" + repr(group_key)
    digest = hashlib.sha256(material.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big", signed=False)


def seeded_noise(query_id: str, group_key: tuple, scale: float) -> float:
    """以 (query_id, 分组键) 派生确定性种子的 Laplace 噪声。

    Laplace 逆变换：X = -scale * sgn(U-0.5) * ln(1 - 2|U-0.5|)。
    Python 的 random.Random 算法跨平台稳定，保证结果可复现。
    """
    rng = random.Random(_seed_for(query_id, group_key))
    u = rng.random() - 0.5
    return -scale * math.copysign(1.0, u) * math.log(1.0 - 2.0 * abs(u))


def _row_matches(row: dict, filters: dict) -> bool:
    for key, value in filters.items():
        if row.get(key) != value:
            return False
    return True


def aggregate(req: QueryRequest, settings, rows: list[dict]) -> tuple[list[GroupResult], str | None]:
    """执行过滤、分组、裁剪聚合、抑制与加噪。

    返回 (分组结果列表, 整体抑制原因)。任何被抑制的分组不携带精确值；
    正常分组的 value 只包含 加噪后 的结果。
    """
    matched = [r for r in rows if _row_matches(r, req.filters)]
    if not matched:
        return [], SUPPRESS_NO_DATA

    # 保持分组输出顺序确定：按维度键排序
    dims = req.dimensions
    buckets: dict[tuple, list[dict]] = OrderedDict()
    for row in sorted(matched, key=lambda r: tuple(str(r.get(d)) for d in dims)):
        key = tuple(row.get(d) for d in dims)
        buckets.setdefault(key, []).append(row)

    domain = COLUMN_DOMAINS.get(req.column) if req.column else None
    scale = 1.0 / settings.unit_epsilon  # = sensitivity / epsilon（epsilon=cost）
    k = settings.min_group_size

    results: list[GroupResult] = []
    for key, members in buckets.items():
        n = len(members)
        if n == 0:
            # 理论上不会出现（桶来自实际行），保留显式分支
            results.append(GroupResult(key=list(key), status="suppressed",
                                       group_size=0, suppress_reason=SUPPRESS_EMPTY_GROUP))
            continue
        if n < k:
            # 小分组：仅说明"低于公开阈值 k"，不回传真实行数，也不输出任何聚合值
            results.append(GroupResult(key=list(key), status="suppressed",
                                       group_size=None, suppress_reason=SUPPRESS_SMALL_GROUP))
            continue

        if req.agg == "count":
            exact = float(n)
        else:
            assert domain is not None and req.column is not None
            lo, hi = domain
            values = [min(hi, max(lo, float(m[req.column]))) for m in members]
            if req.agg == "sum":
                exact = sum(values)
            else:  # mean
                exact = sum(values) / n

        noisy = exact + seeded_noise(req.query_id, key, scale)
        # group_size 不回传精确值（即使 count 查询也只能拿到加噪结果）
        results.append(GroupResult(key=list(key), status="ok",
                                   group_size=None, value=round(noisy, 6)))
    return results, None
