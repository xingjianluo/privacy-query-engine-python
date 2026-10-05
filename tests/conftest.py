"""公共夹具：每个测试使用独立临时数据目录，并开启预算判定日志。

日志会打印每次预算扣减前后的数值与判定依据（basis），
满足“单测日志中需要打印预算扣减前后的数值与判定依据”的要求。

另外通过 pytest 终端摘要钩子，逐用例打印执行状态（PASS/FAIL/SKIP），
并在会话开始时打印各测试文件覆盖的业务，方便从单测日志看出覆盖范围。
"""
import logging
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings
from app.dataset import load_dataset
from app.storage import Ledger

# 各测试文件对应的业务覆盖说明（pytest 启动时打印）
BUSINESS_COVERAGE = {
    "test_budget.py": "预算模型：耗尽拒绝不扣费、非法参数分类、授权校验、avg 计费",
    "test_idempotency.py": "幂等：重复提交载荷一致只扣一次、参数冲突 409、跨重启保留",
    "test_concurrency.py": "并发：同 ID 单次扣减、不同 ID 不超支、落盘失败无半完成状态",
    "test_privacy.py": "隐私：空/小分组抑制、阈值边界、噪声确定性、不泄漏精确值",
    "test_api.py": "HTTP 全链路：错误码映射、非法 JSON/内部异常脱敏",
    "test_pools.py": "共享预算池：成员控制、错误码区分、池重放、并发共用不超支",
    "test_adjustments.py": "预算调整：追加/回收、单号幂等、负余额回滚、跨重启保留",
    "test_multiuser.py": "多用户隔离：同名 query_id 不串结果扣费、旧账本迁移可重放",
    "test_history.py": "查询历史：筛选/分页/越权拒绝/不带精确值/拒绝也入历史",
    "test_legacy_history.py": (
        "旧账本兼容：无时间范围可见老记录、有时间范围排除、反复加载不重复补录"
        "不重复扣费、老查询可重放、新用户同编号隔离、仅加噪载荷"
    ),
}


def pytest_configure(config: pytest.Config) -> None:
    print("\n========== 业务覆盖说明 ==========", file=sys.stdout)
    for module, coverage in BUSINESS_COVERAGE.items():
        print(f"[COVERAGE] {module}: {coverage}", file=sys.stdout)
    print("==================================\n", file=sys.stdout)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo):
    """逐用例打印执行结果（call 阶段：通过/失败/跳过）。"""
    outcome = yield
    report = outcome.get_result()
    if report.when != "call":
        return
    if report.passed:
        mark = "PASS"
    elif report.failed:
        mark = "FAIL"
    elif report.skipped:
        mark = "SKIP"
    else:
        mark = "OTHER"
    print(f"[CASE] {mark} {item.nodeid}", file=sys.stdout)


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        default_budget_epsilon=10.0,
        max_query_epsilon=2.0,
        min_group_size=3,
    )


@pytest.fixture(autouse=True)
def _business_logging(caplog: pytest.LogCaptureFixture) -> logging.Handler:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[TEST] %(name)s: %(message)s"))
    # pqe.service：预算扣减前后数值与判定依据；
    # pqe.compat：旧账本兼容用例打印覆盖的业务场景。
    for name in ("pqe.service", "pqe.compat"):
        logger = logging.getLogger(name)
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        caplog.set_level(logging.INFO, logger=name)
    yield handler
    for name in ("pqe.service", "pqe.compat"):
        logging.getLogger(name).removeHandler(handler)


@pytest.fixture()
def ledger(settings: Settings) -> Ledger:
    return Ledger(settings.ledger_path, settings.lock_path)


@pytest.fixture()
def dataset(settings: Settings):
    return load_dataset(settings.dataset_path)


@pytest.fixture()
def client(settings: Settings) -> TestClient:
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def granted_client(client: TestClient) -> TestClient:
    """已授权 alice（预算 10）与 bob（预算 2.5）的客户端。"""
    client.post("/admin/grant", json={"principal": "alice", "budget_epsilon": 10.0})
    client.post("/admin/grant", json={"principal": "bob", "budget_epsilon": 2.5})
    return client
