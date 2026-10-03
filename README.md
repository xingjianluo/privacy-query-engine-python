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
| GET | `/budget/{principal}` | 查询个人剩余/已耗预算与授权状态 |
| POST | `/query` | 提交 DP 聚合查询（可选 `pool_id` 走共享预算池） |
| POST | `/admin/pools` | 创建共享预算池（`{"pool_id", "budget_epsilon", "members"}`） |
| GET | `/pools/{pool_id}` | 查询池子额度、已耗与成员名单 |
| PUT | `/admin/pools/{pool_id}/members` | 整体替换池子成员名单 |
| POST | `/admin/adjustments` | 预算调整（追加/回收，幂等） |
| GET | `/history/{principal}` | 查看自己的提交历史（筛选 + 分页） |
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
- `pool_id`：可选。缺省时从**个人额度**扣减（旧客户端行为不变）；
  填写时从该共享预算池扣减，要求 `principal` 是池子成员。

## 2. 隐私预算模型（epsilon-ledger）

- 预算以 **ε（epsilon）** 为单位记账。每个授权主体有独立预算，
  查询必须声明 `epsilon`，审批时执行 `remaining = budget - spent`，
  仅当 `remaining >= epsilon` 才扣减，扣减量恰为 `epsilon`。
- 扣费来源二选一（请求里的 `pool_id`）：
  - 缺省：从**个人额度**扣减，要求主体已授权；
  - 指定 `pool_id`：从**共享预算池**扣减，要求主体是池子成员
    （成员资格即授权，不要求同时持有个人额度）。
- 敏感度决定**噪声大小而非记账单位**：
  - `count` 的 L1 敏感度为 **1**（相邻数据集相差一行）；
  - `sum(x)` 的全局敏感度取**全表** `max|x|`（与具体分组无关的统一上界）；
  - `avg` 实现为“带噪求和 ÷ 带噪计数”两个机制的**串行组合**：
    ε 在二者间平分（各 ε/2），总记账仍为 ε；
  - 一次查询内各分组共享同一机制，按并行组合（parallel composition）
    不重复计费：**每次查询只扣一次 ε**。
- 记账、判定依据与扣减前后数值均写入服务日志：

  ```
  budget decision | query_id=b1 principal=bob source=personal replayed=false \
    before=2.500000 required=1.000000 after=1.500000 basis=charged
  budget decision | query_id=b3 principal=bob source=personal replayed=false \
    before=0.500000 required=1.000000 after=0.500000 basis=insufficient_budget_denied
  budget decision | query_id=p2 principal=alice source=pool:team-a replayed=false \
    before=0.000000 required=1.000000 after=0.000000 basis=insufficient_budget_denied
  ```

  `source` 取值：`personal`（个人额度）或 `pool:<pool_id>`（共享预算池）；
  `basis` 取值：`charged`（成功扣减）、`insufficient_budget_denied`（预算不足拒绝）、
  `idempotent_cache_hit`（幂等重放，不扣预算）。

### 跨重启持久化

预算余额、授权状态、预算池、调整单与已完成查询记录全部持久化在
`data/ledger.json`，重启后从磁盘恢复：已耗预算不会回滚，已完成查询可直接重放。

## 3. 共享预算池

团队可以把一部分额度集中成**共享预算池**，池内额度由全体成员共用：

- `POST /admin/pools` 创建池子：`{"pool_id": "team-a", "budget_epsilon": 5.0,
  "members": ["alice", "bob"]}`。同名池子重复创建返回 `409 pool_conflict`。
- 成员名单即访问控制：只有 `members` 里的主体能在查询时用 `pool_id` 走池子扣费；
  非成员得到 `403 unauthorized`。名单可随时用
  `PUT /admin/pools/{pool_id}/members` 整体替换，立即生效。
- 查询时 `"pool_id": "team-a"` 表示本次从池子扣费；池子额度不足时返回
  **`429 pool_budget_exhausted`**，与个人额度不足的 `429 budget_exhausted`
  是**两个不同的错误码**，调用方可以明确区分。池子拒绝既不扣池子也不扣个人。
- 池子是多人共享的：扣减在与查询记录落盘相同的**单事务**内完成
  （进程内锁 + 文件锁），并发提交不会把池子扣超，也不会出现
  “查询做完了没记账”或“记了账却没有结果”的半完成状态。
- 池子余额、已耗与成员名单随账本持久化，重启后保留。

## 4. 预算调整（追加 / 回收）

`POST /admin/adjustments` 对个人额度或预算池做调整：

