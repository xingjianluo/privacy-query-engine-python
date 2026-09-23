"""本地内存数据集与列定义域。所有数值列在加噪前按定义域裁剪。

数据集（10 行，region × product 分组规模刻意设计为 4/2/3/1）：
    north/A=4, north/B=2, south/A=3, south/B=1
配合默认 min_group_size=3，分组结果覆盖 通过/边界/抑制 三种情形。
"""

from __future__ import annotations

from typing import Any

# 数值列名 -> (下限, 上限)；sum/mean 前先裁剪，敏感度由定义域宽度决定
COLUMN_DOMAINS: dict[str, tuple[float, float]] = {
    "units": (1.0, 10.0),   # 宽度 9  -> sum 敏感度 9, mean 敏感度 9/k
    "score": (0.0, 1.0),    # 宽度 1  -> sum 敏感度 1（低成本查询，便于并发测试）
}

DIMENSION_COLUMNS: set[str] = {"region", "product", "gender"}

ROWS: list[dict[str, Any]] = [
    {"region": "north", "product": "A", "gender": "M", "units": 2, "score": 0.2},
    {"region": "north", "product": "A", "gender": "F", "units": 4, "score": 0.4},
    {"region": "north", "product": "A", "gender": "M", "units": 6, "score": 0.6},
    {"region": "north", "product": "A", "gender": "F", "units": 8, "score": 0.8},
    {"region": "north", "product": "B", "gender": "M", "units": 3, "score": 0.3},
    {"region": "north", "product": "B", "gender": "F", "units": 5, "score": 0.5},
    {"region": "south", "product": "A", "gender": "M", "units": 1, "score": 0.1},
    {"region": "south", "product": "A", "gender": "F", "units": 7, "score": 0.7},
    {"region": "south", "product": "A", "gender": "M", "units": 9, "score": 0.9},
    {"region": "south", "product": "B", "gender": "F", "units": 10, "score": 1.0},
]


def get_rows() -> list[dict[str, Any]]:
    """返回行数据的副本，避免调用方意外修改数据集。"""
    return [dict(r) for r in ROWS]


def numeric_columns() -> set[str]:
    return set(COLUMN_DOMAINS.keys())


def dimension_columns() -> set[str]:
    return set(DIMENSION_COLUMNS)


def known_columns() -> set[str]:
    return numeric_columns() | dimension_columns()
