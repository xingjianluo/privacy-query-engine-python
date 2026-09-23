"""本地数据集与精确分组聚合。

数据集在首次启动时以确定性内容生成并落盘，重启后保持不变。
精确聚合值仅供 service 层内部使用——对外只允许输出加噪/抑制后的结果。
"""
import json
from dataclasses import dataclass
from pathlib import Path

from app.errors import InvalidQueryError

DIMENSIONS = ("city", "department")
VALUE_COLUMNS = ("age", "salary")


def _seed_rows() -> list[dict]:
    """确定性的内置样本：特意保留小分组用于抑制边界验证。"""
    return [
        # Beijing 5 人；Shanghai 2 人；Shenzhen 1 人；Guangzhou 4 人
        {"id": 1, "city": "Beijing", "department": "eng", "age": 29, "salary": 21000},
        {"id": 2, "city": "Beijing", "department": "eng", "age": 34, "salary": 24000},
        {"id": 3, "city": "Beijing", "department": "sales", "age": 41, "salary": 18000},
        {"id": 4, "city": "Beijing", "department": "sales", "age": 38, "salary": 17500},
        {"id": 5, "city": "Beijing", "department": "hr", "age": 31, "salary": 15000},
        {"id": 6, "city": "Shanghai", "department": "eng", "age": 27, "salary": 22000},
        {"id": 7, "city": "Shanghai", "department": "hr", "age": 45, "salary": 16000},
        {"id": 8, "city": "Shenzhen", "department": "eng", "age": 30, "salary": 26000},
        {"id": 9, "city": "Guangzhou", "department": "sales", "age": 36, "salary": 17000},
        {"id": 10, "city": "Guangzhou", "department": "sales", "age": 33, "salary": 16500},
        {"id": 11, "city": "Guangzhou", "department": "eng", "age": 28, "salary": 23000},
        {"id": 12, "city": "Guangzhou", "department": "eng", "age": 50, "salary": 31000},
    ]


@dataclass(frozen=True)
class Dataset:
    rows: list[dict]

    def grouped(self, dimension: str, group_filter: list[str] | None) -> dict[str, list[dict]]:
        buckets: dict[str, list[dict]] = {}
        if group_filter is not None:
            for name in group_filter:
                buckets[name] = []
        for row in self.rows:
            key = str(row[dimension])
            if group_filter is None:
                buckets.setdefault(key, []).append(row)
            elif key in buckets:
                buckets[key].append(row)
        return dict(sorted(buckets.items()))


def validate_fields(dimension: str, value_column: str | None) -> None:
    if dimension not in DIMENSIONS:
        raise InvalidQueryError(
            f"unknown dimension '{dimension}'; allowed: {', '.join(DIMENSIONS)}"
        )
    if value_column is not None and value_column not in VALUE_COLUMNS:
        raise InvalidQueryError(
            f"unknown value_column '{value_column}'; allowed: {', '.join(VALUE_COLUMNS)}"
        )


def load_dataset(path: Path) -> Dataset:
    """从磁盘加载数据集；不存在则生成种子数据并原子落盘。"""
    if path.exists():
        with path.open("r", encoding="utf-8") as fh:
            return Dataset(rows=json.load(fh))
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = _seed_rows()
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(rows, fh, ensure_ascii=False, indent=2)
    tmp.replace(path)
    return Dataset(rows=rows)
