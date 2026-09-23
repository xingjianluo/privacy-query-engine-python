"""授权管理：API Key -> 主体，以及主体允许使用的聚合类型。"""

from __future__ import annotations

from .errors import ForbiddenError, UnauthorizedError

# api_key -> (主体标识, 允许的聚合类型集合)
KEY_REGISTRY: dict[str, tuple[str, set[str]]] = {
    "key-analyst": ("analyst", {"count", "sum", "mean"}),
    "key-counter": ("counter", {"count"}),
}


def authenticate(api_key: str | None) -> str:
    """校验 API Key，返回主体标识。

    未提供或无法识别的 Key 一律拒绝，且不回显传入值，避免探测泄漏。
    """
    if not api_key or not isinstance(api_key, str):
        raise UnauthorizedError("缺少 API Key，请在 api_key 字段中提供凭证")
    record = KEY_REGISTRY.get(api_key)
    if record is None:
        raise UnauthorizedError("API Key 无效")
    return record[0]


def grants(principal: str) -> set[str]:
    for _, (p, allowed) in KEY_REGISTRY.items():
        if p == principal:
            return set(allowed)
    return set()


def authorize(principal: str, agg: str) -> None:
    """校验主体是否有权使用该聚合类型。已认证但权限不足属于 403。"""
    if agg not in grants(principal):
        raise ForbiddenError(f"主体 {principal} 无权执行 {agg} 聚合")
