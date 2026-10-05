"""公共夹具：每个测试使用独立临时数据目录，并开启预算判定日志。

日志会打印每次预算扣减前后的数值与判定依据（basis），
满足“单测日志中需要打印预算扣减前后的数值与判定依据”的要求。

此外在测试会话开始时汇总每个测试模块覆盖的业务（从模块 docstring 的
“覆盖业务”段提取），并在用例开始/结束时逐条打印执行情况，
满足“跑单测时日志要能看出覆盖了哪些业务，以及每个用例的执行情况”。
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

# 让 tests 包之外也能引用 tests 目录（如手工脚本）
TESTS_DIR = Path(__file__).parent
coverage_logger = logging.getLogger("pqe.test.coverage")
result_logger = logging.getLogger("pqe.test.result")


def _module_business_summary(module: object) -> list[str]:
    """从测试模块 docstring 的“覆盖业务：”段提取业务要点。"""
    doc = getattr(module, "__doc__", None) or ""
    lines: list[str] = []
    in_section = False
    for raw in doc.splitlines():
        line = raw.strip()
        if line.startswith("覆盖业务"):
            in_section = True
            continue
        if not in_section:
            continue
        if not line:
            # 段落内空行容忍；遇到后续标题（以中文冒号结尾）则结束
            continue
        if line.endswith("：") and len(lines) > 0:
            break
        # 去掉列表序号前缀（如 "1. " / "- "）
        cleaned = line
        if len(cleaned) >= 2 and cleaned[0].isdigit():
            dot = cleaned.find(".")
            if 0 < dot <= 2:
                cleaned = cleaned[dot + 1 :].strip()
        if cleaned.startswith(("- ", "* ")):
            cleaned = cleaned[2:].strip()
        lines.append(cleaned)
    return lines


def pytest_configure(config: pytest.Config) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[TEST-COVERAGE] %(message)s"))
    coverage_logger.addHandler(handler)
    coverage_logger.setLevel(logging.INFO)

    result_handler = logging.StreamHandler(sys.stdout)
    result_handler.setFormatter(logging.Formatter("[TEST-RESULT] %(message)s"))
    result_logger.addHandler(result_handler)
    result_logger.setLevel(logging.INFO)


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """收集完用例后，按模块打印“覆盖了哪些业务”。"""
    counts: dict[str, int] = {}
    for item in items:
        counts[item.module.__name__] = counts.get(item.module.__name__, 0) + 1
    seen: set[str] = set()
    for item in items:
        name = item.module.__name__
        if name in seen:
            continue
        seen.add(name)
        summary = _module_business_summary(item.module)
        coverage_logger.info("模块 %s 共 %d 个用例", name, counts[name])
        if summary:
            for point in summary:
                coverage_logger.info("  覆盖业务 %s", point)
        else:
            coverage_logger.info("  覆盖业务（见用例命名）")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):  # type: ignore[no-untyped-def]
    outcome = yield
    report = outcome.get_result()
    if report.when == "setup" and report.outcome == "skipped":
        result_logger.info("SKIP  %s", item.nodeid)
    elif report.when == "call":
        if report.passed:
            result_logger.info("PASS  %s (%.3fs)", item.nodeid, report.duration)
        elif report.failed:
            result_logger.info("FAIL  %s (%.3fs)", item.nodeid, report.duration)
    elif report.when == "setup" and report.failed:
        result_logger.info("ERROR %s", item.nodeid)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    result_logger.info(
        "测试会话结束：%d 个用例收集，退出码 %s",
        len(session.items),
        exitstatus,
    )


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
    """打开预算扣减与账本迁移的业务日志（含补录 count 与扣减前后数值）。"""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[TEST] %(name)s: %(message)s"))
    loggers = [logging.getLogger("pqe.service"), logging.getLogger("pqe.storage")]
    for logger in loggers:
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        caplog.set_level(logging.INFO, logger=logger.name)
    yield handler
    for logger in loggers:
        logger.removeHandler(handler)


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