```json
{
  "adjustment_id": "adj-2026-001",
  "target_type": "principal",   // 或 "pool"
  "target_id": "alice",         // 主体名或 pool_id
  "delta_epsilon": 5.0          // 正数 = 追加；负数 = 回收；不允许为 0
}
```

- **幂等**：`adjustment_id` 是幂等键。同一次调整重复提交（网络重试等）
  只生效一次，后续提交返回与首次完全相同的结果（`replayed=true`）；
  同一 `adjustment_id` 携带不同参数返回 `409 adjustment_conflict`。
- **回收保护**：若回收后 `spent > 新预算`（会出现负余额），整笔调整被拒绝
  （`409 adjustment_rejected`），账本保持原样——不存在部分生效。
  回收到恰好等于已耗是允许的（剩余归零）。
- 调整目标必须已存在（已授权的主体 / 已创建的池子），否则
  `404 adjustment_target_not_found`。
- 调整结果与幂等记录随账本持久化，重启后重放仍然只认第一次。
- 每笔调整写日志，包含调整前后预算、已耗与判定依据：

  ```
  adjustment decision | adjustment_id=adj-1 target=principal:alice delta=5.000000 \
    budget_before=10.000000 budget_after=15.000000 spent=1.000000 basis=applied
  ```

  `basis` 取值：`applied`（已生效）、`idempotent_replay`（幂等重放）、
  `reclaim_would_overdraw_denied`（回收过度被拒）。

## 5. 多用户查询编号隔离与提交历史

- `query_id` 按 **(principal, query_id)** 命名空间隔离：不同用户可以使用
  相同的查询编号（如 `daily-count`、`q-001`），互不影响——
  不会重放到别人的结果，扣费各算各的，参数冲突判定也只作用于本人。
- `GET /history/{principal}?requester=<本人>` 查看自己的提交历史：
  - 筛选：`aggregation`、`dimension`、`status`（`completed`/`rejected`）、
    `start`/`end`（ISO 8601 时间，闭区间）；
  - 分页：`page`（从 1 开始）、`page_size`（1–100，默认 20），
    响应含 `total` 与当前页 `items`，按提交顺序排列；
  - **越权拒绝**：`requester` 必须等于路径里的 `principal`，否则
    `403 unauthorized`——只能看到自己的记录；
  - 历史条目只含元数据（编号、聚合类型、维度、ε、状态、扣费来源、时间戳），
    **不含分组结果，更不含原始精确值**；被拒绝的提交也会留痕（含错误码）。
- 加噪仍按 `query_id` 确定性派生：不同用户使用相同编号与参数时会得到
  数值相同的结果（各自独立计费，合计 ε 消耗满足串行组合上界）。

## 6. 加噪与抑制规则

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

## 7. 幂等与错误类型

以 **(principal, query_id)** 作为幂等键（不同用户的同名编号互不影响）：

- 相同 `query_id` + 相同语义参数（聚合类型、维度、数值列、ε、groups、pool_id）：
  返回首次结果的完整缓存，`replayed=true`，**不二次扣预算、结果不变**；
  仅传输层元数据 `replayed` 与首次不同，DP 载荷逐字段一致。
- 相同 `query_id` + 不同参数 → `409 query_conflict`，防止复用 ID 顶替查询。
- 预算耗尽后的重试：已完成查询仍可重放；新查询一律拒绝且不写半条记录。

可区分的错误码（`{"error": code, "message": ...}`）：

| HTTP | error code | 含义 |
| --- | --- | --- |
| 422 | `invalid_query` | 参数非法：未知维度/列、sum/avg 缺 value_column、ε 超单次上限、请求体不合法 |
| 403 | `unauthorized` | 主体未授权，或不是所指定预算池的成员，或越权查看他人历史 |
| 429 | `budget_exhausted` | **个人**剩余 ε 不足（message 含剩余与所需数值） |
| 429 | `pool_budget_exhausted` | **共享预算池**剩余额度不足（与个人不足可区分） |
| 404 | `pool_not_found` | 引用的预算池不存在 |
| 409 | `pool_conflict` | 同名预算池已存在 |
| 409 | `query_conflict` | query_id 已存在但参数不一致 |
| 409 | `adjustment_conflict` | adjustment_id 已存在但调整参数不一致 |
| 409 | `adjustment_rejected` | 回收后已耗额度将超过新预算，整笔调整被拒绝 |
| 404 | `adjustment_target_not_found` | 调整目标（主体/池子）不存在 |
| 500 | `internal_error` | 内部/持久化失败：仅返回脱敏信息与追踪 ref，堆栈只入服务日志 |

参数校验失败、未授权、预算不足都发生在任何状态修改之前，不扣预算、不改账本
（被拒绝的查询仅在历史中留一条 `rejected` 记录，不占用 query_id、不影响幂等语义）。

