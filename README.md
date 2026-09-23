# 本地差分隐私聚合查询服务（Local DP Aggregation Query Service）

基于 FastAPI 的本地差分隐私（Differential Privacy, DP）聚合查询服务。
使用内置本地数据集与本地 JSON 持久化模拟隐私预算账本与查询审计，
保证并发查询、重复提交、预算耗尽与服务重启场景下结论一致、可解释、可复现。

## 1. 快速开始

```bash
uv sync
uv run uvicorn main:app --reload        # 启动服务（默认 http://127.0.0.1:8000）
uv run pytest                           # 运行全部测试（含预算判定日志）
```

数据目录默认 `./data`（首次启动自动生成确定性数据集 `dataset.json` 与账本
`ledger.json`），可用环境变量 `PQE_DATA_DIR` 覆盖（测试即用临时目录隔离）。
交互式文档：`http://127.0.0.1:8000/docs`。

### API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/admin/grant` | 授权主体并设置预算（`{"principal": "alice", "budget_epsilon": 10}`，缺省 10） |
| GET | `/budget/{principal}` | 查询剩余/已耗预算与授权状态 |
| POST | `/query` | 提交 DP 聚合查询 |
| GET | `/health` | 健康检查 |

查询请求示例：

```json
{
  "query_id": "q-001",
  "principal": "alice",
  "aggregation": "count",
  "dimension": "city",
  "value_column": null,
  "epsilon": 1.0,
  "groups": ["Beijing", "Shanghai"]
}
```

- `aggregation`：`count` / `sum` / `avg`。
- `dimension`：`city` 或 `department`。
- `value_column`：`sum`/`avg` 必填，取值 `age` 或 `salary`；`count` 省略。
- `epsilon`：本次查询申请的隐私预算，`(0, 2.0]`。
- `groups`：可选维度取值白名单；列表中不存在的取值作为“空分组”返回抑制原因。

## 2. 隐私预算模型（epsilon-ledger）

- 预算以 **ε（epsilon）** 为单位记账。每个授权主体有独立预算，
  查询必须声明 `epsilon`，审批时执行 `remaining = budget - spent`，
  仅当 `remaining >= epsilon` 才扣减，扣减量恰为 `epsilon`。
- 敏感度决定**噪声大小而非记账单位**：
  - `count` 的 L1 敏感度为 **1**（相邻数据集相差一行）；
  - `sum(x)` 的全局敏感度取**全表** `max|x|`（与具体分组无关的统一上界）；
  - `avg` 实现为“带噪求和 ÷ 带噪计数”两个机制的**串行组合**：
    ε 在二者间平分（各 ε/2），总记账仍为 ε；
  - 一次查询内各分组共享同一机制，按并行组合（parallel composition）
    不重复计费：**每次查询只扣一次 ε**。
- 记账、判定依据与扣减前后数值均写入服务日志：

  ```
  budget decision | query_id=b1 principal=bob replayed=false \
    before=2.500000 required=1.000000 after=1.500000 basis=charged
  budget decision | query_id=b3 principal=bob replayed=false \
    before=0.500000 required=1.000000 after=0.500000 basis=insufficient_budget_denied
  ```

  `basis` 取值：`charged`（成功扣减）、`insufficient_budget_denied`（预算不足拒绝）、
  `idempotent_cache_hit`（幂等重放，不扣预算）。

### 跨重启持久化

预算余额、授权状态与已完成查询记录全部持久化在 `data/ledger.json`，
重启后从磁盘恢复：已耗预算不会回滚，已完成查询可直接重放。

## 3. 加噪与抑制规则

### 加噪（Laplace 机制）

- 噪声分布：`Lap(0, Δf/ε)`；`avg` 的两个机制各用 `Lap(0, Δf/(ε/2))`
  与 `Lap(0, 1/(ε/2))`。
- 噪声**完全确定性派生**：以 SHA-256 从 `(query_id, group, 用途)` 派生均匀样本，
  经 Laplace 逆 CDF 得到噪声。不使用全局随机源，因此：
  - 同一 `query_id` 相同参数的任何重试（含跨重启）结果逐位一致；
  - 不同 `query_id` 统计独立；跨平台、跨进程可复现。
- 计数结果四舍五入为非负整数；求和保留 2 位小数；均值保留 4 位。
  这些都是对带噪结果的后处理，不额外消耗隐私。

