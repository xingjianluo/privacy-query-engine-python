# 本地差分隐私聚合查询服务（Local-DP Aggregation Query Service）

基于 FastAPI 的本地差分隐私（LDP/DDP 风格）聚合查询服务。使用本地内存数据集与本地
JSON 文件持久化，模拟**隐私预算账本**与**查询审计**，保证并发查询、重复提交与预算
耗尽场景下结论一致、可复现、可解释，且不泄漏任何原始精确值。

---

## 1. 快速开始

```bash
uv sync --group dev          # 安装运行与测试依赖
uv run uvicorn main:app --port 8000   # 启动服务
uv run pytest                # 运行全部测试（含预算扣减日志）
```

健康检查：`GET /health` → `{"status":"ok"}`

### 请求示例

```bash
curl -X POST http://localhost:8000/query \
  -H 'Content-Type: application/json' \
  -d '{
    "query_id": "q-20260923-01",
    "agg": "count",
    "dimensions": ["region", "product"],
    "filters": {"gender": "M"},
    "api_key": "key-analyst"
  }'
```

凭证也可以放在请求头 `X-API-Key` 中。

### 内置凭证（`dp_engine/authorizer.py`）

| API Key | 主体 | 授权聚合 |
| --- | --- | --- |
| `key-analyst` | `analyst` | `count` / `sum` / `mean` |
| `key-counter` | `counter` | `count` |

---

## 2. 隐私预算模型

- 每个**主体（principal）**拥有独立的总预算 `B`，默认 `B = 3.0`（以 ε 计）。
- 每次查询按其**敏感度 Δ** 扣减预算：

  ```
  cost(query) = unit_epsilon × Δ
  ```

  其中 `unit_epsilon` 默认 `0.1`，敏感度按聚合类型计算（数值列先裁剪到公开定义域 `[L, U]`）：

  | 聚合 | 敏感度 Δ | 默认成本 |
  | --- | --- | --- |
  | `count` | `1` | `0.1` |
  | `sum` | `U − L` | `0.1 × (U−L)` |
  | `mean` | `(U − L) / k`（k 为最小分组阈值的保守分母） | `0.1 × (U−L)/k` |

- 判定规则：`已用 + cost ≤ B` 才扣减；否则**拒绝且不扣减**，返回
  `402 budget_exhausted`，错误体带 `required_budget` 与 `remaining_budget`。
- **预算账本跨重启保留**：状态持久化在单个 JSON 文件（`DP_STATE_PATH`，
  默认 `./data/state.json`），重启后已消耗预算与历史裁决仍然生效。
- 预算参数环境变量：`DP_BUDGET_TOTAL`、`DP_UNIT_EPSILON`、`DP_MIN_GROUP_SIZE`、
  `DP_STATE_PATH`。

数值列定义域（`dp_engine/dataset.py`）：`units ∈ [1,10]`（宽 9）、`score ∈ [0,1]`（宽 1）。

---

## 3. 加噪规则

- 机制：**Laplace 机制**。每个通过抑制检查的输出分组独立加噪：

  ```
  epsilon = cost = unit_epsilon × Δ
  scale   = Δ / epsilon = 1 / unit_epsilon      （默认 10）
  noisy   = exact_clipped_aggregate + Laplace(scale)
  ```

- **确定性噪声**：噪声随机数种子由 `SHA256(query_id | 分组键)` 派生。
  因此：
  - 同一 `query_id` + 相同参数的任何重试，返回**逐位一致**的加噪结果；
  - 不同 `query_id` 使用不同噪声，多次查询不会相互抵消；
  - 不依赖外部随机源，结果可复现、可审计。
- `sum`/`mean` 的输入在聚合前按公开列定义域**裁剪**，保证敏感度有界。

---

## 4. 抑制规则（不泄漏精确值）

- **小分组抑制**：分组真实行数 `n < k`（默认 `k = 3`）时，该分组返回
  `status="suppressed"`、`suppress_reason="group_below_min_size"`、`value=null`。
- **空结果抑制**：过滤后没有任何行时，整条查询返回
  `status="suppressed"`、`suppress_reason="no_rows_match_filter"`、`groups=[]`。
- 任何被抑制的分组都**不返回真实行数**（`group_size` 恒为 `null`），也不返回精确
  聚合值；阈值 `k` 通过响应中的 `suppression_threshold` 公开（阈值本身不是秘密）。
- 正常分组的 `value` 只包含**加噪后**的值；任何响应路径都不会出现未加噪的精确值。

内置数据集共 10 行，`region×product` 分组规模为 `north/A=4、north/B=2、
south/A=3、south/B=1`，天然覆盖「通过 / 边界 / 抑制」三种情形。

---

## 5. 幂等、重试与查询标识

- 客户端必须为每个业务查询提供唯一的 `query_id`。服务端以
  `(principal, query_id)` 为幂等键，记录该查询的**参数指纹**
  （agg/column/dimensions/filters 的规范化 JSON）与**完整响应裁决**。
- 相同 `query_id` + 相同参数：回放缓存结果，`cached=true`，**不再扣减预算**，
  结果载荷（分组、噪声值、状态）与首次完全一致。
- 相同 `query_id` + 不同参数：返回 `409 query_conflict`（只读拒绝，不扣预算、
  不留记录）。
