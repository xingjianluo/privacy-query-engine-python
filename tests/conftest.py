"""公共夹具：每个测试使用独立临时数据目录，并开启预算判定日志。

日志会打印每次预算扣减前后的数值与判定依据（basis），
满足“单测日志中需要打印预算扣减前后的数值与判定依据”的要求。
"""
import logging
import sys
from pathlib import Path

import pytest

from app.api import create_app
from app.config import Settings
from app.dataset import load_dataset
from app.storage import Ledger
from fastapi.testclient import TestClient


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        default_budget_epsilon=10.0,
        max_query_epsilon=2.0,
        min_group_size=3,
    )


@pytest.fixture(autouse=True)
def _budget_logging(caplog: pytest.LogCaptureFixture) -> logging.Handler:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[TEST] %(name)s: %(message)s"))
    logger = logging.getLogger("pqe.service")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    caplog.set_level(logging.INFO, logger="pqe.service")
    yield handler
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