## 8. 并发与失败语义

- 每次提交在**单个事务**内完成「查缓存 → 鉴权 → 预算判定 → 加噪计算 →
  扣减 → 落盘（查询记录 + 历史）」。事务期间持有：
  - 进程内 `threading.RLock`（同进程多线程并发）；
  - `fcntl.flock` 排他文件锁（多进程/多 worker）。
- 账本通过临时文件 `fsync` + `os.replace` **原子替换**，读侧只见完整版本。
- 事务体抛异常时不调用落盘，内存修改整体丢弃：**不会出现扣了预算却没有查询记录，
  或落了记录却没扣预算的半完成状态**（见 `test_persistence_failure_leaves_no_half_state`）。
- 并发保证（测试用 16/12 线程压测）：
  - 同一 `query_id` 并发提交：恰好 1 次扣减、1 条记录，其余全部重放同一结果；
  - 不同 `query_id` 并发提交：成功数 × ε ≤ 预算总额，其余得到
    `budget_exhausted`，无超额扣减、无丢失、无重复计数；
  - 多人并发共用同一预算池：成功扣减总额 ≤ 池子额度，其余得到
    `pool_budget_exhausted`，每条成功记录都有对应结果、每次扣减都有对应记录。

## 9. 兼容范围（老数据与老接口）

- **老接口**：`/admin/grant`、`/budget/{principal}`、`/query`、`/health`
  的请求/响应字段与语义不变；`/query` 不传 `pool_id` 时行为与旧版完全一致
  （从个人额度扣减）。响应新增字段（如 `pool_id`）均有默认值，旧客户端可忽略。
- **老数据**：旧版账本（v1 schema：裸 query_id 键、5 元组 cache_key）在首次
  加载时**自动迁移**为 v2（按主体命名空间、补充 pools/adjustments/history），
  迁移后原样落盘。已耗预算不回滚、已完成的老查询可按原参数重放且不二次扣费，
  老记录同时补入提交历史（无时间戳，不参与时间范围筛选）。
- 迁移是单向的：v2 账本不能被旧版代码读取。

## 10. 验证方法

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
  非领域异常脱敏为 `internal_error`；
- `tests/test_pools.py`：池子创建与成员配置、池内扣减不动个人额度、
  `pool_budget_exhausted` 与 `budget_exhausted` 可区分、非成员/未知池子拒绝、
  12 线程并发共用池子不超支且账实一致、池子状态跨重启保留；
- `tests/test_adjustments.py`：追加/回收、同一调整单重复提交只生效一次且
  返回一致、参数不一致 409、回收导致负余额整笔回滚、调整跨重启保留；
- `tests/test_isolation.py`：不同用户同名 query_id 各自计费互不串结果、
  参数冲突按用户隔离、隔离性跨重启保留；
- `tests/test_history.py`：按聚合类型/维度/状态/时间范围筛选、分页遍历无重无漏、
  越权查看他人历史被拒、历史不含分组结果与精确值；
- `tests/test_migration.py`：v1 老账本自动迁移后预算不回滚、老查询可重放、
  新功能在老账本上照常工作。

### 手工验证新能力（本地）

```bash
export PQE_DATA_DIR=/tmp/pqe-demo
uv run uvicorn main:app --port 8000 &
# 授权 + 建池
curl -X POST localhost:8000/admin/grant -H 'content-type: application/json' \
  -d '{"principal": "alice", "budget_epsilon": 10}'
curl -X POST localhost:8000/admin/pools -H 'content-type: application/json' \
  -d '{"pool_id": "team-a", "budget_epsilon": 1.0, "members": ["alice"]}'
# 走池子查询：第一次成功，第二次 pool_budget_exhausted
curl -X POST localhost:8000/query -H 'content-type: application/json' \
  -d '{"query_id": "d1", "principal": "alice", "aggregation": "count",
       "dimension": "city", "epsilon": 1.0, "pool_id": "team-a"}'
# 追加池子额度（幂等：重复提交同一 adjustment_id 只生效一次）
curl -X POST localhost:8000/admin/adjustments -H 'content-type: application/json' \
  -d '{"adjustment_id": "adj-1", "target_type": "pool",
       "target_id": "team-a", "delta_epsilon": 2.0}'
# 查看自己的历史
curl 'localhost:8000/history/alice?requester=alice&status=rejected'
```

## 11. 内置数据集

首次启动确定性生成 12 行员工记录（`data/dataset.json`），
按城市的分组规模刻意设计为 5 / 4 / 2 / 1，便于验证阈值 3 的抑制边界；
另有不存在的城市取值用于验证 `empty_group`。
