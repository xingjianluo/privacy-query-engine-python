"""旧版账本兼容：升级后老查询历史补录、时间范围排除、幂等补录、重放与隔离。

账本夹具 tests/fixtures/legacy_ledger.json 由
tools/make_legacy_ledger.py 用升级前版本（git a61b8d2）的真实代码生成，
内容是旧版服务实际写出的 ledger.json：扁平 queries、5 分量 cache_key、
记录无 created_at，包含两名老用户 alice（3 条成功查询）与 carol（1 条）。
"""
import json
import logging
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.config import Settings

FIXTURE = Path(__file__).parent / "fixtures" / "legacy_ledger.json"

logger = logging.getLogger("pqe.compat")


@pytest.fixture()
def legacy_dir(settings: Settings) -> Path:
    """把真实旧版账本放进临时数据目录。"""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURE, settings.ledger_path)
    return settings.data_dir


@pytest.fixture()
def legacy_client(settings: Settings, legacy_dir: Path) -> TestClient:
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


def _count(client: TestClient, query_id: str, principal: str, epsilon: float = 1.0):
    return client.post(
        "/query",
        json={
            "query_id": query_id,
            "principal": principal,
            "aggregation": "count",
            "dimension": "city",
            "epsilon": epsilon,
        },
    )


def test_legacy_records_visible_without_time_range(legacy_client):
    """业务：不加时间范围时，旧版成功查询出现在本人历史里，且归属正确。"""
    logger.info("兼容场景: 旧版成功查询在无时间范围历史中可见且按用户归位")
    r = legacy_client.get("/history/alice", params={"viewer": "alice"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    ids = [i["query_id"] for i in body["items"]]
    assert sorted(ids) == [
        "legacy-count-all",
        "legacy-count-suppressed",
        "legacy-sum-age",
    ]
    # 全部归属 alice，绝不串到别人
    assert all(i["query_id"] for i in body["items"])
    r = legacy_client.get("/history/carol", params={"viewer": "carol"})
    assert r.json()["total"] == 1
    assert [i["query_id"] for i in r.json()["items"]] == ["q-001"]
    # alice 看不到 carol 的记录，反之亦然
    assert "q-001" not in ids
    logger.info("兼容场景通过: alice=3 条, carol=1 条, 互不串号")


def test_legacy_records_carry_only_noisy_payload(legacy_client):
    """业务：补录历史只带加噪结果与抑制原因，绝不含原始精确值。"""
    logger.info("兼容场景: 补录条目不泄露精确值，抑制原因保留")
    items = {
        i["query_id"]: i
        for i in legacy_client.get("/history/alice", params={"viewer": "alice"}).json()["items"]
    }
    # count 全表查询：带噪计数，sum 不附带计数
    all_groups = {g["group"]: g for g in items["legacy-count-all"]["results"]}
    assert all_groups["Beijing"] == {"group": "Beijing", "value": 5.0, "noisy_count": 5}
    assert items["legacy-sum-age"]["results"][0]["noisy_count"] is None
    # 抑制原因原样补录（小分组 + 空分组），且没有精确行数字段
    sup = items["legacy-count-suppressed"]["suppressed"]
    reasons = sorted((s["group"], s["reason"]) for s in sup)
    assert ("Nowhere", "empty_group") in reasons
    assert ("Shanghai", "small_group") in reasons
    assert ("Shenzhen", "small_group") in reasons
    assert all(set(s.keys()) <= {"group", "reason", "detail"} for s in sup)
    blob = json.dumps(items, ensure_ascii=False)
    # 精确分组行数（Shanghai=2/Shenzhen=1）绝不出现
    assert '"exact"' not in blob.lower()


def test_legacy_records_excluded_when_time_range_given(legacy_client):
    """业务：老记录没有提交时间；一旦指定起止时间就必须排除。"""
    logger.info("兼容场景: 指定任意时间范围时老记录被排除，时间筛选行为不被破坏")
    base = {"viewer": "alice"}
    assert legacy_client.get("/history/alice", params=base).json()["total"] == 3
    # 只给 start / 只给 end / 两者都给：都排除老记录
    assert (
        legacy_client.get("/history/alice", params={**base, "start": 0.0}).json()["total"]
        == 0
    )
    assert (
        legacy_client.get("/history/alice", params={**base, "end": 99999999999.0}).json()[
            "total"
        ]
        == 0
    )
    assert (
        legacy_client.get(
            "/history/alice",
            params={**base, "start": 0.0, "end": 99999999999.0},
        ).json()["total"]
        == 0
    )
    # 新增一条带真实时间的新查询后：时间范围内只看得到新记录
    assert _count(legacy_client, "new-after-upgrade", "alice").status_code == 200
    r = legacy_client.get(
        "/history/alice", params={**base, "start": 0.0, "end": 99999999999.0}
    )
    assert [i["query_id"] for i in r.json()["items"]] == ["new-after-upgrade"]
    # 不带时间范围时新老记录都在
    r = legacy_client.get("/history/alice", params=base)
    assert r.json()["total"] == 4
    logger.info("兼容场景通过: 时间范围仅返回新记录, 无范围时新老合计 4 条")


def test_repeated_load_backfills_once_and_never_recharges(settings: Settings, legacy_dir):
    """业务：反复加载/重启不重复补录、不重复扣费。"""
    logger.info("兼容场景: 反复重启幂等补录且预算账实不变")

    def _reload_and_check() -> dict:
        with TestClient(create_app(settings)) as c:
            assert c.get("/history/alice", params={"viewer": "alice"}).json()["total"] == 3
            assert c.get("/history/carol", params={"viewer": "carol"}).json()["total"] == 1
            budget_a = c.get("/budget/alice").json()
            budget_c = c.get("/budget/carol").json()
        return {
            "a_spent": budget_a["spent_epsilon"],
            "c_spent": budget_c["spent_epsilon"],
        }

    first = _reload_and_check()
    for _ in range(3):
        again = _reload_and_check()
        assert again == first == {"a_spent": 3.5, "c_spent": 1.0}

    disk = json.loads(settings.ledger_path.read_text(encoding="utf-8"))
    legacy_entries = [e for e in disk["history"] if e.get("legacy")]
    assert len(legacy_entries) == 4
    assert sorted((e["principal"], e["query_id"]) for e in legacy_entries) == [
        ("alice", "legacy-count-all"),
        ("alice", "legacy-count-suppressed"),
        ("alice", "legacy-sum-age"),
        ("carol", "q-001"),
    ]
    logger.info("兼容场景通过: 4 条补录, alice spent=3.5, carol spent=1.0 始终不变")


def test_legacy_query_replays_unchanged_without_charge(legacy_client):
    """业务：老查询仍可原样重放，结果不变、不重复扣预算。"""
    logger.info("兼容场景: 老查询原样重放、结果逐字段一致、不二次扣费")
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    expected = fixture["queries"]["legacy-count-all"]["response"]

    r = _count(legacy_client, "legacy-count-all", "alice")
    assert r.status_code == 200
    body = r.json()
    assert body["replayed"] is True
    assert body["results"] == [
        {"group": g["group"], "value": g["value"], "noisy_count": g["noisy_count"]}
        for g in expected["results"]
    ]
    # 抑制列表也与旧版响应逐字段一致（该查询未限定 groups，小分组被抑制）
    assert body["suppressed"] == expected["suppressed"]
    budget = legacy_client.get("/budget/alice").json()
    assert budget["spent_epsilon"] == 3.5  # 仍是旧账本里的 1.0+1.0+1.5
    # 再重放一次仍不扣费；重放本身会记一条 replayed 历史，但不会重复补录老记录
    r = _count(legacy_client, "legacy-count-all", "alice")
    assert r.json()["replayed"] is True
    assert legacy_client.get("/budget/alice").json()["spent_epsilon"] == 3.5
    r = legacy_client.get(
        "/history/alice",
        params={"viewer": "alice", "status": "completed"},
    )
    assert r.json()["total"] == 3  # 老补录仍各一条，没有翻倍
    logger.info("兼容场景通过: 重放结果一致, spent 维持 3.5")


def test_new_user_same_query_id_isolated(legacy_client):
    """业务：新用户用相同编号不受老记录影响。"""
    logger.info("兼容场景: 新用户 bob 使用老编号 q-001 独立执行独立扣费")
    r = legacy_client.post(
        "/admin/grant", json={"principal": "bob", "budget_epsilon": 5.0}
    )
    assert r.status_code == 201
    # carol 的老记录占着旧版全局 q-001；bob 是新主体，应首次执行而非重放
    r = _count(legacy_client, "q-001", "bob")
    assert r.status_code == 200
    assert r.json()["replayed"] is False
    assert legacy_client.get("/budget/bob").json()["spent_epsilon"] == 1.0
    # bob 重放自己的，不会撞到 carol 的老记录
    assert _count(legacy_client, "q-001", "bob").json()["replayed"] is True
    # carol 老记录仍可独立重放，历史仍只归 carol
    assert _count(legacy_client, "q-001", "carol").json()["replayed"] is True
    r = legacy_client.get("/history/bob", params={"viewer": "bob"})
    assert [i["query_id"] for i in r.json()["items"] if i["status"] == "completed"] == [
        "q-001"
    ]
    r = legacy_client.get("/history/carol", params={"viewer": "carol"})
    assert [i["query_id"] for i in r.json()["items"] if i["status"] == "completed"] == [
        "q-001"
    ]
    logger.info("兼容场景通过: bob/carol 同编号各自独立，互不串号")


def test_legacy_history_filters_and_pagination(legacy_client):
    """业务：老补录条目仍受聚合/维度/状态筛选与分页约束。"""
    logger.info("兼容场景: 老记录在历史筛选/分页下行为正确")
    r = legacy_client.get(
        "/history/alice",
        params={"viewer": "alice", "aggregation": "sum", "dimension": "department"},
    )
    assert [i["query_id"] for i in r.json()["items"]] == ["legacy-sum-age"]
    r = legacy_client.get(
        "/history/alice", params={"viewer": "alice", "status": "completed"}
    )
    assert r.json()["total"] == 3
    r = legacy_client.get(
        "/history/alice", params={"viewer": "alice", "page": 1, "page_size": 2}
    )
    body = r.json()
    assert body["total"] == 3 and len(body["items"]) == 2
    r = legacy_client.get(
        "/history/alice", params={"viewer": "alice", "page": 2, "page_size": 2}
    )
    assert [i["query_id"] for i in r.json()["items"]] == ["legacy-sum-age"]


def test_migration_on_disk_shape_and_marker(settings: Settings, legacy_dir):
    """业务：迁移后账本结构升级，补录带 legacy 标记且 created_at 为 null。"""
    logger.info("兼容场景: 迁移落盘结构正确（嵌套 queries、新分区、legacy 标记）")
    with TestClient(create_app(settings)) as c:
        # 迁移在首个账本事务内惰性触发：查一次历史即触发迁移落盘
        assert c.get("/history/alice", params={"viewer": "alice"}).status_code == 200
    disk = json.loads(settings.ledger_path.read_text(encoding="utf-8"))
    assert set(disk["queries"].keys()) == {"alice", "carol"}
    assert disk["queries"]["alice"]["legacy-count-all"]["cache_key"][-1] is None
    assert disk["queries"]["alice"]["legacy-count-all"]["created_at"] is None
    assert disk["pools"] == {} and disk["adjustments"] == {}
    assert isinstance(disk["history"], list) and len(disk["history"]) == 4
    for event in disk["history"]:
        assert event["legacy"] is True
        assert event["created_at"] is None
        assert event["status"] == "completed"


def test_backfill_from_already_nested_migrated_ledger(settings: Settings, legacy_dir):
    """业务：账本已被旧的新版迁移过（嵌套 queries、created_at=null、history 空），

    修复版首次加载仍需补录，且之后重启不重复。这正是线上"升级后历史为空"
    的真实落盘形态。
    """
    logger.info("兼容场景: 已被旧新版迁移过的账本仍能补录且只补一次")
    # 先用当前迁移规则模拟一次"旧新版"落盘：嵌套归位 + 空 history
    raw = json.loads(settings.ledger_path.read_text(encoding="utf-8"))
    nested: dict = {}
    for query_id, record in raw["queries"].items():
        record["created_at"] = None
        if len(record["cache_key"]) == 5:
            record["cache_key"] = [*record["cache_key"], None]
        nested.setdefault(record["principal"], {})[query_id] = record
    raw["queries"] = nested
    raw["pools"] = {}
    raw["adjustments"] = {}
    raw["history"] = []
    settings.ledger_path.write_text(json.dumps(raw), encoding="utf-8")

    for _ in range(2):
        with TestClient(create_app(settings)) as c:
            assert (
                c.get("/history/alice", params={"viewer": "alice"}).json()["total"] == 3
            )
            assert (
                c.get("/history/carol", params={"viewer": "carol"}).json()["total"] == 1
            )
    disk = json.loads(settings.ledger_path.read_text(encoding="utf-8"))
    assert sum(1 for e in disk["history"] if e.get("legacy")) == 4
    logger.info("兼容场景通过: 嵌套无时间老记录补录 4 条, 重启不重复")
