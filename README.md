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
| POST | `/query` | 提交 DP 聚合查询（可选 `pool_id` 从共享池扣费） |
| POST | `/admin/pool` | 创建共享预算池或整体替换成员名单 |
| GET | `/pool/{pool_id}` | 查看池成员与额度（总额/已耗/剩余） |
| POST | `/admin/adjust` | 预算调整（追加/回收，按调整单号幂等） |
| GET | `/history/{principal}?viewer=...` | 查看本人提交历史（筛选+分页，仅本人可见） |
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
- `pool_id`：可选。缺省从个人额度扣费；给出时从该共享预算池扣费
  （要求 `principal` 在池成员名单内）。

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
    before=2.500000 required=1.000000 after=1.500000 basis=charged source=personal
  budget decision | query_id=b3 principal=bob replayed=false \
    before=0.500000 required=1.000000 after=0.500000 basis=insufficient_budget_denied source=personal
  ```

  `basis` 取值：`charged`（成功扣减）、`insufficient_budget_denied`（个人预算不足拒绝）、
  `insufficient_pool_budget_denied`（池额度不足拒绝）、
  `idempotent_cache_hit`（幂等重放，不扣预算）。
  `source` 取值：`personal` 或 `pool:<pool_id>`，标明扣费账户。

## 2.1 共享预算池（team pool）

团队可以把一部分额度集中成共享预算池，由成员共同消耗：

- `POST /admin/pool` 创建池：`{"pool_id": "team-a", "members": ["alice", "bob"],
  "budget_epsilon": 5.0}`。对已存在的池再次调用只**整体替换成员名单**，
  请求里的 `budget_epsilon` 被忽略——池额度只能经 `/admin/adjust` 变更，
  保证每次额度变动都有调整单号可查。
- 查询时带 `pool_id` 即从池子扣费：要求 `principal` 在成员名单内，
  否则 `403 pool_access_denied`；池不存在则 `404 unknown_pool`。
  池成员不要求个人已授权（成员名单即访问控制）。
- 池额度不足时返回 `429 pool_budget_exhausted`，与个人额度不足的
  `429 budget_exhausted` 是**两个可区分的错误码**，日志 `basis` 同样区分。
- 池是多人共享账户：所有扣减在同一事务锁内判定+扣减+落盘，
  并发提交不会扣超（成功扣减总额 ≤ 池额度），也不会出现
  “查询做完了没记账”或“记了账没结果”的半完成状态。
- 响应里的 `budget_source`（`personal`/`pool`）与 `pool_id` 标明本次扣费来源；
  `remaining_budget` 对池查询指**池的剩余额度**。

## 2.2 预算调整（追加 / 回收）

`POST /admin/adjust` 对个人额度或预算池做追加（`delta>0`）与回收（`delta<0`）：

```json
{"adjustment_id": "adj-2026-001", "target_type": "pool", "target": "team-a", "delta": 3.0}
```

- `adjustment_id` 是幂等键：同一单号相同参数重复提交（如网络重试）
  **只生效一次**，返回与首次完全一致的结果（`replayed=true`）；
  同一单号不同参数 → `409 adjustment_conflict`。
- 回收安全性：若 `新额度 < 已消耗`，整笔调整拒绝
  （`422 adjustment_rejected`，message 含已消耗与调整后数值），
  **账本保持原样**——不会出现负余额或一半生效。
  被拒绝的单号未占位，修正参数后可复用同一单号重新提交。
- 调整目标必须存在：个人须已授权、池须已创建，否则 `422 adjustment_rejected`。
- 每次调整（生效 / 幂等重放 / 拒绝）都写日志，含调整前后额度与已消耗：

  ```
  budget adjustment | adjustment_id=adj-1 target=pool:team-a delta=3.000000 \
    before=5.000000 after=8.000000 spent=1.000000 basis=applied
  ```

- 调整记录与查询消耗一样持久化在 `ledger.json`，重启后重放仍命中幂等缓存。

### 跨重启持久化

预算余额、授权状态、预算池、调整记录、已完成查询与提交历史全部持久化在
`data/ledger.json`，重启后从磁盘恢复：已耗预算不会回滚，已完成查询可直接重放，
已生效的调整单号重启后重试仍命中幂等缓存。

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

## 4. 幂等、多用户隔离与错误类型

以 `(principal, query_id)` 作为幂等键——**查询编号按用户隔离**：

- 不同用户可以使用同一个 `query_id`（如 `daily-count`、`q-001`）：
  各自独立执行、各自扣费、各自缓存，**不会拿到别人的结果**，
  参数冲突判定也互不影响。
- 相同 `query_id` + 相同语义参数（聚合类型、维度、数值列、ε、groups、扣费来源）：
  返回首次结果的完整缓存，`replayed=true`，**不二次扣预算、结果不变**；
  仅传输层元数据 `replayed` 与首次不同，DP 载荷逐字段一致。
- 相同 `query_id` + 不同参数 → `409 query_conflict`，防止复用 ID 顶替查询。
- 预算耗尽后的重试：已完成查询仍可重放；新查询一律拒绝且不写半条记录。

### 查询历史

`GET /history/{principal}?viewer=<principal>` 查看提交历史：

- **仅本人可见**：`viewer` 必须与路径中的 `principal` 一致，否则 `403 unauthorized`；
- 支持筛选：`aggregation`、`dimension`、`status`（`completed`/`replayed`/`rejected`）、
  `start`/`end`（epoch 秒时间范围）；支持 `page`/`page_size` 分页，
  响应含 `total` 与当前页 `items`；
- 成功、重放与被拒（含参数非法、未授权、预算不足）的提交都会入历史；
- 历史条目只携带**加噪结果与抑制原因**，绝不包含原始精确值。
- **旧账本升级补录的老记录**：升级前版本没有历史分区，这些成功查询在迁移时
  补录进提交者本人历史，条目带 `"legacy": true` 且 `"created_at": null`
  （老数据没有提交时间，不做任何时间猜测）。呈现与筛选规则：
  - **不加时间范围**（不带 `start`/`end`）：老记录纳入本人历史，按用户归位、
    不会串到别人，排序时排在有时间的新记录之前，分页同样可见；
  - **指定任意时间范围**（只给 `start`、只给 `end` 或两者都给）：老记录一律
    排除——它们没有真实提交时间，不会被塞进任何时间段；该规则不影响有时间的
    新记录原本的时间筛选行为；
  - 老记录在历史中状态为 `completed`，因此按 `status=completed/replayed` 等
    非时间条件筛选时仍可命中；按 `status=rejected` 筛选不会命中；
  - 迁移补录幂等：反复加载/重启同一条老记录只补一次；补录只读取老查询记录里
    已持久化的加噪响应，不改动预算与查询账实，老查询仍可原样重放
    （结果不变、不重复扣费）。用户升级后主动重放老查询，会按常规产生一条新的
    `replayed` 历史事件，与补录的 `legacy` 条目并存、互不影响。

可区分的错误码（`{"error": code, "message": ...}`）：

| HTTP | error code | 含义 |
| --- | --- | --- |
| 422 | `invalid_query` | 参数非法：未知维度/列、sum/avg 缺 value_column、ε 超单次上限、请求体不合法 |
| 403 | `unauthorized` | 主体未授权，或越权查看他人历史 |
| 403 | `pool_access_denied` | 主体不在所请求预算池的成员名单内 |
| 404 | `unknown_pool` | 引用了不存在的预算池 |
| 429 | `budget_exhausted` | 个人剩余 ε 不足（message 含剩余与所需数值） |
| 429 | `pool_budget_exhausted` | 池剩余 ε 不足（与个人不足可区分） |
| 409 | `query_conflict` | query_id 已存在但参数不一致 |
| 409 | `adjustment_conflict` | 调整单号已存在但参数不一致 |
| 422 | `adjustment_rejected` | 调整被整体拒绝（回收将低于已消耗、目标不存在、delta 为 0） |
| 500 | `internal_error` | 内部/持久化失败：仅返回脱敏信息与追踪 ref，堆栈只入服务日志 |

参数校验失败、未授权、预算不足都发生在任何状态修改之前，不扣预算、不留查询记录
（仅留一条被拒绝的历史事件）。

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

## 6. 兼容范围

- **老接口**：`/admin/grant`、`/budget/{principal}`、`/query`、`/health`
  的请求/响应字段保持不变；`/query` 不带 `pool_id` 时行为与旧版完全一致
  （从个人额度扣费），响应新增字段（`budget_source`、`pool_id`）为纯增量。
- **老数据**：旧版 `ledger.json` 加载时自动就地迁移（幂等，可反复执行）：
  - 扁平的 `queries` 记录按记录内的 `principal` 归位到按用户隔离的结构，
    旧记录仍可正常重放、不会重复扣费（JSON 读回的 list 型 `cache_key`
    在迁移时归一化为 tuple 比较，老查询重放不会被误判 409 参数冲突）；
  - 旧版 5 分量 `cache_key` 自动补上 `pool_id=None` 分量；
  - 补齐 `pools` / `adjustments` / `history` 三个新分区；
  - **旧版成功查询自动补录进本人历史**（`legacy=true`、`created_at=null`）：
    不带时间范围可见、带时间范围排除，按提交用户归位，详见上文「查询历史」；
    反复加载/重启不重复补录，不影响老查询的重放与扣费，补录内容只有加噪结果
    与抑制原因。

## 7. 验证方法

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
- `tests/test_pools.py`：池成员控制、池/个人额度不足错误码可区分、
  池重放不重复扣费、12 线程多成员并发共用一池不超支且账实相符、
  不带 `pool_id` 的老用法不变；
- `tests/test_adjustments.py`：追加/回收、同一调整单号重复提交只生效一次且
  结果一致、回收导致负余额时整笔回滚且单号可复用、池额度调整、
  调整记录跨重启保留；
- `tests/test_multiuser.py`：不同用户同名 `query_id` 互不串结果与扣费、
  旧版扁平账本自动迁移后旧记录可重放、新查询按用户隔离；
- `tests/test_history.py`：按聚合/维度/状态/时间范围筛选、分页、
  越权查看他人历史被拒、历史条目不携带精确值、被拒查询也入历史；
- `tests/test_legacy_history.py`：**旧账本升级兼容**（用升级前真实版本代码
  生成的 `tests/fixtures/ledger.json` 造数）：不加时间范围老记录可见且按用户
  归位、带任意时间范围老记录被排除、与新记录混合时筛选行为不变、反复
  加载/重启不重复补录且预算账实不变、老查询原样重放不二次扣费、新用户用
  相同编号不受影响、补录条目不含原始精确值。
- 跑测试时日志分两类前缀：`[TEST-COVERAGE]` 在会话开始时按模块列出覆盖的
  业务要点，`[TEST-RESULT]` 逐条打印每个用例的 PASS/FAIL/ERROR 与耗时。

### 本地验证旧账本升级后的历史兼容

测试夹具 `tests/fixtures/ledger.json` 由升级前版本（git 历史中 pools/history
特性之前的提交）的真实服务代码生成，是一份扁平 `queries`、无 `history`
分区、记录无 `created_at` 的旧账本。复现与验证步骤：

```bash
# 1) 直接运行兼容用例（会把真实旧账本复制到临时目录后启动新版）
uv run pytest tests/test_legacy_history.py -v