- 幂等空间按主体隔离：不同主体可各自使用相同的 `query_id`，互不可见、各自计费。
- **预算不足的拒绝也会被缓存**：被拒绝的 `query_id` 重试时，始终得到同样的
  `budget_exhausted` 裁决（`remaining_budget` 反映当前余额），不会因为后续预算
  变化而产生不同结论。

---

## 6. 并发语义与事务保证

- 一次查询在一个**持锁事务**内完成「读状态 → 幂等/冲突判定 → 预算判定 →
  加噪聚合 → 写账本/幂等记录/审计 → 原子落盘」。
- 锁：进程内 `threading.RLock` + 跨进程 `fcntl.flock` 排他文件锁。FastAPI 线程池
  并发与多进程（多 worker）并发都会被串行化。
- 落盘：临时文件 + `fsync` + `os.replace`，主状态文件永远是完整的某一版。
- 因此在任意并发度下保证：
  - **无超额扣减**：预算边界上成功的查询数严格等于 `floor(B / cost)`；
  - **无丢失记录**：每个首次出现的 `query_id`（无论成功还是被预算拒绝）恰有一条裁决；
  - **无重复计数**：相同 `query_id` 的并发重试只有一次计费；
  - **无半完成状态**：聚合或持久化失败时事务整体回滚，预算、幂等记录、审计
    三者要么全部生效，要么全部不生效。
- 加噪聚合在事务内、扣预算之前执行：计算失败时不会扣预算。

---

## 7. 错误类型（稳定错误码，彼此可区分）

统一错误信封：`{"error": {"code": ..., "message": ...}}`

| HTTP | code | 触发条件 |
| --- | --- | --- |
| 401 | `unauthorized` | 缺少/无法识别 API Key |
| 403 | `forbidden` | 已认证但主体无权使用该聚合；跨主体查审计 |
| 422 | `invalid_parameter` | pydantic 类型错误，或语义非法（sum/mean 缺数值列、未知列、重复维度等） |
| 402 | `budget_exhausted` | 预算不足（不扣预算）；含 `required_budget`、`remaining_budget` |
| 409 | `query_conflict` | `query_id` 被不同参数占用 |
| 500 | `internal_error` | 基础设施/未预期异常：对外仅固定文案（未预期异常附 `incident` 用于对账） |

**内部异常细节（堆栈、cause、注入故障信息）只写服务端日志，绝不回传客户端。**

---

## 8. 审计

`GET /audit?principal=<主体>`（携带本人 API Key）返回该主体的全部审计条目，
每条包含：`query_id`、`agg`、参数指纹、`sensitivity`、`charged_budget`、
`budget_before`、`budget_after`、`decision`（`charged` / `rejected_budget` /
`cached`）、判定依据 `detail` 与 UTC 时间戳。

服务端同时通过 `dp_engine.budget` logger 打印每次预算判定，例如：

```
预算判定 query_id=q1 principal=analyst 判定依据=扣减 | 敏感度=1.000000
unit_epsilon=0.100000 需要成本=0.100000 | 扣减前已用=0.000000 总预算=3.000000
剩余=3.000000 扣减后已用=0.100000
```

---

## 9. 验证方法

```bash
uv run pytest -s
```

测试矩阵（共 28 个用例，分 5 个文件）：

| 文件 | 覆盖内容 |
| --- | --- |
| `tests/test_budget.py` | 成本按敏感度计算；预算恰好耗尽；`budget_exhausted` 与其他错误码可区分；拒绝不计费且有审计；断言日志含扣减前后数值与判定依据 |
| `tests/test_idempotency.py` | 重复提交结果逐字段一致且不重复扣费；缓存跨重启保留；参数冲突 409；按主体隔离；拒绝裁决幂等；不同 id 噪声独立 |
| `tests/test_concurrency.py` | 12~16 线程并发：不同 id 无丢失/同 id 只计一次/预算边界 30 成功 10 拒绝；**4 进程跨 `flock` 并发**合计恰好 30 次成功、40 条裁决无丢失 |
| `tests/test_suppression.py` | 小分组（n=1/2）与边界（n=k=3）抑制；无匹配数据整体抑制；sum/mean/count 均不回显精确值与真实行数 |
| `tests/test_errors_persistence.py` | 401/403/422/402/500 错误码区分；预算与缓存跨重启；计算失败/写盘失败/读盘失败的事务原子性；500 响应不含内部细节 |

### 代码结构

```
dp_engine/
  config.py       环境变量配置（预算/阈值/状态路径）
  errors.py       可区分错误类型（code + http_status，内部细节仅进 cause）
  models.py       请求/响应/持久化状态的 pydantic 模型
  dataset.py      本地数据集与数值列公开定义域
  dp_engine.py    敏感度、预算成本、确定性 Laplace 噪声、分组聚合与抑制
  authorizer.py   API Key 认证与聚合类型授权
  storage.py      JSON 文件持久化：RLock + fcntl 锁、原子写、事务、故障注入
  service.py      查询编排：认证→授权→校验→幂等→预算→聚合→原子提交→审计
  api.py          FastAPI 装配、统一错误信封、/query 与 /audit
tests/            上述五组测试与公共夹具
```
