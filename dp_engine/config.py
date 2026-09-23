"""全局配置：存储路径、预算参数与最小分组阈值均来自环境变量。"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_STATE_PATH = "./data/state.json"
# 每个主体的总隐私预算（以 epsilon 计）
DEFAULT_BUDGET_TOTAL = 3.0
# 单位敏感度对应的 epsilon 成本；噪声 epsilon = cost = unit_epsilon * sensitivity
DEFAULT_UNIT_EPSILON = 0.1
# 分组真实行数低于该值则抑制（k-匿名式小分组抑制）
DEFAULT_MIN_GROUP_SIZE = 3


@dataclass(frozen=True)
class Settings:
    state_path: str
    budget_total: float
    unit_epsilon: float
    min_group_size: int

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            state_path=os.environ.get("DP_STATE_PATH", DEFAULT_STATE_PATH),
            budget_total=float(os.environ.get("DP_BUDGET_TOTAL", DEFAULT_BUDGET_TOTAL)),
            unit_epsilon=float(os.environ.get("DP_UNIT_EPSILON", DEFAULT_UNIT_EPSILON)),
            min_group_size=int(os.environ.get("DP_MIN_GROUP_SIZE", DEFAULT_MIN_GROUP_SIZE)),
        )


_cached: Settings | None = None


def get_settings() -> Settings:
    """进程内缓存；测试通过 reset_settings() 重新读取环境变量。"""
    global _cached
    if _cached is None:
        _cached = Settings.from_env()
    return _cached


def reset_settings() -> None:
    global _cached
    _cached = None