# 2) 手工端到端：拿这份旧账本启动新版服务
mkdir -p /tmp/pqe-demo
cp tests/fixtures/ledger.json tests/fixtures/dataset.json /tmp/pqe-demo/
PQE_DATA_DIR=/tmp/pqe-demo uv run uvicorn main:app --port 8000

# 3) 另开终端验证
# 不加时间范围：alice 的 2 条老记录可见（legacy=true, created_at=null）
curl 'localhost:8000/history/alice?viewer=alice'
# 加任意时间范围：老记录被排除，total=0
curl 'localhost:8000/history/alice?viewer=alice&start=0'
# 老查询原样重放：replayed=true、结果不变、已耗预算不增加
curl -X POST localhost:8000/query -H 'content-type: application/json' \
  -d '{"query_id":"legacy-count-1","principal":"alice","aggregation":"count",
       "dimension":"city","epsilon":1.0,
       "groups":["Beijing","Shanghai","Shenzhen","Nowhere"]}'
curl localhost:8000/budget/alice      # spent_epsilon 仍为 2.0
# 重启服务后再查历史：老记录仍只有一份，不重复补录
```

### 手动验证新能力

```bash
uv run uvicorn main:app --reload   # 另开终端执行下列命令

# 1) 建池（alice/bob 共用 5.0），成员走池子查询
curl -X POST localhost:8000/admin/pool -H 'content-type: application/json' \
  -d '{"pool_id":"team-a","members":["alice","bob"],"budget_epsilon":5.0}'
curl -X POST localhost:8000/query -H 'content-type: application/json' \
  -d '{"query_id":"q-001","principal":"alice","aggregation":"count","dimension":"city","epsilon":1.0,"pool_id":"team-a"}'
curl localhost:8000/pool/team-a

# 2) 预算调整：追加 3.0；同一单号重试返回相同结果且不重复生效
curl -X POST localhost:8000/admin/adjust -H 'content-type: application/json' \
  -d '{"adjustment_id":"adj-001","target_type":"pool","target":"team-a","delta":3.0}'

# 3) 另一用户用同一编号查询（互不影响），再查看各自历史
curl -X POST localhost:8000/query -H 'content-type: application/json' \
  -d '{"query_id":"q-001","principal":"bob","aggregation":"count","dimension":"city","epsilon":1.0,"pool_id":"team-a"}'
curl 'localhost:8000/history/alice?viewer=alice&status=completed&page=1&page_size=10'
```

## 8. 内置数据集

首次启动确定性生成 12 行员工记录（`data/dataset.json`），
按城市的分组规模刻意设计为 5 / 4 / 2 / 1，便于验证阈值 3 的抑制边界；
另有不存在的城市取值用于验证 `empty_group`。
