"""服务配置：数据集、预算与抑制阈值。

测试可通过环境变量 ``PQE_DATA_DIR`` 指向临时目录，从而隔离持久化数据。
"""
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import os


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    # 新授权主体的初始隐私预算（以 epsilon 计）
    default_budget_epsilon: float = 10.0
    # 单次查询允许申请的最大 epsilon
    max_query_epsilon: float = 2.0
    # 分组抑制阈值：分组原始行数 < 该值则抑制该分组
    min_group_size: int = 3
    # 均值查询两次机制（计数 + 求和）之间拆分 epsilon 的比例下限保护
    min_count_for_average: int = 1

    @property
    def ledger_path(self) -> Path:
        return self.data_dir / "ledger.json"

    @property
    def dataset_path(self) -> Path:
        return self.data_dir / "dataset.json"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "ledger.lock"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    data_dir = Path(os.environ.get("PQE_DATA_DIR", "data"))
    return Settings(data_dir=data_dir)


def reset_settings_cache() -> None:
    """仅供测试使用：清除配置缓存。"""
    get_settings.cache_clear()