### 抑制（Suppression）

分组在加噪**之前**依据精确行数判定，被抑制的分组只返回原因，绝不输出精确值：

| reason | 触发条件 | 对外信息 |
| --- | --- | --- |
| `empty_group` | 请求的维度取值在数据集中无任何行 | 固定文案，不含行数 |
| `small_group` | 原始行数 `< min_group_size`（默认 3） | 仅给阈值，不给精确行数 |
| `unstable_average` | 仅 `avg`：带噪分母计数过小（<1），比值不可靠 | 固定文案 |

通过阈值的分组输出带噪值。响应里的 `suppressed` 列表与 `results` 互斥覆盖所有分组，
空分组/小分组不会“静默消失”，也不会以零值伪装。

## 4. 幂等与错误类型

以 `query_id` 作为幂等键：

- 相同 `query_id` + 相同语义参数（聚合类型、维度、数值列、ε、groups）：
  返回首次结果的完整缓存，`replayed=true`，**不二次扣预算、结果不变**；
  仅传输层元数据 `replayed` 与首次不同，DP 载荷逐字段一致。
- 相同 `query_id` + 不同参数 → `409 query_conflict`，防止复用 ID 顶替查询。
- 预算耗尽后的重试：已完成查询仍可重放；新查询一律拒绝且不写半条记录。

可区分的错误码（`{"error": code, "message": ...}`）：

| HTTP | error code | 含义 |
| --- | --- | --- |
| 422 | `invalid_query` | 参数非法：未知维度/列、sum/avg 缺 value_column、ε 超单次上限、请求体不合法 |
| 403 | `unauthorized` | 主体未授权 |
| 429 | `budget_exhausted` | 剩余 ε 不足（message 含剩余与所需数值） |
| 409 | `query_conflict` | query_id 已存在但参数不一致 |
| 500 | `internal_error` | 内部/持久化失败：仅返回脱敏信息与追踪 ref，堆栈只入服务日志 |

参数校验失败、未授权、预算不足都发生在任何状态修改之前，不扣预算、不留记录。

## 5. 并发与失败语义

- 每次提交在**单个事务**内完成「查缓存 → 鉴权 → 预算判定 → 加噪计算 →
  扣减 → 落盘」。事务期间持有：
  - 进程内 `threading.RLock`（同进程多线程并发）；
  - `fcntl.flock` 排他文件锁（多进程/多 worker）。
- 账本通过临时文件 `fsync` + `os.replace` **原子替换**，读侧只见完整版本。
- 事务体抛异常时不调用落盘，内存修改整体丢弃：**不会出现扣了预算却没有查询记录，
  或落了记录却没扣预算的半完成状态**（见 `test_persistence_failure_leaves_no_half_state`）。
- 并发保证（测试用 16/12 线程压测）：
  - 同一 `query_id` 并发提交：恰好 1 次扣减、1 条记录，其余全部重放同一结果；
  - 不同 `query_id` 并发提交：成功数 × ε ≤ 预算总额，其余得到
    `budget_exhausted`，无超额扣减、无丢失、无重复计数。

## 6. 验证方法

```bash
uv run pytest -v
```

测试覆盖：

- `tests/test_budget.py`：预算耗尽、拒绝不扣预算、非法参数分类、授权校验、
  avg 的 ε 计费；日志断言扣减前后数值与 `basis`；
- `tests/test_idempotency.py`：重复提交载荷一致且只扣一次、参数冲突 409、
  耗尽后仍可重放、跨“重启”（新建存储/应用实例）预算与结果保留；
- `tests/test_concurrency.py`：同 ID 并发单次扣减、不同 ID 并发绝不超支、
  持久化失败无半完成状态且对外不泄漏内部异常文本；
- `tests/test_privacy.py`：空/小分组抑制原因、阈值边界（2 抑制、3+ 放行）、
  噪声确定性与统计有效性、sum/avg 不泄漏精确值、sum 不无偿附带计数；
- `tests/test_api.py`：HTTP 全链路、非法 JSON 脱敏为 `invalid_query`、
  非领域异常脱敏为 `internal_error`。

## 7. 内置数据集

首次启动确定性生成 12 行员工记录（`data/dataset.json`），
按城市的分组规模刻意设计为 5 / 4 / 2 / 1，便于验证阈值 3 的抑制边界；
另有不存在的城市取值用于验证 `empty_group`。
