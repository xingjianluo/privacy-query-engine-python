"""确定性拉普拉斯（Laplace）加噪。

同一 ``(query_id, group, 噪声用途)`` 与同一 scale 永远产生相同噪声，
因此重复提交/跨重启重放得到完全一致的结果；不同键则统计独立。

不使用全局随机源，仅依赖 SHA-256 派生均匀样本，再经逆 CDF 得到
Laplace 分布样本，保证跨平台、跨进程可复现。
"""
import hashlib
import math


def _uniform_unit(key: str) -> float:
    """由 key 确定性地派生 (0, 1) 上的均匀样本。"""
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    # 取前 8 字节为无符号整数；避开 0 与 2^64 以保证 log 合法
    u_int = int.from_bytes(digest[:8], "big")
    return (u_int + 1) / (2**64 + 2)


def laplace_noise(scale: float, key: str) -> float:
    """返回确定性的 Laplace(0, scale) 样本。

    Laplace 逆 CDF：X = -scale * sgn(U-0.5) * ln(1 - 2|U-0.5|)，
    其中 U ~ Uniform(0,1)。
    """
    if scale <= 0:
        raise ValueError("scale must be positive")
    u = _uniform_unit(key)
    centered = u - 0.5
    # 等价写法，避免 1-2|...| 恰好为 0
    x = -scale * math.copysign(1.0, centered) * math.log(1.0 - 2.0 * abs(centered))
    return x


def group_noise_key(query_id: str, group: str, purpose: str) -> str:
    return f"{query_id}|{group}|{purpose}"
